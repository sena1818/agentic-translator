"""
文档级 LangGraph 编排引擎

按 ADR-0001 将多 Agent 流程建成一张 StateGraph：

    START → analyze（分析员节点）→ 条件边 fan-out（Send API，每块一个翻译分支）
          → translate（翻译分支，复用翻译客户端 + 质量流水线）→ aggregate（汇总节点）→ END

翻译分支复用宿主引擎的既有组件（缓存、翻译客户端、质量流水线、顺序输出），
并发上限由图执行的 max_concurrency 控制，不使用 checkpointer
（断点恢复由 chunk 级翻译缓存承担）。
"""
from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ...core.chunk_planner import TextChunk
from ...core.output_manager import OutputManager
from ...domain.models.translation_models import TranslationResult
from .document_translation_pipeline import ProgressCallback


class _DocState(TypedDict):
    """文档级图状态。

    index 由 Send 分支载荷注入，供翻译节点定位块；
    results 用 operator.add 归并各分支产出的单块结果。
    """

    index: int
    results: Annotated[list[TranslationResult], operator.add]


class LangGraphTranslationOrchestrator:
    """文档级 LangGraph StateGraph 编排引擎（ADR-0001）。"""

    def __init__(self, host):
        self.host = host

    async def run(
        self,
        *,
        text: str,
        chunks: list[TextChunk],
        output_path: Path,
        bilingual: bool,
        progress_callback: ProgressCallback,
    ) -> list[TranslationResult]:
        output_manager = OutputManager(str(output_path), bilingual=bilingual)
        graph = self._build_graph(text, chunks, output_manager, progress_callback)
        final = await graph.ainvoke(
            {"results": []},
            config={"max_concurrency": self.host.config.max_concurrent},
        )
        return sorted(final["results"], key=lambda result: result.chunk_index)

    def _build_graph(
        self,
        text: str,
        chunks: list[TextChunk],
        output_manager: OutputManager,
        progress_callback: ProgressCallback,
    ):
        host = self.host

        async def analyze(_state: _DocState) -> dict:
            """分析员节点：文档画像 + 缓存初始化 + 上报分块完成。"""
            await host._prepare_document(text, chunks, progress_callback)
            return {}

        def fan_out(_state: _DocState):
            """条件边：为每个块派发一个翻译分支（Send API）。"""
            if not chunks:
                return ["aggregate"]
            return [Send("translate", {"index": index}) for index in range(len(chunks))]

        async def translate(state: _DocState) -> dict:
            """翻译分支：缓存命中或实时翻译 + 质检修复，并按序写盘。"""
            chunk = chunks[state["index"]]
            result = await host._translate_and_emit(chunk, output_manager, progress_callback)
            return {"results": [result]}

        async def aggregate(_state: _DocState) -> dict:
            """汇总节点：所有翻译分支的汇合点（顺序写盘已在分支内完成）。"""
            return {}

        builder = StateGraph(_DocState)
        builder.add_node("analyze", analyze)
        builder.add_node("translate", translate)
        builder.add_node("aggregate", aggregate)
        builder.add_edge(START, "analyze")
        builder.add_conditional_edges("analyze", fan_out, ["translate", "aggregate"])
        builder.add_edge("translate", "aggregate")
        builder.add_edge("aggregate", END)
        return builder.compile()
