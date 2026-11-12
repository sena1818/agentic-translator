"""术语 agent 的工具：查出现处（分页）、查已有译法分布、查术语表与账本、按关键词或按意思找段。

术语 agent 在翻译前运行，这时还没有任务、也没有落盘的全书索引，所以这些工具不收 task_id，
依赖从 ctx.extras 取：
- index：全书索引（BookIndex，内存视图）；
- glossary：用户术语表；ledger：本书账本；
- suggestions：同作者 / 同领域其他书的参考译名 [(term, rendering, label)]；
- search_by_meaning：可选，query → [(position, source, translation)]。

用 term_agent_registry() 组装给运行框架；不在 DEFAULT_MODULES 里，不暴露给 MCP
（MCP 面向已翻译的任务，已有 term_occurrences / term_renderings / read_term_ledger / search_book）。
每条输出都用"[段 N]"标出全书顺序号，agent 提交结论时引用这些段号，框架据此核对证据确实查到过。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from ...domain.index.book_index import BookIndex
from ...domain.terminology.term_decisions import segment_marker
from ...infrastructure.agent import ToolRegistry
from .base import ToolContext, shared_tool

PAGE_SIZE = 6
EXCERPT_CHARS = 220

# (term, rendering, label) 三元组：其他书的建议译名等只读参考
Suggestion = tuple[str, str, str]
# 按意思找段：(query, limit) → [(position, source, translation or None)]
MeaningSearch = Callable[[str, int], Sequence[tuple[int, str, str | None]]]

TOOL_NAMES = ("find_occurrences", "rendering_distribution", "lookup_glossary", "search_segments")
MEANING_TOOL = "search_by_meaning"


def _index(ctx: ToolContext) -> BookIndex:
    return ctx.extras["index"]


def _excerpt(text: str, start: int | None = None, end: int | None = None) -> str:
    text = " ".join(text.split())
    if start is None or len(text) <= EXCERPT_CHARS:
        return text[:EXCERPT_CHARS]
    half = (EXCERPT_CHARS - (end - start)) // 2
    left = max(0, start - max(half, 20))
    snippet = text[left : left + EXCERPT_CHARS]
    return ("…" if left else "") + snippet + ("…" if left + EXCERPT_CHARS < len(text) else "")


def _section(path: Sequence[str]) -> str:
    return " > ".join(path[-2:]) if path else "（无章节）"


@shared_tool("find_occurrences")
async def find_occurrences(ctx: ToolContext, term: str, offset: int = 0) -> str:
    """查某词在全书的出现处（按段分页，每页 6 段），给出段号、章节、原文片段和已有译文"""
    page = _index(ctx).occurrences(term, offset=max(0, offset), limit=PAGE_SIZE)
    if not page.total_segments:
        return f"全书没有找到 {term}。"
    forms = "、".join(f"{form}×{count}" for form, count in list(page.forms.items())[:6])
    lines = [
        f"{term}：全书 {page.total_matches} 次，分布在 {page.total_segments} 段；写法 {forms}。"
        f"本页第 {offset + 1}–{offset + len(page.items)} 段："
    ]
    for item in page.items:
        seg = item.segment
        start, end, _ = item.matches[0]
        line = f"{segment_marker(seg.position)} {_section(seg.section_path)}｜原文：{_excerpt(seg.source, start, end)}"
        if seg.translation:
            line += f"｜译文：{_excerpt(seg.translation)}"
        lines.append(line)
    if offset + len(page.items) < page.total_segments:
        lines.append(
            f"（还有 {page.total_segments - offset - len(page.items)} 段，用 offset={offset + PAGE_SIZE} 翻页）"
        )
    return "\n".join(lines)


@shared_tool("rendering_distribution")
async def rendering_distribution(ctx: ToolContext, term: str) -> str:
    """查某词在已有译文里的译法分布及例段（翻译前运行时通常还没有译文）"""
    mapping = {**ctx.extras.get("ledger", {}), **ctx.extras.get("glossary", {})}
    known = [value.split(" (", 1)[0] for key, value in mapping.items() if key.casefold() == term.casefold()]
    dist = _index(ctx).renderings(term, known=known)
    if not dist.segments:
        return (
            f"{term} 所在的 {dist.untranslated_segments} 段还没有译文（翻译前运行时正常），"
            "无法统计已有译法；请根据原文上下文判断。"
        )
    lines = [f"{term}：{dist.segments} 段已有译文，{dist.untranslated_segments} 段没有。"]
    for rendering in dist.renderings:
        marks = " ".join(segment_marker(p) for p in rendering.examples)
        lines.append(f"- {rendering.text}：{rendering.count} 段，例 {marks}")
    return "\n".join(lines)


@shared_tool("lookup_glossary")
async def lookup_glossary(ctx: ToolContext, term: str) -> str:
    """查用户术语表、本书账本和同作者/同领域其他书的参考译名里与该词有关的条目"""
    key = term.casefold()
    words = set(key.split())
    lines = []
    sources = (
        ("用户术语表（必须遵守）", ctx.extras.get("glossary", {})),
        ("本书术语账本", ctx.extras.get("ledger", {})),
    )
    for label, mapping in sources:
        for entry, value in mapping.items():
            folded = entry.casefold()
            if folded == key or words & set(folded.split()):
                lines.append(f"- [{label}] {entry}: {value}")
    for entry, rendering, label in ctx.extras.get("suggestions", ()):
        if entry.casefold() == key:
            lines.append(f"- [{label}·仅供参考] {entry}: {rendering}")
    return "\n".join(lines[:20]) if lines else f"术语表、账本和参考译名里都没有 {term} 或含它的条目。"


@shared_tool("search_segments")
async def search_segments(ctx: ToolContext, query: str, offset: int = 0) -> str:
    """按关键词在原文里找段（空白分隔的词须同时出现），用来找定义、全名或搭配，如 'Captain Mission'"""
    page = _index(ctx).search(query, field="source", offset=max(0, offset), limit=PAGE_SIZE)
    if not page.total:
        return f"原文里没有同时包含「{query}」各词的段。"
    lines = [f"「{query}」：原文中 {page.total} 段命中，本页："]
    for hit in page.items:
        seg = hit.segment
        lines.append(f"{segment_marker(seg.position)} {_section(seg.section_path)}｜{_excerpt(seg.source)}")
    return "\n".join(lines)


@shared_tool(MEANING_TOOL)
async def search_by_meaning(ctx: ToolContext, query: str, offset: int = 0) -> str:
    """按意思找段（中英文均可），用来找某个概念被解释或定义的地方"""
    search: MeaningSearch = ctx.extras["search_by_meaning"]
    hits = list(search(query, PAGE_SIZE + max(0, offset)))[max(0, offset) :]
    if not hits:
        return f"没有找到与「{query}」意思相近的段。"
    return "\n".join(f"{segment_marker(position)} {_excerpt(source)}" for position, source, _ in hits)


def term_agent_registry(
    *,
    index: BookIndex,
    glossary: Mapping[str, str],
    ledger: Mapping[str, str] | None = None,
    suggestions: Sequence[Suggestion] = (),
    search_by_meaning: MeaningSearch | None = None,
) -> ToolRegistry:
    """组装术语 agent 的工具子集（同一批共享工具函数，绑定到这本书的内存索引）。"""
    import sys

    from . import build_tool_suite

    extras = {
        "index": index,
        "glossary": dict(glossary),
        "ledger": dict(ledger or {}),
        "suggestions": list(suggestions),
        "search_by_meaning": search_by_meaning,
    }
    suite = build_tool_suite(ToolContext(extras=extras), modules=[sys.modules[__name__]])
    names = [*TOOL_NAMES, *([MEANING_TOOL] if search_by_meaning is not None else [])]
    return suite.subset(names)


__all__ = ["MeaningSearch", "Suggestion", "TOOL_NAMES", "term_agent_registry"]
