"""
纠正记忆（Correction Memory）的纯逻辑：不读写文件、不调用 pandoc 或模型。

- 纠正记录：人工编辑一块时，记下改了哪些片段（旧译法 → 新译法）、在哪、何时；
- 译名纠正识别：被改掉的片段恰好是某个术语的译名、且该块原文含这个术语，就是一次译名纠正；
- 书内传播：用全书索引找出该术语的其他出现段，只替换这些段里的旧译名，
  旧译名作为更长译名的一部分出现时（"虚构" 在 "超虚构" 里）不动；
- 术语条目：带适用范围（本书 / 同一作者 / 同一领域）和决定来源；
  翻译新书时同作者、同领域的条目只作为建议，不进入强制校验。
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from ..index.book_index import STATUS_TRANSLATED, IndexedSegment, term_pattern

SCOPE_BOOK = "book"
SCOPE_AUTHOR = "author"
SCOPE_DOMAIN = "domain"
SCOPES = (SCOPE_BOOK, SCOPE_AUTHOR, SCOPE_DOMAIN)

SOURCE_HUMAN = "human"
SOURCE_GLOSSARY = "glossary"
SOURCE_LEDGER = "ledger"
SOURCE_AGENT = "agent"
SOURCE_LABELS = {
    SOURCE_HUMAN: "人工纠正",
    SOURCE_GLOSSARY: "术语表",
    SOURCE_LEDGER: "术语账本",
    SOURCE_AGENT: "术语 agent",
}
# 同一术语有多条来源时的优先级：人工纠正 > 术语表 > agent > 账本
_SOURCE_RANK = {SOURCE_HUMAN: 0, SOURCE_GLOSSARY: 1, SOURCE_AGENT: 2, SOURCE_LEDGER: 3}

_GLOSS_SUFFIX = re.compile(r"\s*[（(][^（）()]*[)）]\s*$")


def preferred_rendering(value: str) -> str:
    """术语表的值 "超虚构 (Hyperstition)" → 译名 "超虚构"。"""
    return _GLOSS_SUFFIX.sub("", value).strip() or value.strip()


def with_rendering(value: str, rendering: str) -> str:
    """把术语表值里的译名换成新译名，保留原有的括注写法。"""
    match = _GLOSS_SUFFIX.search(value)
    return f"{rendering}{match.group(0)}" if match else rendering


# --------------------------------------------------------------------------- #
# 纠正记录
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChangedSpan:
    """一处被人工改动的片段（旧译文中的位置与内容 → 新译文中的内容）。"""

    old_start: int
    old_end: int
    old_text: str
    new_text: str


@dataclass(frozen=True)
class Correction:
    task_id: str
    book: str
    chunk_index: int | None
    kind: str  # edit | term | propagation
    old_text: str
    new_text: str
    term: str | None = None
    positions: tuple[int, ...] = ()
    created_at: str = ""
    id: int | None = None


def changed_spans(old: str, new: str) -> list[ChangedSpan]:
    """字符级差异：返回每处替换、插入或删除。"""
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return [
        ChangedSpan(i1, i2, old[i1:i2], new[j1:j2]) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"
    ]


def map_span(old: str, new: str, start: int, end: int) -> tuple[int, int]:
    """旧文本的 [start, end) 在新文本中对应的区间；落在改动块里时扩到改动块边界。"""
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)

    def project(pos: int, left: bool) -> int:
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal" and i1 <= pos <= i2:
                return j1 + (pos - i1)
            if i1 <= pos < i2 or (tag == "insert" and pos == i1 and not left):
                return j1 if left else j2
        return len(new)

    new_start, new_end = project(start, True), project(end, False)
    if end > start and new_end < new_start:
        new_end = new_start
    return new_start, new_end


@dataclass(frozen=True)
class TermChange:
    """从一次人工编辑里识别出的译名纠正。"""

    term: str
    old_rendering: str
    new_rendering: str
    count: int  # 该块里被改掉的次数


def detect_term_changes(source: str, old: str, new: str, glossary: Mapping[str, str]) -> list[TermChange]:
    """人工编辑里改掉的是不是某个术语的译名。

    条件：原文含该术语；旧译文里该译名的某处出现落在改动范围内，且新译文对应位置换成了另一个词；
    新译文里该译名出现次数减少。同一术语取最常见的新写法。
    """
    if old == new:
        return []
    spans = changed_spans(old, new)
    changes: list[TermChange] = []
    for term, value in sorted(glossary.items(), key=lambda item: -len(preferred_rendering(item[1]))):
        rendering = preferred_rendering(value)
        if not rendering or not term_pattern(term).search(source):
            continue
        if new.count(rendering) >= old.count(rendering):
            continue
        replacements: dict[str, int] = {}
        for match in re.finditer(re.escape(rendering), old):
            if not any(
                span.old_start < match.end()
                and match.start() < span.old_end
                or (span.old_start == span.old_end and match.start() < span.old_start < match.end())
                for span in spans
            ):
                continue
            start, end = map_span(old, new, match.start(), match.end())
            candidate = new[start:end].strip()
            if candidate and candidate != rendering and rendering not in candidate:
                replacements[candidate] = replacements.get(candidate, 0) + 1
        if replacements:
            best, count = max(replacements.items(), key=lambda item: (item[1], len(item[0])))
            if not any(c.old_rendering == rendering for c in changes):
                changes.append(TermChange(term, rendering, best, count))
    return changes


# --------------------------------------------------------------------------- #
# 受保护的替换
# --------------------------------------------------------------------------- #


def protected_strings(old: str, new: str, renderings: Iterable[str]) -> list[str]:
    """旧译名作为这些更长写法的一部分出现时不替换：其他术语的译名，以及包含旧译名的新译名本身。"""
    protected = {item for item in renderings if item and old in item and item != old}
    if old in new and new != old:
        protected.add(new)
    return sorted(protected, key=len, reverse=True)


def replaceable_spans(text: str, old: str, protected: Sequence[str] = ()) -> list[tuple[int, int]]:
    """text 中可以替换的旧译名位置（不落在任何受保护写法之内）。"""
    if not old:
        return []
    guarded = [(m.start(), m.end()) for item in protected for m in re.finditer(re.escape(item), text)]
    spans = []
    for match in re.finditer(re.escape(old), text):
        if not any(start <= match.start() and match.end() <= end for start, end in guarded):
            spans.append((match.start(), match.end()))
    return spans


def replace_unprotected(text: str, old: str, new: str, protected: Sequence[str] = ()) -> tuple[str, int]:
    """只替换不在受保护写法之内的旧译名，返回 (新文本, 替换次数)。其余文字一字不动。"""
    spans = replaceable_spans(text, old, protected)
    if not spans:
        return text, 0
    parts, last = [], 0
    for start, end in spans:
        parts.append(text[last:start])
        parts.append(new)
        last = end
    parts.append(text[last:])
    return "".join(parts), len(spans)


# --------------------------------------------------------------------------- #
# 书内传播
# --------------------------------------------------------------------------- #

REASON_ALREADY_NEW = "already_new"  # 已经是新译名
REASON_OTHER_RENDERING = "other_rendering"  # 该段没用旧译名（可能用了别的译法）
REASON_NOT_ALIGNED = "not_aligned"  # 该段没有段级译文（未译、失败或块结构对不上），需手动处理
REASON_PROTECTED_ONLY = "protected_only"  # 旧译名只出现在更长的写法里


@dataclass(frozen=True)
class PropagationItem:
    """预览中的一段：原文含该术语、译文含可替换的旧译名。"""

    position: int
    seg_id: int | None
    chunk_index: int
    source: str
    translation: str
    preview: str  # 替换后的段译文
    count: int


@dataclass(frozen=True)
class SkippedSegment:
    position: int
    seg_id: int | None
    chunk_index: int
    reason: str


@dataclass
class PropagationPlan:
    term: str
    old_rendering: str
    new_rendering: str
    protected: list[str]
    items: list[PropagationItem] = field(default_factory=list)
    skipped: list[SkippedSegment] = field(default_factory=list)

    @property
    def total_replacements(self) -> int:
        return sum(item.count for item in self.items)


def plan_propagation(
    segments: Iterable[IndexedSegment],
    term: str,
    old_rendering: str,
    new_rendering: str,
    renderings: Iterable[str] = (),
    *,
    case_sensitive: bool = False,
) -> PropagationPlan:
    """找出原文含该术语、译文仍用旧译名的段，并给出替换后的预览。

    只看原文含该术语的段：其他段里出现的同样两个汉字可能是别的意思，不碰。
    """
    if not old_rendering or not new_rendering or old_rendering == new_rendering:
        raise ValueError("旧译名与新译名不能为空且不能相同")
    pattern = term_pattern(term, case_sensitive=case_sensitive)
    protected = protected_strings(old_rendering, new_rendering, renderings)
    plan = PropagationPlan(term, old_rendering, new_rendering, protected)
    for seg in segments:
        if not pattern.search(seg.source):
            continue
        if seg.status != STATUS_TRANSLATED or seg.translation is None:
            plan.skipped.append(SkippedSegment(seg.position, seg.seg_id, seg.chunk_index, REASON_NOT_ALIGNED))
            continue
        preview, count = replace_unprotected(seg.translation, old_rendering, new_rendering, protected)
        if count:
            plan.items.append(
                PropagationItem(seg.position, seg.seg_id, seg.chunk_index, seg.source, seg.translation, preview, count)
            )
            continue
        if new_rendering in seg.translation:
            reason = REASON_ALREADY_NEW
        elif old_rendering in seg.translation:
            reason = REASON_PROTECTED_ONLY
        else:
            reason = REASON_OTHER_RENDERING
        plan.skipped.append(SkippedSegment(seg.position, seg.seg_id, seg.chunk_index, reason))
    return plan


# --------------------------------------------------------------------------- #
# 术语条目与跨书建议
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TermEntry:
    """一条结构化术语决定。evidence 是全书顺序号（证据段）。"""

    task_id: str
    book: str
    term: str
    rendering: str
    scope: str = SCOPE_BOOK
    source: str = SOURCE_GLOSSARY
    author: str | None = None
    domain: str | None = None
    sense: str = ""
    evidence: tuple[int, ...] = ()
    updated_at: str = ""


def default_scope(author: str | None, domain: str | None) -> str:
    """填了作者就按作者共享，只填领域就按领域共享，都没填只用于本书。"""
    if author and author.strip():
        return SCOPE_AUTHOR
    if domain and domain.strip():
        return SCOPE_DOMAIN
    return SCOPE_BOOK


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def entry_applies(entry: TermEntry, author: str | None, domain: str | None) -> bool:
    """条目能否作为另一本书的建议：同作者的 author 条目、同领域的 domain 条目。本书条目不外溢。"""
    if entry.scope == SCOPE_AUTHOR:
        return bool(_norm(author)) and _norm(entry.author) == _norm(author)
    if entry.scope == SCOPE_DOMAIN:
        return bool(_norm(domain)) and _norm(entry.domain) == _norm(domain)
    return False


@dataclass(frozen=True)
class TermSuggestion:
    term: str
    rendering: str
    book: str
    task_id: str
    scope: str
    source: str

    @property
    def label(self) -> str:
        scope = "同作者" if self.scope == SCOPE_AUTHOR else "同领域"
        return f"{scope}《{self.book}》·{SOURCE_LABELS.get(self.source, self.source)}"


def rank_suggestions(
    entries: Iterable[TermEntry],
    author: str | None,
    domain: str | None,
    *,
    exclude_task: str | None = None,
    known_terms: Iterable[str] = (),
) -> list[TermSuggestion]:
    """为一本新书挑选跨书建议：每个术语一条，人工纠正优先、同作者优先于同领域、新的优先。

    已在本书术语表里的术语不再建议（它们已经是硬约束）。
    """
    known = {term.casefold() for term in known_terms}
    best: dict[str, TermEntry] = {}
    # 新的在前；之后只有来源或范围更优才替换，同等条件保留较新的那条
    for entry in sorted(entries, key=lambda e: e.updated_at, reverse=True):
        if entry.task_id == exclude_task or entry.term.casefold() in known:
            continue
        if not entry_applies(entry, author, domain):
            continue
        key = entry.term.casefold()
        current = best.get(key)
        if current is None or _rank(entry) < _rank(current):
            best[key] = entry
    return [
        TermSuggestion(e.term, preferred_rendering(e.rendering), e.book, e.task_id, e.scope, e.source)
        for e in sorted(best.values(), key=lambda e: e.term.casefold())
    ]


def _rank(entry: TermEntry) -> tuple[int, int]:
    return (_SOURCE_RANK.get(entry.source, 9), 0 if entry.scope == SCOPE_AUTHOR else 1)


def suggestions_prompt(suggestions: Sequence[TermSuggestion], relevant_terms: Iterable[str] | None = None) -> str:
    """翻译提示里的"参考译名"一节；relevant_terms 给出时只列本块出现的术语。"""
    wanted = {term.casefold() for term in relevant_terms} if relevant_terms is not None else None
    lines = [
        f"- {s.term}: {s.rendering}（{s.label}）" for s in suggestions if wanted is None or s.term.casefold() in wanted
    ]
    if not lines:
        return ""
    return "\n\n【参考译名】(来自同作者或同领域的其他书，仅供参考，可按本书语境调整):\n" + "\n".join(lines)
