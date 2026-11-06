# -*- coding: utf-8 -*-
"""方案指导 Agent：根据基线、措施清单、文献证据和历史反馈生成下一轮方案。

Qwen 负责“选什么措施、为什么”，Python 负责约束校验和生产模型反馈。
"""

import json
import os
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai import OpenAI

from config import CONFIG
from production_model import (
    ASSUMPTIONS,
    BASELINE,
    PRODUCTS,
    enforce_constraints,
    format_kpi_row,
    format_plan,
    measure_table,
    plan_cost,
)


class PlanAgentError(RuntimeError):
    pass


def _strip_code_fence(text):
    text = str(text or "").strip()

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    return text.strip()


def parse_plan_json(content):
    """解析模型返回的方案 JSON。"""

    text = _strip_code_fence(content)
    candidates = [text]

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for payload in candidates:

        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue

        if isinstance(data, dict) and "measures" in data:
            return data

    raise ValueError("模型返回内容不是可解析的方案 JSON")


def _rule_plan(round_index, history):
    """Qwen 不可用时的规则兜底：先上低风险组合，再逐步追加措施。"""

    if not history:
        return {
            "action": "explore",
            "measures": {"M1": 1.0, "M2": 1.0, "M3": 1.0},
            "reason": "首轮采用低风险的测厚闭环、预测性维护和快速换型组合。",
        }

    best = max(history, key=lambda item: item["objective"])
    measures = dict(best.get("measures") or best.get("plan", {}).get("measures") or {})

    tried = {
        tuple(sorted((item.get("measures") or {}).items()))
        for item in history
    }

    for measure_id in ("M3", "M7", "M4", "M5", "M6"):

        if measure_id in measures:
            continue

        candidate = dict(measures)
        candidate[measure_id] = 1.0
        cost = plan_cost({"measures": candidate})

        if (
            cost["capex_wan"] > BASELINE["budget_wan"]
            or cost["install_h"] > ASSUMPTIONS["max_total_install_h"]
        ):
            continue

        enforced, _ = enforce_constraints(
            {"action": "exploit", "measures": candidate}
        )
        signature = tuple(sorted(enforced["measures"].items()))

        # 预算/窗口不足时 enforce 会删掉新措施，等同于重复方案
        if measure_id not in enforced["measures"] or signature in tried:
            continue

        return {
            "action": "exploit",
            "measures": enforced["measures"],
            "reason": f"在上一轮最优方案上追加 {measure_id}，继续提升目标值。",
        }

    return {
        "action": "stop",
        "measures": measures,
        "reason": "剩余措施受预算或停机窗口限制，停止迭代。",
    }


