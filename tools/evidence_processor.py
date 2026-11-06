
import re


# =========================================================
# 1. 参数识别规则
# =========================================================

PARAMETER_RULES = {
    "拉伸温度": [
        "stretching temperature",
        "stretch temperature",
        "draw temperature",
        "drawing temperature",
        "temperature during stretching",
        "temperature during drawing",
        "stretched at",
        "stretching at",
        # PDF 表格中常见的粘连写法
        "stretchingtemperature",
    ],
    "温度字段候选": [
        "temperature (°c)",
        "temperature (c)",
    ],
    "拉伸倍率": [
        "draw ratio",
        "draw ratios",
        "stretch ratio",
        "drawing ratio",
        "biaxial draw ratio",
        "uniaxial draw ratio",
        # PDF 表格中常见的粘连写法
        "stretchingratio",
        "drawratio",
    ],
    "取向比": [
        "orientation ratio",
        "orientation ratios",
    ],
    "拉伸速度": [
        "stretching speed",
        "drawing speed",
        "draw speed",
        "stretch speed",
    ],
    "热收缩率": [
        "thermal shrinkage",
        "heat shrinkage",
        "shrinkage",
    ],
    "线速度": [
        "line speed",
        "web speed",
    ],
    "收卷张力": [
        "winding tension",
        "web tension",
    ],
    "结晶温度": [
        "crystallization temperature",
    ],
    "热定型温度": [
        "heat-setting temperature",
        "heat setting temperature",
    ],
    "模头出口速度": [
        "die exit velocity",
        "die exit speed",
    ],
}


def identify_parameter(text):
    """识别证据中出现的参数名称。"""

    text = str(text).lower()
    matched = []

    for parameter, keywords in PARAMETER_RULES.items():
        if any(keyword in text for keyword in keywords):
            matched.append(parameter)

    return matched or ["待人工识别"]


# =========================================================
# 2. 文本标准化
# =========================================================

