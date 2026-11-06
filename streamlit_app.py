# -*- coding: utf-8 -*-
"""BOPET 文献问答 + 生产方案自循环的 Streamlit 前端。

本地运行：
    streamlit run streamlit_app.py

Streamlit Cloud：
- 在 Secrets 中配置 SILICONFLOW_API_KEY；
- 确保 data/database/bopet.db 已随仓库上传；
- 依赖见 requirements.txt。
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from config import CONFIG
from qa_agent import (
    QAAgent,
    QAError,
    check_database,
    fallback_keywords,
    keep_measurement_for_parameter,
    material_note,
)
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
)
from system_demo import best_increment, build_guidance, collect_evidence
from tools.evidence_retriever import DB_PATH


# =========================================================
# 页面配置与密钥
# =========================================================

st.set_page_config(
    page_title="BOPET 文献与生产系统 AI Demo",
    page_icon="🎞️",
    layout="wide",
)

st.markdown(
    """
    <style>
    .block-container {padding-top: 2rem; padding-bottom: 2rem;}
    div[data-testid="stMetricValue"] {font-size: 1.45rem;}
    .small-note {color: #6b7280; font-size: 0.85rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


def load_api_key():
    """优先环境变量，其次 Streamlit Secrets，最后返回空。"""

    key = os.getenv("SILICONFLOW_API_KEY")

    if key:
        return key

    try:
        key = st.secrets.get("SILICONFLOW_API_KEY")
    except Exception:
        key = None

    if key:
        os.environ["SILICONFLOW_API_KEY"] = str(key)

    return str(key) if key else ""


API_KEY = load_api_key()


@st.cache_data(show_spinner=False)
def db_snapshot():
    return check_database()


@st.cache_resource(show_spinner=False)
def qa_agent():
    return QAAgent(CONFIG)


@st.cache_resource(show_spinner=False)
def plan_agent():
    return PlanAgent(CONFIG)


# =========================================================
# 侧边栏
# =========================================================

def render_sidebar():
    with st.sidebar:
        st.title("🎞️ BOPET AI Demo")
        st.caption("文献证据问答 + 生产方案闭环")

        status = db_snapshot()
        st.divider()
        st.subheader("数据库")
        st.caption(f"SQLite：{status['path']}")

        if status.get("exists") and status.get("tables"):
            st.success("数据库已连接")
            for table, count in status["tables"].items():
                st.caption(f"{table}：{count:,} 条")
        else:
            st.error("数据库未找到或表为空")
            st.caption("请确认 data/database/bopet.db 已上传。")

        st.divider()
        st.subheader("Qwen")
        if API_KEY:
            st.success("API Key 已配置")
        else:
            st.warning("未配置 API Key")
            st.caption(
                "本地：设置环境变量 SILICONFLOW_API_KEY；"
                "云端：在 Streamlit Secrets 中添加同名项。"
            )

        st.divider()
        st.caption(
            "数据边界：文献库来自 PDF 抽取；生产反馈为竞赛基线"
            "下的工程估算，不是企业真实产线数据。"
        )


# =========================================================
# 文献问答
# =========================================================

def render_qa_result(result):
    question = result.get("question", "")
    keywords = result.get("keywords") or []
    evidence = result.get("evidence") or []
    answer = result.get("answer") or ""

    st.markdown(f"#### 问题：{question}")

    if keywords:
        st.markdown(
            "**检索关键词：** "
            + " ".join(f"`{keyword}`" for keyword in keywords)
        )

    if answer:
        heading = (
            "### AI 回答"
            if result.get("answered_by_model")
            else "### 检索结果"
        )
        st.markdown(heading)
        st.markdown(answer)

    if not evidence:
        st.info("当前数据库未检索到相关证据。")
        return

    st.markdown(f"### 检索证据（{len(evidence)} 条）")

    for index, item in enumerate(evidence, 1):

        page = item.get("page")
        page_text = f"P.{page}" if page is not None else "页码缺失"
        source = item.get("evidence_source") or "未知来源"

        with st.expander(
            f"证据 {index}｜论文 {item.get('paper_id')}｜{page_text}｜{source}",
            expanded=index <= 2,
        ):
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"**{item.get('title') or '题名缺失'}**")
            c2.metric("检索分数", item.get("retrieval_score") or 0)

            st.caption(
                f"主题：{item.get('topic') or '—'} ｜ "
                f"数据类型：{item.get('data_type') or '—'} ｜ "
                f"材料初判：{item.get('material_candidate') or '—'}"
            )

            st.markdown(
                f"**参数：** {item.get('parameter_type') or '未识别'} ｜ "
                f"**测量值：** "
                + (
                    "、".join(item.get("measurement") or [])
                    or "未自动提取"
                )
            )

            note = material_note(item.get("material_candidate"))
            if "非 BOPET" in note or "待核实" in note:
                st.warning(note)

            if item.get("target_parameter_match") is False:
                st.caption("目标参数匹配：否（该数值可能不是问题所问参数）")

            st.code(item.get("original_text") or "", language=None)

    rows = []

    for item in evidence:

        if item.get("target_parameter_match") is not True:
            continue

        values = [
            value for value in item.get("measurement") or []
            if keep_measurement_for_parameter(
                value, item.get("parameter_type")
            )
        ]

        if not values:
            continue

        rows.append({
            "论文编号": item.get("paper_id"),
            "页码": item.get("page"),
            "参数": item.get("parameter_type"),
            "数值": "、".join(values),
            "材料初判": item.get("material_candidate"),
            "证据来源": item.get("evidence_source"),
        })

    if rows:
        st.markdown("### 程序汇总（规则提取，未经模型改写）")
        st.dataframe(rows, hide_index=True, width="stretch")

    st.markdown("### 文献来源")
    st.dataframe(
        [
            {
                "证据": f"证据 {index}",
                "论文编号": item.get("paper_id"),
                "页码": item.get("page"),
                "来源": item.get("evidence_source"),
            }
            for index, item in enumerate(evidence, 1)
        ],
        hide_index=True,
        width="stretch",
    )


