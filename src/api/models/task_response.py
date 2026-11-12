"""公开任务契约；前端类型由这个模型生成。"""

from pydantic import BaseModel

from ...domain.models.task_models import TaskProgress, TaskStatus


class TaskResponse(BaseModel):
    task_id: str
    filename: str
    status: TaskStatus
    glossary_id: str | None
    bilingual: bool
    progress: TaskProgress
    result_url: str | None
    error: str | None
    created_at: str
    updated_at: str
    stage: str
    quality_summary: dict[str, int]
    author: str | None = None
    domain: str | None = None
