import asyncio

from langchain_core.runnables import RunnableLambda

from src.domain.models.translation_models import DocumentProfile
from src.domain.rules.chunk_planning import TextChunk
from src.pipelines.translate.prompt_builder import TranslationPromptBuilder
from src.pipelines.translate.translation_client import TranslationClient


def test_translation_client_retries_then_succeeds():
    class Config:
        def get(self, key, default=None):
            if key == "api.translator.max_retries":
                return 2
            return default

    class DummyRateLimiter:
        async def acquire(self):
            return None

    calls = {"count": 0}

    async def flaky_translator(payload):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary")
        return "译文结果"

    client = TranslationClient(
        llm_translator=RunnableLambda(flaky_translator),
        config=Config(),
        prompt_builder=TranslationPromptBuilder(),
        rate_limiter=DummyRateLimiter(),
        clean_output=lambda text: text.strip(),
        glossary={},
    )

    async def scenario():
        return await client.translate(
            chunk=TextChunk(
                index=0,
                chunk_id="chunk-0",
                text="body",
                section_path=["Section"],
                section_title="Section",
                context_text="ctx",
            ),
            document_profile=DocumentProfile(summary="profile"),
        )

    translation, retry_count, _duration = asyncio.run(scenario())
    assert translation == "译文结果"
    assert retry_count == 1