def render_qa_tab():
    st.subheader("文献数据库问答")
    st.caption(
        "Qwen 理解问题 → 生成检索关键词 → 查询本地 SQLite → "
        "把真实证据交给 Qwen → 输出带论文编号和页码的回答。"
    )

    examples = [
        "有没有关于拉伸温度的实验数据？",
        "PET 薄膜的热收缩率是多少？",
        "有没有关于收卷张力的研究？",
    ]

    st.caption("示例：" + " ｜ ".join(examples))

    with st.form("qa_form"):
        question = st.text_area(
            "输入问题",
            key="qa_question",
            height=90,
            placeholder="例如：有没有关于薄膜拉伸温度的实验数据？",
        )

        only_retrieval = st.checkbox(
            "仅检索数据库（不调用 Qwen）",
            value=not bool(API_KEY),
            help="不调用模型时，只展示 SQLite 检索到的原始证据。",
        )

        submitted = st.form_submit_button(
            "开始问答",
            type="primary",
            width="stretch",
        )

    if submitted:

        if not question.strip():
            st.warning("请输入问题。")
            return

        with st.status("正在处理...", expanded=True) as status:

            try:
                agent = qa_agent()

                if only_retrieval:
                    keywords = fallback_keywords(question)
                    st.write("系统：检索关键词：" + "、".join(keywords))
                    st.write("系统：正在查询 SQLite 数据库...")
                    evidence = agent.retrieve(keywords)
                    result = {
                        "question": question.strip(),
                        "keywords": keywords,
                        "evidence": evidence,
                        "answer": "（仅检索模式：未调用 Qwen 生成回答）",
                        "answered_by_model": False,
                    }
                else:
                    result = agent.ask(
                        question.strip(),
                        on_step=lambda message: st.write("系统：" + message),
                    )

                st.session_state["qa_result"] = result
                status.update(label="完成", state="complete")

            except QAError as exc:
                st.session_state["qa_result"] = None
                status.update(label="失败", state="error")
                st.error(f"流程失败（{exc.step}）：{exc.message}")

            except Exception as exc:
                st.session_state["qa_result"] = None
                status.update(label="失败", state="error")
                st.error(f"未预期错误：{type(exc).__name__}: {exc}")

    result = st.session_state.get("qa_result")

    if result:
        st.divider()
        render_qa_result(result)


