import asyncio
import tempfile
from pathlib import Path

from src.core.translator import TranslationEngine, TranslationResult
from src.domain.models.translation_models import DocumentProfile
from src.domain.rules.chunk_planning import TextChunk
from src.infrastructure.cache.translation_cache import TranslationCache


def test_cache_roundtrip():
    async def scenario():
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TranslationCache(Path(temp_dir) / "cache.db")
            await cache.set(
                cache_key="abc",
                chunk_id="chunk-1",
                translation="测试译文",
                quality_report={"passed": True, "issue_count": 0, "issues": []},
                repaired=False,
            )
            return await cache.get("abc")

    entry = asyncio.run(scenario())
    assert entry is not None
    assert entry.chunk_id == "chunk-1"
    assert entry.translation == "测试译文"
    assert entry.quality_report["passed"]


def test_translate_batch_uses_cache_hits():
    async def scenario():
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = TranslationEngine.__new__(TranslationEngine)
            engine.cache = TranslationCache(Path(temp_dir) / "cache.db")
            engine.prompt_version = "v2"
            engine.glossary = {}

            class Config:
                model_name = "test-model"
                max_concurrent = 2

            engine.config = Config()

            chunks = [
                TextChunk(
                    index=0,
                    chunk_id="chunk-0",
                    text="cached chunk",
                    section_path=["Section"],
                    section_title="Section",
                    context_text="",
                ),
                TextChunk(
                    index=1,
                    chunk_id="chunk-1",
                    text="live chunk",
                    section_path=["Section"],
                    section_title="Section",
                    context_text="ctx",
                ),
            ]

            engine.plan_chunks = lambda text: chunks
            engine._build_cache_key = lambda chunk: chunk.chunk_id

            async def fake_analyze(text, chunks):
                return DocumentProfile.empty()

            engine.analyze_document = fake_analyze

            live_calls = []

            async def fake_translate(chunk, callback):
                live_calls.append(chunk.chunk_id)
                return TranslationResult(
                    chunk_index=chunk.index,
                    original=chunk.text,
                    translation="实时翻译",
                    success=True,
                    chunk_id=chunk.chunk_id,
                )

            engine.translate_chunk = fake_translate

            await engine.cache.set(
                cache_key="chunk-0",
                chunk_id="chunk-0",
                translation="缓存译文",
                quality_report={"passed": True, "issue_count": 0, "issues": []},
                repaired=False,
            )

            output_path = Path(temp_dir) / "out.md"
            output_path.write_text("", encoding="utf-8")
            results = await TranslationEngine.translate_batch(
                engine,
                text="dummy",
                output_path=output_path,
                prepared_chunks=chunks,
            )
            return results, live_calls

    results, live_calls = asyncio.run(scenario())

    assert len(results) == 2
    assert results[0].cached
    assert results[0].translation == "缓存译文"
    assert live_calls == ["chunk-1"]
