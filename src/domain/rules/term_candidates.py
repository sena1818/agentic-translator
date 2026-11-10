"""
术语账本候选抽取（确定性、无模型调用）

从全书抽取"反复出现、但术语表未覆盖"的专名：人名、地名、组织缩写、自造概念。
它们是跨块译法飘忽的主要来源（实测：Parsani → 帕萨尼 / 帕尔萨尼）。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

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


@dataclass
class TermCandidate:
    term: str
    count: int
    context: str  # 首次出现处的上下文片段，供模型判断词义


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
        words = phrase.split()
        while words and words[0] in _ARTICLES:  # "The Templeton Episode" → "Templeton Episode"
            words.pop(0)  # 只剥冠词："One God Universe" 是完整专名，不能剥成 "God Universe"
        if len(words) > 1:
            record(" ".join(words), match.start() + phrase.find(words[0]))
        # 单词也计数：姓氏常与全名、句首词粘连（"Hamid Parsani"、"Later Parsani's"）
        for word_match in re.finditer(r"\S+", phrase):
            record(word_match.group(0), match.start() + word_match.start())
    for match in _ACRONYM_RE.finditer(prose):
        record(match.group(0), match.start())

    candidates = [
        TermCandidate(term, count, contexts[term])
        for term, count in counts.most_common()
        if count >= 2 and mid_sentence[term] >= 1 and not _covered(term, glossary_lower)
    ]
    # 多词短语已覆盖其组成部分时保留两者：人名 "Hamid Parsani" 与单独出现的 "Parsani" 都需要统一
    return candidates[:max_terms]
