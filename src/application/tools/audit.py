"""审计与用量工具：运行全书非文字审计、读质量汇总、读 token 与耗时。全部只读。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .base import ToolContext, shared_tool

_SOURCE_NAME = "source.md"


async def _task(ctx: ToolContext, task_id: str):
    task = await ctx.service.get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    return task


def _audit(service, task) -> dict:
    from ...infrastructure.document.pandoc import pandoc_available
    from ...pipelines.structure.audit import image_checker, run_audit
    from ...pipelines.structure.exporter import StructuredExporter

    directory = service.files.artifacts(task)
    source = directory / _SOURCE_NAME
    if not source.exists() or not pandoc_available():
        raise ValueError("该任务没有结构化结果（或未安装 pandoc），无法做非文字审计")
    config = service._task_config(task)
    document = StructuredExporter.from_config(config).assemble(
        source.read_text(encoding="utf-8"), service.files.records(task)
    )
    if document is None:
        raise ValueError("块划分与记录不一致，无法审计（源文或分块参数已变化）")
    roots = [directory / "assets" / task.task_id, directory / "assets", Path(directory)]
    report = run_audit(document, image_checker(roots))
    return {"summary": report.summary, "findings": [finding.as_dict() for finding in report.findings]}


def _stage_usage(service, task) -> dict:
    """翻译时写下的阶段用量；人工审校会把结果发布到新目录，此时回到最近一次翻译运行的目录里找。"""
    name = f"{task.task_id}.usage.json"
    current = service.files.artifacts(task) / name
    candidates = [current] if current.exists() else []
    runs = service.files.root / "results" / "runs" / task.task_id
    if not candidates and runs.exists():
        candidates = sorted(runs.glob(f"*/{name}"), key=lambda path: path.stat().st_mtime, reverse=True)
    return json.loads(candidates[0].read_text(encoding="utf-8")) if candidates else {}


@shared_tool("run_nontext_audit")
async def run_nontext_audit(
    ctx: ToolContext, task_id: str, origin: str | None = None, skip: int = 0, limit: int = 50
) -> dict:
    """对当前译文重新运行全书非文字审计（图片、公式、代码、表格、脚注、链接、编号引用、图注），不调用模型、不写文件。

    每条发现带 origin：translation（翻译引入）、source（原文本身的问题）、notice（仅提示，如图片可能含英文）。

    Args:
        task_id: 结构化翻译的任务 ID。
        origin: 只看某一类来源：translation / source / notice。
        skip: 分页偏移。
        limit: 每页条数（1~200）。
    """
    task = await _task(ctx, task_id)
    report = await asyncio.to_thread(_audit, ctx.service, task)
    findings = report.get("findings", [])
    if origin:
        findings = [item for item in findings if item.get("origin") == origin]
    limit = max(1, min(limit, 200))
    return {"summary": report.get("summary", {}), "total": len(findings), "findings": findings[skip : skip + limit]}


@shared_tool("quality_summary")
async def quality_summary(ctx: ToolContext, task_id: str) -> dict:
    """读取任务的质量汇总：块数、失败、待复核、修复过、缓存命中，结构化导出的段数、缺图、非文字审计计数等。

    Args:
        task_id: 任务 ID。
    """
    task = await _task(ctx, task_id)
    return {
        "task_id": task_id,
        "status": task.status.value,
        "error": task.error,
        "quality_summary": dict(task.quality_summary or {}),
    }


@shared_tool("usage_report")
async def usage_report(ctx: ToolContext, task_id: str) -> dict:
    """读取任务的耗时与用量：各块翻译耗时与重试、整体进度耗时，以及各可选阶段单独记录的 token 与调用次数。

    stages 来自翻译时记下的阶段用量（术语账本、语义质检、分类型修复）；翻译调用本身的 token 未按任务记录，
    需要时请接 Langfuse 查看（见 docs/observability.md）。

    Args:
        task_id: 任务 ID。
    """
    task = await _task(ctx, task_id)
    records = ctx.service.files.records(task)
    durations = [float(item.get("duration") or 0) for item in records]
    return {
        "task_id": task_id,
        "chunks": len(records),
        "cached": sum(bool(item.get("cached")) for item in records),
        "retries": sum(int(item.get("retry_count") or 0) for item in records),
        "chunk_seconds_total": round(sum(durations), 2),
        "chunk_seconds_max": round(max(durations), 2) if durations else 0.0,
        "elapsed_seconds": round(task.progress.elapsed, 2) if task.progress else None,
        "stages": _stage_usage(ctx.service, task),
        "translation_tokens": None,
    }