def normalize_text(text):
    """统一 PDF 中常见的单位字符写法。

    只做字符层面的等价转换（如全角数字 ７５ -> 75），
    不会修复或补全任何缺失的实验数据。
    """

    text = str(text)

    replacements = {
        "–": "-",
        "−": "-",
        "—": "-",
        "⁻": "-",
        "¹": "1",
        "²": "2",
        "³": "3",
        "μ": "u",
        "µ": "u",
        "：": ":",
        "（": "(",
        "）": ")",
        "％": "%",
        "．": ".",
        "，": ",",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # 全角数字转半角，避免表格中的 ７５ 无法识别
    for index in range(10):
        text = text.replace(chr(0xFF10 + index), str(index))

    # PDF 中常见的 95oC / 80oF 写法（只在数字后转换，避免破坏普通单词）
    text = re.sub(r"(?<=\d)\s*o\s*C\b", " °C", text)
    text = re.sub(r"(?<=\d)\s*o\s*F\b", " °F", text)

    return text


# =========================================================
# 3. 数值与单位提取
# =========================================================

# 表格行首列中可能出现的单位写法（按优先级匹配）
TABLE_UNIT_PATTERNS = [
    (r"℃", "℃"),
    (r"°\s*C\b", "°C"),
    (r"°\s*F\b", "°F"),
    (r"％|%", "%"),
    (r"mm\s*s\s*[-−]?\s*1", "mm/s"),
    (r"m\s*min\s*[-−]?\s*1", "m/min"),
    (r"mm/s", "mm/s"),
    (r"m/min", "m/min"),
    (r"MPa", "MPa"),
    (r"GPa", "GPa"),
    (r"kPa", "kPa"),
    (r"N/m", "N/m"),
    (r"μm|um", "μm"),
    (r"\bmm\b", "mm"),
    (r"\bcm\b", "cm"),
    (r"\[N\]", "N"),
]

# 首列没有单位、但明确是参数名时，也允许提取数值
TABLE_PARAMETER_HINTS = (
    "ratio",
    "temperature",
    "speed",
    "tension",
    "thickness",
    "strength",
    "modulus",
    "strain",
    "stress",
    "温度",
    "速度",
    "张力",
    "厚度",
    "倍率",
    "强度",
    "应变",
    "模量",
)


def _detect_table_unit(label):
    for pattern, unit in TABLE_UNIT_PATTERNS:
        if re.search(pattern, label, re.IGNORECASE):
            return unit

    return None


def extract_table_row_measurements(text):
    """从表格行中提取"首列标签 + 数值列"的数据。

    例如：``Stretchingtemperature（℃） | ７５ | ７５``
    单位来自行首标签，数值来自后续单元格，属于"单位继承"候选，
    只用于展示和检索，不代表已核实实验条件。
    """

    raw = str(text)

    if "|" not in raw:
        return []

    cells = [cell.strip() for cell in raw.split("|") if cell.strip()]

    if len(cells) < 2:
        return []

    label = cells[0].lower()
    unit = _detect_table_unit(label)
    has_parameter_hint = any(
        hint in label for hint in TABLE_PARAMETER_HINTS
    )

    if not unit and not has_parameter_hint:
        return []

    number_pattern = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
    results = []

    for cell in cells[1:]:

        value = cell.replace(" ", "")

        if not number_pattern.fullmatch(value):
            continue

        results.append({
            "数值": value,
            "单位": unit,
            "原始表达": cell,
            "数值类型": (
                "表格行单位继承候选"
                if unit
                else "表格行参数数值候选"
            ),
        })

        if len(results) >= 8:
            break

    return results



def extract_measurements(text):
    """
    提取数值及单位。

    规则：
    1. PDF 编码异常直接拦截。
    2. 支持连续提取多个比例。
    3. 排除角度、图号和参考文献编号。
    4. 无单位数值必须与明确参数表达关联。
    """

    text = normalize_text(text)

    if re.search(r"\(cid:\s*\d+\)", text, re.IGNORECASE):
        return []

    results = []
    occupied_spans = []

    # --------------------------------------------------
    # A. 比例提取：支持一段文字中出现多个比例
    # --------------------------------------------------

    ratio_pattern = (
        r"(?<![\w.])"
        r"(\d+(?:\.\d+)?)"
        r"\s*:\s*"
        r"(\d+(?:\.\d+)?)"
        r"(?![\w.])"
    )

    for match in re.finditer(ratio_pattern, text):

        results.append({
            "数值": f"{match.group(1)}:{match.group(2)}",
            "单位": None,
            "原始表达": match.group(0),
            "数值类型": "比例候选",
        })

        occupied_spans.append(match.span())

    # --------------------------------------------------
    # B. 带单位数值提取
    # --------------------------------------------------

    number = r"[-+]?\d+(?:\.\d+)?"

    unit_pattern = (
        r"mm\s*s\s*-\s*1|"
        r"m\s*min\s*-\s*1|"
        r"mm/s|m/min|"
        r"°\s*[CF]|℃|"
        r"MPa|GPa|kPa|"
        r"um|mm|cm|"
        r"kg|%|min|s"
    )

    measurement_pattern = (
        r"(?<![\w.])"
        r"(" + number + r")"
        r"\s*"
        r"(" + unit_pattern + r")"
        r"(?!\w)"
    )

    for match in re.finditer(
        measurement_pattern,
        text,
        flags=re.IGNORECASE
    ):

        if any(
            match.start() < end and match.end() > start
            for start, end in occupied_spans
        ):
            continue

        unit = re.sub(r"\s+", " ", match.group(2)).strip()

        if re.fullmatch(r"mm\s*s\s*-\s*1", unit, re.I):
            unit = "mm/s"

        elif re.fullmatch(r"m\s*min\s*-\s*1", unit, re.I):
            unit = "m/min"

        elif unit.lower() == "um":
            unit = "μm"

        results.append({
            "数值": match.group(1),
            "单位": unit,
            "原始表达": match.group(0),
            "数值类型": "带单位数值",
        })

        occupied_spans.append(match.span())

    # --------------------------------------------------
    # C. 明确参数关联的无单位数值
    # --------------------------------------------------

    # 只对 draw ratio 等明确参数表达提取无单位数字。
    # orientation ratio 的数字由 A 部分的比例规则负责。

    parameter_pattern = re.compile(
        r"(?:draw\s+ratios?|"
        r"stretch\s+ratios?|"
        r"drawing\s+ratios?)"
        r"\s*(?:was|were|of|is|:|=)?\s*"
        r"(" + number + r")",
        re.IGNORECASE
    )

    for match in parameter_pattern.finditer(text):

        value_start, value_end = match.span(1)

        # 跳过已提取的数字
        if any(
            value_start < end and value_end > start
            for start, end in occupied_spans
        ):
            continue

        # 排除角度，例如 45°
        after_value = text[value_end:]

        if re.match(r"\s*°", after_value):
            continue

        results.append({
            "数值": match.group(1),
            "单位": None,
            "原始表达": match.group(1),
            "数值类型": "无单位参数数值候选",
        })

        occupied_spans.append((value_start, value_end))

    # --------------------------------------------------
    # D. 表格行单位继承
    # --------------------------------------------------

    if "|" in text:

        existing = {
            (str(item["数值"]), str(item["单位"]))
            for item in results
        }

        for item in extract_table_row_measurements(text):

            key = (item["数值"], str(item["单位"]))

            if key not in existing:
                existing.add(key)
                results.append(item)

    return results

    """
    提取数值及单位。
    优先识别比例和带单位数值。
    无单位数值必须与参数名称明确关联。
    """

    text = normalize_text(text)

    # 1. 编码异常直接拦截
    if re.search(r"\(cid:\s*\d+\)", text, re.IGNORECASE):
        return []

    results = []
    occupied_spans = []

    # 2. 识别比例：1 : 5.2、1:5.5
    ratio_pattern = (
        r"(?<![\w.])"
        r"(\d+(?:\.\d+)?)"
        r"\s*:\s*"
        r"(\d+(?:\.\d+)?)"
        r"(?![\w.])"
    )

    for match in re.finditer(ratio_pattern, text):

        results.append({
            "数值": f"{match.group(1)}:{match.group(2)}",
            "单位": None,
            "原始表达": match.group(0),
            "数值类型": "比例候选",
        })

        occupied_spans.append(match.span())

    # 3. 识别带单位数值
    number = r"[-+]?\d+(?:\.\d+)?"

    unit_pattern = (
        r"mm\s*s\s*-\s*1|"
        r"m\s*min\s*-\s*1|"
        r"mm/s|m/min|"
        r"°\s*[CF]|℃|"
        r"MPa|GPa|kPa|"
        r"um|mm|cm|"
        r"kg|%|min|s"
    )

    measurement_pattern = (
        r"(?<![\w.])"
        r"(" + number + r")"
        r"\s*"
        r"(" + unit_pattern + r")"
        r"(?!\w)"
    )

    for match in re.finditer(
        measurement_pattern,
        text,
        flags=re.IGNORECASE
    ):

        if any(
            match.start() < end and match.end() > start
            for start, end in occupied_spans
        ):
            continue

        unit = re.sub(r"\s+", " ", match.group(2)).strip()

        if re.fullmatch(r"mm\s*s\s*-\s*1", unit, re.I):
            unit = "mm/s"

        elif re.fullmatch(r"m\s*min\s*-\s*1", unit, re.I):
            unit = "m/min"

        elif unit.lower() == "um":
            unit = "μm"

        results.append({
            "数值": match.group(1),
            "单位": unit,
            "原始表达": match.group(0),
            "数值类型": "带单位数值",
        })

        occupied_spans.append(match.span())

    # 4. 识别与参数名称明确关联的无单位数值
    parameter_patterns = [
        r"draw\s+ratio",
        r"stretch\s+ratio",
        r"drawing\s+ratio",
        r"orientation\s+ratio",
    ]

    parameter_regex = re.compile(
        "|".join(parameter_patterns),
        re.IGNORECASE
    )

    # 只检查参数名称之后、下一个句子边界之前的数字
    for parameter_match in parameter_regex.finditer(text):

        start = parameter_match.end()

        # 参数名称与数字之间不能跨越句号、分号
        tail = re.split(r"[.;\n]", text[start:], maxsplit=1)[0]

        # 允许表达：was 4.0、of 4.0、: 4.0
        candidate = re.search(
            r"(?:was|of|is|at|:|=)?\s*"
            r"(" + number + r")"
            r"(?![\w.])",
            tail,
            re.IGNORECASE
        )

        if not candidate:
            continue

        value_start = start + candidate.start(1)
        value_end = start + candidate.end(1)

        # 排除已经提取过的比例或带单位数字
        if any(
            value_start < end and value_end > span_start
            for span_start, end in occupied_spans
        ):
            continue

        results.append({
            "数值": candidate.group(1),
            "单位": None,
            "原始表达": candidate.group(1),
            "数值类型": "无单位参数数值候选",
        })

        occupied_spans.append((value_start, value_end))

    return results


# =========================================================
# 4. 证据类型识别
# =========================================================

def identify_evidence_type(text):
    """判断正文、表头或表格数据行。"""

    text = str(text)

    if "|" not in text:
        return "正文证据候选"

    # 删除 Markdown 表格分隔符
    cleaned = re.sub(r"\|?[\s:-]+\|?[\s:-]*", "", text)

    has_number = bool(re.search(r"\d", cleaned))

    if not has_number:
        return "表头候选"

    return "表格数值行候选"


# =========================================================
# 5. 证据质量判断
# =========================================================

def assess_quality(text):
    """初步检查证据质量。"""

    text = str(text)
    issues = []

    if not text.strip():
        issues.append("空证据")

    if re.search(r"\(cid:\s*\d+\)", text, re.IGNORECASE):
        issues.append("存在PDF编码异常")
        return issues

    measurements = extract_measurements(text)

    if not measurements:
        issues.append("没有识别到有效候选数值")

    if not issues:
        issues.append("初步解析成功，仍需核验")

    return issues


# =========================================================
# 6. 生成结构化证据
# =========================================================

def process_evidence(text):
    """生成结构化证据结果。"""

    return {
        "参数类型": identify_parameter(text),
        "证据类型": identify_evidence_type(text),
        "测量值": extract_measurements(text),
        "质量标记": assess_quality(text),
        "原始证据": str(text),
    }


# =========================================================
# 7. 测试
# =========================================================

if __name__ == "__main__":

    samples = [
        "stretched films taken at stretching speed of 75 mm s–1.",

        "orientation ratio of 1 : 5.2 and 1 : 5.5.",

        "Temperature (°C) | Draw Ratio | Die Exit Velocity (mm/s)",

        "PSO | -(cid:31)0.4351",

        "Figure 2 Ultimate strain as a function of orientation ratio.",

        "diagonal (45°), because the orientation ratios in ma-",

        "Figure 4 (a) Storage modulus as a function of orientation ratio.",

        "The draw ratio was 4.0.",

        "The samples were stretched at 95°C.",
    ]

    for sample in samples:

        print("\n" + "=" * 60)

        result = process_evidence(sample)

        for key, value in result.items():
            print(f"{key}: {value}")