class PlanAgent:
    """方案指导 Agent，复用同一个 OpenAI Client。"""

    def __init__(self, config=None):
        self.config = config or CONFIG
        self.llm_config = self.config.get("llm", {})
        self._client = None
        self.last_raw = None
        self.last_source = None
        self.last_warning = None

    def _get_client(self):
        if self._client is not None:
            return self._client

        api_key = os.getenv("SILICONFLOW_API_KEY")

        if not api_key:
            raise PlanAgentError("没有找到环境变量 SILICONFLOW_API_KEY")

        self._client = OpenAI(
            api_key=api_key,
            base_url=self.llm_config.get(
                "base_url", "https://api.siliconflow.cn/v1"
            ),
            timeout=float(self.llm_config.get("timeout", 90.0)),
            max_retries=1,
        )

        return self._client

    def _chat(self, messages):
        client = self._get_client()

        kwargs = {
            "model": self.llm_config.get("model", "Qwen/Qwen3-8B"),
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 800,
        }

        try:
            return client.chat.completions.create(
                **kwargs,
                extra_body={"enable_thinking": False},
            )
        except Exception as exc:
            if "enable_thinking" not in str(exc).lower():
                raise
            return client.chat.completions.create(**kwargs)

    def _system_prompt(self):
        return (
            "你是 PET 薄膜生产系统优化工程师。"
            "目标是在预算和停机窗口约束下，提高合格产品有效产出、"
            "订单交付率和整体经济效益。\n\n"
            "你只能从给定措施清单中选择措施，并给出 0-1 的实施强度。"
            "不要编造措施、成本或产线数据。\n\n"
            "约束：\n"
            f"1. 改造投资不超过 {BASELINE['budget_wan']:.0f} 万元。\n"
            f"2. 单次集中停机不超过 {BASELINE['shutdown_window_h']:.0f} 小时，"
            f"总安装时间不超过 {ASSUMPTIONS['max_total_install_h']:.0f} 小时。\n"
            "3. 不增加一线生产人员，不降低质量与安全标准。\n"
            "4. 方案效果是工程估算，必须说明关键假设和风险。\n\n"
            "只输出合法 JSON，不要输出 Markdown 代码块或解释。格式：\n"
            '{"action": "explore", "measures": [{"id": "M1", "level": 1.0}], '
            '"reason": "选择理由"}\n\n'
            "action 只能是 explore、exploit、stop。"
            "第一轮优先 explore；拿到反馈后，如果仍有改善空间用 exploit，"
            "如果预算/窗口已用尽或继续加措施没有意义，用 stop。\n"
            "从第 2 轮开始，必须在“当前最优方案”的基础上做增量调整："
            "保留其中已被反馈验证有效的措施，最多新增/调整 1-2 项，"
            "不要丢弃全部措施重新开始。"
        )

    def _user_prompt(self, round_index, history, evidence, best_result):
        history_lines = []

        for item in history[-4:]:
            history_lines.append(
                f"第 {item['round']} 轮 | {item['plan_text']} | "
                f"{format_kpi_row(item['result'])} | 目标值={item['objective']:,.0f}"
            )

        if not history_lines:
            history_lines.append("暂无历史反馈。")

        evidence_lines = []

        for item in evidence or []:
            evidence_lines.append(
                f"[论文 {item.get('paper_id')}，P.{item.get('page')}] "
                f"{(item.get('original_text') or '')[:180]}"
            )

        if not evidence_lines:
            evidence_lines.append("本轮没有检索到直接相关的文献证据。")

        best_text = "暂无"

        if best_result:
            best_text = (
                f"{format_plan(best_result['plan'])}；"
                f"目标值={best_result['objective']:,.0f}；"
                f"合格={best_result['qualified_t']:.1f} t"
            )

        product_lines = [
            f"{item['id']} {item['name']}（{item['thickness']}）："
            f"需求 {item['demand_t']:.0f} t，"
            f"单价 {item['price_cny_t']:.0f} 元/t，"
            f"基线合格 {item['qualified_t']:.0f} t"
            for item in PRODUCTS
        ]

        return (
            f"当前轮次：第 {round_index} 轮\n\n"
            f"基线：理论 {BASELINE['theoretical_output_t']:.0f} t，"
            f"实际 {BASELINE['actual_output_t']:.0f} t，"
            f"合格 {BASELINE['qualified_output_t']:.0f} t，"
            f"订单 {BASELINE['demand_t']:.0f} t，"
            f"非计划停机 {BASELINE['unplanned_downtime_h']:.0f} h，"
            f"换型 {BASELINE['changeover_count']:.0f} 次 / "
            f"{BASELINE['transition_loss_h']:.0f} h。\n\n"
            "产品结构：\n"
            + "\n".join(product_lines)
            + "\n\n可选措施：\n"
            + "\n".join(measure_table())
            + "\n\n文献参考（来自本地 BOPET 文献库，仅作机理参考，"
            "不是本企业产线数据）：\n"
            + "\n".join(evidence_lines)
            + "\n\n历史反馈：\n"
            + "\n".join(history_lines)
            + f"\n\n当前最优方案：{best_text}\n\n"
            "请给出下一轮方案。"
        )

    def propose(self, round_index, history, evidence, best_result):
        """返回 (plan, info)。plan 已通过预算/安装时间校验。"""

        self.last_raw = None
        self.last_source = None
        self.last_warning = None

        messages = [
            {"role": "system", "content": self._system_prompt()},
            {
                "role": "user",
                "content": self._user_prompt(
                    round_index, history, evidence, best_result
                ),
            },
        ]

        try:
            response = self._chat(messages)
            content = response.choices[0].message.content
            self.last_raw = content

            if not content or not content.strip():
                raise ValueError("模型返回空内容")

            raw_plan = parse_plan_json(content)
            plan = enforce_constraints(raw_plan)[0]
            plan["action"] = str(raw_plan.get("action") or "explore").lower()

            if plan["action"] not in ("explore", "exploit", "stop"):
                plan["action"] = "explore"

            # 首轮不允许直接停止，也不允许空方案
            if not plan["measures"] or (
                round_index == 1 and plan["action"] == "stop"
            ):
                plan, _ = enforce_constraints(
                    _rule_plan(round_index, history)
                )
                plan["action"] = "explore"
                self.last_warning = "首轮方案为空或要求停止，已改用规则首轮方案"

            # 第 2 轮起做增量保护：如果 Qwen 完全丢弃当前最优方案，
            # 就把最优方案的措施并回来，避免“从零重选”导致回退。
            if (
                round_index > 1
                and best_result
                and best_result.get("measures")
                and plan["action"] != "stop"
            ):
                best_measures = best_result["measures"]

                if not (set(plan["measures"]) & set(best_measures)):
                    merged = dict(best_measures)
                    merged.update(plan["measures"])
                    plan, _ = enforce_constraints(
                        {"action": "exploit", "measures": merged}
                    )
                    plan["action"] = "exploit"
                    self.last_warning = (
                        "Qwen 方案未保留当前最优措施，已合并最优措施后继续"
                    )

            self.last_source = "qwen"

            return plan, {
                "source": "qwen",
                "warning": self.last_warning,
                "raw": content,
            }

        except Exception as exc:
            plan, warnings = enforce_constraints(
                _rule_plan(round_index, history)
            )
            self.last_source = "rule"
            self.last_warning = str(exc)

            return plan, {
                "source": "rule",
                "warning": str(exc),
                "raw": self.last_raw,
            }
