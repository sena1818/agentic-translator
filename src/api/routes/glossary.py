"""
术语表 API 路由
"""

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..models.glossary import (
    GlossaryCreateRequest,
    GlossaryModifyRequest,
    GlossaryUpdateRequest,
)
from ..services.glossary_service import GlossaryService

router = APIRouter(prefix="/api/glossary", tags=["glossary"])
service = GlossaryService()


@router.get("/")
async def list_glossaries():
    """
    获取术语表列表

    返回:
    [
        {
            "id": "cpglossary",
            "name": "Cpglossary",
            "term_count": 73,
            "updated_at": "2026-01-15T12:00:00"
        }
    ]
    """
    return await service.list_glossaries()


@router.get("/{glossary_id}")
async def get_glossary(glossary_id: str):
    """
    获取术语表内容

    返回:
    {
        "id": "cpglossary",
        "name": "Cpglossary",
        "terms": {
            "Hyperstition": "超虚构 (Hyperstition)"
        }
    }
    """
    glossary = await service.get_glossary(glossary_id)
    if not glossary:
        raise HTTPException(status_code=404, detail="术语表不存在")
    return glossary


@router.post("/")
async def create_glossary(payload: GlossaryCreateRequest):
    """
    创建新术语表

    请求:
    {
        "name": "我的术语表",
        "terms": {"English": "中文"}
    }
    """
    return await service.create_glossary(payload.name, payload.terms)


@router.put("/{glossary_id}")
async def update_glossary(glossary_id: str, payload: GlossaryUpdateRequest):
    """
    更新术语表内容 (全量更新)

    请求:
    {
        "terms": {"English": "中文"}
    }
    """
    success = await service.update_glossary(glossary_id, payload.terms)
    if not success:
        raise HTTPException(status_code=404, detail="术语表不存在")
    return {"message": "更新成功"}


@router.patch("/{glossary_id}/terms")
async def modify_terms(glossary_id: str, payload: GlossaryModifyRequest):
    """
    增量修改术语 (单个增删)

    请求:
    {
        "add": {"New Term": "新术语"},
        "remove": ["Old Term"]
    }
    """
    result = await service.modify_terms(glossary_id, payload.add, payload.remove)
    if result is None:
        raise HTTPException(status_code=404, detail="术语表不存在")
    return {"message": "修改成功", "term_count": result}


@router.delete("/{glossary_id}")
async def delete_glossary(glossary_id: str):
    """删除术语表"""
    success = await service.delete_glossary(glossary_id)
    if not success:
        raise HTTPException(status_code=404, detail="术语表不存在")
    return {"message": "删除成功"}


@router.post("/import")
async def import_glossary(file: UploadFile = File(...), name: str = Form("")):
    """
    导入 JSON 术语表

    请求: multipart/form-data
    - file: JSON 文件
    - name: 术语表名称
    """
    if not name:
        name = file.filename.replace(".json", "")

    content = await file.read()
    glossary = await service.import_from_file(content, name)
    return glossary


@router.get("/{glossary_id}/export")
async def export_glossary(glossary_id: str):
    """
    导出术语表为 JSON

    返回: JSON 文件下载
    """
    from fastapi.responses import FileResponse

    file_path = await service.export_to_file(glossary_id)
    if not file_path:
        raise HTTPException(status_code=404, detail="术语表不存在")

    return FileResponse(path=file_path, filename=f"{glossary_id}.json", media_type="application/json")


@router.post("/draft")
async def suggest_glossary(file: UploadFile = File(...), glossary_id: str | None = Form(None)):
    """生成可编辑的候选草稿，不自动修改已有术语表。"""
    import asyncio
    import tempfile
    from dataclasses import asdict
    from pathlib import Path

    from ...converters.document_converter import DocumentConverter
    from ...infrastructure.filesystem.task_files import validate_upload
    from ...infrastructure.llm.chat_model_factory import ChatModelFactory
    from ...pipelines.glossary.glossary_extractor import GlossaryExtractor
    from ...pipelines.translate.prompt_builder import TranslationPromptBuilder
    from ...utils.config_loader import get_config

    content = await file.read(50 * 1024 * 1024 + 1)
    name = validate_upload(content, file.filename)
    existing = await service.get_glossary(glossary_id) if glossary_id else None
    if glossary_id and existing is None:
        raise HTTPException(404, "术语表不存在")
    config = get_config()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / name
        await asyncio.to_thread(source.write_bytes, content)
        markdown = await DocumentConverter().convert_async(source, root / "converted")
        text = await asyncio.to_thread(markdown.read_text, encoding="utf-8-sig")
        extractor = GlossaryExtractor(
            ChatModelFactory(config).create_analyst(), config, TranslationPromptBuilder(config)
        )
        candidates = await extractor.extract(text, existing["terms"] if existing else {})
    if not candidates:
        raise HTTPException(502, "未取得候选术语，请检查模型配置或稍后重试")
    return {"candidates": [asdict(item) for item in candidates]}
