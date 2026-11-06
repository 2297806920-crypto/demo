# -*- coding: utf-8 -*-
"""PET 薄膜生产系统的简化工程模型（含 A/B/C 产品结构）。

用途：
- 把赛题给出的 30 天生产基线转换成可计算的质量、产量、成本指标；
- 对“改造方案”给出可解释的反馈数据，供 Agent 做下一轮决策。

边界：
- 这是基于赛题基线和明确假设的工程估算，不是 A 公司真实生产数据；
- 所有假设集中在 BASELINE / ASSUMPTIONS / PRODUCTS 中，便于替换成真实反馈；
- 不修改原始数据库，也不把模型输出说成真实产线结果。
"""

from copy import deepcopy


# =========================================================
# 1. 赛题基线（附件 1 表 2）
# =========================================================

BASELINE = {
    "days": 30,
    "calendar_hours": 720.0,
    "theoretical_output_t": 720.0,
    "actual_output_t": 620.0,
    "qualified_output_t": 530.0,
    "demand_t": 560.0,
    "unplanned_downtime_h": 45.0,
    "budget_wan": 300.0,
    "shutdown_window_h": 48.0,
    # 720 h - 45 h 非计划停机 - 55 h 换型/过渡 = 620 h
    "transition_loss_h": 55.0,
    "changeover_count": 24,
    "line_rate_tph": 1.0,
}

BASELINE["changeover_h_per_switch"] = (
    BASELINE["transition_loss_h"] / BASELINE["changeover_count"]
)
BASELINE["quality_yield"] = (
    BASELINE["qualified_output_t"] / BASELINE["actual_output_t"]
)


# =========================================================
# 2. 产品结构（赛题表 1 + 合理假设）
# =========================================================

PRODUCTS = [
    {
        "id": "A",
        "name": "通用包装薄膜",
        "thickness": "12-25 μm",
        "demand_t": 300.0,
        "output_t": 324.0,
        "qualified_t": 285.0,
        "price_cny_t": 9500.0,
        "unmet_penalty_cny_t": 2000.0,
    },
    {
        "id": "B",
        "name": "工业功能基膜",
        "thickness": "25-75 μm",
        "demand_t": 180.0,
        "output_t": 198.0,
        "qualified_t": 170.0,
        "price_cny_t": 10500.0,
        "unmet_penalty_cny_t": 3000.0,
    },
    {
        "id": "C",
        "name": "电气绝缘薄膜",
        "thickness": "75-125 μm",
        "demand_t": 80.0,
        "output_t": 98.0,
        "qualified_t": 75.0,
        "price_cny_t": 12500.0,
        "unmet_penalty_cny_t": 5000.0,
    },
]

for _product in PRODUCTS:
    _product["quality_yield"] = (
        _product["qualified_t"] / _product["output_t"]
    )
    _product["output_share"] = (
        _product["output_t"] / BASELINE["actual_output_t"]
    )

BASELINE["qualified_by_product"] = {
    item["id"]: item["qualified_t"] for item in PRODUCTS
}
BASELINE["demand_by_product"] = {
    item["id"]: item["demand_t"] for item in PRODUCTS
}


# =========================================================
# 3. 工程假设（可替换为真实数据）
# =========================================================

ASSUMPTIONS = {
    "material_price_cny_t": 7000.0,
    "energy_price_cny_kwh": 0.65,
    "energy_kwh_per_t": 550.0,
    "capex_amortize_months": 36.0,
    "excess_discount": 0.60,
    "excess_penalty_cny_t": 800.0,
    "baseline_recovery_rate": 0.30,
    # 速度每提高 1%，各产品合格率下降 0.15 个百分点
    "speed_quality_penalty_per_pct": 0.0015,
    "min_downtime_h": 15.0,
    "min_transition_h": 10.0,
    # 允许分多个 48 h 窗口
    "max_total_install_h": 192.0,
    "min_changeover_count": 12,
}


# =========================================================
# 4. 可选改造措施
# =========================================================

