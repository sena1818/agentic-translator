"""供 CLI、Web 和 MCP 使用的翻译用例。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path

from ...core.translator import TranslationEngine
from ...domain.models.translation_models import TranslationPipelineOutput

ProgressCallback = Callable[[dict], Awaitable[None]] | None


class RunTranslationPipeline:
    def __init__(self, glossary=None, engine_cls=TranslationEngine, config=None):
        options = {"glossary": glossary or {}}
        if config is not None and "config" in inspect.signature(engine_cls).parameters:
            options["config"] = config
        self.engine = engine_cls(**options)

    def plan_chunks(self, text):
        return self.engine.plan_chunks(text)

    async def execute(
        self,
        text: str,
        output_path: Path,
        bilingual=False,
        progress_callback: ProgressCallback = None,
        prepared_chunks=None,
    ):
        chunks = prepared_chunks if prepared_chunks is not None else self.plan_chunks(text)
        results = await self.engine.translate_batch(
            text=text,
            output_path=output_path,
            bilingual=bilingual,
            progress_callback=progress_callback,
            prepared_chunks=chunks,
        )
        return TranslationPipelineOutput(chunks, results, self.engine)

    run = execute
