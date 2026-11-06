# -*- coding: utf-8 -*-
"""BOPET 文献问答 Agent。

职责分界：
- Qwen：理解用户问题、生成检索关键词、依据检索证据整理回答；
- SQLite：提供真实文献证据。

Qwen 不会在没有检索证据的情况下回答文献数据问题。
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
from tools.evidence_retriever import (
    DB_PATH,
    GENERIC_TERMS,
    SYNONYMS,
    build_evidence_items,
    get_search_groups,
    get_table_counts,
    search_multiple,
)


NO_RESULT_MESSAGE = "当前数据库未检索到相关证据。"

# 这些词太宽泛，命中大量无关文献，不作为检索关键词
KEYWORD_STOPWORDS = {
    "薄膜",
    "实验数据",
    "数据",
    "文献",
    "研究",
    "论文",
    "数据库",
    "相关",
    "实验",
    "拉伸工艺",
    "加工参数",
    "工艺参数",
    "film",
    "films",
    "data",
    "experiment",
    "experiments",
    "experimental data",
    "paper",
    "papers",
    "study",
    "studies",
    "database",
    "stretching process",
    "process parameters",
}

# 材料名只用于限定语境；单独拿材料名检索会命中大量无关文献
MATERIAL_ONLY_KEYWORDS = {
    "pet",
    "bopet",
    "pe",
    "pla",
    "pbat",
    "pps",
    "pmma",
    "polymer",
    "polyester",
    "聚酯",
    "聚合物",
    "薄膜",
    "film",
    "films",
}


class QAError(RuntimeError):
    """问答流程中的可定位错误，step 表示失败环节。"""

    def __init__(self, step, message):
        self.step = step
        self.message = str(message)
        super().__init__(f"{step}失败：{self.message}")


def material_note(material):
    """把规则初判的材料转换成"能否作为 BOPET 依据"的明确提示。

    该提示直接来自检索器的文本规则，不依赖模型判断。
    """

    material = str(material or "未知，待核实")

    if "未知" in material:
        return "材料归属待核实，不能作为 BOPET 的直接实验依据"

    if "共聚酯" in material:
        return "共聚酯（Co-PES），不是已确认的 BOPET，只能按原文语境判断"

    if "PET/BOPET" in material:
        others = {
            tag for tag in ("PPS", "PLA", "PBAT", "PMMA", "PE")
            if tag in material.split("；")
        }

        if others:
            return "规则初判同时含 PET/BOPET 与其他材料，必须区分具体证据"

        return "规则初判含 PET/BOPET，仍需核实具体实验材料"

    for tag in ("PLA", "PBAT", "PPS", "PMMA"):
        if tag in material:
            return f"{tag}，非 BOPET，只能作为跨材料参考"

    if material.strip() == "PE":
        return "PE，非 BOPET，只能作为跨材料参考"

    return f"{material}，需核实材料归属"


# 参数类型 -> 合理单位；用于过滤"95 °C 被当成热收缩率"这类量纲错误
PARAMETER_UNIT_HINTS = {
    "拉伸温度": {"℃", "°C", "°F", "K"},
    "结晶温度": {"℃", "°C", "°F", "K"},
    "热定型温度": {"℃", "°C", "°F", "K"},
    "热收缩率": {"%"},
    "拉伸速度": {"mm/s", "m/min"},
    "收卷张力": {"N", "N/m"},
    "厚度": {"μm", "um", "mm", "cm"},
    "拉伸强度": {"MPa", "GPa", "kPa"},
}

DIMENSIONLESS_PARAMETERS = {"拉伸倍率", "取向度"}


def measurement_unit(text):
    text = str(text).strip()

    for unit in ("mm/s", "m/min", "N/m", "MPa", "GPa", "kPa",
                 "℃", "°C", "°F", "%", "μm", "um", "mm", "cm", "kg"):
        if text.endswith(unit):
            return unit

    return None


def keep_measurement_for_parameter(measurement, parameter_type):
    """按参数类型过滤明显不同量纲的数值。"""

    parameter_type = str(parameter_type or "")

    allowed = None

    for name, units in PARAMETER_UNIT_HINTS.items():
        if name in parameter_type:
            allowed = units
            break

    if allowed is None and any(
        name in parameter_type for name in DIMENSIONLESS_PARAMETERS
    ):
        allowed = {None}

    if allowed is None:
        return True

    return measurement_unit(measurement) in allowed


def check_database():
    """检查数据库文件和目标表，返回可供 Demo 展示的状态。"""
    status = {
        "path": str(DB_PATH),
        "exists": DB_PATH.exists(),
        "tables": {},
    }

    if status["exists"]:
        try:
            status["tables"] = get_table_counts()
        except Exception as exc:  # 数据库损坏或权限问题
            status["error"] = str(exc)

    return status


def _strip_code_fence(text):
    text = str(text or "").strip()

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    return text.strip()


def parse_keywords_json(content):
    """解析模型返回的关键词 JSON，兼容代码块和少量多余文字。"""

    text = _strip_code_fence(content)

    payload_candidates = [text]

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end > start:
        payload_candidates.append(text[start:end + 1])

    for payload in payload_candidates:

        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue

        if isinstance(data, dict):
            keywords = (
                data.get("keywords")
                or data.get("关键词")
                or data.get("terms")
            )
        elif isinstance(data, list):
            keywords = data
        else:
            keywords = None

        if isinstance(keywords, list):
            return keywords

    raise ValueError("模型返回内容不是可解析的关键词 JSON")


def clean_keywords(items, limit=8):
    """过滤空值、泛词和重复关键词。"""

    keywords = []

    for item in items or []:

        keyword = str(item or "").strip()

        if not keyword or len(keyword) > 40:
            continue

        if keyword.lower() in KEYWORD_STOPWORDS:
            continue

        if keyword in keywords:
            continue

        keywords.append(keyword)

    # 如果还有具体参数词，就去掉单独的材料名，避免检索被泛化
    specific = [
        keyword for keyword in keywords
        if keyword.lower() not in MATERIAL_ONLY_KEYWORDS
    ]

    if specific:
        keywords = specific

    return keywords[:limit]


def keyword_groups(keyword):
    """找出关键词所属的参数组。"""

    keyword_lower = str(keyword or "").lower()
    groups = []

    for group, terms in SYNONYMS.items():

        if group in keyword:
            groups.append(group)
            continue

        for term in terms:
            if term in keyword_lower or keyword_lower in term:
                groups.append(group)
                break

    return groups


def grounded_keywords(keywords, question, limit=8):
    """只保留能在用户问题中找到依据的关键词。

    检索器本身会把中文参数扩展成英文同义词，因此模型额外补充的
    "相关但不同"的参数（如问热收缩率时补充拉伸温度）会被过滤掉。
    """

    question_lower = str(question or "").lower()
    grounded = []

    for keyword in keywords:

        keyword_lower = keyword.lower()

        if keyword_lower in question_lower:
            grounded.append(keyword)
            continue

        matched = False

        for group in keyword_groups(keyword):

            if group.lower() in question_lower:
                matched = True
                break

            if any(
                term in question_lower
                for term in SYNONYMS.get(group, [])
            ):
                matched = True
                break

        if matched:
            grounded.append(keyword)

    # 如果过滤后为空，说明这是开放性问题，保留原关键词
    return (grounded or keywords)[:limit]


def target_parameter_groups(keywords):
    """从检索关键词反推用户真正询问的参数组。"""

    groups = []

    for keyword in keywords or []:

        for group, _ in get_search_groups(keyword):

            if group.lower() in MATERIAL_ONLY_KEYWORDS:
                continue

            if group not in groups:
                groups.append(group)

    return groups


def item_matches_parameter(item, groups):
    """判断一条证据是否真的对应目标参数。

    依据：
    - 规则识别出的 parameter_type；
    - 原始证据中是否出现目标参数的具体表达（不使用 temperature 等泛词）。
    """

    if not groups:
        return None

    parameter_type = str(item.get("parameter_type") or "")

    if any(group in parameter_type for group in groups):
        return True

    text = str(item.get("original_text") or "").lower()
    compact = re.sub(r"\s+", "", text)

    for group in groups:

        terms = [group] + [
            term for term in SYNONYMS.get(group, [])
            if term not in GENERIC_TERMS
        ]

        for term in terms:

            term = term.lower()

            if term in text or re.sub(r"\s+", "", term) in compact:
                return True

    return False


def fallback_keywords(question, limit=8):
    """关键词生成失败时，用规则从问题中提取检索词。"""

    question = str(question or "")
    lowered = question.lower()
    keywords = []

    for group, english_terms in SYNONYMS.items():

        if group in question:
            keywords.append(group)

            if english_terms:
                keywords.append(english_terms[0])

            continue

        for term in english_terms:
            if len(term) >= 4 and term in lowered:
                keywords.append(term)
                keywords.append(group)
                break

    if not keywords:
        cleaned = re.sub(
            r"有没有|是否|关于|哪些|什么|多少|请问|的实验数据|实验数据|数据|文献|研究|中|的|吗|呢",
            " ",
            question,
        )
        cleaned = re.sub(r"[，。？！、,.?!；;：:\s]+", " ", cleaned)

        for token in cleaned.split():
            if len(token) >= 2:
                keywords.append(token)

    return clean_keywords(keywords, limit) or [question.strip()[:20]]


class QAAgent:
    """文献问答 Agent，复用同一个 OpenAI Client。"""

    def __init__(self, config=None):
        self.config = config or CONFIG
        self.qa_config = self.config.get("qa", {})
        self._client = None
        self.last_keyword_raw = None
        self.last_keyword_source = None
        self.last_keyword_warning = None

    # -----------------------------------------------------
    # Qwen 客户端
    # -----------------------------------------------------

    def _get_client(self):
        if self._client is not None:
            return self._client

        api_key = os.getenv("SILICONFLOW_API_KEY")

        if not api_key:
            raise QAError(
                "初始化 Qwen 客户端",
                "没有找到环境变量 SILICONFLOW_API_KEY。"
                "请先设置该环境变量后重新运行。",
            )

        self._client = OpenAI(
            api_key=api_key,
            base_url=self.qa_config.get(
                "base_url",
                self.config["llm"]["base_url"],
            ),
            timeout=float(self.qa_config.get("timeout", 60.0)),
            max_retries=int(self.qa_config.get("max_retries", 0)),
        )

        return self._client

    def _chat(self, messages, temperature, max_tokens):
        client = self._get_client()

        kwargs = {
            "model": self.qa_config.get(
                "model",
                self.config["llm"]["model"],
            ),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if self.qa_config.get("enable_thinking") is False:
            try:
                return client.chat.completions.create(
                    **kwargs,
                    extra_body={"enable_thinking": False},
                )
            except Exception as exc:
                # 兼容不支持 enable_thinking 参数的兼容接口
                if "enable_thinking" not in str(exc).lower():
                    raise

        return client.chat.completions.create(**kwargs)

    @staticmethod
    def _response_text(response):
        if not getattr(response, "choices", None):
            raise QAError("调用 Qwen", "模型没有返回任何候选结果")

        content = response.choices[0].message.content

        if not content or not str(content).strip():
            raise QAError("调用 Qwen", "模型返回空内容")

        return str(content).strip()

    # -----------------------------------------------------
    # 功能 A：问题理解与关键词
    # -----------------------------------------------------

    def understand_question(self, question):
        """让 Qwen 把自然语言问题转换成检索关键词。"""

        self.last_keyword_raw = None
        self.last_keyword_source = None
        self.last_keyword_warning = None

        system_prompt = (
            "你是科研文献数据库的检索词生成助手。"
            "请把用户的问题转换成适合在 BOPET/PET 薄膜文献数据库中检索的关键词。\n\n"
            "只输出一个合法 JSON 对象，不要输出 Markdown 代码块、解释或多余文字。\n"
            '格式：{"keywords": ["关键词1", "关键词2"]}\n\n'
            "要求：\n"
            "1. 输出 1-6 个关键词，优先使用中文术语，并补充对应的英文术语。\n"
            "2. 保留问题中的材料、工艺参数和研究对象，"
            "例如 PET、BOPET、拉伸温度、draw ratio。\n"
            "3. 不要输出“薄膜”“实验数据”“数据”“文献”“研究”"
            "“film”“data”等过于宽泛的词。\n"
            "4. 不要加入问题中没有依据的材料或参数。\n"
            "5. 只输出与问题所问参数直接对应的同义表达，"
            "不要把问题扩展到其他参数（例如问热收缩率时不要加入拉伸温度、结晶度、draw ratio）。\n"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"用户问题：{question}"},
        ]

        try:
            response = self._chat(
                messages,
                temperature=float(
                    self.qa_config.get("keyword_temperature", 0.1)
                ),
                max_tokens=int(
                    self.qa_config.get("keyword_max_tokens", 300)
                ),
            )
            content = self._response_text(response)
            self.last_keyword_raw = content

            try:
                raw_keywords = parse_keywords_json(content)
                keywords = clean_keywords(
                    raw_keywords,
                    limit=int(self.qa_config.get("max_keywords", 6)),
                )
                keywords = grounded_keywords(
                    keywords,
                    question,
                    limit=int(self.qa_config.get("max_keywords", 6)),
                )
            except ValueError as exc:
                keywords = fallback_keywords(
                    question,
                    limit=int(self.qa_config.get("max_keywords", 6)),
                )
                self.last_keyword_source = "rule"
                self.last_keyword_warning = (
                    f"模型返回的关键词不是合法 JSON（{exc}），"
                    "已使用规则关键词降级检索。"
                )
                return keywords

            if not keywords:
                raise ValueError("模型返回的关键词为空")

            self.last_keyword_source = "qwen"
            return keywords

        except Exception as exc:
            keywords = fallback_keywords(
                question,
                limit=int(self.qa_config.get("max_keywords", 6)),
            )

            if not keywords:
                raise QAError("关键词理解", str(exc)) from exc

            self.last_keyword_source = "rule"
            self.last_keyword_warning = (
                f"Qwen 关键词生成失败（{exc}），已使用规则关键词降级检索。"
            )

            return keywords

    # -----------------------------------------------------
    # 功能 B/C：数据库检索与证据组织
    # -----------------------------------------------------

    def retrieve(self, keywords):
        """调用 SQLite 检索器，返回统一结构的证据列表。"""

        try:
            rows = search_multiple(
                keywords,
                per_keyword_limit=int(
                    self.qa_config.get("per_keyword_limit", 8)
                ),
                max_results=int(
                    self.qa_config.get("max_evidence", 10)
                ),
                max_per_paper=int(
                    self.qa_config.get("max_per_paper", 2)
                ),
            )
        except FileNotFoundError as exc:
            raise QAError("数据库检索", str(exc)) from exc
        except Exception as exc:
            raise QAError("数据库检索", str(exc)) from exc

        items = build_evidence_items(rows)

        for item in items:
            # 数据库中没有该字段时保持 None，不编造
            item.setdefault("table_no", None)
            item.setdefault("row_no", None)

        # 标记证据是否真的对应问题所问参数（规则判断，不依赖模型）
        groups = target_parameter_groups(keywords)

        for item in items:
            item["target_parameter_match"] = (
                item_matches_parameter(item, groups)
                if groups
                else None
            )
            item["parameter_confirmed"] = bool(
                groups
                and any(
                    group in str(item.get("parameter_type") or "")
                    for group in groups
                )
            )

            # 参数已确认时，过滤量纲明显不符的数值（例如 95 °C 不是收缩率）
            if item["parameter_confirmed"]:
                item["measurement"] = [
                    value for value in item.get("measurement", [])
                    if keep_measurement_for_parameter(
                        value, item.get("parameter_type")
                    )
                ]

        # 去掉既不对应目标参数、又没有数值的噪声证据
        if len(items) > 4:
            filtered = [
                item for item in items
                if item.get("target_parameter_match")
                or item.get("measurement")
            ]

            if len(filtered) >= 3:
                items = filtered

        # 目标参数证据排在前面，便于模型优先引用
        items.sort(
            key=lambda item: (
                0 if item.get("target_parameter_match") is True else 1,
                -(item.get("retrieval_score") or 0),
            )
        )

        # 过滤和排序后重新编号，保证上下文与界面展示的“证据 N”一致
        for index, item in enumerate(items, 1):
            item["evidence_id"] = f"E{index}"

        return items

    @staticmethod
    def build_context(question, evidence):
        """把证据整理成给 Qwen 的上下文 JSON。"""

        compact_evidence = []

        for item in evidence:

            compact_evidence.append({
                "evidence_id": item.get("evidence_id"),
                "paper_id": item.get("paper_id"),
                "title": item.get("title"),
                "topic": item.get("topic"),
                "page": item.get("page"),
                "table_no": item.get("table_no"),
                "row_no": item.get("row_no"),
                "data_type": item.get("data_type"),
                "material_candidate": item.get("material_candidate"),
                "material_note": material_note(
                    item.get("material_candidate")
                ),
                "evidence_source": item.get("evidence_source"),
                "original_text": item.get("original_text"),
                "parameter_type": item.get("parameter_type"),
                "target_parameter_match": item.get(
                    "target_parameter_match"
                ),
                "measurement": item.get("measurement"),
                "quality_flag": item.get("quality_flag"),
                "retrieval_score": item.get("retrieval_score"),
            })

        return json.dumps(
            {
                "question": question,
                "evidence_count": len(compact_evidence),
                "evidence": compact_evidence,
            },
            ensure_ascii=False,
            indent=2,
        )

    # -----------------------------------------------------
    # 功能 D：Qwen 依据证据回答
    # -----------------------------------------------------

    def build_answer_system_prompt(self):
        return (
            "你是一名科研文献证据分析助手，负责帮助用户检索和理解 "
            "BOPET 及相关聚合物薄膜文献中的实验数据。\n\n"
            "你只能依据系统提供的数据库检索证据回答问题。"
            "这些证据来自本地 SQLite 文献数据库，不是你的模型记忆。\n\n"
            "一、证据规则\n\n"
            "1. 不允许根据模型自身记忆编造数据库中的实验数据。\n"
            "2. 不允许虚构论文题目、文献编号、作者、页码、实验条件或测量值。\n"
            "3. 每个重要实验数据必须给出对应文献编号和页码，"
            "并标注证据编号，例如：[证据2｜论文 93，P.3]。\n"
            "4. 应尽可能引用原始证据文本。\n"
            '5. 如果数据库没有检索到相关证据，应明确回答“当前数据库未检索到相关证据”。\n'
            "6. 如果证据存在，但数据缺失或质量标记异常，应明确说明。\n"
            "7. 不允许将推测包装成文献事实。\n"
            "8. 不允许将不同论文中的数据拼接成同一个实验结果。\n\n"
            "二、材料区分规则\n\n"
            "必须严格区分 BOPET、PET、PE、PLA、PBAT、PPS 等材料体系。\n"
            "PET 不等于 BOPET。其他聚合物材料也不等于 BOPET。\n"
            "如果某条证据来自 PE、PLA、PBAT、PPS 等其他材料，"
            "只能作为跨材料参考，不能作为 BOPET 的直接实验依据。\n"
            '如果材料归属不明确，必须标记为“材料归属待核实”。\n'
            "不能仅凭题名或单个关键词，就断定某条证据一定属于 BOPET。\n"
            "材料判断只能引用证据中的 material_candidate 和 material_note 字段。\n"
            "禁止把 PLA、PBAT、共聚酯（Co-PES）或材料未知的证据称为 BOPET。\n"
            "第 4 节只列出回答中实际引用的证据，逐条原文复制 material_note，"
            "不得改写、合并或套用到其他证据。\n"
            "如果被引用证据的 material_note 都不是“规则初判含 PET/BOPET”，"
            "检索结论中不得声称“属于 BOPET 材料体系”。\n"
            "材料归属待核实（包括共聚酯 Co-PES）不等于可作为 BOPET 参考；"
            "只有 material_candidate 明确含 PET/BOPET 时才可称“与 PET/BOPET 相关”，且仍需核实。\n"
            "材料归属待核实的证据，只能说“不能确认属于 BOPET”，不得断言“非 BOPET”。\n"
            "材料归属结论只能出现在第 4 节，其他章节不要判断材料是否属于 BOPET。\n\n"
            "三、数据解释规则\n\n"
            "1. 必须区分实验参数、实验结果、表格字段、文献描述和模型识别结果。\n"
            "2. 不得将 OCR 错误或异常文本自动修复为真实实验数据。\n"
            "3. 不得将图号、页码、角度、日期等无关数字误认为实验参数。\n"
            "4. 不得将机器学习指标误认为真实工艺参数。\n"
            "5. 拉伸倍率、orientation ratio、draw ratio 等表达应依据原始语境判断，"
            "不能未经核实就认为它们完全等价。\n"
            "6. 如果数据只有数值而缺少单位，应明确注明单位缺失。\n"
            "7. 如果证据不足以支持结论，应明确说明证据局限。\n"
            "8. 不得把 MDO temperature、drying temperature、heat-setting temperature、"
            "crystallization temperature 等直接改写为拉伸温度，除非原始证据明确说明。\n"
            "9. 参数类型为“待人工识别”的证据，必须结合原始证据文本说明该数值"
            "实际对应什么参数，不得直接当作目标参数。\n"
            "10. 只有 target_parameter_match 为 true 的证据才能列入“实验数据”一节；"
            "为 false 的数值不得写成目标参数数据，只能放在“证据局限”中说明。\n\n"
            "四、输出要求\n\n"
            "尽量按照以下结构回答：\n"
            "1. 检索结论（只说明是否检索到证据和数据，不做材料归属判断）\n"
            "2. 检索到的相关证据（只引用与问题参数直接相关的证据，不要为凑数引用无关证据）\n"
            "3. 实验数据及其对应来源（必须列出所有 target_parameter_match=true "
            "且 measurement 非空的证据，不得遗漏）\n"
            "4. 材料体系说明（只针对回答中引用的证据，逐条复制 material_note）\n"
            "5. 数据质量或证据局限\n\n"
            "回答应准确、清晰、简洁，适合科研人员阅读。\n"
            "所有数据必须能追溯到系统提供的数据库证据。"
            "你只能对已检索到的证据进行总结和解释。\n"
            "输出前自检：每个数值、论文编号、页码都必须出现在证据中；"
            "每条 BOPET 归属都必须有 material_candidate 支持。"
        )

    def answer(self, question, evidence):
        """把真实证据交给 Qwen，生成可追溯的回答。"""

        if not evidence:
            return NO_RESULT_MESSAGE

        context = self.build_context(question, evidence)

        messages = [
            {
                "role": "system",
                "content": self.build_answer_system_prompt(),
            },
            {
                "role": "user",
                "content": (
                    f"用户问题：{question}\n\n"
                    "下面是系统从 SQLite 数据库中检索到的真实证据。"
                    "请严格依据这些证据回答；"
                    "证据不足时直接说明，不要补充模型自身知识。\n\n"
                    "特别注意：\n"
                    "1. 只能把 original_text 中明确对应问题所问参数的数值，"
                    "列入“实验数据”一节。\n"
                    "2. MDO temperature、drying temperature、heat-setting temperature、"
                    "crystallization temperature 等不是拉伸温度，"
                    "不得写成拉伸温度数据。\n"
                    "3. parameter_type 为“待人工识别”、且原文参数名与问题不一致的证据，"
                    "只能放在“证据局限”中说明，不得当作目标参数数据。\n"
                    "4. 材料以 material_note 为准；非 BOPET 的证据必须保留该限制。\n"
                    "5. 只有 target_parameter_match=true 的证据才能列入“实验数据”一节；"
                    "false 的只能放在“证据局限”中说明。\n\n"
                    f"数据库检索结果：\n{context}"
                ),
            },
        ]

        try:
            response = self._chat(
                messages,
                temperature=float(
                    self.qa_config.get("answer_temperature", 0.2)
                ),
                max_tokens=int(
                    self.qa_config.get("answer_max_tokens", 1500)
                ),
            )
        except QAError:
            raise
        except Exception as exc:
            raise QAError("回答生成", str(exc)) from exc

        return self._response_text(response)

    # -----------------------------------------------------
    # 完整闭环
    # -----------------------------------------------------

    def ask(self, question, on_step=None):
        """完整问答流程：理解问题 -> 检索 -> 组织证据 -> Qwen 回答。"""

        question = str(question or "").strip()

        if not question:
            raise ValueError("问题不能为空")

        # 先确认客户端可用，避免 API Key 缺失时被当成关键词降级处理
        self._get_client()

        def emit(message):
            if on_step:
                on_step(message)

        emit("正在理解问题...")
        keywords = self.understand_question(question)

        if self.last_keyword_warning:
            emit(f"提示：{self.last_keyword_warning}")

        emit("检索关键词：" + "、".join(keywords))
        emit("正在查询 SQLite 数据库...")

        evidence = self.retrieve(keywords)

        emit(f"检索到 {len(evidence)} 条相关证据")

        if not evidence:
            return {
                "question": question,
                "keywords": keywords,
                "evidence": [],
                "answer": NO_RESULT_MESSAGE,
                "answered_by_model": False,
            }

        emit("正在整理文献证据...")
        emit("正在请求 Qwen 根据证据生成回答...")

        answer = self.answer(question, evidence)

        return {
            "question": question,
            "keywords": keywords,
            "evidence": evidence,
            "answer": answer,
            "answered_by_model": True,
        }


if __name__ == "__main__":

    question = (
        sys.argv[1] if len(sys.argv) > 1
        else input("请输入问题：").strip()
    )

    agent = QAAgent(CONFIG)

    try:
        result = agent.ask(question, on_step=print)
    except QAError as exc:
        print(f"\n流程失败（{exc.step}）：{exc.message}")
        raise SystemExit(1)

    print("\n" + "=" * 60)
    print(result["answer"])
