"""
翻译 Prompt 构造器

角色与要求默认面向通用专业文本；可通过配置文件的 prompt: 段按领域覆盖
（translator_role / translation_requirements / repair_role /
repair_requirements / analyst_requirements），参见 config/research_paper.yaml。
"""
from __future__ import annotations

from ...core.chunk_planner import TextChunk
from ...domain.models.translation_models import DocumentProfile
from .langchain_compat import ChatPromptTemplate

DEFAULT_TRANSLATOR_ROLE = (
    "你是资深的专业文本译者，擅长在术语表约束下翻译术语密集的各领域专业文本"
    "（哲学、机器学习、科研论文、技术文档等）。"
)

DEFAULT_TRANSLATION_REQUIREMENTS = [
    "完整保留所有 Markdown 格式（标题、加粗、图片、链接、代码块）",
    "风格：忠实原文文体与论证强度，术语准确、行文严谨",
    "专有名词首次出现时保留英文原文在括号内",
    "严格使用术语表中的译名",
    "直接输出译文，不要任何前言、解释、注释",
]

DEFAULT_REPAIR_ROLE = "你是专业文本翻译审校者。请在不改变原意和 Markdown 结构的前提下修正译文。"

DEFAULT_REPAIR_REQUIREMENTS = [
    "只修正问题相关部分",
    "保留标题、链接、图片、代码块、引用块",
    "不要添加说明或注释",
    "直接输出修正后的完整译文",
]

DEFAULT_ANALYST_REQUIREMENTS = [
    "不要输出 JSON 以外的任何文字",
    "style_notes 最多 4 条",
    "terminology_hints 最多 {max_term_hints} 条",
    "section_overview 只保留最关键的章节线索",
]

DEFAULT_GLOSSARY_EXTRACTOR_ROLE = (
    "你是各领域专业文本的术语专家，擅长识别需要在全书统一译名的关键术语并给出规范中文译名。"
)

DEFAULT_GLOSSARY_EXTRACTION_REQUIREMENTS = [
    "只挑选真正影响一致性的专业术语、专有名词、缩写、人名、作品名、算法/模型/数据集名称",
    "跳过通用词汇与不影响译名统一的普通名词",
    "translation 使用「中文译名 (English Term)」格式；缩写可写为「中文译名 (Full Name, ABBR)」",
    "note 用一句话说明该术语的领域含义或选此译名的理由",
    "最多返回 {max_terms} 条，按重要性排序",
    "不要输出 JSON 以外的任何文字",
]


# 结构化（Document IR）模式下的标签协议：结构由程序持有，LLM 只需搬运标签。
STRUCTURED_TAG_RULES = [
    "输入由若干 <seg id=\"N\">…</seg> 组成；逐段翻译，按原顺序输出同样 id 的 seg，不合并、不拆分、不遗漏",
    "自闭合标签（如 <m1/> 公式、<fn2/> 脚注、<img3/> 图片、<code4/> 代码、<br5/> 换行、<anchor6/> 锚点）"
    "代表必须原样保留的内容：每个标签在译文中恰好出现一次，放在语义对应的位置，不得改写、删除或复制",
    "成对标签（如 <b1>…</b1> 加粗、<i2>…</i2> 斜体、<a3>…</a3> 链接、<q4>…</q4> 引号）包裹带格式的文字："
    "翻译标签内的文字并保留标签，可随中文语序移动位置",
    "&lt; &gt; &amp; 是转义字符，原样保留",
    "只输出 seg 序列，不要任何前言、解释或代码块包裹",
]