MEASURES = [
    {
        "id": "M1",
        "name": "在线测厚闭环控制",
        "capex_wan": 80.0,
        "install_h": 24.0,
        "speed_gain": 0.0,
        "quality_gain_by_product": {"A": 0.015, "B": 0.020, "C": 0.030},
        "downtime_reduction_h": 0.0,
        "changeover_time_reduction": 0.0,
        "changeover_count_reduction": 0.0,
        "energy_saving_kwh_t": 0.0,
        "recovery_gain": 0.0,
        "inventory_cost_cny": 0.0,
        "risk": "低",
        "effect_text": "质量 A/B/C +1.5/+2.0/+3.0 pp",
        "description": "增加在线厚度检测与闭环反馈，降低厚度波动和降级损失，薄规格和高档产品收益更大。",
    },
    {
        "id": "M2",
        "name": "关键设备预测性维护",
        "capex_wan": 60.0,
        "install_h": 36.0,
        "speed_gain": 0.0,
        "quality_gain_by_product": {},
        "downtime_reduction_h": 12.0,
        "changeover_time_reduction": 0.0,
        "changeover_count_reduction": 0.0,
        "energy_saving_kwh_t": 0.0,
        "recovery_gain": 0.0,
        "inventory_cost_cny": 0.0,
        "risk": "低",
        "effect_text": "非计划停机 -12 h",
        "description": "对挤出、拉伸、收卷关键设备做振动/温度监测，减少非计划停机。",
    },
    {
        "id": "M3",
        "name": "快速换型（SMED）标准化",
        "capex_wan": 30.0,
        "install_h": 24.0,
        "speed_gain": 0.0,
        "quality_gain_by_product": {"A": 0.003, "B": 0.004, "C": 0.005},
        "downtime_reduction_h": 0.0,
        "changeover_time_reduction": 0.327,
        "changeover_count_reduction": 0.0,
        "energy_saving_kwh_t": 0.0,
        "recovery_gain": 0.0,
        "inventory_cost_cny": 0.0,
        "risk": "低",
        "effect_text": "换型时间 -32.7%（约 -18 h）",
        "description": "把规格切换的内外部作业分离并标准化，压缩单次换型与过渡料时间。",
    },
    {
        "id": "M4",
        "name": "边料在线回收与配方优化",
        "capex_wan": 70.0,
        "install_h": 36.0,
        "speed_gain": 0.0,
        "quality_gain_by_product": {"A": -0.002, "B": -0.003, "C": -0.005},
        "downtime_reduction_h": 0.0,
        "changeover_time_reduction": 0.0,
        "changeover_count_reduction": 0.0,
        "energy_saving_kwh_t": 0.0,
        "recovery_gain": 0.10,
        "inventory_cost_cny": 0.0,
        "risk": "中",
        "effect_text": "回收率 +10 pp；质量 -0.2/-0.3/-0.5 pp",
        "description": "提高边料/过渡料在线回收比例，降低原料净投入，但需控制回料对质量的影响。",
    },
    {
        "id": "M5",
        "name": "拉伸/热定型工艺窗口优化",
        "capex_wan": 40.0,
        "install_h": 48.0,
        "speed_gain": 0.03,
        "quality_gain_by_product": {"A": -0.003, "B": -0.005, "C": -0.008},
        "downtime_reduction_h": 0.0,
        "changeover_time_reduction": 0.0,
        "changeover_count_reduction": 0.0,
        "energy_saving_kwh_t": 20.0,
        "recovery_gain": 0.0,
        "inventory_cost_cny": 0.0,
        "risk": "中",
        "effect_text": "线速 +3%；质量 -0.3/-0.5/-0.8 pp；能耗 -20 kWh/t",
        "description": "依据文献中的拉伸温度/热定型窗口微调工艺，提高线速并降低单位能耗。",
    },
    {
        "id": "M6",
        "name": "热定型余热回收",
        "capex_wan": 90.0,
        "install_h": 48.0,
        "speed_gain": 0.0,
        "quality_gain_by_product": {},
        "downtime_reduction_h": 0.0,
        "changeover_time_reduction": 0.0,
        "changeover_count_reduction": 0.0,
        "energy_saving_kwh_t": 45.0,
        "recovery_gain": 0.0,
        "inventory_cost_cny": 0.0,
        "risk": "低",
        "effect_text": "能耗 -45 kWh/t",
        "description": "回收热定型段余热用于前段预热，降低单位产品能耗。",
    },
    {
        "id": "M7",
        "name": "订单合并与换型次数优化",
        "capex_wan": 10.0,
        "install_h": 8.0,
        "speed_gain": 0.0,
        "quality_gain_by_product": {},
        "downtime_reduction_h": 0.0,
        "changeover_time_reduction": 0.0,
        "changeover_count_reduction": 6.0,
        "energy_saving_kwh_t": 0.0,
        "recovery_gain": 0.0,
        "inventory_cost_cny": 25000.0,
        "risk": "中",
        "effect_text": "换型次数 -6 次；库存/订单组织成本 2.5 万元/月",
        "description": "通过订单合并和排产优化减少规格切换次数，代价是库存与交付弹性下降。",
    },
]

