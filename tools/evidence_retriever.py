# -*- coding: utf-8 -*-
"""BOPET 文献证据检索模块。

检索方式：SQLite 全表扫描 + 关键词匹配 + 规则评分。
这不是向量检索，也不是语义检索；它只负责把数据库中的原始证据取出来。

设计约束：
- 不修改数据库字段，也不删除原始数据。
- 数据库只读打开，避免误写。
- 字段名通过映射兼容，避免假设三张表结构完全一致。
"""

import re
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path


try:  # 作为 tools.evidence_retriever 导入
    from .evidence_processor import process_evidence
except ImportError:  # 直接运行 python tools/evidence_retriever.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from evidence_processor import process_evidence


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 数据库路径基于项目位置，不依赖当前终端目录
DB_PATH = PROJECT_ROOT / "data" / "database" / "bopet.db"

TABLES = [
    ("evidence_01_50", "原页证据"),
    ("evidence_51_100", "原页证据"),
    ("table_data", "表格数据"),
]

# 不同表的正文字段名可能不同，这里做显式映射
CONTENT_FIELDS = {
    "evidence_01_50": ["原页量化证据（截断）", "原页量化证据"],
    "evidence_51_100": ["原页量化证据（截断）", "原页量化证据"],
    "table_data": ["表格原行（截断）", "表格原行"],
}

FIELD_ALIASES = {
    "paper_id": ["编号", "paper_id", "id"],
    "title": ["题名", "title"],
    "topic": ["主题分类", "topic", "category"],
    "page": ["页码", "page"],
    "data_type": ["数据类型", "data_type"],
    "pdf": ["本地PDF", "local_pdf", "pdf"],
}


# =========================================================
# 1. 检索词与同义词
# =========================================================

# 中文参数名 -> 英文表达。英文关键词也会反向映射回同一组。
SYNONYMS = {
    "拉伸温度": [
        "stretching temperature",
        "stretch temperature",
        "draw temperature",
        "drawing temperature",
        "tenter temperature",
        "temperature",
        "stretching",
        "drawing",
        "orientation",
    ],
    "拉伸倍率": [
        "draw ratio",
        "draw ratios",
        "stretch ratio",
        "drawing ratio",
        "biaxial draw",
        "uniaxial draw",
    ],
    "拉伸速度": [
        "stretching speed",
        "drawing speed",
        "draw speed",
        "line speed",
        "web speed",
    ],
    "拉伸强度": [
        "tensile strength",
        "tensile stress",
    ],
    "热收缩率": [
        "heat shrinkage",
        "thermal shrinkage",
        "shrinkage",
        "heat shrink",
    ],
    "收缩率": [
        "heat shrinkage",
        "thermal shrinkage",
        "shrinkage",
    ],
    "结晶温度": [
        "crystallization temperature",
        "crystallization",
    ],
    "结晶度": [
        "crystallinity",
        "degree of crystallinity",
    ],
    "热定型温度": [
        "heat-setting temperature",
        "heat setting temperature",
        "annealing temperature",
        "heat setting",
    ],
    "收卷张力": [
        "winding tension",
        "web tension",
        "tension",
    ],
    "厚度": [
        "thickness",
        "film thickness",
    ],
    "模头温度": [
        "die temperature",
        "melt temperature",
    ],
    "取向度": [
        "orientation degree",
        "degree of orientation",
    ],
}

# 过于泛化的单词，命中时只给较低权重，避免 "temperature" 淹没真正的参数短语
GENERIC_TERMS = {
    "temperature",
    "stretching",
    "drawing",
    "orientation",
    "tension",
    "film",
    "thickness",
    "speed",
    "ratio",
    "shrinkage",
}

# 英文关键词 -> 中文组名
_EN_TO_GROUP = {}
for _group, _terms in SYNONYMS.items():
    for _term in _terms:
        _EN_TO_GROUP.setdefault(_term.lower(), _group)


def _normalize_lookup(text):
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def get_search_groups(keyword):
    """把用户关键词扩展为一组同义词。

    返回 [(组名, [检索词...]), ...]。当前一次只检索一个参数组。
    """

    keyword = str(keyword or "").strip()

    if not keyword:
        return []

    lookup = _normalize_lookup(keyword)

    if lookup in SYNONYMS:
        terms = [lookup] + [t.lower() for t in SYNONYMS[lookup]]
        return [(lookup, _dedupe(terms))]

    group = _EN_TO_GROUP.get(lookup)

    if group:
        terms = [group.lower()] + [t.lower() for t in SYNONYMS[group]]
        return [(group, _dedupe(terms))]

    return [(keyword, [lookup])]


