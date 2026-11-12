"""术语与记忆工具：查词语出现处与译法、读术语账本、更新术语决定、列纠正记录、书内传播、跨书建议。

典型流程：term_renderings（这个词全书怎么译的）→ update_term_decision（定下译名）
→ propagate_term_correction（预览哪些段要改）→ 同一工具带 confirm=true（确认应用）。
"""

from __future__ import annotations

import asyncio

from ...domain.memory.corrections import preferred_rendering
from ..use_cases.term_ledger_store import load_ledger
from .base import ToolContext, shared_tool


async def _task(ctx: ToolContext, task_id: str):
    task = await ctx.service.get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    return task


@shared_tool("term_occurrences")
async def term_occurrences(
    ctx: ToolContext, task_id: str, term: str, case_sensitive: bool = False, skip: int = 0, limit: int = 20
) -> dict:
    """查一个词（或短语）在全书原文里的全部出现处，每处附所在段的原文、译文和段号。

    词形宽松匹配（复数、所有格、-ed/-ing；空白与连字符互换）；total_matches 是全书总命中数，不受分页影响。

    Args:
        task_id: 结构化翻译的任务 ID。
        term: 英文词或短语。
        case_sensitive: 是否区分大小写。
        skip: 分页偏移。
        limit: 每页条数（1~100）。
    """
    return await ctx.service.term_occurrences(task_id, term, case_sensitive, True, skip, max(1, min(limit, 100)))


@shared_tool("term_renderings")
async def term_renderings(ctx: ToolContext, task_id: str, term: str, case_sensitive: bool = False) -> dict:
    """查一个词在全书的各种译法及每种译法的次数和证据段（不调用模型，按术语表、括注和译文统计）。

    Args:
        task_id: 结构化翻译的任务 ID。
        term: 英文词或短语。
        case_sensitive: 是否区分大小写。
    """
    return await ctx.service.term_renderings(task_id, term, case_sensitive)


@shared_tool("read_term_ledger")
async def read_term_ledger(ctx: ToolContext, task_id: str, term: str | None = None) -> dict:
    """读取这本书当前生效的术语：任务术语表、术语账本与人工决定合并后的结果，每条标明来源。

    Args:
        task_id: 任务 ID。
        term: 只看某个术语（不区分大小写）；不传返回全部。
    """
    task = await _task(ctx, task_id)
    ledger = await load_ledger(ctx.glossary_service, task.filename)
    human = await asyncio.to_thread(ctx.service.memory.human_overrides, task_id)
    sources = {}
    for name, values in (("glossary", task.glossary_snapshot or {}), ("ledger", ledger), ("human", human)):
        for key, value in values.items():
            existing = next((k for k in sources if k.casefold() == key.casefold()), key)
            sources[existing] = {"value": value, "rendering": preferred_rendering(value), "source": name}
    items = [{"term": key, **value} for key, value in sorted(sources.items(), key=lambda kv: kv[0].casefold())]
    if term:
        items = [item for item in items if item["term"].casefold() == term.strip().casefold()]
    return {"task_id": task_id, "total": len(items), "terms": items}


@shared_tool("update_term_decision", writes=True)
async def update_term_decision(
    ctx: ToolContext,
    task_id: str,
    term: str,
    rendering: str,
    scope: str | None = None,
    confirm: bool = False,
) -> dict:
    """定下一个术语在本书的译名（人工决定，覆盖术语表与账本）。不带 confirm=true 时只预览。

    只改术语决定，不改已有译文；已有译文里的旧译名请接着用 propagate_term_correction 预览并应用。
    scope 决定这条决定能否作为建议给同作者（author）或同领域（domain）的其他书，默认按任务填写的作者/领域。

    Args:
        task_id: 任务 ID。
        term: 英文术语。
        rendering: 新译名（只写译名，如"数字图"）。
        scope: book / author / domain。
        confirm: true 才真正写入。
    """
    task = await _task(ctx, task_id)
    if not term.strip() or not rendering.strip():
        raise ValueError("术语和译名都不能为空")
    current = (await read_term_ledger(ctx, task_id, term))["terms"]
    if not confirm:
        return {
            "preview": True,
            "term": term.strip(),
            "current": current[0] if current else None,
            "new_rendering": rendering.strip(),
            "scope": scope or ("author" if task.author else "domain" if task.domain else "book"),
            "hint": "确认后带 confirm=true 再调用一次；已有译文不会被改动",
        }
    entry = await asyncio.to_thread(ctx.service.memory.decide_term, task, term, rendering, scope)
    return {"preview": False, "term": entry.term, "rendering": entry.rendering, "scope": entry.scope}


@shared_tool("propagate_term_correction", writes=True)
async def propagate_term_correction(
    ctx: ToolContext,
    task_id: str,
    term: str,
    old: str,
    new: str,
    positions: list[int] | None = None,
    scope: str | None = None,
    revision: str | None = None,
    confirm: bool = False,
) -> dict:
    """把一个术语的旧译名改成新译名，传播到全书。不带 confirm=true 时只预览将改哪些段、改后什么样。

    只改原文含该术语、译文仍用旧译名的段；更长译名里的旧译名（如"超虚构"里的"虚构"）不动。
    确认后改动的块会按新译名重新质检，并记一条传播纠正。

    Args:
        task_id: 任务 ID。
        term: 英文术语。
        old: 旧译名。
        new: 新译名。
        positions: 只改预览里的这些段（全书顺序号）；不传则改预览中的全部段。
        scope: 记入术语决定的适用范围 book / author / domain。
        revision: 预览返回的 revision；不传则用当前版本。
        confirm: true 才真正应用。
    """
    if not confirm:
        preview = await ctx.service.preview_term_correction(task_id, term, old, new)
        return {"preview": True, **preview, "hint": "确认后带 confirm=true 再调用一次"}
    task = await _task(ctx, task_id)
    applied = await ctx.service.apply_term_correction(
        task_id, term, old, new, positions, revision or task.artifact_dir, scope
    )
    return {"preview": False, **applied}


@shared_tool("list_corrections")
async def list_corrections(ctx: ToolContext, task_id: str, limit: int = 100) -> dict:
    """列出这本书的纠正记录（人工编辑、术语决定、书内传播），最新的在前。

    Args:
        task_id: 任务 ID。
        limit: 最多返回条数（1~500）。
    """
    return await ctx.service.task_corrections(task_id, max(1, min(limit, 500)))


@shared_tool("term_suggestions")
async def term_suggestions(ctx: ToolContext, task_id: str) -> dict:
    """按任务的作者或领域，取其他书已定的术语译名作为建议（每条标明来自哪本书、什么来源）。

    建议只供参考，不是硬约束。任务没填作者和领域时返回空。

    Args:
        task_id: 任务 ID。
    """
    return await ctx.service.term_suggestions(task_id)