# =========================================================
# 方案自循环
# =========================================================

def execute_loop(rounds, use_llm, on_round):
    """运行方案-反馈闭环，返回 baseline/history/best/evidence。"""

    baseline = baseline_result()
    evidence = collect_evidence()
    agent = plan_agent()

    history = []
    best = baseline
    no_improvement = 0

    for round_index in range(1, rounds + 1):

        if use_llm:
            plan, info = agent.propose(
                round_index,
                history,
                evidence,
                best if best["measures"] else None,
            )
        else:
            plan, _ = enforce_constraints(
                _rule_plan(round_index, history)
            )
            info = {"source": "rule", "warning": None}

        action = plan.get("action", "explore")

        if action == "stop":
            on_round({
                "round": round_index,
                "stop": True,
                "reason": plan.get("reason", ""),
            })
            break

        result = evaluate_plan(plan)

        # 与命令行版一致的局部搜索保护
        if (
            use_llm
            and round_index > 1
            and result["objective"] <= best["objective"]
        ):
            guard_plan, guard_result = best_increment(best)

            if guard_plan and guard_result["objective"] > result["objective"]:
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
            "warning": info.get("warning"),
        })

        improved = result["objective"] > best["objective"]

        if improved:
            best = result
            no_improvement = 0
        else:
            no_improvement += 1

        on_round({
            "round": round_index,
            "stop": False,
            "plan_text": plan_text,
            "objective": result["objective"],
            "result": result,
            "improved": improved,
        })

        if (
            len(history) >= 2
            and plan["measures"] == history[-2]["measures"]
            and result["objective"] <= history[-2]["objective"]
        ):
            break

        if round_index >= 2 and no_improvement >= 2:
            break

    return baseline, history, best, evidence


def render_plan_result(data):
    baseline = data["baseline"]
    history = data["history"]
    best = data["best"]
    report = data["report"]

    st.divider()
    st.markdown("### 最优方案")
    st.info(format_plan(best["plan"]))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "合格产量",
        f"{best['qualified_t']:.1f} t",
        f"{best['qualified_t'] - baseline['qualified_t']:+.1f} t",
    )
    c2.metric(
        "交付率",
        f"{best['demand_fill'] * 100:.1f}%",
        f"{(best['demand_fill'] - baseline['demand_fill']) * 100:+.1f} pp",
    )
    c3.metric(
        "月度贡献",
        f"{best['contribution']:,.0f} 元",
        f"{best['monthly_gain']:+,.0f} 元",
    )
    c4.metric(
        "投资 / 回收期",
        f"{best['capex_wan']:.0f} 万",
        (
            f"{best['payback_months']:.1f} 个月"
            if best["payback_months"]
            else "未计算"
        ),
    )

    st.markdown("### 指标对比")

    rows = []
    specs = [
        ("合格产量 (t)", "qualified_t", 1),
        ("总产量 (t)", "output_t", 1),
        ("交付率", "demand_fill", 4),
        ("换型次数", "changeover_count", 0),
        ("换型损失 (h)", "transition_h", 1),
        ("单位成本 (元/t)", "unit_cost", 0),
        ("月度贡献 (元)", "contribution", 0),
        ("目标值 (元)", "objective", 0),
    ]

    for label, key, digits in specs:

        base_value = baseline.get(key)
        best_value = best.get(key)

        if base_value is None or best_value is None:
            continue

        if key == "demand_fill":
            base_text = f"{base_value * 100:.1f}%"
            best_text = f"{best_value * 100:.1f}%"
            delta_text = f"{(best_value - base_value) * 100:+.1f} pp"
        else:
            base_text = f"{base_value:,.{digits}f}"
            best_text = f"{best_value:,.{digits}f}"
            delta_text = f"{best_value - base_value:+,.{digits}f}"

        rows.append({
            "指标": label,
            "基线": base_text,
            "最优方案": best_text,
            "变化": delta_text,
        })

    st.dataframe(rows, hide_index=True, width="stretch")

    st.markdown("### 产品结构对比")

    base_products = {
        item["id"]: item for item in baseline.get("by_product", [])
    }
    best_products = {
        item["id"]: item for item in best.get("by_product", [])
    }

    product_rows = []

    for product in PRODUCTS:

        product_id = product["id"]
        base_p = base_products.get(product_id, {})
        best_p = best_products.get(product_id, {})

        product_rows.append({
            "产品": f"{product_id} {product['name']}",
            "厚度": product["thickness"],
            "需求 (t)": product["demand_t"],
            "基线合格 (t)": base_p.get("qualified_t"),
            "最优合格 (t)": best_p.get("qualified_t"),
            "交付率": f"{best_p.get('demand_fill', 0) * 100:.1f}%",
        })

    st.dataframe(product_rows, hide_index=True, width="stretch")

    st.markdown("### 迭代轨迹")

    st.dataframe(
        [
            {
                "轮次": item["round"],
                "方案": item["plan_text"],
                "投资 (万)": item["result"]["capex_wan"],
                "合格 (t)": item["result"]["qualified_t"],
                "交付率": f"{item['result']['demand_fill'] * 100:.1f}%",
                "目标值": f"{item['objective']:,.0f}",
                "来源": item["source"],
            }
            for item in history
        ],
        hide_index=True,
        width="stretch",
    )

    st.markdown("### 方案指导摘要")
    st.markdown(build_guidance(best, baseline))

    if best.get("warnings"):
        st.caption("约束处理：" + "；".join(best["warnings"]))

    st.download_button(
        "下载 JSON 报告",
        data=json.dumps(report, ensure_ascii=False, indent=2),
        file_name=(
            "bopet_plan_"
            + datetime.now().strftime("%Y%m%d_%H%M%S")
            + ".json"
        ),
        mime="application/json",
        width="stretch",
    )