def get_search_terms(keyword):
    """兼容原有接口：返回扁平化的检索词列表。"""

    terms = []

    for _, group_terms in get_search_groups(keyword):
        terms.extend(group_terms)

    return _dedupe(terms)


def _dedupe(items):
    seen = set()
    result = []

    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)

    return result


def _compact(text):
    """去掉空白，用于匹配 PDF 中粘连的 "Stretchingtemperature" 这类写法。"""

    return re.sub(r"\s+", "", str(text or "").lower())


def looks_like_broken_text(text):
    """粗略识别 PDF 编码异常产生的乱码文本。

    这类文本不能当作实验数据，只降低排序分数并保留质量标记。
    """

    text = str(text or "")

    if len(text) < 20:
        return False

    suspicious = sum(
        1 for char in text
        if 0x80 <= ord(char) <= 0x24F
    )

    return suspicious >= 6 and suspicious / len(text) >= 0.12


# =========================================================
# 2. 字段适配
# =========================================================

def _first_value(row, names, default=None):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]

        # 字段名大小写不一致时做一次宽松匹配
        for key in row:
            if key.lower() == name.lower() and row[key] not in (None, ""):
                return row[key]

    return default


def get_row_content(row, table):
    for name in CONTENT_FIELDS.get(table, []):
        value = row.get(name)

        if value:
            return str(value)

    return ""


def classify_material(text):
    """根据标题和证据文本初步识别材料。

    仅为文本规则判断，不代表已确认实验材料；不能据此断定属于 BOPET。
    """

    text = str(text).lower()

    has_pet = (
        "bopet" in text
        or "polyethylene terephthalate" in text
        or re.search(r"\bpet\b", text)
    )

    has_pla = (
        "polylactide" in text
        or "polylactic" in text
        or "bopla" in text
        or re.search(r"\bpla\b", text)
    )

    has_pbat = (
        "butylene adipate" in text
        or re.search(r"\bpbat\b", text)
    )

    has_pps = (
        "poly(phenylene sulfide)" in text
        or re.search(r"\bpps\b", text)
    )

    has_pe = (
        "polyethylene" in text
        and "terephthalate" not in text
    )

    has_pm = (
        "polymethyl methacrylate" in text
        or "pmma" in text
    )

    has_copolyester = (
        "co-polyester" in text
        or "copolyester" in text
        or "co-pes" in text
    )

    materials = []

    if has_pet:
        materials.append("PET/BOPET")

    if has_pla:
        materials.append("PLA")

    if has_pbat:
        materials.append("PBAT")

    if has_pe:
        materials.append("PE")

    if has_pps:
        materials.append("PPS")

    if has_pm:
        materials.append("PMMA")

    if has_copolyester and not has_pet:
        materials.append("共聚酯（Co-PES，材料归属待核实）")

    if materials:
        return "；".join(materials)

    return "未知，待核实"


# =========================================================
# 3. 评分
# =========================================================

def _term_weight(term):
    if term in GENERIC_TERMS:
        return 2

    # 英文短单词很可能是泛词，多词短语更具体
    if term.isascii() and len(term.split()) == 1 and len(term) <= 4:
        return 4

    return 10


def calculate_score(row, terms, table):
    """计算内容、标题和主题的匹配分数。

    评分只用于排序，不代表证据真实性或材料归属。
    """

    title = str(_first_value(row, FIELD_ALIASES["title"], "") or "").lower()
    topic = str(_first_value(row, FIELD_ALIASES["topic"], "") or "").lower()
    content = get_row_content(row, table).lower()

    compact_content = _compact(content)

    content_score = 0
    title_score = 0
    topic_score = 0
    matched_content = []
    matched_topic = []

    for term in terms:

        hit_content = bool(term) and term in content
        hit_title = bool(term) and term in title
        hit_topic = bool(term) and term in topic

        # 处理 PDF 中粘连的短语，例如 Stretchingtemperature（℃）
        if (
            not hit_content
            and " " in term
            and _compact(term) in compact_content
        ):
            hit_content = True

        if hit_content:
            content_score += _term_weight(term)
            matched_content.append(term)

        if hit_title:
            title_score += 3

        if hit_topic:
            topic_score += 2
            matched_topic.append(term)

    # 同一参数组内多个同义词命中时不无限叠加
    content_score = min(content_score, 18)
    title_score = min(title_score, 6)
    topic_score = min(topic_score, 6)

    score = content_score + title_score + topic_score

    if matched_content:
        match_type = "原文内容命中"
    elif matched_topic:
        match_type = "仅主题分类命中"
    elif score > 0:
        match_type = "论文标题命中"
    else:
        match_type = "未命中"

    return score, match_type, matched_content


