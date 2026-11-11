"""
文件服务 API 路由
"""

import asyncio
import json
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
from src.pipelines.postprocess.export_service import ExportService

from ...core.output_manager import OutputManager
from ...domain.models.task_models import TaskStatus
from ...domain.models.translation_models import TranslationResult
from ...infrastructure.filesystem.task_files import TaskFiles, safe_id
from ..database.db import Database

router = APIRouter(prefix="/api/files", tags=["files"])
db = Database()


def _download_basename(filename: str) -> str:
    """生成稳定、可读的下载文件名前缀"""
    stem = Path(filename).stem.strip()
    return stem or "translation"


def _looks_bilingual_markdown(path: Path) -> bool:
    """粗略判断旧结果文件是否仍是双语 Markdown。"""
    if not path.exists():
        return False
    sample = path.read_text(encoding="utf-8")[:4000]
    return "（双语对照）" in sample or "\n> " in sample and "\n---" in sample


def _ensure_markdown_variants(task_id: str, bilingual_task: bool, result_dir=Path("data/results")) -> tuple[Path, Path]:
    """
    确保单语/双语 Markdown 结果文件存在。
    对历史任务执行一次就地迁移：
    - 旧的 {task_id}.md 若是双语文件，会复制为 .bilingual.md
    - 再从双语内容提取出真正的单语 .md
    """
    mono_path = result_dir / f"{task_id}.md"
    bilingual_path = result_dir / f"{task_id}.bilingual.md"

    records_path = result_dir / f"{task_id}.json"
    if records_path.exists():
        # 现代结果已有明确的块记录，不能用引用/分隔线猜测文件类型。
        if not mono_path.exists() or (bilingual_task and not bilingual_path.exists()):
            records = [TranslationResult(**item) for item in json.loads(records_path.read_text(encoding="utf-8"))]
            if not mono_path.exists():
                OutputManager.write_document(mono_path, task_id, records)
            if bilingual_task and not bilingual_path.exists():
                OutputManager.write_document(bilingual_path, task_id, records, True)
        return mono_path, bilingual_path

    if bilingual_task:
        if bilingual_path.exists():
            source_bilingual = bilingual_path
        elif mono_path.exists() and _looks_bilingual_markdown(mono_path):
            legacy_content = mono_path.read_text(encoding="utf-8")
            bilingual_path.write_text(legacy_content, encoding="utf-8")
            mono_path.write_text(
                ExportService.strip_bilingual_markdown(legacy_content),
                encoding="utf-8",
            )
            source_bilingual = bilingual_path
        else:
            source_bilingual = bilingual_path

        if source_bilingual.exists():
            if (not mono_path.exists()) or _looks_bilingual_markdown(mono_path):
                mono_path.write_text(
                    ExportService.strip_bilingual_markdown(source_bilingual.read_text(encoding="utf-8")),
                    encoding="utf-8",
                )

    elif not bilingual_task and not mono_path.exists() and bilingual_path.exists():
        mono_path.write_text(
            ExportService.strip_bilingual_markdown(bilingual_path.read_text(encoding="utf-8")),
            encoding="utf-8",
        )

    return mono_path, bilingual_path


@router.get("/results/{task_id}")
async def download_result(
    task_id: str,
    format: str = Query("md", description="导出格式: md、html、epub 或 zip"),
    variant: str = Query("mono", description="结果类型: mono 或 bilingual"),
    inline: bool = Query(False, description="HTML 在浏览器内打开（对照阅读器）而非下载"),
):
    """
    下载翻译结果

    参数:
    - format: 导出格式 (md/html/zip)
    - variant: 结果类型 (mono/bilingual)

    返回: Markdown、HTML 或 ZIP 文件
    """
    if format not in {"md", "html", "epub", "zip"}:
        raise HTTPException(status_code=400, detail="不支持的导出格式")
    if variant not in {"mono", "bilingual"}:
        raise HTTPException(status_code=400, detail="不支持的结果类型")

    task = await db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    result_dir = TaskFiles().artifacts(task)
    if task and task.status not in {TaskStatus.COMPLETED, TaskStatus.PARTIAL_SUCCESS}:
        raise HTTPException(status_code=400, detail="任务尚未完成，无法下载结果")
    # Document IR 导出的对照阅读器总是同时包含原文与译文，不依赖双语开关
    reader_path = result_dir / f"{task_id}.html"
    if format == "html" and (result_dir / f"{task_id}.structure.json").exists() and reader_path.exists():
        return _reader_response(reader_path, _download_basename(task.filename), inline)
    if format == "epub":
        return _epub_response(result_dir, task_id, variant, _download_basename(task.filename))
    if format == "html" and not task.bilingual:
        raise HTTPException(400, "仅双语任务支持 HTML 双栏导出")
    if task and variant == "bilingual" and not task.bilingual:
        raise HTTPException(status_code=400, detail="仅双语任务支持双语结果下载")

    base_name = _download_basename(task.filename) if task else f"translation_{task_id}"

    mono_path, bilingual_path = _ensure_markdown_variants(
        task_id=task_id,
        bilingual_task=bool(task.bilingual),
        result_dir=result_dir,
    )

    result_path = bilingual_path if variant == "bilingual" or format == "html" else mono_path
    if not result_path.exists():
        if variant == "bilingual" or format == "html":
            legacy_path = result_dir / f"{task_id}.md"
            if legacy_path.exists():
                result_path = legacy_path
            else:
                raise HTTPException(status_code=404, detail="双语结果文件不存在")
        else:
            raise HTTPException(status_code=404, detail="结果文件不存在")

    if format == "html":
        if task and not task.bilingual:
            raise HTTPException(status_code=400, detail="仅双语任务支持 HTML 双栏导出")

        # 导出为 HTML 双栏格式
        html_path = result_dir / f"{task_id}.html"

        # 如果 HTML 不存在或 Markdown 更新了,重新生成
        if not html_path.exists() or html_path.stat().st_mtime < result_path.stat().st_mtime:
            await asyncio.to_thread(
                ExportService.export_bilingual_html, str(result_path), str(html_path), title=f"翻译结果 - {task_id[:8]}"
            )

        return FileResponse(path=html_path, filename=f"{base_name}.bilingual.html", media_type="text/html")

    if format == "zip":
        bundle_path = await asyncio.to_thread(
            ExportService.create_assets_bundle, task_id=task_id, results_dir=result_dir
        )
        return FileResponse(
            path=bundle_path,
            filename=f"{base_name}.assets.zip",
            media_type="application/zip",
        )

    # 默认返回 Markdown
    return FileResponse(
        path=result_path,
        filename=f"{base_name}{'.bilingual' if variant == 'bilingual' else '.cn'}.md",
        media_type="text/markdown",
    )


