"""
全书索引（Book Index）：以段为单位保存全书原文、译文与位置，提供三种查询。

- 关键词检索：在原文、译文或两者里找包含全部关键词的段；
- 出现处查询：一个英文词或短语在全书的全部出现段、前后文与该段译文，支持大小写与词形变体、分页；
- 译法分布：一个词在各段里对应的中文译法及次数，每种译法带证据段号。

本模块只做纯数据查询，不读写文件、不调用 pandoc 或模型；段由上层从 Document IR 抽取后传入。
它是术语 agent、审校 agent、纠正传播、一致率指标与 MCP 查询的共同底座。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

STATUS_SOURCE_ONLY = "source_only"  # 还没有译文（翻译前或该块尚未完成）
STATUS_TRANSLATED = "translated"
STATUS_UNALIGNED = "unaligned"  # 该块译文结构与原文对不上，无法定位到段
STATUS_FAILED = "failed"

KEPT_ORIGINAL = "（保留原文）"
UNRESOLVED = "（未识别）"

_CJK = re.compile(r"[㐀-鿿]")
_CJK_RUN = re.compile(r"[㐀-鿿·・]+")
# 挖出的候选区分度下限；不够的宁可记"未识别"，也不拿上下文里的无关词充数
_MIN_LIFT = 0.3


@dataclass(frozen=True)
class IndexedSegment:
    """索引中的一段：全书顺序号、对照阅读器段号、所在块与章节、原文与译文（纯文本）。

    position 覆盖全部含文字的位置（包括参考文献等保留原文、不送模型的段）；
    seg_id 只有送去翻译的段才有，与双栏对照阅读器的跳转编号一致。
    """

    position: int
    chunk_index: int
    source: str
    translation: str | None = None
    seg_id: int | None = None
    section_path: tuple[str, ...] = ()
    in_note: bool = False
    verbatim: bool = False
    status: str = STATUS_SOURCE_ONLY


@dataclass(frozen=True)
class Occurrence:
    segment: IndexedSegment
    matches: tuple[tuple[int, int, str], ...]  # (起, 止, 原文中的实际写法)
    before: str  # 上一段原文末尾
    after: str  # 下一段原文开头


@dataclass(frozen=True)
class OccurrencePage:
    term: str
    total_matches: int
    total_segments: int
    offset: int
    limit: int
    forms: dict[str, int]  # 原文中出现的写法（大小写、词形）→ 次数
    items: list[Occurrence]


@dataclass(frozen=True)
class KeywordHit:
    segment: IndexedSegment
    field: str  # source | translation


@dataclass(frozen=True)
class SearchPage:
    query: str
    total: int
    offset: int
    limit: int
    items: list[KeywordHit]


@dataclass(frozen=True)
class Rendering:
    text: str
    count: int
    positions: tuple[int, ...]  # 证据段（全书顺序号）


@dataclass(frozen=True)
class RenderingDistribution:
    term: str
    segments: int  # 含该词且已有段级译文的段数
    untranslated_segments: int  # 含该词但还没有段级译文的段数（未翻译、对不齐或失败）
    renderings: list[Rendering] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 词形匹配
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_JOINER = r"(?:[\s\-‐‑–—]+)"
_POSSESSIVE = r"(?:['’]s|['’])?"


def _inflected(token: str) -> str:
    """最后一个词的常见英语词形：复数、所有格、过去式、进行时。"""
    lower = token.lower()
    if len(token) > 2 and lower.endswith("y") and lower[-2] not in "aeiou":
        stem = re.escape(token[:-1])
        return rf"{stem}(?:y|ies|ied|ying)"
    if len(token) > 2 and lower.endswith("e"):
        stem = re.escape(token[:-1])
        return rf"{stem}(?:e|es|ed|ing)"
    return rf"{re.escape(token)}(?:s|es|ed|ing)?"


def term_pattern(term: str, *, case_sensitive: bool = False, variants: bool = True) -> re.Pattern:
    """把词或短语编译成匹配原文的正则。

    - 短语内的空白与连字符可以互换（time war / time-war）；
    - variants=True 时最后一个词允许复数、所有格、-ed/-ing 等词形；
    - 前后不能紧挨其他字母数字（"war" 不会命中 "warp"、"Tellurian" 不会命中 "Tellurians"
      之外的更长词——后者是词形变体，会命中）。
    """
    tokens = _TOKEN.findall(term)
    if not tokens:
        raise ValueError("查询词为空")
    parts = [re.escape(token) for token in tokens[:-1]]
    parts.append(_inflected(tokens[-1]) if variants else re.escape(tokens[-1]))
    body = _JOINER.join(parts) + (_POSSESSIVE if variants else "")
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(rf"(?<![^\W_]){body}(?![^\W_])", flags | re.UNICODE)


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #


class BookIndex:
    """一本书的段级索引（内存视图）。段按全书顺序排列。"""

    def __init__(self, segments: Iterable[IndexedSegment]):
        self.segments: list[IndexedSegment] = sorted(segments, key=lambda seg: seg.position)
        self._by_position = {seg.position: i for i, seg in enumerate(self.segments)}

    def __len__(self) -> int:
        return len(self.segments)

    def get(self, position: int) -> IndexedSegment | None:
        index = self._by_position.get(position)
        return self.segments[index] if index is not None else None

    def by_seg_id(self, seg_id: int) -> IndexedSegment | None:
        return next((seg for seg in self.segments if seg.seg_id == seg_id), None)

    @property
    def stats(self) -> dict:
        statuses = Counter(seg.status for seg in self.segments)
        return {"segments": len(self.segments), **dict(sorted(statuses.items()))}

    # ------------------------------------------------------------------ #
    # 出现处
    # ------------------------------------------------------------------ #

    def occurrences(
        self,
        term: str,
        *,
        case_sensitive: bool = False,
        variants: bool = True,
        offset: int = 0,
        limit: int = 20,
        context_chars: int = 120,
    ) -> OccurrencePage:
        """某词在全书的全部出现段（按段分页）；total_matches 是全书命中次数，不受分页影响。"""
        pattern = term_pattern(term, case_sensitive=case_sensitive, variants=variants)
        hits: list[tuple[int, tuple[tuple[int, int, str], ...]]] = []
        forms: Counter = Counter()
        for i, seg in enumerate(self.segments):
            matches = tuple((m.start(), m.end(), m.group(0)) for m in pattern.finditer(seg.source))
            if matches:
                hits.append((i, matches))
                forms.update(text for _, _, text in matches)
        items = [
            Occurrence(
                segment=self.segments[i],
                matches=matches,
                before=self.segments[i - 1].source[-context_chars:] if i > 0 else "",
                after=self.segments[i + 1].source[:context_chars] if i + 1 < len(self.segments) else "",
            )
            for i, matches in hits[offset : offset + limit]
        ]
        return OccurrencePage(
            term=term,
            total_matches=sum(len(matches) for _, matches in hits),
            total_segments=len(hits),
            offset=offset,
            limit=limit,
            forms=dict(forms.most_common()),
            items=items,
        )

    # ------------------------------------------------------------------ #
    # 关键词检索
    # ------------------------------------------------------------------ #

    def search(self, query: str, *, field: str = "any", offset: int = 0, limit: int = 20) -> SearchPage:
        """包含全部关键词（空白分隔，不区分大小写）的段。field: source | translation | any。"""
        if field not in {"source", "translation", "any"}:
            raise ValueError("field 只能是 source、translation 或 any")
        keywords = [word.lower() for word in query.split() if word]
        if not keywords:
            raise ValueError("查询词为空")
        fields = ("source", "translation") if field == "any" else (field,)
        hits: list[KeywordHit] = []
        for seg in self.segments:
            for name in fields:
                text = (getattr(seg, name) or "").lower()
                if text and all(word in text for word in keywords):
                    hits.append(KeywordHit(seg, name))
                    break
        return SearchPage(query=query, total=len(hits), offset=offset, limit=limit, items=hits[offset : offset + limit])

    # ------------------------------------------------------------------ #
    # 译法分布
    # ------------------------------------------------------------------ #

    def renderings(
        self,
        term: str,
        *,
        known: Sequence[str] = (),
        case_sensitive: bool = False,
        variants: bool = True,
        max_candidates: int = 8,
        examples: int = 5,
    ) -> RenderingDistribution:
        """某词在各段对应的中文译法及次数。

        不调用模型，用三条证据定每段的译法：
        1. 已知译法（如术语表译名）出现在该段译文里；
        2. 从含该词的段的译文中挖出的候选：在这些段里反复出现、在其余段里少见的中文片段，
           紧挨着"（原词）"括注出现的片段优先；
        3. 都没有时，若原词原样留在译文里记为"保留原文"，否则记为"未识别"。
        这是启发式统计，每种译法附证据段号，供人工或术语 agent 核查。
        """
        pattern = term_pattern(term, case_sensitive=case_sensitive, variants=variants)
        inside: list[IndexedSegment] = []
        untranslated = 0
        for seg in self.segments:
            if not pattern.search(seg.source):
                continue
            if seg.translation and seg.status == STATUS_TRANSLATED:
                inside.append(seg)
            else:
                untranslated += 1
        if not inside:
            return RenderingDistribution(term, 0, untranslated)

        candidates = list(dict.fromkeys(text.strip() for text in known if text and text.strip()))
        candidates += [c for c in self._mined_candidates(pattern, inside, max_candidates) if c not in candidates]

        assigned: dict[str, list[int]] = {}
        for seg in inside:
            translation = seg.translation or ""
            choice = next((c for c in candidates if c in translation), None)
            if choice is None:
                choice = KEPT_ORIGINAL if pattern.search(translation) else UNRESOLVED
            assigned.setdefault(choice, []).append(seg.position)

        renderings = sorted(
            (Rendering(text, len(positions), tuple(positions[:examples])) for text, positions in assigned.items()),
            key=lambda item: (item.text in {KEPT_ORIGINAL, UNRESOLVED}, -item.count, item.text),
        )
        return RenderingDistribution(term, len(inside), untranslated, renderings)

    def _mined_candidates(self, pattern: re.Pattern, inside: list[IndexedSegment], max_candidates: int) -> list[str]:
        """从含该词的段的译文里挖候选译法。

        - 频繁片段：在含该词的段里至少出现于 floor 个段的 2~6 字中文片段；
        - 括注片段：紧挨"（原词）"之前的中文串，取其后缀里区分度最高的一个并加分；
        - 区分度（lift）= 在含该词段里的覆盖比例 − 在其余段里的覆盖比例，低于 _MIN_LIFT 的不要；
        - 互相包含的候选（"大地"与"的大地"、"虚构"与"超虚构"）只留一个：lift 再乘以
          "出现时落在含该词段里的比例"，取更专用的那个。
        """
        inside_texts = [seg.translation or "" for seg in inside]
        inside_ids = {seg.position for seg in inside}
        outside_texts = [
            seg.translation
            for seg in self.segments
            if seg.translation and seg.status == STATUS_TRANSLATED and seg.position not in inside_ids
        ]
        gloss_re = re.compile(rf"([㐀-鿿·・]+)\s*[（(]\s*{pattern.pattern}\s*[)）]", pattern.flags)
        gloss_runs = [match.group(1) for text in inside_texts for match in gloss_re.finditer(text)]

        ngram_df: Counter = Counter()
        for text in inside_texts:
            ngram_df.update(_cjk_ngrams(text))
        floor = max(2, round(0.2 * len(inside))) if len(inside) >= 3 else 1
        pool = {gram for gram, count in ngram_df.items() if count >= floor}
        pool |= {run[-n:] for run in gloss_runs for n in range(2, min(len(run), 8) + 1)}
        if not pool:
            return []

        lift: dict[str, float] = {}
        specificity: dict[str, float] = {}
        for gram in pool:
            hits_in = sum(gram in text for text in inside_texts)
            hits_out = sum(gram in text for text in outside_texts)
            lift[gram] = hits_in / len(inside_texts) - (hits_out / len(outside_texts) if outside_texts else 0.0)
            # 再乘以"出现时有多大比例落在含该词的段里"：通用片段（"虚构"）比专用片段（"超虚构"）低
            specificity[gram] = lift[gram] * hits_in / (hits_in + hits_out) if hits_in else 0.0
        glossed = {
            max((run[-n:] for n in range(2, min(len(run), 8) + 1)), key=lambda g: (specificity[g], len(g)))
            for run in gloss_runs
            if len(run) >= 2
        }

        def score(gram: str) -> float:
            return lift[gram] + (1.0 if gram in glossed else 0.0)

        chosen: list[str] = []
        for gram in sorted(pool, key=lambda g: (score(g), len(g)), reverse=True):
            if score(gram) < _MIN_LIFT:
                break
            clashes = [kept for kept in chosen if gram in kept or kept in gram]
            if clashes:
                if all(gram in glossed or specificity[gram] > specificity[kept] for kept in clashes):
                    chosen = [kept for kept in chosen if kept not in clashes] + [gram]
                continue
            chosen.append(gram)
            if len(chosen) >= max_candidates:
                break
        return chosen


def _cjk_ngrams(text: str, min_n: int = 2, max_n: int = 6) -> set[str]:
    grams: set[str] = set()
    for run in _CJK_RUN.findall(text):
        for n in range(min_n, min(max_n, len(run)) + 1):
            grams.update(run[i : i + n] for i in range(len(run) - n + 1))
    return grams