# =========================================================
# 4. 数据库访问
# =========================================================

@contextmanager
def open_database(db_path=None):
    """只读打开数据库；文件不存在时给出明确错误，不自动建库。"""

    path = Path(db_path) if db_path else DB_PATH

    if not path.exists():
        raise FileNotFoundError(
            f"找不到文献数据库：{path}\n"
            "请确认 data/database/bopet.db 是否存在，程序不会自动创建空数据库。"
        )

    conn = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
    )
    conn.row_factory = sqlite3.Row

    try:
        yield conn
    finally:
        conn.close()


def get_table_counts(db_path=None):
    """返回目标数据表的记录数量，供 Demo 启动检查使用。"""

    counts = {}

    with open_database(db_path) as conn:
        cursor = conn.cursor()

        for table, _ in TABLES:
            try:
                cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                counts[table] = cursor.fetchone()[0]
            except sqlite3.DatabaseError as exc:
                counts[table] = f"读取失败：{exc}"

    return counts


# =========================================================
# 5. 单关键词检索
# =========================================================

def _result_key(row):
    """同一条证据的去重键：来源表 + 编号 + 页码 + 正文。"""

    return (
        row.get("来源数据表"),
        row.get("编号"),
        row.get("页码"),
        re.sub(r"\s+", " ", str(row.get("_正文") or "")).strip()[:200],
    )


def search_evidence(keyword, limit=20):
    """按一个关键词检索证据，并调用 evidence_processor 做规则处理。

    返回的每一行保留数据库原始字段，并附加：
    检索分数、命中类型、命中词、材料体系、证据来源类型、参数类型、测量值、质量标记。
    """

    keyword = str(keyword or "").strip()

    if not keyword:
        return []

    groups = get_search_groups(keyword)
    terms = []

    for _, group_terms in groups:
        terms.extend(group_terms)

    terms = _dedupe(terms)

    results = []
    seen = set()

    with open_database() as conn:
        cursor = conn.cursor()

        for table, source_type in TABLES:

            try:
                cursor.execute(f'SELECT * FROM "{table}"')
            except sqlite3.DatabaseError as exc:
                raise RuntimeError(
                    f"读取数据表 {table} 失败：{exc}"
                ) from exc

            for record in cursor.fetchall():

                row = dict(record)
                content = get_row_content(row, table)

                if not content.strip():
                    continue

                score, match_type, matched = calculate_score(
                    row,
                    terms,
                    table,
                )

                if score <= 0:
                    continue

                row["来源数据表"] = table
                row["_正文"] = content

                key = _result_key(row)

                if key in seen:
                    continue

                seen.add(key)

                title = str(
                    _first_value(row, FIELD_ALIASES["title"], "") or ""
                )

                processed = process_evidence(content)
                quality = list(processed["质量标记"])

                if looks_like_broken_text(content):
                    score = max(score - 5, 1)
                    quality.append("疑似PDF编码异常，文本可能不可靠")

                row["检索分数"] = score
                row["命中类型"] = match_type
                row["命中词"] = matched
                row["材料体系"] = classify_material(title + " " + content)
                row["证据来源类型"] = source_type
                row["参数类型"] = processed["参数类型"]
                row["证据类型"] = processed["证据类型"]
                row["测量值"] = processed["测量值"]
                row["质量标记"] = quality

                results.append(row)

    results.sort(
        key=lambda item: (
            -item["检索分数"],
            item.get("编号") or 0,
            item.get("页码") or 0,
        )
    )

    return results[:limit]


# =========================================================
# 6. 多关键词检索
# =========================================================

