# -*- coding: utf-8 -*-
"""PET 薄膜生产系统“方案指导 + 数据反馈”自循环 Demo。

运行：
    python system_demo.py --rounds 4
    python system_demo.py --rounds 4 --no-llm

说明：
- 方案由 Qwen 依据基线、措施清单、文献证据和历史反馈生成；
- 反馈数据由 production_model.py 的工程模型给出；
- 所有数字都是竞赛基线下的工程估算，不是企业真实生产数据。
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from config import CONFIG
from plan_agent import PlanAgent, _rule_plan
from production_model import (
    ASSUMPTIONS,
    BASELINE,
    MEASURE_BY_ID,
    PRODUCTS,
    baseline_result,
    enforce_constraints,
    evaluate_plan,
    format_kpi_row,
    format_plan,
    measure_table,
)
from tools.evidence_retriever import build_evidence_items, search_evidence


EVIDENCE_KEYWORDS = ["拉伸温度", "热定型温度", "厚度", "收卷张力"]


def configure_stdout():
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


def collect_evidence():
    """从本地文献库取少量机理证据，附在方案输入中。"""

    items = []
    seen = set()

    for keyword in EVIDENCE_KEYWORDS:

        try:
            rows = search_evidence(keyword, limit=6)
        except Exception:
            continue

        for item in build_evidence_items(rows):

            # 只保留带数值、且参数已被规则识别的证据，避免纯概念段落
            if not item.get("measurement"):
                continue

            if not item.get("parameter_type") or item["parameter_type"] == "待人工识别":
                continue

            key = (
                item.get("paper_id"),
                item.get("page"),
                (item.get("original_text") or "")[:60],
            )

            if key in seen:
                continue

            seen.add(key)
            item["query_keyword"] = keyword
            items.append(item)

        if len(items) >= 6:
            break

    return items[:6]


def print_baseline(baseline):
    print("\n" + "=" * 78)
    print("PET 薄膜生产系统：方案指导 + 数据反馈 自循环")
    print("=" * 78)
    print(
        f"基线：理论 {BASELINE['theoretical_output_t']:.0f} t / "
        f"实际 {BASELINE['actual_output_t']:.0f} t / "
        f"合格 {BASELINE['qualified_output_t']:.0f} t / "
        f"订单 {BASELINE['demand_t']:.0f} t"
    )
    print(
        f"约束：投资 ≤ {BASELINE['budget_wan']:.0f} 万元；"
        f"单次停机 ≤ {BASELINE['shutdown_window_h']:.0f} h；"
        f"总安装 ≤ {ASSUMPTIONS['max_total_install_h']:.0f} h"
    )
    print(
        "产品结构："
        + "；".join(
            f"{item['id']} {item['name']} {item['demand_t']:.0f} t"
            f"（{item['price_cny_t']:.0f} 元/t）"
            for item in PRODUCTS
        )
    )
    print(
        "基线拆解："
        f"720 h - {BASELINE['unplanned_downtime_h']:.0f} h 停机 - "
        f"{BASELINE['transition_loss_h']:.0f} h 换型"
        f"（{BASELINE['changeover_count']:.0f} 次 × "
        f"{BASELINE['changeover_h_per_switch']:.2f} h）"
        f" = 620 h"
    )
    print(
        "关键假设："
        f"原料 {ASSUMPTIONS['material_price_cny_t']:.0f} 元/t；"
        f"电耗 {ASSUMPTIONS['energy_kwh_per_t']:.0f} kWh/t；"
        f"电价 {ASSUMPTIONS['energy_price_cny_kwh']:.2f} 元/kWh；"
        "产品价格见上方产品结构"
    )
    print("基线反馈：" + format_kpi_row(baseline))
    print("=" * 78)


def print_measure_catalog():
    print("\n可选措施（措施效果为工程估算，可替换为真实数据）：")

    for line in measure_table():
        print("  " + line)


def print_round(round_index, plan, result, info, baseline):
    print("\n" + "-" * 78)
    print(f"第 {round_index} 轮")
    print("-" * 78)
    print("方案：" + format_plan(plan))
    print(
        f"投资 {result['capex_wan']:.0f} 万元 | "
        f"安装 {result['install_h']:.0f} h | "
        f"最大单次 {result['max_single_h']:.0f} h | "
        f"风险系数 {result['risk']:.3f}"
    )
    print(
        f"换型 {result['changeover_count']:.0f} 次 | "
        f"换型损失 {result['transition_h']:.1f} h | "
        f"停机 {result['downtime_h']:.0f} h"
    )
    print("反馈：" + format_kpi_row(result))

    delta = result["objective"] - baseline["objective"]
    print(
        f"相对基线：目标值 {delta:+,.0f} 元 | "
        f"合格产量 {result['qualified_t'] - baseline['qualified_t']:+.1f} t | "
        f"交付率 {(result['demand_fill'] - baseline['demand_fill']) * 100:+.1f} pp"
    )

    if info.get("source") == "qwen":
        print("Qwen 决策：")
    elif info.get("source") == "rule_guard":
        print("规则增量保护（Qwen 方案未带来改善）：")
    else:
        print("规则兜底决策（Qwen 不可用或返回异常）：")

    print("  理由：" + (plan.get("reason") or "未提供"))

    if info.get("warning"):
        print("  警告：" + info["warning"])

    if result["warnings"]:
        print("  约束处理：" + "；".join(result["warnings"]))


def build_guidance(best, baseline):
    """根据最优方案和反馈生成一段工程化方案说明。"""

    selected = []

    for measure_id in best["measures"]:
        measure = MEASURE_BY_ID[measure_id]
        selected.append(f"{measure_id} {measure['name']}")

    risks = sorted({
        MEASURE_BY_ID[measure_id]["risk"]
        for measure_id in best["measures"]
    })

    windows = max(1, int((best["install_h"] + 47) // 48))

    return (
        "1. 选择依据：基线的主要损失来自 "
        f"{BASELINE['unplanned_downtime_h']:.0f} h 非计划停机和 "
        f"{BASELINE['transition_loss_h']:.0f} h 换型/过渡损失"
        f"（{BASELINE['changeover_count']:.0f} 次换型），"
        f"合格率仅 {BASELINE['quality_yield'] * 100:.1f}%，"
        f"距离订单还差 {BASELINE['demand_t'] - BASELINE['qualified_output_t']:.0f} t。"
        "方案优先处理可量化、低风险的损失项；"
        "高价值 C 产品基线交付率最低，排产时应优先保障其合格产出。\n"
        "2. 方案内容：" + "、".join(selected) + "。"
        "具体措施与效果见措施清单，实施时按安装窗口分阶段停机。\n"
        "3. 预期效果："
        f"合格产量 {best['qualified_t'] - baseline['qualified_t']:+.1f} t，"
        f"交付率 {(best['demand_fill'] - baseline['demand_fill']) * 100:+.1f} pp，"
        f"换型 {baseline['changeover_count']:.0f} 次 → {best['changeover_count']:.0f} 次"
        f"（损失 {baseline['transition_h']:.1f} h → {best['transition_h']:.1f} h），"
        f"月度贡献 {best['monthly_gain']:+,.0f} 元，"
        f"投资 {best['capex_wan']:.0f} 万元，"
        f"回收期 "
        + (f"{best['payback_months']:.1f} 个月。" if best["payback_months"] else "未计算。")
        + "\n4. 风险与局限："
        f"措施风险等级为 {'/'.join(risks)}；"
        f"需要约 {windows} 个停机窗口（单次 ≤ 48 h）；"
        "反馈数据为工程估算，不是企业真实产线数据；"
        "回料比例、速度提升与质量、订单合并与库存之间存在权衡，需先试运行再放大。"
    )


def best_increment(best_result):
    """在当前最优方案上做一次单措施局部搜索，作为停滞保护。"""

    base = dict(best_result.get("measures") or {})
    best_plan = None
    best_eval = None

    for measure_id in MEASURE_BY_ID:

        if measure_id in base:
            continue

        candidate = dict(base)
        candidate[measure_id] = 1.0
        enforced, _ = enforce_constraints(
            {"action": "exploit", "measures": candidate}
        )

        if measure_id not in enforced["measures"]:
            continue

        result = evaluate_plan(enforced)

        if best_eval is None or result["objective"] > best_eval["objective"]:
            best_plan = enforced
            best_eval = result

    return best_plan, best_eval


def print_final(best, baseline, history, evidence, report_path):
    print("\n" + "=" * 78)
    print("最优方案与工程价值")
    print("=" * 78)
    print("方案：" + format_plan(best["plan"]))
    print(
        f"投资 {best['capex_wan']:.0f} 万元 | "
        f"安装 {best['install_h']:.0f} h | "
        f"回收期 "
        + (f"{best['payback_months']:.1f} 个月" if best["payback_months"] else "—")
    )

    guidance = build_guidance(best, baseline)
    print("\n方案指导摘要：")
    print(guidance)

    print("\n指标对比：")
    print(
        f"{'指标':<16}{'基线':>14}{'最优方案':>16}{'变化':>14}"
    )

    rows = [
        ("合格产量 (t)", "qualified_t", 1, False),
        ("总产量 (t)", "output_t", 1, False),
        ("交付率", "demand_fill", 4, True),
        ("换型次数", "changeover_count", 0, False),
        ("换型损失 (h)", "transition_h", 1, False),
        ("单位成本 (元/t)", "unit_cost", 0, False),
        ("月度贡献 (元)", "contribution", 0, False),
        ("目标值 (元)", "objective", 0, False),
    ]

    for label, key, digits, pct in rows:

        base_value = baseline.get(key)
        best_value = best.get(key)

        if base_value is None or best_value is None:
            continue

        delta = best_value - base_value

        if pct:
            base_text = f"{base_value * 100:.1f}%"
            best_text = f"{best_value * 100:.1f}%"
            delta_text = f"{delta * 100:+.1f} pp"
        elif digits == 0:
            base_text = f"{base_value:,.0f}"
            best_text = f"{best_value:,.0f}"
            delta_text = f"{delta:+,.0f}"
        else:
            base_text = f"{base_value:,.{digits}f}"
            best_text = f"{best_value:,.{digits}f}"
            delta_text = f"{delta:+,.{digits}f}"

        print(f"{label:<16}{base_text:>14}{best_text:>16}{delta_text:>14}")

    print("\n产品结构对比：")
    print(f"{'产品':<22}{'基线合格':>12}{'最优合格':>12}{'变化':>10}{'交付率':>10}")

    base_products = {item["id"]: item for item in baseline["by_product"]}
    best_products = {item["id"]: item for item in best["by_product"]}

    for product in PRODUCTS:

        product_id = product["id"]
        base_p = base_products.get(product_id, {})
        best_p = best_products.get(product_id, {})
        delta_p = best_p.get("qualified_t", 0) - base_p.get("qualified_t", 0)

        print(
            f"{product_id} {product['name']:<14}"
            f"{base_p.get('qualified_t', 0):>12.1f}"
            f"{best_p.get('qualified_t', 0):>12.1f}"
            f"{delta_p:>+10.1f}"
            f"{best_p.get('demand_fill', 0) * 100:>9.1f}%"
        )

    print(
        f"\n目标值不确定区间："
        f"{best['objective_range'][0]:,.0f} ~ {best['objective_range'][1]:,.0f} 元"
        f"（按措施风险系数估算）"
    )

    print("\n文献参考（机理参考，不是本企业产线数据）：")

    for item in evidence:
        print(
            f"  [论文 {item.get('paper_id')}，P.{item.get('page')}] "
            f"{(item.get('original_text') or '')[:150]}"
        )

    print("\n迭代轨迹：")

    for item in history:
        print(
            f"  第 {item['round']} 轮 | 目标值 {item['objective']:,.0f} | "
            f"{item['plan_text']}"
        )

    print(f"\n报告已保存：{report_path}")


def save_report(baseline, history, best, evidence, path):
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_mode": "ENGINEERING_ESTIMATE_NOT_REAL_PRODUCTION",
        "baseline": baseline,
        "history": [
            {
                "round": item["round"],
                "plan": item["plan"],
                "measures": item["measures"],
                "plan_text": item["plan_text"],
                "result": item["result"],
                "objective": item["objective"],
                "source": item["source"],
                "reason": item["reason"],
            }
            for item in history
        ],
        "best": best,
        "guidance": build_guidance(best, baseline),
        "evidence": evidence,
        "assumptions": ASSUMPTIONS,
        "constraints": {
            "budget_wan": BASELINE["budget_wan"],
            "shutdown_window_h": BASELINE["shutdown_window_h"],
            "max_total_install_h": ASSUMPTIONS["max_total_install_h"],
        },
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="PET 薄膜生产系统方案-反馈自循环 Demo"
    )
    parser.add_argument("--rounds", type=int, default=4, help="最大迭代轮数")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="不调用 Qwen，只用规则策略验证闭环",
    )
    return parser.parse_args()


def main():
    configure_stdout()
    args = parse_args()
    rounds = max(1, min(6, args.rounds))

    baseline = baseline_result()
    print_baseline(baseline)
    print_measure_catalog()

    evidence = collect_evidence()
    print(f"\n文献证据：已取 {len(evidence)} 条（用于方案机理参考）")

    agent = PlanAgent(CONFIG)
    history = []
    best = baseline
    no_improvement = 0

    for round_index in range(1, rounds + 1):

        if args.no_llm:
            raw_plan, warnings = enforce_constraints(
                _rule_plan(round_index, history)
            )
            info = {"source": "rule", "warning": None}
            plan = raw_plan
        else:
            plan, info = agent.propose(
                round_index,
                history,
                evidence,
                best if best["measures"] else None,
            )

        action = plan.get("action", "explore")

        if action == "stop":
            print("\n" + "-" * 78)
            if args.no_llm:
                print(f"第 {round_index} 轮：规则策略判断可以停止迭代。")
            else:
                print(f"第 {round_index} 轮：Qwen 判断可以停止迭代。")
            if plan.get("reason"):
                print("  理由：" + plan["reason"])
            break

        result = evaluate_plan(plan)

        # 停滞保护：Qwen 没有带来改善时，在最优方案上做单措施局部搜索
        if (
            not args.no_llm
            and round_index > 1
            and result["objective"] <= best["objective"]
        ):
            guard_plan, guard_result = best_increment(best)

            if (
                guard_plan
                and guard_result["objective"] > result["objective"]
            ):
                added = sorted(
                    set(guard_plan["measures"]) - set(best["measures"])
                )
                guard_plan["reason"] = (
                    "在当前最优方案上追加 "
                    + "、".join(added)
                    + "，单措施局部搜索反馈更优。"
                )
                plan = guard_plan
                result = guard_result
                info = {
                    "source": "rule_guard",
                    "warning": "Qwen 方案未带来改善，已用单措施局部搜索补齐",
                }

        plan_text = format_plan(plan)

        history.append({
            "round": round_index,
            "plan": plan,
            "measures": plan.get("measures", {}),
            "plan_text": plan_text,
            "result": result,
            "objective": result["objective"],
            "source": info.get("source"),
            "reason": plan.get("reason", ""),
        })

        print_round(round_index, plan, result, info, baseline)

        if result["objective"] > best["objective"]:
            best = result
            no_improvement = 0
        else:
            no_improvement += 1

        # 方案与上一轮完全相同且没有改善，再迭代没有意义
        if (
            len(history) >= 2
            and plan["measures"] == history[-2]["measures"]
            and result["objective"] <= history[-2]["objective"]
        ):
            print("\n方案与上一轮相同且没有改善，停止迭代。")
            break

        if round_index >= 2 and no_improvement >= 2:
            print("\n连续两轮没有改善，停止迭代。")
            break

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = Path("outputs") / f"system_loop_{stamp}.json"
    save_report(baseline, history, best, evidence, report_path)

    print_final(best, baseline, history, evidence, report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