_SEG_OUTPUT = '对每个段输出一行 <seg id="N">完整译文</seg>，不要回显原文或问题，不要输出其他内容。'
_EDIT_OUTPUT = (
    "不要输出整段译文。对每处需要改的地方输出一行：\n"
    '<edit id="N"><from>当前译文中要替换的片段（逐字照抄）</from><to>替换后的片段</to></edit>\n'
    "程序会把 from 原样替换成 to，其余文字保持不变；不要输出其他内容。"
)
TYPED_REPAIR_INSTRUCTIONS = {
    "retranslate": (
        "重译：该段缺失、未翻译或被截断",
        _SEG_OUTPUT,
        ["依据原文完整翻译该段，不要遗漏任何句子", "遵守术语表", "不要添加说明或注释"],
    ),
    "structure": (
        "只修标签：标签缺失、未闭合或写错",
        _SEG_OUTPUT,
        [
            "只增删或移动标签，标签外的文字一个字也不要改",
            "缺失的自闭合标签放回语义对应的位置；未闭合的成对标签在其包裹的文字之后闭合",
            "程序会核对：修复前后去掉标签的文字必须完全相同，否则修复作废",
        ],
    ),
    "number": (
        "只修数字：译文中的数字与原文不符或缺失",
        _EDIT_OUTPUT,
        [
            "from 取包含错误数字（或缺数字位置）的最短片段",
            "to 只改数字及紧邻的单位、量词，其他文字不变",
            "原文数字可以写成中文习惯（如 55 million → 5500万）",
        ],
    ),
    "terminology": (
        "只修术语：术语没有按术语表翻译",
        _EDIT_OUTPUT,
        [
            "from 取当前译文中该术语的错误译法（术语被漏掉时，取它应在位置前后的几个字）",
            "to 只把那一处换成术语表中的中文译名（不带括注的英文），前后文字逐字保留",
            "每个术语每处出现各输出一行 edit",
        ],
    ),
}


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1))