def render_plan_tab():
    st.subheader("生产系统方案自循环")
    st.caption(
        "Qwen 选择措施组合 → 工程模型反馈产量/质量/成本 → "
        "历史反馈回传 → 增量调整或停止。"
    )

    baseline = baseline_result()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("基线合格产量", f"{baseline['qualified_t']:.0f} t")
    c2.metric("基线交付率", f"{baseline['demand_fill'] * 100:.1f}%")
    c3.metric("基线换型", f"{baseline['changeover_count']:.0f} 次")
    c4.metric("基线月度贡献", f"{baseline['contribution']:,.0f} 元")

    st.markdown("#### 产品结构")
    st.dataframe(
        [
            {
                "产品": f"{item['id']} {item['name']}",
                "厚度": item["thickness"],
                "需求 (t)": item["demand_t"],
                "单价 (元/t)": item["price_cny_t"],
                "基线合格 (t)": item["qualified_t"],
                "基线交付率": f"{item['qualified_t'] / item['demand_t'] * 100:.1f}%",
            }
            for item in PRODUCTS
        ],
        hide_index=True,
        width="stretch",
    )

    with st.container(border=True):
        cc1, cc2, cc3 = st.columns([1, 1, 2])
        rounds = cc1.slider("最大迭代轮数", 2, 6, 4)
        rule_only = cc2.toggle(
            "规则模式（不调用 Qwen）",
            value=not bool(API_KEY),
        )
        run = cc3.button(
            "运行方案自循环",
            type="primary",
            width="stretch",
            disabled=(not API_KEY and not rule_only),
        )

    if run:

        progress = st.progress(0.0, text="准备中...")

        with st.status("正在运行方案-反馈闭环...", expanded=True) as status:

            def on_round(info):
                if info.get("stop"):
                    status.write(
                        f"第 {info['round']} 轮：模型判断停止 —— "
                        f"{info.get('reason') or '无理由'}"
                    )
                    return

                status.write(
                    f"第 {info['round']} 轮｜{info['plan_text']}｜"
                    f"目标值 {info['objective']:,.0f}"
                )
                progress.progress(
                    min(1.0, info["round"] / rounds),
                    text=f"第 {info['round']} 轮完成",
                )

            try:
                baseline2, history, best, evidence = execute_loop(
                    rounds,
                    not rule_only,
                    on_round,
                )
                status.update(label="闭环完成", state="complete")
                progress.progress(1.0, text="完成")

                report = {
                    "generated_at": datetime.now().isoformat(
                        timespec="seconds"
                    ),
                    "data_mode": "ENGINEERING_ESTIMATE_NOT_REAL_PRODUCTION",
                    "baseline": baseline2,
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
                    "evidence": evidence,
                    "assumptions": ASSUMPTIONS,
                    "constraints": {
                        "budget_wan": BASELINE["budget_wan"],
                        "shutdown_window_h": BASELINE["shutdown_window_h"],
                        "max_total_install_h": ASSUMPTIONS["max_total_install_h"],
                    },
                    "guidance": build_guidance(best, baseline2),
                }

                st.session_state["plan_result"] = {
                    "baseline": baseline2,
                    "history": history,
                    "best": best,
                    "report": report,
                }

            except Exception as exc:
                status.update(label="运行失败", state="error")
                st.error(f"{type(exc).__name__}: {exc}")

    data = st.session_state.get("plan_result")

    if data and data.get("history"):
        render_plan_result(data)
    elif data:
        st.info("没有产生有效方案，请检查基线或模型输出。")


