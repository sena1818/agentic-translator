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
    author: str | None = Form(None, max_length=200),
    domain: str | None = Form(None, max_length=200),
):
    """
    创建翻译任务

    请求:
    - file: 上传的文件 (PDF/EPUB/Markdown)
    - glossary_id: 术语表 ID (可选)
    - bilingual: 是否启用双语对照模式 (默认 False)
    - author / domain: 书的作者与领域 (可选)；同作者、同领域的其他书的术语会作为参考译名提示

    返回:
    {
        "task_id": "uuid-string",
        "status": "pending",
        "filename": "book.md",
        "created_at": "2026-01-16T10:30:00"
    }
    """
    content = await file.read(50 * 1024 * 1024 + 1)
    task = await service.create_task(content, file.filename, glossary_id, bilingual, author, domain)

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
    """保存人工编辑：记一条纠正；改掉的是术语译名时在 term_changes 里给出，供前端提示全书传播。"""
    return await service.edit_chunk(task_id, index, payload.translation, payload.revision)


class ReviewRequest(BaseModel):
    revision: str | None = None
    reviewed: bool = True


@router.post("/tasks/{task_id}/chunks/{index}/review")
async def mark_reviewed(task_id: str, index: int, payload: ReviewRequest):
    """标记该块已审校（与"人工编辑过"是两种状态）。"""
    return await service.mark_reviewed(task_id, index, payload.revision, payload.reviewed)


class TermCorrectionRequest(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    old: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=1, max_length=200)


class ApplyTermCorrectionRequest(TermCorrectionRequest):
    positions: list[int] | None = None
    revision: str | None = None
    scope: str | None = Field(None, pattern="^(book|author|domain)$")


@router.post("/tasks/{task_id}/corrections/preview")
async def preview_term_correction(task_id: str, payload: TermCorrectionRequest):
    """书内传播预览：原文含该术语、译文仍用旧译名的段，以及替换后的样子。"""
    return await service.preview_term_correction(task_id, payload.term, payload.old, payload.new)


@router.post("/tasks/{task_id}/corrections/apply")
async def apply_term_correction(task_id: str, payload: ApplyTermCorrectionRequest):
    """把预览里选中的段（不传 positions 即全部）批量改为新译名，改动的块重新质检后发布。"""
    return await service.apply_term_correction(
        task_id, payload.term, payload.old, payload.new, payload.positions, payload.revision, payload.scope
    )


@router.get("/tasks/{task_id}/corrections")
async def list_corrections(task_id: str, limit: int = Query(100, ge=1, le=500)):
    """本书的纠正记录（编辑与传播），新的在前。"""
    return await service.task_corrections(task_id, limit)


class ReviewAgentRequest(BaseModel):
    revision: str | None = None


@router.get("/tasks/{task_id}/review-agent")
async def review_agent_status(task_id: str):
    """审校 agent 是否启用，以及最近一次分诊的结果与成本。"""
    return await service.review_agent_status(task_id)


@router.post("/tasks/{task_id}/review-agent")
async def run_review_agent(task_id: str, payload: ReviewAgentRequest):
    """对质检后仍被标记的问题分诊：能修的修（须通过逐类型不退步检查）、误报的驳回、拿不准的转人工。"""
    return await service.run_review_agent(task_id, payload.revision)


@router.get("/tasks/{task_id}/term-suggestions")
async def term_suggestions(task_id: str):
    """同作者、同领域其他书的术语建议，每条标明来源书与决定来源。"""
    return await service.term_suggestions(task_id)


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


@router.get("/tasks/{task_id}/index/semantic")
async def search_meaning(
    task_id: str,
    q: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(10, ge=1, le=50),
):
    """按意思检索：中英文查询，段级关键词 + 向量混合排序；未配置向量模型时为仅关键词并附提示。"""
    return await service.search_meaning(task_id, q, limit)


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

