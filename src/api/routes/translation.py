"""
翻译任务 API 路由
"""

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from ..models.task_response import TaskResponse
from ..services.translation_service import TranslationService

router = APIRouter(prefix="/api/translation", tags=["translation"])
service = TranslationService()


@router.post("/tasks", status_code=201, response_model=TaskResponse)
async def create_translation_task(
    file: UploadFile = File(...),
    glossary_id: str | None = Form(None),
    bilingual: bool | None = Form(False),
):
    """
    创建翻译任务

    请求:
    - file: 上传的文件 (PDF/EPUB/Markdown)
    - glossary_id: 术语表 ID (可选)
    - bilingual: 是否启用双语对照模式 (默认 False)

    返回:
    {
        "task_id": "uuid-string",
        "status": "pending",
        "filename": "book.md",
        "created_at": "2026-01-16T10:30:00"
    }
    """
    content = await file.read(50 * 1024 * 1024 + 1)
    task = await service.create_task(content, file.filename, glossary_id, bilingual)

    return task.to_dict()


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task_status(task_id: str):
    """
    查询任务状态 (轮询端点)

    返回:
    {
        "task_id": "...",
        "status": "processing",
        "progress": {
            "current": 50,
            "total": 100,
            "percentage": 50.0,
            "speed": 12.5,
            "elapsed": 180
        },
        "result_url": null,
        "error": null
    }
    """
    task = await service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task.to_dict()


@router.get("/tasks")
async def list_tasks(
    skip: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), search: str = "", status: str | None = None
):
    """
    获取任务列表

    返回:
    {
        "tasks": [...],
        "total": 100
    }
    """
    tasks, total = await service.list_tasks(skip, limit, search=search, status=status)
    return {"tasks": [TaskResponse.model_validate(t.to_dict()).model_dump() for t in tasks], "total": total, "stats": await service.db.task_stats()}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除任务 (不可删除正在运行的任务)"""
    success = await service.delete_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="任务不存在或无法删除")
    return {"message": "删除成功"}


class RetryRequest(BaseModel):
    indices: list[int] | None = None


class EditRequest(BaseModel):
    translation: str = Field(min_length=1, max_length=100000)
    revision: str | None = None


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    result = await service.cancel_task(task_id)
    if result is None:
        raise HTTPException(404, "任务不存在")
    if not result:
        raise HTTPException(409, "任务已结束，无法取消")
    return {"message": "任务已取消"}


@router.post("/tasks/{task_id}/retry", response_model=TaskResponse)
async def retry_task(task_id: str, payload: RetryRequest):
    return (await service.retry_task(task_id, payload.indices)).to_dict()


@router.get("/tasks/{task_id}/chunks")
async def get_chunks(task_id: str, skip: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    return await service.get_chunks(task_id, skip, limit)


@router.put("/tasks/{task_id}/chunks/{index}")
async def edit_chunk(task_id: str, index: int, payload: EditRequest):
    return await service.edit_chunk(task_id, index, payload.translation, payload.revision)


@router.get("/tasks/{task_id}/index/search")
async def search_book(
    task_id: str,
    q: str = Query(..., min_length=1),
    field: str = Query("any", pattern="^(any|source|translation)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    """全书关键词检索：原文、译文或两者里包含全部关键词的段。"""
    return await service.search_book(task_id, q, field, skip, limit)


@router.get("/tasks/{task_id}/index/occurrences")
async def term_occurrences(
    task_id: str,
    term: str = Query(..., min_length=1),
    case_sensitive: bool = False,
    variants: bool = True,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    """某词在全书的全部出现段（含前后文与该段译文），按段分页。"""
    return await service.term_occurrences(task_id, term, case_sensitive, variants, skip, limit)


@router.get("/tasks/{task_id}/index/renderings")
async def term_renderings(task_id: str, term: str = Query(..., min_length=1), case_sensitive: bool = False, variants: bool = True):
    """某词在各段对应的中文译法及次数（启发式统计，附证据段号）。"""
    return await service.term_renderings(task_id, term, case_sensitive, variants)