def search_multiple(
    keywords,
    per_keyword_limit=8,
    max_results=12,
    max_per_paper=2,
):
    """分别检索多个关键词，合并并按分数排序。

    - 同一关键词组内的结果先去重；
    - 不同关键词命中同一条证据时保留一条，并记录全部匹配关键词；
    - 限制单篇文献占用的条数，避免上下文被一篇文献占满。
    """

    merged = {}
    order = []

    for keyword in keywords or []:

        for row in search_evidence(keyword, limit=per_keyword_limit):

            key = _result_key(row)

            if key not in merged:
                row = dict(row)
                row["匹配关键词"] = [keyword]
                merged[key] = row
                order.append(key)
            else:
                existing = merged[key]
                existing["匹配关键词"].append(keyword)
                # 多关键词共同命中的证据略微加权
                existing["检索分数"] += 2

    ordered = sorted(
        (merged[key] for key in order),
        key=lambda item: (
            -item["检索分数"],
            item.get("编号") or 0,
            item.get("页码") or 0,
        ),
    )

    selected = []
    per_paper = {}

    for row in ordered:

        paper_id = row.get("编号")
        used = per_paper.get(paper_id, 0)

        if used >= max_per_paper:
            continue

        per_paper[paper_id] = used + 1
        selected.append(row)

        if len(selected) >= max_results:
            break

    # 单篇证据超过上限时不再强行补足，避免同一篇文献占满上下文
    return selected[:max_results]


# =========================================================
# 7. 统一证据结构
# =========================================================

def format_measurement(measurements):
    """把 process_evidence 的测量值列表格式化为可读字符串列表。"""

    formatted = []

    for item in measurements or []:

        value = item.get("数值")
        unit = item.get("单位")

        if value in (None, ""):
            continue

        formatted.append(f"{value} {unit}".strip() if unit else str(value))

    return formatted


def to_evidence_item(row, index=None):
    """把数据库行转换成统一的证据字典。

    只使用真实字段；字段不存在时返回 None 或空列表，不编造内容。
    """

    measurements = row.get("测量值") or []
    parameters = row.get("参数类型") or []
    quality = row.get("质量标记") or []

    item = {
        "evidence_id": f"E{index}" if index is not None else None,
        "paper_id": _first_value(row, FIELD_ALIASES["paper_id"]),
        "title": _first_value(row, FIELD_ALIASES["title"]),
        "topic": _first_value(row, FIELD_ALIASES["topic"]),
        "page": _first_value(row, FIELD_ALIASES["page"]),
        "data_type": _first_value(row, FIELD_ALIASES["data_type"]),
        "material_candidate": row.get("材料体系"),
        "evidence_source": row.get("证据来源类型"),
        "original_text": get_row_content(
            row, row.get("来源数据表", "")
        ) or row.get("_正文"),
        "parameter_type": "、".join(parameters) if parameters else None,
        "measurement": format_measurement(measurements),
        "quality_flag": "；".join(quality) if quality else None,
        "retrieval_score": row.get("检索分数"),
        "matched_keywords": row.get("匹配关键词") or [],
        "match_type": row.get("命中类型"),
        "pdf_path": _first_value(row, FIELD_ALIASES["pdf"]),
    }

    # 表格数据补充表序号/行序号，正文证据没有这两个字段
    if row.get("来源数据表") == "table_data":
        item["table_no"] = row.get("表序号")
        item["row_no"] = row.get("行序号")
    else:
        item["table_no"] = None
        item["row_no"] = None

    return item


def build_evidence_items(rows):
    return [
        to_evidence_item(row, index=index)
        for index, row in enumerate(rows, 1)
    ]


# =========================================================
# 8. 命令行调试
# =========================================================

if __name__ == "__main__":

    keyword = (
        sys.argv[1] if len(sys.argv) > 1
        else input("请输入检索关键词：").strip()
    )

    if not keyword:
        print("关键词不能为空")
        raise SystemExit

    try:
        rows = search_evidence(keyword)
    except Exception as exc:
        print(f"检索失败：{exc}")
        raise SystemExit(1)

    print(f"\n关键词：{keyword}")
    print(f"找到相关记录：{len(rows)} 条")
    print("=" * 65)

    for i, item in enumerate(build_evidence_items(rows), 1):

        print(f"\n========== 证据 {i} ==========")
        print("论文编号：", item["paper_id"])
        print("题名：", item["title"])
        print("页码：", item["page"])
        print("主题：", item["topic"])
        print("证据来源：", item["evidence_source"])
        print("材料（规则初判）：", item["material_candidate"])
        print("参数类型：", item["parameter_type"])
        print("测量值：", item["measurement"])
        print("质量标记：", item["quality_flag"])
        print("命中关键词：", item["matched_keywords"])
        print("检索分数：", item["retrieval_score"])
        print("原始证据：", item["original_text"])
        print("-" * 65)
