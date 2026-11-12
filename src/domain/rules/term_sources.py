"""
多路术语候选（确定性、无模型调用）

术语账本原先只抽大写专名、每个候选只带首次出现处一段上下文：作者自造的小写概念词（polytics、
hyperstition）抽不到，Mission、Gates 这类词又因为只看到一处上下文被误判。这里把候选扩成三路：

- proper     大写专名与缩写（沿用 term_candidates.extract_term_candidates，不拆专名短语）；
- keyness    关键度：在本书反复出现、在通用英语中罕见的词和短语（本书频率与 wordfreq 频率的 zipf 差）；
- structure  文档结构线索：Document IR 里被强调、加引号的词，以及标题里的词，且正文中至少再出现一次。

每个候选从全书索引取 3–5 处差异较大的上下文（首次出现、带定义意味的、不同章节的），
并用确定性信号标注是否有歧义：大小写混用、可能只是句首大写、同时是常用英语词、已有不同译法。
不用向量相似度做抽取：它回答的是"和已知的像不像"，不是"这是不是术语"。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from ..index.book_index import KEPT_ORIGINAL, UNRESOLVED, BookIndex, IndexedSegment, term_pattern
from .term_candidates import TermCandidate, extract_term_candidates

PROPER = "proper"
KEYNESS = "keyness"
STRUCTURE = "structure"
READING = "reading"  # 阅读遍（#17）记下的概念与特殊用词；只在阅读遍开启并传入时出现
ALL_SOURCES = (PROPER, KEYNESS, STRUCTURE, READING)
SOURCE_LABELS = {PROPER: "专名", KEYNESS: "关键度", STRUCTURE: "强调/引号/标题", READING: "阅读遍笔记"}

MIXED_CASE = "mixed_case"
SENTENCE_INITIAL = "sentence_initial"
COMMON_WORD = "common_word"
DIVERGENT = "divergent_renderings"
AMBIGUITY_LABELS = {
    MIXED_CASE: "大小写混用",
    SENTENCE_INITIAL: "可能只是句首大写",
    COMMON_WORD: "同时是常用英语词",
    DIVERGENT: "已有不同译法",
}

Zipf = Callable[[str], float]


def default_zipf() -> Zipf | None:
    """wordfreq 的英语 zipf 频率（log10 每十亿词出现次数）；未安装时返回 None，关键度一路随之关闭。"""
    try:
        from wordfreq import zipf_frequency
    except ImportError:  # pragma: no cover - 可选依赖
        return None
    return lambda text: zipf_frequency(text, "en")


@dataclass
class SourceConfig:
    min_keyness_count: int = 3  # 关键度候选在本书至少出现的次数
    min_word_keyness: float = 2.5  # 单词：本书 zipf − 通用 zipf 的下限
    min_phrase_keyness: float = 1.0  # 短语：wordfreq 对短语的估计接近其中最罕见的词，门槛相应放低
    min_structure_count: int = 2  # 强调/引号/标题里的词在正文中至少出现的次数
    common_zipf: float = 4.0  # 单词通用 zipf 不低于此值视为"同时是常用英语词"
    # 各路配额：专名一路与旧做法完全相同（按次数取前 max_proper 条），另两路只补专名之外的新词
    max_proper: int = 120
    max_keyness: int = 80
    max_structure: int = 40
    max_terms: int = 240  # 合并后的总上限（阅读遍一路另有配额，不占这个上限）
    max_reading: int = 60  # 阅读遍一路：前几路之外最多补 N 条
    contexts: int = 3  # 每个候选附的上下文数；有歧义的多给一处（上限 5）
    context_chars: int = 70  # 每处上下文在命中词两侧各取的字符数
    sources: tuple[str, ...] = ALL_SOURCES


# --------------------------------------------------------------------------- #
# 分词与通用小工具
# --------------------------------------------------------------------------- #

_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*[A-Za-z]|[A-Za-z]")
_SENTENCE_SPLIT = re.compile(r"[.!?;:()\[\]{}\"“”—]+|\n")
_POSSESSIVE = re.compile(r"['’]s?$")
_STOP = set(
    "the a an of to in on for and or but nor with as at by is are was were be been being it its this that these "
    "those from into onto than then so not no his her their our your my we they he she you i me him them us which "
    "who whom whose what when where how why there here all any some such each every other another one two three "
    "also only more most very much many can could would should will shall may might must do does did done has have "
    "had if because while between within without through over under about after before again against during upon "
    "up down out off just even still yet both either neither own same too via per".split()
)
_PHRASE_INNER = {"of", "on", "and", "the"}
_SENTENCE_START = re.compile(r"(?:^|[.!?:;\"“(\[]\s*|\n\s*)$")
_DEFINITIONAL = re.compile(
    r"\b(?:is|are|was|were)\s+(?:a|an|the|what|not)\b|\b(?:called|so-called|termed|named|known as|refers? to|"
    r"defined as|definition|means|i\.e\.|that is)\b|[:—]",
    re.IGNORECASE,
)


def _normalize_token(token: str) -> str:
    return _POSSESSIVE.sub("", token.lower()).strip("-'’")


def _book_zipf(count: int, total_words: int) -> float:
    return math.log10(count / max(total_words, 1) * 1e9)


def corpus_text(index: BookIndex) -> str:
    return "\n\n".join(seg.source for seg in index.segments)


# --------------------------------------------------------------------------- #
# 关键度
# --------------------------------------------------------------------------- #


@dataclass
class KeynessCandidate:
    key: str
    count: int
    keyness: float


def keyness_candidates(text: str, zipf: Zipf, config: SourceConfig | None = None) -> dict[str, KeynessCandidate]:
    """本书频率显著高于通用英语的词和短语。

    - 单词：小写化、去所有格后计数，至少 min_keyness_count 次、长度 ≥ 4，zipf 差 ≥ min_word_keyness；
      复数并到单数（trisons → trison）。
    - 短语：句内 2 词或"A of/on/and/the B"三词，首尾不是虚词；zipf 差 ≥ min_phrase_keyness。
      "专名 + 常见词"的搭配（parsani writes）不要：其中一个词本身是候选、其余词都常见、且短语只占该词出现的一小部分。
    - 主要作为更长短语一部分出现的单词不单独收（commissa ⊂ crux commissa）。
    """
    config = config or SourceConfig()
    words: Counter = Counter()
    grams: Counter = Counter()
    for sentence in _SENTENCE_SPLIT.split(text):
        tokens = [_normalize_token(match.group(0)) for match in _WORD.finditer(sentence)]
        tokens = [token for token in tokens if token]
        words.update(tokens)
        for n in (2, 3):
            for start in range(len(tokens) - n + 1):
                gram = tokens[start : start + n]
                if gram[0] in _STOP or gram[-1] in _STOP:
                    continue
                if n == 2 and gram[1] in _STOP:
                    continue
                if n == 3 and gram[1] not in _PHRASE_INNER:
                    continue
                grams[" ".join(gram)] += 1
    total = sum(words.values())
    general: dict[str, float] = {}

    def common(word: str) -> float:
        if word not in general:
            general[word] = zipf(word)
        return general[word]

    merged_words: Counter = Counter()
    for word, count in words.items():  # 复数并到单数（trisons → trison），先合并再过门槛
        stem = next((s for s in ((word[:-1], word[:-2]) if word.endswith("s") else ()) if s in words), None)
        merged_words[stem or word] += count
    singles: dict[str, KeynessCandidate] = {}
    for word, count in merged_words.items():
        if count < config.min_keyness_count or len(word) < 4 or word in _STOP:
            continue
        score = _book_zipf(count, total) - common(word)
        if score >= config.min_word_keyness:
            singles[word] = KeynessCandidate(word, count, round(score, 2))

    phrases: dict[str, KeynessCandidate] = {}
    for phrase, count in grams.items():
        if count < config.min_keyness_count:
            continue
        score = _book_zipf(count, total) - zipf(phrase)
        if score < config.min_phrase_keyness:
            continue
        parts = [part for part in phrase.split() if part not in _PHRASE_INNER]
        # "候选词 + 后面跟常见词"的搭配（parsani writes、parsani notes）多是主谓结构，不是术语；
        # 修饰语在前的（radical openness、islamic warmachines）保留
        head = singles.get(parts[0])
        if head and len(parts) > 1 and all(part not in singles and common(part) >= 4.0 for part in parts[1:]):
            if count < 0.25 * head.count:
                continue
        phrases[phrase] = KeynessCandidate(phrase, count, round(score, 2))

    found = {**singles, **phrases}
    # 被更长短语完全包含且次数不多于它的子短语/单词，不单独收录
    for key in sorted(found, key=len):
        for longer in phrases:
            if key != longer and re.search(rf"(?<![\w'’-]){re.escape(key)}(?![\w'’-])", longer):
                if found.get(longer) and found[longer].count >= found[key].count:
                    found.pop(key, None)
                    break
    return found


# --------------------------------------------------------------------------- #
# 文档结构线索
# --------------------------------------------------------------------------- #


def _inline_text(inlines: list) -> str:
    parts: list[str] = []
    for node in inlines or []:
        if not isinstance(node, dict):
            continue
        kind, content = node.get("t"), node.get("c")
        if kind == "Str":
            parts.append(content)
        elif kind in {"Space", "SoftBreak", "LineBreak"}:
            parts.append(" ")
        elif kind in {"Emph", "Strong", "SmallCaps", "Underline", "Strikeout", "Superscript", "Subscript"}:
            parts.append(_inline_text(content))
        elif kind == "Quoted":
            parts.append(_inline_text(content[1]))
        elif kind in {"Link", "Span"}:
            parts.append(_inline_text(content[1]))
    return "".join(parts)


def structure_cues(blocks: Iterable[dict]) -> Counter:
    """Document IR（pandoc AST 块）里的强调、引号与标题文字 → Counter[(类别, 文字)]。纯数据遍历，不调 pandoc。"""
    cues: Counter = Counter()

    def walk(node) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        kind, content = node.get("t"), node.get("c")
        if kind in {"Emph", "Strong"}:
            cues[("emph", _inline_text(content))] += 1
        elif kind == "Quoted":
            cues[("quote", _inline_text(content[1]))] += 1
        elif kind == "Header":
            cues[("header", _inline_text(content[2]))] += 1
            return
        if content is not None:
            walk(content)

    walk(list(blocks))
    return cues


_CUE_NOISE = re.compile(r"\([^)]*\)|\{[^}]*\}|\[[^\]]*\]")
_CUE_WORD = re.compile(r"^[A-Za-z][A-Za-z'’\-]*$")


def clean_cue(text: str, zipf: Zipf | None) -> str | None:
    """把强调/引号/标题文字清理成候选词形；不像术语（太长、虚词开头结尾、极常见单词、含数字符号）时返回 None。"""
    text = _CUE_NOISE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .,;:!?'’\"“”‘…-—/")
    words = text.split()
    while words and words[0].lower() in {"the", "a", "an"}:
        words.pop(0)
    if not 1 <= len(words) <= 4 or not all(_CUE_WORD.match(word) for word in words):
        return None
    lowered = [word.lower() for word in words]
    if lowered[0] in _STOP or lowered[-1] in _STOP:
        return None
    if any(word in _STOP and word not in _PHRASE_INNER for word in lowered[1:-1]):
        return None
    term = " ".join(words)
    if term.isupper() and len(term) > 4:
        term = term.lower()
    if len(words) == 1 and (len(term) < 3 or (zipf is not None and zipf(term.lower()) >= 5.0)):
        return None
    return term


def structure_candidates(
    cues: Counter, text: str, zipf: Zipf | None, config: SourceConfig | None = None
) -> dict[str, tuple[str, int]]:
    """结构线索 → {小写键: (词形, 正文出现次数)}；正文里至少出现 min_structure_count 次才收。"""
    config = config or SourceConfig()
    grams = _ngram_counts(text, 4)
    found: dict[str, tuple[str, int]] = {}
    for (_kind, raw), _ in cues.most_common():
        term = clean_cue(raw, zipf)
        if term is None or term.lower() in found:
            continue
        key = " ".join(_normalize_token(word) for word in _WORD.findall(term))
        count = grams.get(key, 0) + grams.get(key + "s", 0) + grams.get(key + "es", 0)
        if count >= config.min_structure_count:
            found[term.lower()] = (term, count)
    return found


def _ngram_counts(text: str, max_n: int) -> Counter:
    """全文 1..max_n 词的小写 n-gram 次数（不跨句、去所有格），供快速计数。"""
    counts: Counter = Counter()
    for sentence in _SENTENCE_SPLIT.split(text):
        tokens = [token for token in (_normalize_token(m.group(0)) for m in _WORD.finditer(sentence)) if token]
        for n in range(1, max_n + 1):
            for start in range(len(tokens) - n + 1):
                counts[" ".join(tokens[start : start + n])] += 1
    return counts


# --------------------------------------------------------------------------- #
# 上下文与歧义信号
# --------------------------------------------------------------------------- #


@dataclass
class _Hit:
    segment: IndexedSegment
    start: int
    end: int
    form: str
    sentence_initial: bool


def _hits(index: BookIndex, term: str) -> list[_Hit]:
    pattern = term_pattern(term)
    hits = []
    for segment in index.segments:
        for match in pattern.finditer(segment.source):
            before = segment.source[max(0, match.start() - 4) : match.start()]
            hits.append(_Hit(segment, match.start(), match.end(), match.group(0), bool(_SENTENCE_START.search(before))))
    return hits


def _snippet(hit: _Hit, chars: int) -> str:
    source = hit.segment.source
    start, end = max(0, hit.start - chars), min(len(source), hit.end + chars)
    text = re.sub(r"\s+", " ", source[start:end]).strip()
    text = ("…" if start > 0 else "") + text + ("…" if end < len(source) else "")
    section = hit.segment.section_path[-1] if hit.segment.section_path else ""
    return f"〔{section}〕{text}" if section else text


def _overlap(a: str, b: str) -> float:
    left, right = set(re.findall(r"\w+", a.lower())), set(re.findall(r"\w+", b.lower()))
    return len(left & right) / max(1, min(len(left), len(right)))


def select_contexts(hits: Sequence[_Hit], k: int = 4, chars: int = 110) -> list[str]:
    """从全部出现处挑 k 处差异较大的上下文：首次出现 → 带定义意味的 → 不同章节的 → 按全书位置均匀补齐。

    片段之间词重合度高（> 0.6）的跳过，避免同一句话的几次重复占满名额。
    """
    if not hits:
        return []
    chosen: list[_Hit] = []
    snippets: list[str] = []

    def take(hit: _Hit) -> None:
        if len(chosen) >= k or hit in chosen:
            return
        text = _snippet(hit, chars)
        if any(_overlap(text, other) > 0.6 for other in snippets):
            return
        chosen.append(hit)
        snippets.append(text)

    take(hits[0])
    for hit in hits[1:]:
        window = hit.segment.source[max(0, hit.start - 60) : hit.end + 60]
        if _DEFINITIONAL.search(window):
            take(hit)
            break
    sections_seen = {hit.segment.section_path for hit in chosen}
    for hit in hits:
        if hit.segment.section_path not in sections_seen:
            take(hit)
            sections_seen.add(hit.segment.section_path)
    if len(chosen) < k and len(hits) > 1:
        step = (len(hits) - 1) / max(1, k - 1)
        for i in range(k):
            take(hits[round(i * step)])
    order = {id(hit): i for i, hit in enumerate(hits)}
    paired = sorted(zip(chosen, snippets, strict=True), key=lambda pair: order[id(pair[0])])
    return [text for _, text in paired]


def ambiguity_signals(
    term: str,
    hits: Sequence[_Hit],
    zipf: Zipf | None,
    renderings: Sequence[str] = (),
    common_zipf: float = 4.0,
) -> list[str]:
    """确定性歧义信号；有任何一条，候选就交给术语 agent（或在账本里逐条细看）。

    - mixed_case：小写出现与不在句首的大写出现都成规模（Mission / mission）；
    - sentence_initial：只以大写出现、且多数大写出现在句首——可能只是因位置大写的普通词；
    - common_word：单词本身是常用英语词（wordfreq zipf ≥ common_zipf），专名与普通词义可能并存；
    - divergent_renderings：全书索引里已有两种以上不同译法。
    """
    signals: list[str] = []
    lower = sum(1 for hit in hits if hit.form[:1].islower())
    upper = [hit for hit in hits if hit.form[:1].isupper()]
    upper_mid = sum(1 for hit in upper if not hit.sentence_initial)
    # 两种写法都要成规模（各 ≥ 2 次、少数一方 ≥ 10%），标题式大写的偶发一两次不算
    if min(lower, upper_mid) >= 2 and min(lower, upper_mid) / (lower + upper_mid) >= 0.1:
        signals.append(MIXED_CASE)
    if upper and not lower and sum(hit.sentence_initial for hit in upper) / len(upper) >= 0.5:
        signals.append(SENTENCE_INITIAL)
    if zipf is not None and " " not in term.strip() and zipf(term.lower()) >= common_zipf:
        signals.append(COMMON_WORD)
    if len([text for text in renderings if text not in {KEPT_ORIGINAL, UNRESOLVED}]) >= 2:
        signals.append(DIVERGENT)
    return signals


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #


@dataclass
class _Merged:
    term: str
    count: int
    sources: set[str] = field(default_factory=set)
    keyness: float | None = None


def _covered(key: str, glossary_lower: set[str]) -> bool:
    return key in glossary_lower or key.rstrip("s") in glossary_lower or f"{key}s" in glossary_lower


def gather_candidates(
    index: BookIndex,
    glossary: dict[str, str],
    *,
    text: str | None = None,
    cues: Counter | None = None,
    config: SourceConfig | None = None,
    zipf: Zipf | None = None,
    with_renderings: bool = True,
    reading: Sequence[tuple[str, int]] | None = None,
) -> list[TermCandidate]:
    """三路候选合并、排序、截断，并为每个候选附多处上下文与歧义信号。

    text：用于专名抽取的原文（含 Markdown 的全文；不给时用索引段拼接）。
    cues：structure_cues() 的结果；没有 Document IR 时不给，结构一路跳过。
    reading：阅读遍笔记里的 (原文写法, 次数)；不给（阅读遍关闭）时这一路跳过，结果与旧版一致。
    顺序与配额：先是专名一路（与旧做法同一列表，保证不回退），再按强度补关键度一路的新词、
    按正文次数补结构一路的新词；同一个词被多路命中时合并来源标记，不重复占配额。
    """
    config = config or SourceConfig()
    if zipf is None:
        zipf = default_zipf()
    corpus = corpus_text(index)
    text = text if text is not None else corpus
    glossary_lower = {key.lower() for key in glossary}
    merged: dict[str, _Merged] = {}
    order: list[str] = []

    def add(term: str, count: int, source: str, keyness: float | None = None, cap: int | None = None) -> bool:
        """加入或合并一个候选；返回是否新增了一条。"""
        key = term.lower()
        if _covered(key, glossary_lower):
            return False
        item = merged.get(key)
        if item is None:
            if len(order) >= (config.max_terms if cap is None else cap):
                return False
            merged[key] = _Merged(term, count, {source}, keyness)
            order.append(key)
            return True
        item.sources.add(source)
        item.count = max(item.count, count)
        if keyness is not None:
            item.keyness = keyness if item.keyness is None else max(item.keyness, keyness)
        return False

    # 专名：与旧做法同一列表、同一顺序（不回退）
    if PROPER in config.sources:
        for candidate in extract_term_candidates(text, glossary, max_terms=config.max_proper):
            add(candidate.term, candidate.count, PROPER)
    # 关键度：次数越多、超过门槛越多越靠前；已在专名里的只补来源标记，不占配额
    if KEYNESS in config.sources and zipf is not None:

        def strength(item: KeynessCandidate) -> float:
            floor = config.min_phrase_keyness if " " in item.key else config.min_word_keyness
            return math.log2(item.count) + item.keyness - floor

        added = 0
        found = keyness_candidates(corpus, zipf, config)
        for item in sorted(found.values(), key=lambda item: (-strength(item), item.key)):
            if item.key in merged:
                add(item.key, item.count, KEYNESS, item.keyness)
            elif added < config.max_keyness and add(item.key, item.count, KEYNESS, item.keyness):
                added += 1
    # 结构：正文次数多的优先，同样只有新词占配额
    if STRUCTURE in config.sources and cues:
        added = 0
        found_cues = structure_candidates(cues, corpus, zipf, config)
        for key, (term, count) in sorted(found_cues.items(), key=lambda pair: (-pair[1][1], pair[0])):
            if key in merged:
                add(term, count, STRUCTURE)
            elif added < config.max_structure and add(term, count, STRUCTURE):
                added += 1

    # 阅读遍：模型逐章读后记下的概念；已有的只补来源标记，新词另有配额
    if reading:
        cap = len(order) + config.max_reading
        for term, count in reading:
            add(term, count, READING, cap=cap)

    total_words = max(1, len(_WORD.findall(corpus)))
    for item in merged.values():
        if item.keyness is None and zipf is not None and item.count:
            item.keyness = round(_book_zipf(item.count, total_words) - zipf(item.term.lower()), 2)
    ordered = [merged[key] for key in order]
    candidates = []
    for item in ordered:
        hits = _hits(index, item.term)
        renderings: list[str] = []
        if with_renderings and any(hit.segment.translation for hit in hits):
            renderings = [r.text for r in index.renderings(item.term).renderings]
        ambiguity = ambiguity_signals(item.term, hits, zipf, renderings, config.common_zipf)
        contexts = select_contexts(hits, min(5, config.contexts + (1 if ambiguity else 0)), config.context_chars)
        candidates.append(
            TermCandidate(
                term=item.term,
                count=item.count,
                context=contexts[0] if contexts else "",
                sources=tuple(source for source in ALL_SOURCES if source in item.sources),
                contexts=contexts,
                ambiguity=ambiguity,
                keyness=item.keyness,
            )
        )
    return candidates


# --------------------------------------------------------------------------- #
# 没有 Document IR 时的段落索引
# --------------------------------------------------------------------------- #

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MARKUP = re.compile(r"!\[[^\]]*\]\([^)]*\)|\]\([^)]*\)|\{[^}\n]*\}|<[^>]+>|```[\s\S]*?```|`[^`]*`")
_EMPHASIS = re.compile(r"(?<![\w*])[*_]{1,3}|[*_]{1,3}(?![\w*])")


def paragraph_index(text: str) -> BookIndex:
    """把 Markdown 按空行拆段建一个只含原文的索引，章节路径取自 # 标题。不依赖 pandoc。"""
    segments: list[IndexedSegment] = []
    path: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        heading = _HEADING.match(block.splitlines()[0])
        if heading:
            level = len(heading.group(1))
            path = path[: level - 1] + [_EMPHASIS.sub("", _MARKUP.sub("", heading.group(2))).replace("[", "").strip()]
        plain = re.sub(r"\s+", " ", _EMPHASIS.sub("", _MARKUP.sub(" ", block)).replace("[", "")).strip(" #>")
        if re.search(r"[A-Za-z]", plain):
            segments.append(IndexedSegment(len(segments), 0, plain, section_path=tuple(path)))
    return BookIndex(segments)