def _reader_response(path: Path, base_name: str, inline: bool) -> FileResponse:
    """对照阅读器内含脚本：以沙箱 CSP 提供，脚本运行在不透明源中，无法访问本站 Cookie 与存储。"""
    headers = {
        "Content-Security-Policy": (
            "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; default-src 'none'; "
            "img-src data: https: http:; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
        ),
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(
        path=path,
        filename=f"{base_name}.reader.html",
        media_type="text/html",
        headers=headers,
        content_disposition_type="inline" if inline else "attachment",
    )


def _epub_response(result_dir: Path, task_id: str, variant: str, base_name: str) -> FileResponse:
    """结构化导出时生成的 EPUB；历史任务没有，重新执行后才有。"""
    suffix = ".bilingual.epub" if variant == "bilingual" else ".epub"
    path = result_dir / f"{task_id}{suffix}"
    if not path.exists():
        raise HTTPException(404, "该任务没有 EPUB 结果，重新执行后生成")
    return FileResponse(
        path=path,
        filename=f"{base_name}{'.bilingual' if variant == 'bilingual' else '.cn'}.epub",
        media_type="application/epub+zip",
    )


@router.get("/results/{task_id}/structure")
async def result_structure(task_id: str):
    """结构化导出统计：段数、失效内链、缺失图片清单、已生成的 EPUB 变体。历史任务返回 404。"""
    task = await db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    path = TaskFiles().artifacts(task) / f"{task_id}.structure.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="该任务没有结构化导出")
    stats = json.loads(path.read_text(encoding="utf-8"))
    return {
        "task_id": task_id,
        "segments": stats.get("segments", 0),
        "dead_links": stats.get("dead_links", []),
        "missing_images": stats.get("missing_images", []),
        "epub": stats.get("epub", []),
    }


@router.get("/results/{task_id}/preview")
async def preview_result(task_id: str, skip: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    """
    双语对照预览（只读 JSON）

    返回原文/译文成对的段落列表，供 Web 界面内双栏渲染，无需下载。
    仅双语且已完成（或部分完成）的任务可用，契约与双语下载约束一致。
    """
    task = await db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status not in {TaskStatus.COMPLETED, TaskStatus.PARTIAL_SUCCESS}:
        raise HTTPException(status_code=400, detail="任务尚未完成，无法预览")
    if not task.bilingual:
        raise HTTPException(status_code=400, detail="仅双语任务支持双语预览")

    result_dir = TaskFiles().artifacts(task)
    _, bilingual_path = _ensure_markdown_variants(task_id=task_id, bilingual_task=True, result_dir=result_dir)
    if not bilingual_path.exists():
        legacy_path = result_dir / f"{task_id}.md"
        if legacy_path.exists():
            bilingual_path = legacy_path
        else:
            raise HTTPException(status_code=404, detail="双语结果文件不存在")

    records = TaskFiles().records(task)
    from src.pipelines.postprocess.export_service import BilingualParagraph

    paragraphs = (
        [BilingualParagraph(item["original"], item["translation"]) for item in records]
        if records
        else ExportService.parse_bilingual_markdown(bilingual_path.read_text(encoding="utf-8"))
    )
    return {
        "task_id": task_id,
        "filename": task.filename,
        "bilingual": True,
        "count": len(paragraphs),
        "paragraphs": [{"source": p.source, "translation": p.translation} for p in paragraphs[skip : skip + limit]],
    }


@router.get("/uploads/{filename}")
async def get_upload_file(filename: str):
    """获取上传的原始文件"""
    if Path(filename).name != filename or "\\" in filename:
        raise HTTPException(400, "文件名无效")
    file_path = Path("data/uploads") / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(path=file_path)


@router.get("/results/{task_id}/assets/{filename}")
async def result_asset(task_id: str, filename: str):
    safe_id(task_id)
    if Path(filename).name != filename or "\\" in filename:
        raise HTTPException(400, "资源文件名无效")
    task = await db.get_task(task_id)
    if task is None or task.status not in {TaskStatus.COMPLETED, TaskStatus.PARTIAL_SUCCESS}:
        raise HTTPException(404, "结果不存在")
    root = (TaskFiles().artifacts(task) / "assets" / task_id).resolve()
    path = (root / filename).resolve()
    if (
        not path.is_relative_to(root)
        or not path.is_file()
        or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
    ):
        raise HTTPException(404, "资源不存在")
    return FileResponse(
        path,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
        },
    )
