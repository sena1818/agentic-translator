"""
编排引擎等价性测试套件

在翻译流水线用例边界注入符合 Runnable 协议的假 LLM，固化 translate_batch 的对外契约：
按序写盘的输出文件、结果列表、失败块占位符、进度事件序列。

同一套契约断言对 native 与 langgraph 两个引擎参数化执行；另有一条直接断言两引擎
逐字段等价。它既是迁移的验收标准，也是未来删除 native 路径的回归依据（ADR-0001）。
"""
from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from src.core.translator import TranslationEngine
from src.domain.models.translation_models import TranslationResult
from src.domain.rules.chunk_planning import TextChunk
from src.infrastructure.cache.translation_cache import TranslationCache
from src.utils.config_loader import get_config

ENGINES = ["native", "langgraph"]

_SOURCE_RE = re.compile(r"【待翻译文本】:\n(.*?)\n\n---\n【翻译要求】", re.S)


def _extract_source(rendered_prompt: str) -> str:
    """从渲染后的翻译 Prompt 中还原待译原文。"""
    match = _SOURCE_RE.search(rendered_prompt)
    return match.group(1).strip() if match else rendered_prompt.strip()


def _fake_translate(prompt_value):
    body = _extract_source(prompt_value.to_string())
    if "BOOM" in body:
        raise RuntimeError("fake translator boom")
    return f"译文::{body}"


def _fake_repair(prompt_value):
    # 只有带术语表的用例触发修复：给出真正修好术语的译文（只有修好目标问题、且不新增问题才会被采用）
    return "阿尔法 修正后的译文"


def _fake_analyst(prompt_value):
    return (
        '{"summary": "测试文档", "style_notes": ["学术"], '
        '"terminology_hints": [], "section_overview": ["Root"]}'
    )


class FakeModelFactory:
    """产出符合 Runnable 协议的假 LLM，替换真实 ChatModelFactory。"""

    def create_translator(self):
        return RunnableLambda(_fake_translate)

    def create_checker(self):
        return RunnableLambda(_fake_repair)

    def create_analyst(self):
        return RunnableLambda(_fake_analyst)


def _sample_chunks() -> list[TextChunk]:
    return [
        TextChunk(0, "chunk-0", "Alpha body one.", ["S1"], "S1", ""),
        TextChunk(1, "chunk-1", "Beta body two.", ["S1"], "S1", "prev-ctx"),
        TextChunk(2, "chunk-2", "Gamma BOOM fails.", ["S2"], "S2", ""),
        TextChunk(3, "chunk-3", "Delta body four.", ["S2"], "S2", "prev-ctx"),
    ]


def _normalize_result(result: TranslationResult) -> tuple:
    """只保留对外契约相关字段，剔除时长等非确定性数据。"""
    return (
        result.chunk_index,
        result.original,
        result.translation,
        result.success,
        result.repaired,
        result.cached,
        result.chunk_id,
        (result.quality_report or {}).get("passed"),
    )


def _normalize_events(events: list[dict]) -> list[dict]:
    """归一化进度事件：split_completed 置顶，其余按块索引排序，剔除时长。"""
    head = [e for e in events if e.get("event") == "split_completed"]
    per_chunk = [e for e in events if e.get("event") != "split_completed"]
    per_chunk.sort(key=lambda e: e["chunk_index"])
    normalized = []
    for e in head + per_chunk:
        item = {k: v for k, v in e.items() if k != "duration"}
        normalized.append(item)
    return normalized


async def _run_engine(engine_name: str, bilingual: bool = False, glossary: dict | None = None):
    """用指定引擎跑一遍固定输入，回收对外契约三件套。"""
    get_config(Path("config/config.yaml"))  # 固定为主配置，隔离其他用例的单例污染
    chunks = _sample_chunks()
    events: list[dict] = []

    async def progress_callback(event: dict):
        events.append(event)

    with tempfile.TemporaryDirectory() as temp_dir:
        cache = TranslationCache(Path(temp_dir) / f"{engine_name}_cache.db")
        engine = TranslationEngine(
            glossary=glossary or {},
            model_factory=FakeModelFactory(),
            engine=engine_name,
            cache=cache,
        )
        output_path = Path(temp_dir) / f"{engine_name}_output.md"
        output_path.write_text("", encoding="utf-8")

        results = await engine.translate_batch(
            text="Alpha body one.\n\nBeta body two.\n\nGamma BOOM fails.\n\nDelta body four.",
            output_path=output_path,
            progress_callback=progress_callback,
            bilingual=bilingual,
            prepared_chunks=chunks,
        )
        file_text = output_path.read_text(encoding="utf-8")

    return file_text, results, events