class TranslationPromptBuilder:
    """统一生成翻译与审校 Prompt。

    注意：要求条目会直接进入 ChatPromptTemplate，除 analyst_requirements 中的
    {max_term_hints} 占位符外，配置文本中不要使用花括号。
    """

    def __init__(self, config=None):
        def _get(key: str, default):
            value = config.get(key) if config is not None else None
            return value if value else default

        self.translator_role = _get("prompt.translator_role", DEFAULT_TRANSLATOR_ROLE)
        self.translation_requirements = _get(
            "prompt.translation_requirements", DEFAULT_TRANSLATION_REQUIREMENTS
        )
        self.repair_role = _get("prompt.repair_role", DEFAULT_REPAIR_ROLE)
        self.repair_requirements = _get(
            "prompt.repair_requirements", DEFAULT_REPAIR_REQUIREMENTS
        )
        self.analyst_requirements = _get(
            "prompt.analyst_requirements", DEFAULT_ANALYST_REQUIREMENTS
        )
        self.glossary_extractor_role = _get(
            "prompt.glossary_extractor_role", DEFAULT_GLOSSARY_EXTRACTOR_ROLE
        )
        self.glossary_extraction_requirements = _get(
            "prompt.glossary_extraction_requirements", DEFAULT_GLOSSARY_EXTRACTION_REQUIREMENTS
        )

    def build_translation_prompt(
        self,
        chunk: TextChunk,
        document_profile: DocumentProfile | None = None,
    ) -> ChatPromptTemplate:
        template = f"""{self.translator_role}

【文档分析员备忘】:
{{document_profile}}

【核心术语表】(必须严格遵守):
{{glossary}}

【当前章节】:
{{section_title}}

【上文语境】:
{{context}}

【待翻译文本】:
{{text}}

---
【翻译要求】:
{_numbered(self.translation_requirements)}

开始翻译:
"""
        return ChatPromptTemplate.from_template(template)

    def build_structured_translation_prompt(self) -> ChatPromptTemplate:
        """结构化模式：按段翻译带内联标签的文本。"""
        template = f"""{self.translator_role}

【文档分析员备忘】:
{{document_profile}}

【核心术语表】(必须严格遵守):
{{glossary}}

【当前章节】:
{{section_title}}

【上文语境】(仅供参考，不要翻译):
{{context}}

【翻译要求】:
{_numbered(self.translation_requirements)}

【标签规则】(最高优先级):
{_numbered(STRUCTURED_TAG_RULES)}
{{retry_note}}
【待翻译段落】:
{{segments}}
"""
        return ChatPromptTemplate.from_template(template)

    def build_typed_repair_prompt(self, route: str) -> ChatPromptTemplate:
        """结构化模式的分类型修复：每一路只修一类问题，回复格式随类型而定。"""
        task, output, rules = TYPED_REPAIR_INSTRUCTIONS[route]
        tag_rules = f"\n【标签规则】(最高优先级):\n{_numbered(STRUCTURED_TAG_RULES)}\n" if route in {"retranslate", "structure"} else ""
        template = f"""{self.repair_role}

【修复类型】{task}

下面每个【段 N】给出了原文、当前译文和质检发现的问题。
{output}

【文档分析员备忘】:
{{document_profile}}

【术语表】(必须严格遵守):
{{glossary}}

【章节】:
{{section_title}}

【待修复段落】:
{{segments}}

---
要求：
{_numbered(rules)}
{tag_rules}"""
        return ChatPromptTemplate.from_template(template)

    def build_repair_prompt(self) -> ChatPromptTemplate:
        template = f"""{self.repair_role}

【章节】:
{{section_title}}

【原文】:
{{original}}

【当前译文】:
{{translation}}

【发现的问题】:
{{issues}}

【文档分析员备忘】:
{{document_profile}}

【术语表】(必须严格遵守):
{{glossary}}

---
要求：
{_numbered(self.repair_requirements)}
"""
        return ChatPromptTemplate.from_template(template)

    def build_analyst_prompt(self) -> ChatPromptTemplate:
        template = f"""你是翻译团队中的“文档分析员”。请基于以下文档片段和章节信息，为后续翻译提供全局指导。

输出必须是 JSON 对象，格式如下：
{{{{
  "summary": "一句到三句的文档摘要",
  "style_notes": ["风格提示1", "风格提示2"],
  "terminology_hints": ["术语提示1", "术语提示2"],
  "section_overview": ["章节1", "章节2"]
}}}}

要求：
{_numbered(self.analyst_requirements)}

【章节列表】:
{{sections}}

【文档片段】:
{{excerpt}}
"""
        return ChatPromptTemplate.from_template(template)

    def build_term_ledger_prompt(self) -> ChatPromptTemplate:
        """术语账本：为全书反复出现的专名统一译名。"""
        template = f"""{self.glossary_extractor_role}

下面是从整本书中自动抽取的、反复出现但尚未收入术语表的候选专名，每条附首次出现的上下文。
请为需要全书统一译法的条目给出唯一译名，保证同一个名字在几百个片段里始终译得一样。

输出必须是 JSON 对象，格式如下：
{{{{
  "terms": [
    {{{{"term": "原文", "translation": "译名", "keep": true}}}}
  ]
}}}}

要求：
1. 只保留专有名词。普通英语单词即使首字母大写（如 Gate、Time、Old、Five、State、Book）、无需统一的条目：keep 为 false
1a. 结合上下文判断词义：同一个词可能是人名而非普通词（如海盗 Captain Mission 中的 Mission 是人名「米申」，不是「使命」）
2. 人名、地名：按通行音译只给中文，如「帕萨尼」；与【已有术语】中的全名保持一致（姓与全名用同一译法）
3. 自造概念、组织、作品名：用「中文译名 (English)」格式；译名一律用名词形式，不要带「的」（Lemurian 译「利莫里亚」，不是「利莫里亚的」）
4. 首字母缩写（如 AOE、CCRU）：通常保留原文，translation 写原文本身
5. 不要输出 JSON 以外的任何文字

【文档摘要】:
{{summary}}

【已有术语】(必须与之保持一致):
{{existing_terms}}

【候选专名】:
{{candidates}}
"""
        return ChatPromptTemplate.from_template(template)

    def build_glossary_extraction_prompt(self) -> ChatPromptTemplate:
        template = f"""{self.glossary_extractor_role}

请从下面的文档片段中提取需要在全书统一译名的关键术语，并给出建议译名。

输出必须是 JSON 对象，格式如下：
{{{{
  "terms": [
    {{{{"term": "English Term", "translation": "中文译名 (English Term)", "note": "一句话说明"}}}}
  ]
}}}}

要求：
{_numbered(self.glossary_extraction_requirements)}

【已有术语（无需重复提取）】:
{{existing_terms}}

【文档片段】:
{{excerpt}}
"""
        return ChatPromptTemplate.from_template(template)