MEASURE_BY_ID = {item["id"]: item for item in MEASURES}

RISK_WEIGHT = {"低": 0.01, "中": 0.025, "高": 0.05}


# 基线月度贡献（按产品价格与当前假设计算）
_BASE_SCRAP = (
    BASELINE["actual_output_t"] - BASELINE["qualified_output_t"]
)
_BASE_MATERIAL_INPUT = (
    BASELINE["actual_output_t"]
    - _BASE_SCRAP * ASSUMPTIONS["baseline_recovery_rate"]
)
BASELINE_CONTRIBUTION = (
    sum(
        item["qualified_t"] * item["price_cny_t"]
        for item in PRODUCTS
    )
    - _BASE_MATERIAL_INPUT * ASSUMPTIONS["material_price_cny_t"]
    - BASELINE["actual_output_t"]
    * ASSUMPTIONS["energy_kwh_per_t"]
    * ASSUMPTIONS["energy_price_cny_kwh"]
)


# =========================================================
# 5. 基础工具
# =========================================================

def _clamp(value, low, high):
    return max(low, min(high, value))


def _r(value, digits=2):
    return round(float(value), digits)


def baseline_result():
    return evaluate_plan({"measures": {}})


def measure_priority(measure_id):
    """粗略的“单位投资收益”，用于预算裁剪和规则兜底。"""

    measure = MEASURE_BY_ID.get(measure_id)

    if not measure:
        return 0.0

    quality_benefit = (
        sum(measure["quality_gain_by_product"].values()) * 1200.0
    )
    benefit = (
        quality_benefit
        + measure["speed_gain"] * 800.0
        + measure["downtime_reduction_h"] * 12.0
        + measure["changeover_time_reduction"]
        * BASELINE["transition_loss_h"]
        * 9.0
        + measure["changeover_count_reduction"]
        * BASELINE["changeover_h_per_switch"]
        * 12.0
        + measure["energy_saving_kwh_t"] * 2.5
        + measure["recovery_gain"] * 900.0
        - measure["inventory_cost_cny"] / 1000.0
    )

    return benefit / max(measure["capex_wan"], 1.0)


def normalize_plan(plan):
    plan = deepcopy(plan or {})
    raw_measures = plan.get("measures") or {}
    measures = {}

    if isinstance(raw_measures, dict):
        items = raw_measures.items()
    else:
        items = [
            (item.get("id"), item.get("level", 1.0))
            for item in raw_measures
            if isinstance(item, dict)
        ]

    for measure_id, level in items:

        measure_id = str(measure_id or "").strip().upper()

        if measure_id not in MEASURE_BY_ID:
            continue

        try:
            level = float(level)
        except (TypeError, ValueError):
            continue

        level = _clamp(level, 0.0, 1.0)

        if level > 0:
            measures[measure_id] = level

    return {
        "action": str(plan.get("action") or "explore"),
        "measures": measures,
        "reason": str(plan.get("reason") or ""),
    }


def plan_cost(plan):
    plan = normalize_plan(plan)
    capex = 0.0
    install = 0.0
    max_single = 0.0
    risk = 0.0

    for measure_id, level in plan["measures"].items():
        measure = MEASURE_BY_ID[measure_id]
        capex += measure["capex_wan"] * level
        install += measure["install_h"] * level
        max_single = max(max_single, measure["install_h"] * level)
        risk += RISK_WEIGHT.get(measure["risk"], 0.02) * level

    return {
        "capex_wan": _r(capex),
        "install_h": _r(install),
        "max_single_h": _r(max_single),
        "risk": _r(risk, 4),
    }


def enforce_constraints(plan):
    plan = normalize_plan(plan)
    warnings = []

    while plan["measures"]:
        cost = plan_cost(plan)

        if cost["capex_wan"] <= BASELINE["budget_wan"] + 1e-6:
            break

        drop_id = min(
            plan["measures"],
            key=lambda measure_id: measure_priority(measure_id),
        )
        plan["measures"].pop(drop_id)
        warnings.append(f"预算超限，已移除 {drop_id}")

    while plan["measures"]:
        cost = plan_cost(plan)

        if cost["install_h"] <= ASSUMPTIONS["max_total_install_h"] + 1e-6:
            break

        drop_id = min(
            plan["measures"],
            key=lambda measure_id: measure_priority(measure_id),
        )
        plan["measures"].pop(drop_id)
        warnings.append(f"安装时间超限，已移除 {drop_id}")

    cost = plan_cost(plan)

    if cost["max_single_h"] > BASELINE["shutdown_window_h"] + 1e-6:
        warnings.append("存在超过 48 h 的单次停机窗口")

    if not plan["measures"]:
        warnings.append("方案为空，等同于基线")

    return plan, warnings