# =========================================================
# 数据库概览
# =========================================================

def render_database_tab():
    st.subheader("数据库与部署说明")

    status = db_snapshot()
    st.code(str(DB_PATH), language=None)

    if not status.get("exists"):
        st.error("找不到数据库。请把 data/database/bopet.db 一起上传到 GitHub。")
        return

    if DB_PATH.exists():
        st.caption(f"文件大小：{DB_PATH.stat().st_size / 1024 / 1024:.2f} MB")

    st.markdown("#### 数据表")

    try:
        conn = sqlite3.connect(
            f"file:{DB_PATH.as_posix()}?mode=ro",
            uri=True,
        )
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' ORDER BY name"
        )
        table_names = [row["name"] for row in cursor.fetchall()]

        rows = []

        for table in table_names:
            try:
                count = cursor.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
            except sqlite3.DatabaseError:
                count = "读取失败"

            rows.append({"表名": table, "记录数": count})

        st.dataframe(rows, hide_index=True, width="stretch")

        target_options = [
            table for table in
            ("evidence_01_50", "evidence_51_100", "table_data")
            if table in table_names
        ]

        if target_options:
            target = st.selectbox("查看字段", target_options)

            columns = cursor.execute(
                f'PRAGMA table_info("{target}")'
            ).fetchall()
            st.dataframe(
                [
                    {
                        "字段": column["name"],
                        "类型": column["type"],
                    }
                    for column in columns
                ],
                hide_index=True,
                width="stretch",
            )

        conn.close()

    except Exception as exc:
        st.error(f"读取数据库失败：{type(exc).__name__}: {exc}")

    st.markdown("#### 部署到 Streamlit Cloud")

    st.markdown(
        """
1. 把整个项目上传到 GitHub，确认包含 `data/database/bopet.db`。
2. 在 Streamlit Cloud 新建 App，入口选择 `streamlit_app.py`。
3. 在 **Settings → Secrets** 添加：

```toml
SILICONFLOW_API_KEY = "sk-你的key"
```

4. 依赖由 `requirements.txt` 自动安装。
5. 没有 API Key 时，页面仍可用“仅检索 / 规则模式”演示。
        """
    )


# =========================================================
# 主页面
# =========================================================

def main():
    render_sidebar()

    st.title("BOPET 文献智能问答 + 生产方案自循环")
    st.caption(
        "左侧是文献数据库问答，右侧是赛题基线下的方案-反馈闭环；"
        "所有回答都带来源，所有反馈都标明是工程估算。"
    )

    tab_qa, tab_plan, tab_db = st.tabs(
        ["文献问答", "方案自循环", "数据库与部署"]
    )

    with tab_qa:
        render_qa_tab()

    with tab_plan:
        render_plan_tab()

    with tab_db:
        render_database_tab()


if __name__ == "__main__":
    main()
