"""共享工具注册表（#23）：内部 agent 与 MCP 走同一段实现；写入类工具不确认只预览。

沿用临时数据库 + 假引擎：结构化翻译用假翻译员（RunnableLambda）跑一遍真流程，再通过 MCP 的工具调用路径
（FastMCP 按签名校验参数）和内部 ToolRegistry 分别调用。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import types
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from src.application.tools import ToolContext, build_tool_suite, shared_tool
from src.infrastructure.document import pandoc

needs_pandoc = pytest.mark.skipif(not pandoc.pandoc_available(), reason="需要 pandoc")

DOC = """# The Numogram

The Numogram maps the decimal numbers onto zones.

Hyperstition is a *fiction*. The Numogram is not a fiction.

Oil moves through politics.
"""

TRANSLATIONS = {
    "The Numogram": "数图",
    "The Numogram maps the decimal numbers onto zones.": "数图把十进制数映射到区域上。",
    "Hyperstition is a <i1>fiction</i1>. The Numogram is not a fiction.": "超虚构是一种<i1>虚构</i1>。数图不是虚构。",
    "Oil moves through politics.": "石油穿行于政治，而非数图。",
}
_SEG_RE = re.compile(r'<seg id="(\d+)">(.*?)</seg>', re.S)


def _translate(prompt_value):
    body = prompt_value.to_string().split("【待翻译段落】:")[-1]
    return "\n".join(f'<seg id="{i}">{TRANSLATIONS.get(src, src)}</seg>' for i, src in _SEG_RE.findall(body))


class _Factory:
    def create_translator(self):
        return RunnableLambda(_translate)

    def create_checker(self):
        return RunnableLambda(lambda prompt: "")

    def create_analyst(self):
        return RunnableLambda(lambda prompt: '{"summary": "测试"}')


async def _translated_book(tmp_path, monkeypatch):
    """跑一本小书的结构化翻译，返回 (MCP server, service, task_id)。"""
    from src.application.use_cases import translation_tasks
    from src.core.translator import TranslationEngine
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.infrastructure.filesystem.task_files import TaskFiles
    from src.infrastructure.persistence.database import Database
    from src.infrastructure.persistence.glossary_repository import GlossaryService
    from src.infrastructure.persistence.task_repository import TaskRepository
    from src.interfaces.mcp.server import TranslatorMCPServer
    from src.utils.config_loader import get_config

    config = get_config(Path("config/config.yaml"))

    def engine_factory(glossary=None, config=None):
        return TranslationEngine(
            glossary=glossary,
            config=config,
            model_factory=_Factory(),
            cache=TranslationCache(tmp_path / "cache.db"),
        )

    monkeypatch.setattr(translation_tasks, "TranslationEngine", engine_factory)
    glossaries = GlossaryService()
    glossaries.glossary_dir = tmp_path / "glossaries"
    glossaries.glossary_dir.mkdir()
    (glossaries.glossary_dir / "ccru.json").write_text(
        json.dumps({"Numogram": "数图 (Numogram)", "Hyperstition": "超虚构 (Hyperstition)"}), encoding="utf-8"
    )
    db = Database(str(tmp_path / "tasks.db"))
    await db.initialize()
    service = translation_tasks.TranslationService(
        repository=TaskRepository(db), files=TaskFiles(tmp_path / "data"), config=config, glossary_service=glossaries
    )
    task = await service.create_task(DOC.encode(), "first.md", "ccru", author="Nick Land")
    await service.start_translation(task.task_id)
    server = TranslatorMCPServer(translation_service=service)
    return server, service, task.task_id


async def _mcp(server, name, **args):
    """走 MCP 的工具调用路径：FastMCP 按函数签名校验参数后调用。"""
    return await server.mcp._tool_manager.call_tool(name, args)


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --------------------------------------------------------------------------- #
# 同一段实现
# --------------------------------------------------------------------------- #


def test_mcp_and_internal_registry_share_the_same_function_objects(tmp_path, monkeypatch):
    from src.interfaces.mcp.server import TranslatorMCPServer

    monkeypatch.chdir(tmp_path)
    server = TranslatorMCPServer()
    manager = server.mcp._tool_manager
    assert set(server.registry.names()) == {tool.name for tool in manager.list_tools()}
    for name, tool in server.registry.tools.items():
        assert manager.get_tool(name).fn is tool.function, name
    # 写入类工具在 MCP 里不是只读，只读工具标了 readOnlyHint
    assert manager.get_tool("propagate_term_correction").annotations.readOnlyHint is False
    assert manager.get_tool("term_renderings").annotations.readOnlyHint is True
    # 内部 agent 取子集：仍是同一批函数
    subset = server.tools.subset(["term_renderings", "read_segment"])
    assert subset.tools["term_renderings"].function is server.registry.tools["term_renderings"].function


def test_registry_validates_arguments_from_type_hints(tmp_path, monkeypatch):
    from src.interfaces.mcp.server import TranslatorMCPServer

    monkeypatch.chdir(tmp_path)
    registry = TranslatorMCPServer().registry
    schema = registry.tools["propagate_term_correction"].parameters
    assert schema["required"] == ["task_id", "term", "old", "new"]
    assert schema["properties"]["positions"] == {"type": "array", "items": {"type": "integer"}}
    assert schema["properties"]["confirm"] == {"type": "boolean"}

    result = asyncio.run(registry.call("read_chunk", {"task_id": "x", "chunk_index": "zero"}))
    assert not result.ok and "chunk_index 应为 integer" in result.output
    result = asyncio.run(registry.call("read_chunk", {"task_id": "x"}))
    assert not result.ok and "缺少必填参数 chunk_index" in result.output


def test_new_tool_modules_hook_in_and_write_tools_must_preview():
    calls = []

    @shared_tool("echo_term")
    async def echo_term(ctx: ToolContext, term: str, times: int = 1) -> dict:
        """回显术语。"""
        calls.append(ctx.extras["tag"])
        return {"term": term * times}

    module = types.SimpleNamespace(echo_term=echo_term)
    suite = build_tool_suite(ToolContext(extras={"tag": "agent"}), modules=[module])
    assert suite.registry.names() == ["echo_term"]
    result = asyncio.run(suite.registry.call("echo_term", {"term": "ab", "times": 2}))
    assert result.ok and json.loads(result.output) == {"term": "abab"} and calls == ["agent"]

    with pytest.raises(TypeError, match="confirm"):

        @shared_tool("unsafe_write", writes=True)
        async def unsafe_write(ctx: ToolContext, task_id: str) -> dict:
            return {}


# --------------------------------------------------------------------------- #
# 端到端：查译法 → 更新决定 → 预览传播 → 确认应用（MCP 路径）
# --------------------------------------------------------------------------- #


@needs_pandoc
def test_mcp_client_can_rename_a_term_across_the_book(tmp_path, monkeypatch):
    async def run():
        server, service, task_id = await _translated_book(tmp_path, monkeypatch)

        renderings = await _mcp(server, "term_renderings", task_id=task_id, term="Numogram")
        assert [item["text"] for item in renderings["renderings"]] == ["数图"]

        preview = await _mcp(server, "update_term_decision", task_id=task_id, term="Numogram", rendering="数字图")
        assert preview["preview"] is True and preview["current"]["rendering"] == "数图"
        assert preview["scope"] == "author"
        decided = await _mcp(
            server, "update_term_decision", task_id=task_id, term="Numogram", rendering="数字图", confirm=True
        )
        assert decided == {"preview": False, "term": "Numogram", "rendering": "数字图", "scope": "author"}
        ledger = await _mcp(server, "read_term_ledger", task_id=task_id, term="numogram")
        assert ledger["terms"][0]["rendering"] == "数字图" and ledger["terms"][0]["source"] == "human"

        planned = await _mcp(
            server, "propagate_term_correction", task_id=task_id, term="Numogram", old="数图", new="数字图"
        )
        assert planned["preview"] is True
        assert [item["preview"] for item in planned["items"]] == [
            "数字图",
            "数字图把十进制数映射到区域上。",
            "超虚构是一种虚构。数字图不是虚构。",
        ]
        applied = await _mcp(
            server,
            "propagate_term_correction",
            task_id=task_id,
            term="Numogram",
            old="数图",
            new="数字图",
            revision=planned["revision"],
            confirm=True,
        )
        assert applied["preview"] is False and applied["replacements"] == 3
        assert all(chunk["passed"] for chunk in applied["chunks"])

        after = await _mcp(server, "term_renderings", task_id=task_id, term="Numogram")
        assert [item["text"] for item in after["renderings"]] == ["数字图"]
        chunk = await _mcp(server, "read_chunk", task_id=task_id, chunk_index=0)
        assert "石油穿行于政治，而非数图。" in chunk["translation"]  # 原文没有该术语的段不动
        kinds = [item["kind"] for item in (await _mcp(server, "list_corrections", task_id=task_id))["items"]]
        assert sorted(kinds) == ["propagation", "term"]

    asyncio.run(run())


@needs_pandoc
def test_write_tools_without_confirm_change_no_data(tmp_path, monkeypatch):
    async def run():
        server, service, task_id = await _translated_book(tmp_path, monkeypatch)
        chunk = await _mcp(server, "read_chunk", task_id=task_id, chunk_index=0)
        before = _snapshot(tmp_path)
        task_before = (await service.get_task(task_id)).to_dict()

        edited = await _mcp(
            server,
            "edit_chunk",
            task_id=task_id,
            chunk_index=0,
            translation=chunk["translation"].replace("数图把", "数字图把"),
        )
        assert edited["preview"] is True and "数字图把" in edited["after"]
        assert (await _mcp(server, "retranslate_chunks", task_id=task_id, chunk_indices=[0]))["preview"] is True
        assert (await _mcp(server, "resume_task", task_id=task_id))["preview"] is True
        assert (await _mcp(server, "update_term_decision", task_id=task_id, term="Numogram", rendering="数字图"))[
            "preview"
        ]
        assert (
            await _mcp(server, "propagate_term_correction", task_id=task_id, term="Numogram", old="数图", new="数字图")
        )["preview"]

        assert _snapshot(tmp_path) == before
        assert (await service.get_task(task_id)).to_dict() == task_before

        # 确认后才真正写入：编辑保存并识别出译名纠正
        saved = await _mcp(
            server,
            "edit_chunk",
            task_id=task_id,
            chunk_index=0,
            translation=edited["after"],
            revision=chunk["revision"],
            confirm=True,
        )
        assert saved["preview"] is False and saved["term_changes"][0]["new_rendering"] == "数字图"
        assert _snapshot(tmp_path) != before

        queued = await _mcp(server, "retranslate_chunks", task_id=task_id, chunk_indices=[0], confirm=True)
        assert queued == {"preview": False, "status": "pending", "queued": [0]}

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 只读工具
# --------------------------------------------------------------------------- #


@needs_pandoc
def test_read_tools_report_issues_segments_audit_and_usage(tmp_path, monkeypatch):
    async def run():
        server, service, task_id = await _translated_book(tmp_path, monkeypatch)

        issues = await _mcp(server, "list_quality_issues", task_id=task_id)
        assert issues["total"] == len(issues["issues"])

        found = await _mcp(server, "search_book", task_id=task_id, query="politics")
        segment = found["items"][0]["seg_id"]
        read = await _mcp(server, "read_segment", task_id=task_id, segment=segment)
        assert read["source"] == "Oil moves through politics." and read["translation"].startswith("石油")

        occurrences = await _mcp(server, "term_occurrences", task_id=task_id, term="numogram")
        assert occurrences["total_matches"] == 3

        audit = await _mcp(server, "run_nontext_audit", task_id=task_id)
        assert audit["summary"]["translation_issues"] == 0

        summary = await _mcp(server, "quality_summary", task_id=task_id)
        assert summary["status"] == "completed" and summary["quality_summary"]["total"] == 1
        usage = await _mcp(server, "usage_report", task_id=task_id)
        assert usage["chunks"] == 1 and usage["translation_tokens"] is None
        assert usage["stages"]["typed_repair"]["model_calls"] == 0  # 翻译时记下的阶段用量
        assert usage["stages"]["term_ledger"]["calls"] >= 0

        suggestions = await _mcp(server, "term_suggestions", task_id=task_id)
        assert suggestions["author"] == "Nick Land"
        meaning = await _mcp(server, "search_book_by_meaning", task_id=task_id, query="oil and politics")
        assert meaning["mode"] == "lexical" and meaning["hint"]

        # 同一工具经内部注册表调用，结果与 MCP 路径一致
        result = await server.registry.call("term_occurrences", {"task_id": task_id, "term": "numogram"})
        assert result.ok and json.loads(result.output)["total_matches"] == 3

    asyncio.run(run())


@needs_pandoc
def test_mcp_wire_protocol_serializes_shared_tool_results(tmp_path, monkeypatch):
    async def run():
        server, _, task_id = await _translated_book(tmp_path, monkeypatch)
        content = await server.mcp.call_tool("quality_summary", {"task_id": task_id})
        blocks = content[0] if isinstance(content, tuple) else content
        assert '"status": "completed"' in blocks[0].text

    asyncio.run(run())
