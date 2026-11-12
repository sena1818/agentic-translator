"""
术语账本候选抽取（确定性、无模型调用）

从全书抽取"反复出现、但术语表未覆盖"的专名：人名、地名、组织缩写、自造概念。
它们是跨块译法飘忽的主要来源（实测：Parsani → 帕萨尼 / 帕尔萨尼）。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_CAPITALIZED = r"[A-Z][a-z][A-Za-z’'\-]*"
_PHRASE_RE = re.compile(rf"(?<![\w’'\-]){_CAPITALIZED}(?:\s+{_CAPITALIZED}){{0,3}}(?![\w’'\-])")
_ACRONYM_RE = re.compile(r"(?<![\w\-])[A-Z][A-Z0-9]{1,6}(?![\w\-])")
_ROMAN_RE = re.compile(r"^[IVXLCDM]+$")
_SENTENCE_START = re.compile(r"(?:^|[.!?:;“\"(\[]\s*|\n\s*(?:[#>*\-]+\s*|\d+\.\s+)?)$")

# 常见句首词与功能词：单独出现时不是专名
_STOPWORDS = {
    "The",
    "A",
    "An",
    "In",
    "On",
    "At",
    "It",
    "Its",
    "This",
    "That",
    "These",
    "Those",
    "But",
    "And",
    "Or",
    "For",
    "Of",
    "To",
    "From",
    "With",
    "As",
    "By",
    "If",
    "When",
    "While",
    "Where",
    "What",
    "Which",
    "Who",
    "Whom",
    "Whose",
    "Why",
    "How",
    "There",
    "Here",
    "They",
    "Their",
    "We",
    "Our",
    "You",
    "Your",
    "He",
    "His",
    "She",
    "Her",
    "Him",
    "One",
    "Two",
    "Three",
    "First",
    "Second",
    "Third",
    "Some",
    "Such",
    "No",
    "Not",
    "So",
    "Yet",
    "All",
    "Each",
    "Every",
    "Many",
    "Most",
    "Much",
    "More",
    "Other",
    "Another",
    "Both",
    "Even",
    "Only",
    "Also",
    "Then",
    "Thus",
    "Hence",
    "However",
    "Although",
    "Because",
    "Since",
    "Before",
    "After",
    "Chapter",
    "Part",
    "Section",
    "Figure",
    "Table",
    "Note",
    "Notes",
    "See",
    "Mr",
    "Mrs",
    "Dr",
    "Is",
    "Was",
    "Are",
    "Were",
    "Be",
    "Being",
    "Has",
    "Have",
    "Had",
    "Do",
    "Does",
    "Did",
    "Can",
    "Could",
    "Would",
    "Should",
    "Will",
    "Shall",
    "May",
    "Might",
    "Must",
    "Now",
    "Once",
    "Perhaps",
    "According",
    "Yes",
}


_ARTICLES = {"The", "A", "An"}

# 补充：数词、月份、常见副词/介词/代词——在长篇中大量出现于句首或标题，几乎从不是专名
_STOPWORDS |= {
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Zero",
    "Despite",
    "Something",
    "Nothing",
    "Everything",
    "Anything",
    "Between",
    "Within",
    "Without",
    "Through",
    "Against",
    "Among",
    "During",
    "Until",
    "Upon",
    "Under",
    "Over",
    "Into",
    "Onto",
    "Instead",
    "Rather",
    "Whether",
    "Still",
    "Again",
    "January",
    "February",
    "March",
    "April",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
}


@dataclass
class TermCandidate:
    term: str
    count: int
    context: str  # 首次出现处的上下文片段，供模型判断词义
    sources: tuple[str, ...] = ("proper",)  # 候选来源：proper / keyness / structure（见 term_sources）
    contexts: list[str] = field(default_factory=list)  # 全书中差异较大的多处上下文
    ambiguity: list[str] = field(default_factory=list)  # 确定性歧义信号；为空表示无歧义
    keyness: float | None = None  # 本书频率相对通用英语的对数比（zipf 差）

    @property
    def ambiguous(self) -> bool:
        return bool(self.ambiguity)


def _covered(term: str, glossary_lower: set[str]) -> bool:
    lowered = term.lower()
    return lowered in glossary_lower or lowered.rstrip("s") in glossary_lower


def extract_term_candidates(text: str, glossary: dict[str, str], max_terms: int = 80) -> list[TermCandidate]:
    """按出现频次返回术语表未覆盖、至少出现两次且至少一次不在句首的专名候选。"""
    # 去掉代码、链接地址、图片、公式，避免把 URL 和代码标识符当作专名
    prose = re.sub(r"```[\s\S]*?```|`[^`]*`|\]\([^)]*\)|\$\$[\s\S]*?\$\$|\$[^$\n]+\$|\{[^}\n]*\}", " ", text)
    glossary_lower = {key.lower() for key in glossary}
    counts: Counter = Counter()
    mid_sentence: Counter = Counter()
    contexts: dict[str, str] = {}

    def record(term: str, start: int):
        term = re.sub(r"[’']s?$", "", term)
        if len(term) < 3 or _ROMAN_RE.match(term) or term in _STOPWORDS:
            return
        if " " not in term and term.lower() in glossary_lower:
            return
        counts[term] += 1
        if not _SENTENCE_START.search(prose[max(0, start - 6) : start]):
            mid_sentence[term] += 1
        if term not in contexts:
            contexts[term] = re.sub(r"\s+", " ", prose[max(0, start - 80) : start + len(term) + 80]).strip()

    for match in _PHRASE_RE.finditer(prose):
        phrase = match.group(0)
        spans = [(word.group(0), match.start() + word.start()) for word in re.finditer(r"\S+", phrase)]
        while spans and spans[0][0] in _ARTICLES:  # "The Templeton Episode" → "Templeton Episode"
            spans.pop(0)  # 只剥冠词："One God Universe" 是完整专名，不能剥成 "God Universe"
        if not spans:
            continue
        # 按完整短语计数，不拆成单词（拆开会把 "New York" 变成 "York"、"Sunken Track" 变成 "Track"）
        record(" ".join(word for word, _ in spans), spans[0][1])
        # 句首的首词可能只是因位置而大写（"Later Parsani's"），另按去掉首词的形式记一次
        if len(spans) > 1 and _SENTENCE_START.search(prose[max(0, spans[0][1] - 6) : spans[0][1]]):
            record(" ".join(word for word, _ in spans[1:]), spans[1][1])
    for match in _ACRONYM_RE.finditer(prose):
        record(match.group(0), match.start())

    eligible = {
        term: count
        for term, count in counts.items()
        if count >= 2 and mid_sentence[term] >= 1 and not _covered(term, glossary_lower)
    }
    phrases = [term for term in eligible if " " in term]

    def mostly_inside_phrase(term: str) -> bool:
        # 单词大多作为更长专名的一部分出现时不单独收录（York ⊂ New York）；
        # 单独出现更多的仍保留（Parsani 多于 Hamid Parsani）
        return " " not in term and any(
            re.search(rf"(?<![\w'’-]){re.escape(term)}(?![\w'’-])", phrase) and eligible[phrase] >= eligible[term]
            for phrase in phrases
        )

    candidates = [
        TermCandidate(term, count, contexts[term])
        for term, count in sorted(eligible.items(), key=lambda item: -item[1])
        if not mostly_inside_phrase(term)
    ]
    return candidates[:max_terms]
