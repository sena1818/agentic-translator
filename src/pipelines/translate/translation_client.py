"""
翻译模型调用客户端

负责：
- 调用 translator LLM
- 执行重试
- 清理输出
"""

from __future__ import annotations

import asyncio
import time

from ...core.chunk_planner import TextChunk
from ...domain.models.translation_models import DocumentProfile
from ...domain.rules.glossary_selection import respect_case as _respect_case
from ...domain.rules.glossary_selection import select_relevant_glossary
from ...infrastructure.llm.rate_limits import QuotaExhaustedError, classify
from .langchain_compat import StrOutputParser
from .prompt_builder import TranslationPromptBuilder


class TranslationClient:
    """封装单块翻译时的模型调用与重试逻辑。"""

    def __init__(
        self,
        llm_translator,
        config,
        prompt_builder: TranslationPromptBuilder,
        rate_limiter,
        clean_output,
        glossary,
    ):
        self.llm_translator = llm_translator
        self.config = config
        self.prompt_builder = prompt_builder
        self.rate_limiter = rate_limiter
        self.clean_output = clean_output
        self.glossary = glossary

    async def translate(
        self,
        chunk: TextChunk,
        document_profile: DocumentProfile,
    ) -> tuple[str, int, float]:
        prompt = self.prompt_builder.build_translation_prompt(chunk, document_profile)
        chain = prompt | self.llm_translator | StrOutputParser()
        max_retries = self.config.get("api.translator.max_retries", 3)

        relevant_glossary = select_relevant_glossary(
            self.glossary, chunk.text, chunk.context_text, respect_case=_respect_case(self.config)
        )

        for retry in range(max_retries + 1):
            try:
                await self.rate_limiter.acquire()
                start_time = time.monotonic()
                translation = await chain.ainvoke(
                    {
                        "document_profile": document_profile.to_prompt_text(),
                        "glossary": "\n".join([f"- {en}: {zh}" for en, zh in relevant_glossary.items()]),
                        "section_title": " > ".join(chunk.section_path) if chunk.section_path else chunk.section_title,
                        "context": chunk.context_text,
                        "text": chunk.text,
                    }
                )
                translation = self.clean_output(translation)
                duration = time.monotonic() - start_time
                return translation, retry, duration
            except Exception as exc:
                if isinstance(exc, QuotaExhaustedError):
                    raise
                status = getattr(exc, "status_code", None)
                if status is not None and 400 <= status < 500 and status not in {408, 409, 429}:
                    raise
                kind, wait = classify(exc)
                if kind == "rate" and retry < max_retries:
                    # 服务端给出的等待时间比固定指数退避更可靠；没有提示时按分钟级窗口退避（上限 2 分钟）
                    await asyncio.sleep(min((wait + 1) if wait else 15 * (retry + 1), 120))
                    continue
                if retry < max_retries:
                    await asyncio.sleep(2**retry)
                else:
                    raise
