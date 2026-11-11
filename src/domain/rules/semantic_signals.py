"""
段级语义信号：否定、情态强弱、数字方向、漏译与增译的确定性启发式。

这些检查不理解句意，只比对两侧的表面信号（否定词、情态词、"from X to Y" 里数字的先后、
长度比例），因此给出的都是提示：命中说明值得复核，没命中不说明没问题。
真正判断句意要交给模型语义检查（见 pipelines/translate/semantic_checker.py）。

纯函数、无 I/O，便于单测与注入错误基准直接调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SEMANTIC_TYPES = ("negation", "modality", "number_role", "omission", "addition", "uncertain")
SEMANTIC_LABELS = {
    "negation": "否定",
    "modality": "情态强弱",
    "number_role": "数字角色",
    "omission": "漏译",
    "addition": "增译",
    "uncertain": "无法判定",
}


@dataclass(frozen=True)
class SemanticFinding:
    """一条语义提示：类型、严重度、理由，以及（能定位时）原文与译文片段。"""

    kind: str
    severity: str
    reason: str
    source_span: str = ""
    target_span: str = ""
    origin: str = "heuristic"  # heuristic | model

    def message(self) -> str:
        source = "规则" if self.origin == "heuristic" else "模型"
        text = f"[语义提示·{source}] {SEMANTIC_LABELS.get(self.kind, self.kind)}：{self.reason}"
        spans = []
        if self.source_span:
            spans.append(f"原文「{self.source_span[:60]}」")
        if self.target_span:
            spans.append(f"译文「{self.target_span[:60]}」")
        return text + (f"（{' → '.join(spans)}）" if spans else "")


# --------------------------------------------------------------------------- #
# 预处理
# --------------------------------------------------------------------------- #

_PROTECTED = re.compile(r"```[\s\S]*?```|`[^`\n]*`|\]\([^)]*\)|https?://\S+|\{[#.=][^}]*\}|<[^>]+>")
_CJK = re.compile(r"[一-鿿]")
_EN_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*")


def _prose(text: str) -> str:
    """去掉代码、链接地址、锚点属性和标签，只留自然语言。"""
    return _PROTECTED.sub(" ", text)


# --------------------------------------------------------------------------- #
# 风险判定（决定"只查高风险段"时哪些段送模型）
# --------------------------------------------------------------------------- #

_EN_NEGATION = re.compile(
    r"\b(?:not|never|no|none|nothing|nobody|nowhere|neither|nor|cannot|without|hardly|barely|scarcely)\b|n[’']t\b",
    re.I,
)
_EN_WEAK = re.compile(
    r"\b(?:may|might|could|possibly|perhaps|probably|likely|unlikely|suggests?|suggested|appears?|seems?|"
    r"seemingly|presumably|arguably|potentially|indicates?)\b",
    re.I,
)
_EN_STRONG = re.compile(
    r"\b(?:must|always|certainly|proves?|proved|proven|definitely|necessarily|inevitably|undoubtedly|"
    r"surely|clearly|demonstrates?)\b",
    re.I,
)
_NUMBER = re.compile(r"(?<![0-9A-Za-z_.])\d+(?:[.,]\d+)*(?![0-9A-Za-z_])")
LONG_SENTENCE_WORDS = 35


def risk_factors(source: str) -> list[str]:
    """原文里有哪些高风险信号：否定、情态、数字、长句。"""
    prose = _prose(source)
    factors = []
    if _EN_NEGATION.search(prose):
        factors.append("negation")
    if _EN_WEAK.search(prose) or _EN_STRONG.search(prose):
        factors.append("modality")
    if _NUMBER.search(prose):
        factors.append("number")
    sentences = re.split(r"(?<=[.!?;])\s+", prose)
    if any(len(_EN_WORD.findall(sentence)) >= LONG_SENTENCE_WORDS for sentence in sentences):
        factors.append("long")
    return factors


def is_high_risk(source: str) -> bool:
    return bool(risk_factors(source))


# --------------------------------------------------------------------------- #
# 启发式检查
# --------------------------------------------------------------------------- #

# 中文否定：不、没、未、无、非、别、勿、莫、否；以及常见的否定性表达
_ZH_NEGATION = re.compile(r"[不没未无非别勿莫否]|缺乏|缺少|难以|毫无|拒绝|避免|失去|零|免于|远非|绝非")
# 含否定字但本身不表否定的常见词，计数时先去掉
_ZH_NOT_NEGATION = re.compile(
    r"不断|不同|不仅|不但|不久|不少|不管|不论|无论|非常|无数|无限|无穷|未来|别人|别的|特别|区别|分别|告别|"
    r"莫名|否则|是否|能否|可否|无比|毫不犹豫|不得不|无非"
)
_ZH_WEAK = re.compile(
    r"可能|或许|也许|似乎|好像|大概|大约|恐怕|兴许|看来|看似|表明|暗示|显示|意味|未必|不一定|能够|可以|会|或"
)
# 断言性措辞：原文是推测语气时不该出现
_ZH_ASSERT = re.compile(r"必然|必定|一定|肯定|无疑|毫无疑问|证明|证实|显然|绝对|必将|注定")
# 强语气的全部表达：原文是断言时译文至少应有其一
_ZH_STRONG = re.compile(
    r"必然|必定|必须|一定|肯定|无疑|证明|证实|显然|总是|始终|永远|务必|确实|确定|绝对|必将|注定|须|得|都|总|一直"
)


def negation_counts(source: str, translation: str) -> tuple[int, int]:
    """(原文否定词数, 译文否定表达数)；译文先去掉"不断""无论"这类不表否定的词。"""
    target = _ZH_NOT_NEGATION.sub("", _prose(translation))
    return len(_EN_NEGATION.findall(_prose(source))), len(_ZH_NEGATION.findall(target))


def check_negation(source: str, translation: str) -> list[SemanticFinding]:
    """原文有否定，译文一个否定表达都没有（medium）；或译文的否定表达比原文的否定词少（low）。"""
    match = _EN_NEGATION.search(_prose(source))
    if not match or not _CJK.search(translation):
        return []
    source_count, target_count = negation_counts(source, translation)
    if target_count == 0:
        return [
            SemanticFinding(
                "negation", "medium", "原文含否定，译文未见任何否定表达，否定可能被反转或丢失", match.group(0)
            )
        ]
    if target_count < source_count:
        return [
            SemanticFinding(
                "negation",
                "low",
                f"原文有 {source_count} 处否定，译文只见 {target_count} 处否定表达，可能有否定被丢失",
                match.group(0),
            )
        ]
    return []


def check_modality(source: str, translation: str) -> list[SemanticFinding]:
    """情态强弱被改变：原文是推测语气、译文出现断言词；或原文是断言、译文只剩推测词。"""
    prose_source, prose_target = _prose(source), _prose(translation)
    if not _CJK.search(prose_target):
        return []
    findings = []
    weak = _EN_WEAK.search(prose_source)
    strong_source = _EN_STRONG.search(prose_source)
    strong_target = _ZH_STRONG.search(prose_target)
    asserted = _ZH_ASSERT.search(prose_target)
    if weak and not strong_source and asserted:
        findings.append(
            SemanticFinding(
                "modality",
                "medium",
                "原文是推测/弱化语气，译文出现断言性措辞，论证强度可能被加强",
                weak.group(0),
                asserted.group(0),
            )
        )
    if strong_source and not strong_target:
        weak_target = _ZH_WEAK.search(prose_target)
        if weak_target:
            findings.append(
                SemanticFinding(
                    "modality",
                    "low",
                    "原文是断言/必然语气，译文只见推测性措辞，论证强度可能被削弱",
                    strong_source.group(0),
                    weak_target.group(0),
                )
            )
    return findings


_DIRECTIONAL = [
    re.compile(
        r"\bfrom\s+(?:about\s+|around\s+|roughly\s+)?(\d+(?:[.,]\d+)*)\b[^.;]{0,40}?\bto\s+(\d+(?:[.,]\d+)*)", re.I
    ),
    re.compile(r"\bbetween\s+(\d+(?:[.,]\d+)*)\b[^.;]{0,30}?\band\s+(\d+(?:[.,]\d+)*)", re.I),
    re.compile(
        r"(?<![\w.])(\d+(?:[.,]\d+)*)\s*(?:-|–|—|to)\s*(\d+(?:[.,]\d+)*)\s*(?:%|percent|years?|days?|hours?|times)",
        re.I,
    ),
]
_EN_UP = re.compile(r"\b(?:rose|rise[sn]?|increas\w*|grew|grow[sn]?|gain\w*|improv\w*|climb\w*|doubl\w*|up)\b", re.I)
_EN_DOWN = re.compile(
    r"\b(?:fell|fall[sn]?|decreas\w*|declin\w*|dropp?\w*|shr[ai]nk\w*|reduc\w*|lower\w*|halv\w*|down)\b", re.I
)
_ZH_UP = re.compile(r"上升|提升|提高|增加|增长|增至|升至|升高|上涨|翻[了一]?倍|改善|攀升|涨")
_ZH_DOWN = re.compile(r"下降|降低|降至|减少|减至|下跌|跌至|缩小|缩减|减半|回落|跌|降")


def _ordered(translation: str, first: str, second: str) -> bool | None:
    """译文里 first 是否出现在 second 之前；任一缺失返回 None（缺失由规则质检的数字检查负责）。"""

    def position(number: str) -> int:
        match = re.search(rf"(?<![0-9.]){re.escape(number)}(?![0-9])", translation)
        return match.start() if match else -1

    a, b = position(first), position(second)
    if a < 0 or b < 0:
        return None
    return a < b


def check_number_roles(source: str, translation: str) -> list[SemanticFinding]:
    """数字方向：from X to Y / between X and Y 在译文里先后颠倒；涨跌方向相反。"""
    prose_source, prose_target = _prose(source), _prose(translation)
    findings = []
    for pattern in _DIRECTIONAL:
        for match in pattern.finditer(prose_source):
            first, second = match.group(1), match.group(2)
            if first == second:
                continue
            if _ordered(prose_target, first, second) is False:
                findings.append(
                    SemanticFinding(
                        "number_role",
                        "medium",
                        f"原文 {first} 在前、{second} 在后，译文顺序相反，起止或区间可能被写反",
                        match.group(0),
                        "",
                    )
                )
    up, down = _EN_UP.search(prose_source), _EN_DOWN.search(prose_source)
    if _NUMBER.search(prose_source) and bool(up) != bool(down):
        target_up, target_down = _ZH_UP.search(prose_target), _ZH_DOWN.search(prose_target)
        if up and target_down and not target_up:
            findings.append(
                SemanticFinding(
                    "number_role", "medium", "原文表示增长，译文表示下降", up.group(0), target_down.group(0)
                )
            )
        if down and target_up and not target_down:
            findings.append(
                SemanticFinding(
                    "number_role", "medium", "原文表示下降，译文表示增长", down.group(0), target_up.group(0)
                )
            )
    return findings


# 每个英文单词大约对应的汉字数；按 Cyclonopedia 基线与自带段对校准（中位约 1.6～1.8）
OMISSION_RATIO = 0.95
ADDITION_RATIO = 3.6
MIN_WORDS_FOR_LENGTH = 8


def length_ratio(source: str, translation: str) -> float | None:
    words = len(_EN_WORD.findall(_prose(source)))
    if words < MIN_WORDS_FOR_LENGTH:
        return None
    return len(_CJK.findall(_prose(translation))) / words


def check_length(source: str, translation: str) -> list[SemanticFinding]:
    """译文汉字数相对原文词数明显偏少（可能漏译）或偏多（可能增译）。"""
    ratio = length_ratio(source, translation)
    if ratio is None or not _CJK.search(translation):
        return []
    if ratio < OMISSION_RATIO:
        return [
            SemanticFinding(
                "omission", "medium", f"译文长度只有原文的常见比例的一半左右（每词 {ratio:.2f} 字），可能有漏译"
            )
        ]
    if ratio > ADDITION_RATIO:
        return [SemanticFinding("addition", "low", f"译文明显长于原文（每词 {ratio:.2f} 字），可能有增译")]
    return []


def _clause_count(text: str, pattern: str) -> int:
    return len([part for part in re.split(pattern, text) if part.strip()])


_EN_CONTRAST = re.compile(r"\b(?:but|although|though|whereas|however|unless|except)\b", re.I)
_ZH_CONTRAST = re.compile(r"但|却|然而|尽管|虽然|虽|不过|而|除非|除了|只是|可是|反而|则")


def check_contrast(source: str, translation: str) -> list[SemanticFinding]:
    """原文有转折/让步，译文没有任何转折词：转折后的半句常整段丢失。"""
    match = _EN_CONTRAST.search(_prose(source))
    if not match or not _CJK.search(translation) or _ZH_CONTRAST.search(_prose(translation)):
        return []
    return [SemanticFinding("omission", "low", "原文有转折/让步，译文未见对应连接词，后半句可能被漏译", match.group(0))]


def heuristic_findings(source: str, translation: str) -> list[SemanticFinding]:
    """全部确定性语义信号。"""
    return [
        *check_negation(source, translation),
        *check_modality(source, translation),
        *check_number_roles(source, translation),
        *check_length(source, translation),
        *check_contrast(source, translation),
    ]