def _run(engine_name: str, bilingual: bool = False, glossary: dict | None = None):
    async def scenario():
        return await _run_engine(engine_name, bilingual, glossary)

    return asyncio.run(scenario())


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """失败块会触发指数退避重试；测试里跳过真实等待。"""
    async def _instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


@pytest.mark.parametrize("engine_name", ENGINES)
def test_engine_satisfies_output_contract(engine_name):
    """按序写盘 + 失败块占位符：输出文件契约。"""
    file_text, _results, _events = _run(engine_name)

    pos_0 = file_text.find("译文::Alpha body one.")
    pos_1 = file_text.find("译文::Beta body two.")
    pos_3 = file_text.find("译文::Delta body four.")
    placeholder_pos = file_text.find("[翻译失败 - Chunk 2]")

    assert -1 not in (pos_0, pos_1, pos_3, placeholder_pos)
    # 按原文顺序写盘，失败块占位符落在其应有位置（chunk-1 与 chunk-3 之间）
    assert pos_0 < pos_1 < placeholder_pos < pos_3
    # 失败块原文进入占位符，供人工补全
    assert "Gamma BOOM fails." in file_text


@pytest.mark.parametrize("engine_name", ENGINES)
def test_engine_satisfies_result_contract(engine_name):
    """结果列表契约：按块索引有序、成功/失败标记正确。"""
    _file_text, results, _events = _run(engine_name)

    assert [r.chunk_index for r in results] == [0, 1, 2, 3]
    assert [_normalize_result(r) for r in results] == [
        (0, "Alpha body one.", "译文::Alpha body one.", True, False, False, "chunk-0", True),
        (1, "Beta body two.", "译文::Beta body two.", True, False, False, "chunk-1", True),
        (2, "Gamma BOOM fails.", "[翻译失败: fake translator boom]", False, False, False, "chunk-2", False),
        (3, "Delta body four.", "译文::Delta body four.", True, False, False, "chunk-3", True),
    ]


@pytest.mark.parametrize("engine_name", ENGINES)
def test_engine_satisfies_progress_contract(engine_name):
    """进度事件序列契约：split_completed 置顶，每块一条完成/失败事件。"""
    _file_text, _results, events = _run(engine_name)
    normalized = _normalize_events(events)

    assert normalized[0] == {"event": "split_completed", "total_chunks": 4}

    per_chunk = normalized[1:]
    assert [e["chunk_index"] for e in per_chunk] == [0, 1, 2, 3]
    assert [e["status"] for e in per_chunk] == ["completed", "completed", "failed", "completed"]
    assert per_chunk[0]["translation"] == "译文::Alpha body one."
    assert per_chunk[2]["error"] == "fake translator boom"


@pytest.mark.parametrize("bilingual", [False, True])
def test_native_and_langgraph_are_equivalent(bilingual):
    """两引擎对同一输入逐字段等价：输出文件、结果列表、进度事件序列完全一致。

    注入触发术语修复的术语表，让审校员修复循环也纳入等价比对（repaired 标记）。
    """
    glossary = {"Alpha": "阿尔法 (Alpha)"}

    native_file, native_results, native_events = _run("native", bilingual, glossary)
    graph_file, graph_results, graph_events = _run("langgraph", bilingual, glossary)

    # 1. 按序写盘的输出文件逐字节一致
    assert graph_file == native_file
    # 2. 结果列表逐字段一致
    assert [_normalize_result(r) for r in graph_results] == [_normalize_result(r) for r in native_results]
    # 3. 进度事件序列（归一化后）一致
    assert _normalize_events(graph_events) == _normalize_events(native_events)
    # 修复路径确实被覆盖：chunk-0 触发术语修复
    assert native_results[0].repaired is True


def test_langgraph_isolates_failed_send_branch():
    """单块失败时该 Send 分支写入占位符，其余分支产出不受影响。"""
    file_text, results, _events = _run("langgraph")

    assert [r.success for r in results] == [True, True, False, True]
    assert "[翻译失败 - Chunk 2]" in file_text
    assert "译文::Delta body four." in file_text  # 失败块之后的分支照常产出
