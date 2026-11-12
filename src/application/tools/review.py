"""审校工具：列质检问题、读段、读块、编辑块、重译指定块、恢复任务。

写入类工具（edit_chunk / retranslate_chunks / resume_task）不带 confirm 时只返回预览。
"""

from __future__ import annotations

from ...core.validator import TranslationValidator
from ...domain.models.task_models import TaskStatus
from .base import ToolContext, shared_tool

_FINISHED = {TaskStatus.COMPLETED, TaskStatus.PARTIAL_SUCCESS, TaskStatus.FAILED}
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


async def _task(ctx: ToolContext, task_id: str):
    task = await ctx.service.get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    return task


async def _records(ctx: ToolContext, task_id: str) -> tuple[object, list[dict]]:
    task = await _task(ctx, task_id)
    return task, ctx.service.files.records(task)


def _record(records: list[dict], chunk_index: int) -> dict:
    record = next((item for item in records if item["chunk_index"] == chunk_index), None)
    if record is None:
        raise ValueError(f"块不存在: {chunk_index}")
    return record


@shared_tool("list_quality_issues")
async def list_quality_issues(
    ctx: ToolContext,
    task_id: str,
    severity: str | None = None,
    kind: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> dict:
    """列出一个任务的质检问题（含翻译失败的块），按严重度排序，可按 severity / kind 过滤。

    每条带 chunk_index 与 segment（对照阅读器段号，有则可跳转），可接着用 read_segment / read_chunk 查看。

    Args:
        task_id: 任务 ID。
        severity: 只看某个严重度：high / medium / low。
        kind: 只看某类问题，如 terminology、structure、semantic、untranslated。
        skip: 分页偏移。
        limit: 每页条数（1~200）。
    """
    _, records = await _records(ctx, task_id)
    issues = []
    for record in records:
        if not record.get("success"):
            issues.append(
                {"chunk_index": record["chunk_index"], "kind": "failed", "severity": "high", "message": "该块翻译失败"}
            )
        for issue in (record.get("quality_report") or {}).get("issues", []):
            issues.append({"chunk_index": record["chunk_index"], **issue})
    if severity:
        issues = [item for item in issues if item.get("severity") == severity]
    if kind:
        issues = [item for item in issues if item.get("kind") == kind]
    issues.sort(key=lambda item: (_SEVERITY_ORDER.get(item.get("severity"), 3), item["chunk_index"]))
    limit = max(1, min(limit, 200))
    return {"total": len(issues), "issues": issues[skip : skip + limit]}


@shared_tool("read_chunk")
async def read_chunk(ctx: ToolContext, task_id: str, chunk_index: int) -> dict:
    """读取一块的原文、译文、质检报告与当前结果版本（revision，编辑时用于防止覆盖别人的修改）。

    Args:
        task_id: 任务 ID。
        chunk_index: 块索引（list_quality_issues 返回的 chunk_index）。
    """
    task, records = await _records(ctx, task_id)
    record = _record(records, chunk_index)
    return {
        "chunk_index": chunk_index,
        "original": record.get("original", ""),
        "translation": record.get("translation", ""),
        "success": record.get("success", False),
        "quality_report": record.get("quality_report") or {},
        "revision": task.artifact_dir,
    }


@shared_tool("read_segment")
async def read_segment(ctx: ToolContext, task_id: str, segment: int) -> dict:
    """读取一段的原文、译文和该段的问题（需要全书索引，即结构化翻译的任务）。

    Args:
        task_id: 任务 ID。
        segment: 对照阅读器段号（问题列表里的 segment 字段）。
    """
    task, records = await _records(ctx, task_id)
    index = ctx.service.book_index.load(task_id)
    if index is None:
        raise ValueError("该任务没有全书索引（只有结构化翻译的任务才有段级数据）")
    found = index.by_seg_id(segment)
    if found is None:
        raise ValueError(f"段不存在: {segment}")
    record = next((item for item in records if item["chunk_index"] == found.chunk_index), {})
    issues = [
        issue for issue in (record.get("quality_report") or {}).get("issues", []) if issue.get("segment") == segment
    ]
    return {
        "segment": segment,
        "position": found.position,
        "chunk_index": found.chunk_index,
        "section": " > ".join(found.section_path),
        "source": found.source,
        "translation": found.translation,
        "status": found.status,
        "issues": issues,
        "revision": task.artifact_dir,
    }


@shared_tool("edit_chunk", writes=True)
async def edit_chunk(
    ctx: ToolContext,
    task_id: str,
    chunk_index: int,
    translation: str,
    revision: str | None = None,
    confirm: bool = False,
) -> dict:
    """人工修改一块译文。不带 confirm=true 时只预览：返回新旧译文和新译文的质检结果，不保存。

    确认后保存并重新发布结果，记一条纠正记录；若识别出某个术语译名被改，返回 term_changes，
    可接着用 propagate_term_correction 传播到全书。

    Args:
        task_id: 任务 ID。
        chunk_index: 块索引。
        translation: 新的完整块译文（Markdown）。
        revision: read_chunk 返回的 revision；不传则用当前版本。
        confirm: true 才真正保存。
    """
    task, records = await _records(ctx, task_id)
    if task.status not in _FINISHED:
        raise ValueError("任务执行结束后才能审校")
    if not translation.strip():
        raise ValueError("译文不能为空")
    record = _record(records, chunk_index)
    revision = revision or task.artifact_dir
    if not confirm:
        glossary = await ctx.service._review_glossary(task)
        report = TranslationValidator().validate(record["original"], translation, glossary).to_dict()
        return {
            "preview": True,
            "chunk_index": chunk_index,
            "before": record.get("translation", ""),
            "after": translation,
            "quality_report": report,
            "revision": revision,
            "hint": "确认无误后带 confirm=true 再调用一次",
        }
    result = await ctx.service.edit_chunk(task_id, chunk_index, translation, revision)
    _, records = await _records(ctx, task_id)  # result["chunks"] 只是第一页，按索引重新取
    saved = _record(records, chunk_index)
    return {
        "preview": False,
        "chunk_index": chunk_index,
        "quality_report": saved.get("quality_report") or {},
        "term_changes": result.get("term_changes", []),
        "revision": result["revision"],
    }


@shared_tool("retranslate_chunks", writes=True)
async def retranslate_chunks(ctx: ToolContext, task_id: str, chunk_indices: list[int], confirm: bool = False) -> dict:
    """把指定的块重新排队翻译。不带 confirm=true 时只预览将重译哪些块及其当前状态。

    确认后任务回到待处理状态，由后台 worker 重译这些块，其余块沿用现有译文。

    Args:
        task_id: 任务 ID。
        chunk_indices: 要重译的块索引列表。
        confirm: true 才真正排队。
    """
    task, records = await _records(ctx, task_id)
    if not chunk_indices:
        raise ValueError("至少指定一个块")
    selected = [_record(records, index) for index in chunk_indices]
    if not confirm:
        return {
            "preview": True,
            "status": task.status.value,
            "chunks": [
                {
                    "chunk_index": item["chunk_index"],
                    "success": item.get("success", False),
                    "passed": (item.get("quality_report") or {}).get("passed"),
                }
                for item in selected
            ],
            "hint": "确认后带 confirm=true 再调用一次；重译会覆盖这些块的现有译文（含人工修改）",
        }
    updated = await ctx.service.retry_task(task_id, list(chunk_indices))
    return {"preview": False, "status": updated.status.value, "queued": list(chunk_indices)}


@shared_tool("resume_task", writes=True)
async def resume_task(ctx: ToolContext, task_id: str, confirm: bool = False) -> dict:
    """恢复一个已结束但有失败块的任务：只重译失败的块。不带 confirm=true 时只预览会重译哪些块。

    Args:
        task_id: 任务 ID。
        confirm: true 才真正排队。
    """
    task, records = await _records(ctx, task_id)
    if task.status in {TaskStatus.PENDING, TaskStatus.PROCESSING}:
        return {"preview": True, "status": task.status.value, "failed": [], "hint": "任务仍在执行，无需恢复"}
    failed = [item["chunk_index"] for item in records if not item.get("success")]
    if not confirm or not failed:
        hint = "确认后带 confirm=true 再调用一次" if failed else "没有失败的块"
        return {"preview": True, "status": task.status.value, "failed": failed, "hint": hint}
    updated = await ctx.service.retry_task(task_id, failed)
    return {"preview": False, "status": updated.status.value, "queued": failed}