# =========================================================
# 6. 方案评估
# =========================================================

def evaluate_plan(plan):
    plan, warnings = enforce_constraints(plan)
    measures = plan["measures"]

    downtime_h = BASELINE["unplanned_downtime_h"]
    speed_factor = 1.0
    changeover_count = float(BASELINE["changeover_count"])
    changeover_time_reduction = 0.0
    energy_saving = 0.0
    recovery_gain = 0.0
    inventory_cost = 0.0
    quality_gain = {item["id"]: 0.0 for item in PRODUCTS}

    for measure_id, level in measures.items():

        measure = MEASURE_BY_ID[measure_id]

        downtime_h -= measure["downtime_reduction_h"] * level
        speed_factor += measure["speed_gain"] * level
        changeover_count -= measure["changeover_count_reduction"] * level
        changeover_time_reduction += (
            measure["changeover_time_reduction"] * level
        )
        energy_saving += measure["energy_saving_kwh_t"] * level
        recovery_gain += measure["recovery_gain"] * level
        inventory_cost += measure["inventory_cost_cny"] * level

        for product_id, gain in measure["quality_gain_by_product"].items():
            quality_gain[product_id] += gain * level

    downtime_h = max(ASSUMPTIONS["min_downtime_h"], downtime_h)
    changeover_count = max(
        ASSUMPTIONS["min_changeover_count"],
        changeover_count,
    )
    changeover_time_reduction = _clamp(
        changeover_time_reduction, 0.0, 0.80
    )

    transition_h = (
        changeover_count
        * BASELINE["changeover_h_per_switch"]
        * (1.0 - changeover_time_reduction)
    )
    transition_h = max(ASSUMPTIONS["min_transition_h"], transition_h)

    available_h = (
        BASELINE["calendar_hours"] - downtime_h - transition_h
    )

    output_t = (
        available_h
        * BASELINE["line_rate_tph"]
        * speed_factor
    )

    speed_pct = (speed_factor - 1.0) * 100.0
    speed_penalty = (
        ASSUMPTIONS["speed_quality_penalty_per_pct"] * speed_pct
    )

    by_product = []
    qualified_t = 0.0
    revenue = 0.0
    unmet_t = 0.0
    excess_t = 0.0
    unmet_penalty = 0.0

    for product in PRODUCTS:

        product_id = product["id"]
        output_p = output_t * product["output_share"]

        quality_p = (
            product["quality_yield"]
            + quality_gain[product_id]
            - speed_penalty
        )
        quality_p = _clamp(quality_p, 0.75, 0.95)

        qualified_p = output_p * quality_p
        qualified_t += qualified_p

        demand_p = product["demand_t"]
        sold_p = min(qualified_p, demand_p)
        excess_p = max(0.0, qualified_p - demand_p)
        unmet_p = max(0.0, demand_p - qualified_p)

        excess_t += excess_p
        unmet_t += unmet_p
        unmet_penalty += unmet_p * product["unmet_penalty_cny_t"]

        revenue_p = (
            sold_p * product["price_cny_t"]
            + excess_p
            * product["price_cny_t"]
            * ASSUMPTIONS["excess_discount"]
        )
        revenue += revenue_p

        by_product.append({
            "id": product_id,
            "name": product["name"],
            "thickness": product["thickness"],
            "output_t": _r(output_p),
            "qualified_t": _r(qualified_p),
            "demand_t": demand_p,
            "quality_yield": _r(quality_p, 4),
            "demand_fill": _r(sold_p / demand_p, 4),
            "unmet_t": _r(unmet_p),
            "price_cny_t": product["price_cny_t"],
            "revenue": _r(revenue_p),
        })

    scrap_t = max(0.0, output_t - qualified_t)
    recovery_rate = _clamp(
        ASSUMPTIONS["baseline_recovery_rate"] + recovery_gain,
        0.0,
        0.80,
    )
    material_input_t = output_t - scrap_t * recovery_rate
    material_cost = (
        material_input_t * ASSUMPTIONS["material_price_cny_t"]
    )

    energy_kwh_t = max(
        300.0,
        ASSUMPTIONS["energy_kwh_per_t"] - energy_saving,
    )
    energy_cost = (
        output_t * energy_kwh_t * ASSUMPTIONS["energy_price_cny_kwh"]
    )

    cost = plan_cost(plan)
    capex_monthly = (
        cost["capex_wan"]
        * 10000.0
        / ASSUMPTIONS["capex_amortize_months"]
    )

    contribution = (
        revenue
        - material_cost
        - energy_cost
        - capex_monthly
        - inventory_cost
    )

    objective = (
        contribution
        - unmet_penalty
        - excess_t * ASSUMPTIONS["excess_penalty_cny_t"]
    )

    monthly_gain = contribution - BASELINE_CONTRIBUTION

    payback = (
        cost["capex_wan"] * 10000.0 / monthly_gain
        if monthly_gain > 1.0
        else None
    )

    quality_yield = (
        qualified_t / output_t if output_t > 0 else 0.0
    )
    unit_cost = (
        (material_cost + energy_cost + capex_monthly) / qualified_t
        if qualified_t > 0
        else None
    )

    uncertainty = cost["risk"]

    return {
        "plan": plan,
        "measures": measures,
        "warnings": warnings,
        "capex_wan": cost["capex_wan"],
        "install_h": cost["install_h"],
        "max_single_h": cost["max_single_h"],
        "downtime_h": _r(downtime_h),
        "changeover_count": _r(changeover_count),
        "transition_h": _r(transition_h),
        "available_h": _r(available_h),
        "speed_factor": _r(speed_factor, 3),
        "quality_yield": _r(quality_yield, 4),
        "output_t": _r(output_t),
        "qualified_t": _r(qualified_t),
        "scrap_t": _r(scrap_t),
        "recovery_rate": _r(recovery_rate, 3),
        "material_input_t": _r(material_input_t),
        "demand_fill": _r(
            (BASELINE["demand_t"] - unmet_t) / BASELINE["demand_t"],
            4,
        ),
        "unmet_t": _r(unmet_t),
        "excess_t": _r(excess_t),
        "revenue": _r(revenue),
        "material_cost": _r(material_cost),
        "energy_cost": _r(energy_cost),
        "energy_kwh_t": _r(energy_kwh_t, 1),
        "inventory_cost": _r(inventory_cost),
        "capex_monthly": _r(capex_monthly),
        "contribution": _r(contribution),
        "unit_cost": _r(unit_cost) if unit_cost else None,
        "objective": _r(objective),
        "monthly_gain": _r(monthly_gain),
        "payback_months": _r(payback, 1) if payback else None,
        "oee": _r(output_t / BASELINE["theoretical_output_t"], 3),
        "risk": cost["risk"],
        "objective_range": [
            _r(objective * (1.0 - uncertainty)),
            _r(objective * (1.0 + uncertainty)),
        ],
        "by_product": by_product,
    }


