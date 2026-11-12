"""MCP 工具处理器测试。

经既有 service 层接缝验证：每个用例都在临时工作目录（隔离 data/ 下所有
相对路径）中注入符合引擎契约的假引擎（FakeEngine），从而不触碰真实 LLM，
只断言工具对外行为——返回契约、任务状态语义、术语表存储互通。
"""
import asyncio
import contextlib
import os
import shutil
import tempfile

import pytest

from src.api.services import translation_service as tsm
from src.api.services.glossary_service import GlossaryService
from src.core.translator import TranslationResult
from src.domain.rules.chunk_planning import TextChunk
from src.interfaces.mcp.server import TranslatorMCPServer


class FakeConverter:
    def convert(self, input_path, output_dir):
        return input_path


class GlossaryEchoEngine:
    """把术语表回显进译文，用于验证术语约束确实流经引擎。"""

    def __init__(self, glossary=None):
        self.glossary = glossary or {}

    def plan_chunks(self, text):
        return [
            TextChunk(index=0, chunk_id="c0", text=text, section_path=[], section_title="", context_text="")
        ]

    async def translate_batch(self, text, output_path, progress_callback=None, bilingual=False, prepared_chunks=None):
        if self.glossary:
            translation = "译文（术语：" + "、".join(self.glossary.values()) + "）"
        else:
            translation = "纯译文"
        if progress_callback:
            await progress_callback({"chunk_index": 0, "status": "completed", "translation": translation})
        return [TranslationResult(0, text, translation, True)]


class TwoChunkSuccessEngine:
    """两块全部成功，落到 COMPLETED 终态。"""

    def __init__(self, glossary=None):
        self.glossary = glossary or {}

    def plan_chunks(self, text):
        return [
            TextChunk(index=0, chunk_id="c0", text="chunk-0", section_path=[], section_title="", context_text=""),
            TextChunk(index=1, chunk_id="c1", text="chunk-1", section_path=[], section_title="", context_text=""),
        ]

    async def translate_batch(self, text, output_path, progress_callback=None, bilingual=False, prepared_chunks=None):
        if progress_callback:
            await progress_callback({"chunk_index": 0, "status": "completed", "translation": "译文 A"})
            await progress_callback({"chunk_index": 1, "status": "completed", "translation": "译文 B"})
        return [
            TranslationResult(0, "chunk-0", "译文 A", True),
            TranslationResult(1, "chunk-1", "译文 B", True),
        ]