# =========================================================
# 7. 展示辅助
# =========================================================

def format_plan(plan):
    plan = normalize_plan(plan)

    if not plan["measures"]:
        return "基线（不改造）"

    parts = []

    for measure_id, level in plan["measures"].items():
        measure = MEASURE_BY_ID[measure_id]
        parts.append(
            f"{measure_id} {measure['name']} {int(round(level * 100))}%"
        )

    return "；".join(parts)


def format_kpi_row(result):
    payback = result["payback_months"]
    payback_text = f"{payback} 个月" if payback else "—"

    return (
        f"目标值={result['objective']:,.0f} | "
        f"产量={result['output_t']:.1f} t | "
        f"合格={result['qualified_t']:.1f} t | "
        f"交付率={result['demand_fill'] * 100:.1f}% | "
        f"贡献={result['contribution']:,.0f} 元 | "
        f"CAPEX={result['capex_wan']:.0f} 万 | "
        f"回收期={payback_text}"
    )


def measure_table():
    rows = []

    for measure in MEASURES:
        rows.append(
            f"{measure['id']} {measure['name']} | "
            f"投资 {measure['capex_wan']:.0f} 万 | "
            f"安装 {measure['install_h']:.0f} h | "
            f"{measure['effect_text']} | "
            f"性价比 {measure_priority(measure['id']):.1f} | "
            f"风险 {measure['risk']}"
        )

    return rows