@contextlib.contextmanager
def sandbox(engine_cls=TwoChunkSuccessEngine, converter_cls=FakeConverter):
    """隔离运行环境：切到临时工作目录并注入假引擎/转换器。

    所有 data/ 相对路径（任务库、上传、结果、术语表）都落在临时目录，
    用例结束后整体清理，互不干扰。
    """
    orig_cwd = os.getcwd()
    orig_engine = tsm.TranslationEngine
    orig_converter = tsm.DocumentConverter
    tmp = tempfile.mkdtemp()
    os.chdir(tmp)
    tsm.TranslationEngine = engine_cls
    tsm.DocumentConverter = converter_cls
    try:
        yield tmp
    finally:
        tsm.TranslationEngine = orig_engine
        tsm.DocumentConverter = orig_converter
        os.chdir(orig_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


async def _make_server(engine_cls=TwoChunkSuccessEngine):
    service = tsm.TranslationService()
    await service.tasks.initialize()
    return TranslatorMCPServer(translation_service=service), service


def test_tool_discovery_exposes_full_toolset_with_descriptions():
    async def scenario():
        with sandbox():
            server, _ = await _make_server()
            tools = await server.mcp.list_tools()
            names = {t.name for t in tools}
            assert names == {
                "translate_text",
                "submit_document",
                "get_task_status",
                "list_tasks",
                "cancel_task",
                "export_result",
                "list_glossaries",
                "get_glossary",
                "create_glossary",
                "modify_glossary_terms",
                "update_glossary",
                "delete_glossary",
                # 共享工具（src/application/tools）：与内部 agent 同一个注册表
                "list_quality_issues",
                "read_chunk",
                "read_segment",
                "edit_chunk",
                "retranslate_chunks",
                "resume_task",
                "term_occurrences",
                "term_renderings",
                "read_term_ledger",
                "update_term_decision",
                "propagate_term_correction",
                "list_corrections",
                "term_suggestions",
                "run_nontext_audit",
                "quality_summary",
                "usage_report",
                "search_book",
                "search_book_by_meaning",
            }
            # schema 描述清晰：每个工具都有非空描述与输入 schema
            for tool in tools:
                assert tool.description and tool.description.strip()
                assert tool.inputSchema.get("type") == "object"

    asyncio.run(scenario())


def test_translate_text_applies_glossary_constraint():
    async def scenario():
        with sandbox(engine_cls=GlossaryEchoEngine):
            server, _ = await _make_server(GlossaryEchoEngine)
            await server.create_glossary("Philo", {"Hyperstition": "超虚构 (Hyperstition)"})

            with_glossary = await server.translate_text("Hyperstition spreads.", glossary_id="philo")
            assert "超虚构 (Hyperstition)" in with_glossary["translation"]
            assert with_glossary["failed_chunks"] == []
            assert with_glossary["chunk_count"] == 1

            without = await server.translate_text("plain text")
            assert without["translation"] == "纯译文"

    asyncio.run(scenario())


def test_submit_poll_export_full_flow():
    async def scenario():
        with sandbox():
            server, service = await _make_server()

            created = await server.submit_document(filename="doc.md", text="# Title\n\nbody\n")
            task_id = created["task_id"]
            assert created["status"] == "pending"

            pending = await server.get_task_status(task_id)
            assert pending["status"] == "pending"

            # 模拟 worker 处理
            await service.start_translation(task_id)

            done = await server.get_task_status(task_id)
            assert done["status"] == "completed"

            listed = await server.list_tasks()
            assert listed["total"] == 1
            assert listed["tasks"][0]["task_id"] == task_id

            exported = await server.export_result(task_id, "mono")
            assert exported["available"] is True
            assert "译文 A" in exported["content"]
            assert "译文 B" in exported["content"]

    asyncio.run(scenario())


def test_export_bilingual_variant():
    async def scenario():
        with sandbox():
            server, service = await _make_server()
            created = await server.submit_document(filename="doc.md", text="# t\n\nbody\n", bilingual=True)
            task_id = created["task_id"]
            await service.start_translation(task_id)

            bilingual = await server.export_result(task_id, "bilingual")
            assert bilingual["available"] is True
            assert "> chunk-0" in bilingual["content"]

    asyncio.run(scenario())


def test_cancel_pending_task_prevents_worker_claim():
    async def scenario():
        with sandbox():
            server, service = await _make_server()
            created = await server.submit_document(filename="doc.md", text="# t\n\nbody\n")
            task_id = created["task_id"]

            result = await server.cancel_task(task_id)
            assert result["cancelled"] is True
            assert result["status"] == "cancelled"

            # 已取消的任务不会被 worker 认领
            claimed = await service.claim_next_pending_task()
            assert claimed is None

    asyncio.run(scenario())


def test_cancel_processing_task_survives_worker_completion():
    async def scenario():
        with sandbox():
            server, service = await _make_server()
            created = await server.submit_document(filename="doc.md", text="# t\n\nbody\n")
            task_id = created["task_id"]

            # worker 认领 -> processing
            claimed = await service.claim_next_pending_task()
            assert claimed.task_id == task_id
            assert claimed.status.value == "processing"

            result = await server.cancel_task(task_id)
            assert result["cancelled"] is True
            assert result["status"] == "cancelled"

            # worker 继续跑完，也不能把 cancelled 覆盖成 completed
            await service.start_translation(task_id, already_claimed=True)

            status = await server.get_task_status(task_id)
            assert status["status"] == "cancelled"
            assert status["progress"]["current"] == 0

            exported = await server.export_result(task_id, "mono")
            assert "译文 A" not in (exported["content"] or "")

    asyncio.run(scenario())


def test_cancel_terminal_task_is_noop():
    async def scenario():
        with sandbox():
            server, service = await _make_server()
            created = await server.submit_document(filename="doc.md", text="# t\n\nbody\n")
            task_id = created["task_id"]
            await service.start_translation(task_id)  # -> completed

            result = await server.cancel_task(task_id)
            assert result["cancelled"] is False
            assert result["status"] == "completed"

    asyncio.run(scenario())


def test_cancel_missing_task_raises():
    async def scenario():
        with sandbox():
            server, _ = await _make_server()
            with pytest.raises(ValueError):
                await server.cancel_task("does-not-exist")

    asyncio.run(scenario())


def test_get_status_and_export_missing_task_raise():
    async def scenario():
        with sandbox():
            server, _ = await _make_server()
            with pytest.raises(ValueError):
                await server.get_task_status("nope")
            with pytest.raises(ValueError):
                await server.export_result("nope", "mono")

    asyncio.run(scenario())


def test_glossary_crud_shares_storage_with_web_layer():
    async def scenario():
        with sandbox():
            server, _ = await _make_server()

            created = await server.create_glossary("My Terms", {"Hyperstition": "超虚构 (Hyperstition)"})
            gid = created["id"]
            assert gid == "my_terms"
            assert created["term_count"] == 1

            fetched = await server.get_glossary(gid)
            assert "Hyperstition" in fetched["terms"]

            modified = await server.modify_glossary_terms(gid, add={"War Machine": "战争机器 (War Machine)"})
            assert modified["term_count"] == 2

            trimmed = await server.modify_glossary_terms(gid, remove=["Hyperstition"])
            assert trimmed["term_count"] == 1

            listed = await server.list_glossaries()
            assert any(g["id"] == gid for g in listed)

            # 与 Web 层互通：独立的 GlossaryService（同一 data/glossaries）看到同样的数据
            web_service = GlossaryService()
            web_view = await web_service.get_glossary(gid)
            assert "War Machine" in web_view["terms"]

            replaced = await server.update_glossary(gid, {"Xeno": "异 (Xeno)"})
            assert replaced["term_count"] == 1

            deleted = await server.delete_glossary(gid)
            assert deleted["deleted"] is True
            with pytest.raises(ValueError):
                await server.get_glossary(gid)

    asyncio.run(scenario())


def test_submit_document_requires_exactly_one_content_source():
    async def scenario():
        with sandbox():
            server, _ = await _make_server()
            with pytest.raises(ValueError):
                await server.submit_document(filename="doc.md")
            with pytest.raises(ValueError):
                await server.submit_document(filename="doc.md", text="a", content_base64="YQ==")

    asyncio.run(scenario())


def test_submit_document_accepts_base64_bytes():
    async def scenario():
        import base64

        with sandbox():
            server, service = await _make_server()
            payload = base64.b64encode(b"# t\n\nbody\n").decode()
            created = await server.submit_document(filename="doc.md", content_base64=payload)
            task_id = created["task_id"]
            await service.start_translation(task_id)
            status = await server.get_task_status(task_id)
            assert status["status"] == "completed"

    asyncio.run(scenario())


def test_export_rejects_unknown_variant():
    async def scenario():
        with sandbox():
            server, service = await _make_server()
            created = await server.submit_document(filename="doc.md", text="# t\n\nbody\n")
            with pytest.raises(ValueError):
                await server.export_result(created["task_id"], "trilingual")

    asyncio.run(scenario())
