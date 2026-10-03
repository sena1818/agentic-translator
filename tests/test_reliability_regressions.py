import asyncio
from pathlib import Path

import pytest

from src.api.database.db import Database
from src.api.services.glossary_service import GlossaryService
from src.core.translator import TranslationEngine
from src.core.validator import TranslationValidator
from src.domain.models.task_models import TaskStatus, TranslationTask
from src.evaluation.consistency import compute_consistency
from src.pipelines.postprocess.export_service import ExportService


def test_cancelled_task_cannot_be_resurrected(tmp_path):
    async def run():
        db = Database(str(tmp_path / "tasks.db"))
        await db.initialize()
        await db.save_task(TranslationTask("demo", "demo.md", TaskStatus.PENDING))
        old = await db.claim_next_pending_task()
        await db.cancel_task("demo")
        await db.update_task(old)
        assert (await db.get_task("demo")).status == TaskStatus.CANCELLED

    asyncio.run(run())


def test_empty_translation_fails_validation():
    assert not TranslationValidator().validate("This is a sentence.", "", {}).passed


def test_clean_output_preserves_real_code_fence():
    source = "```python\nprint(1)\n```"
    assert TranslationEngine.__new__(TranslationEngine).clean_output(source) == source


def test_asset_resolution_rejects_outside_file(tmp_path):
    allowed = tmp_path / "assets"
    allowed.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"fixture")
    assert ExportService._resolve_image_source(outside, allowed / "out.md", [allowed]) is None


def test_glossary_path_and_duplicate_are_rejected(tmp_path):
    async def run():
        service = GlossaryService.__new__(GlossaryService)
        service.glossary_dir = Path(tmp_path)
        with pytest.raises(ValueError):
            await service.create_glossary("../escape", {"A": "甲"})
        await service.create_glossary("Same", {"A": "甲"})
        with pytest.raises(FileExistsError):
            await service.create_glossary("Same", {"B": "乙"})
        assert (await service.get_glossary("same"))["terms"] == {"A": "甲"}

    asyncio.run(run())


def test_empty_consistency_has_no_quality_claim():
    report = compute_consistency([])
    assert report.rate is None
    assert report.decision == "insufficient-data"


def test_expired_execution_cannot_update_or_renew(tmp_path):
    import sqlite3

    async def run():
        db = Database(str(tmp_path / "leases.db"))
        await db.initialize()
        await db.save_task(TranslationTask("lease", "demo.md", TaskStatus.PENDING))
        first = await db.claim_next_pending_task(worker_id="one")
        with sqlite3.connect(db.db_path) as connection:
            connection.execute("UPDATE tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE task_id='lease'")
        assert not await db.heartbeat_task("lease", first.execution_id)
        assert await db.requeue_stale_processing_tasks() == 1
        second = await db.claim_next_pending_task(worker_id="two")
        assert first.execution_id != second.execution_id
        first.status = TaskStatus.COMPLETED
        assert not await db.update_task(first)
        assert (await db.get_task("lease")).execution_id == second.execution_id
        assert await db.heartbeat_task("lease", second.execution_id)

    asyncio.run(run())


def test_profile_is_reused_and_context_affects_cache(tmp_path):
    import copy

    from langchain_core.runnables import RunnableLambda

    from src.domain.rules.chunk_planning import TextChunk
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.utils.config_loader import Config

    calls = {"analysis": 0, "translation": 0}

    def analysis(_):
        calls["analysis"] += 1
        return '{"summary":"profile ' + str(calls["analysis"]) + '"}'

    def translate(_):
        calls["translation"] += 1
        return "中文译文"

    class Factory:
        def create_analyst(self):
            return RunnableLambda(analysis)

        def create_translator(self):
            return RunnableLambda(translate)

        def create_checker(self):
            return RunnableLambda(translate)

    async def run():
        cfg = Config()
        cfg.config = copy.deepcopy(cfg.config)
        cfg.config["quality"]["enable_qa_check"] = False
        chunks = [TextChunk(0, "id", "source", [], "", "context A")]
        for i in range(2):
            engine = TranslationEngine(
                config=cfg, model_factory=Factory(), cache=TranslationCache(tmp_path / "cache.db")
            )
            await engine.translate_batch("source", tmp_path / f"out{i}.md", prepared_chunks=chunks)
        assert calls == {"analysis": 1, "translation": 1}
        before = engine._build_cache_key(chunks[0])
        changed = TextChunk(0, "id", "source", [], "", "context B")
        assert before != engine._build_cache_key(changed)

    asyncio.run(run())


def test_reviewer_failure_preserves_baseline():
    from types import SimpleNamespace

    from src.domain.models.translation_models import DocumentProfile
    from src.domain.rules.chunk_planning import TextChunk
    from src.pipelines.translate.quality_pipeline import TranslationQualityPipeline

    async def unavailable(**kwargs):
        raise RuntimeError("reviewer unavailable")

    pipeline = TranslationQualityPipeline.__new__(TranslationQualityPipeline)
    pipeline.config = SimpleNamespace(enable_qa_check=True, max_fix_attempts=1)
    pipeline.validator = TranslationValidator()
    pipeline.glossary = {"term": "专门术语"}
    pipeline.repair = unavailable
    text, report, repaired = asyncio.run(
        pipeline.run(TextChunk(0, "id", "term", [], "", ""), "已有译文", DocumentProfile.empty())
    )
    assert text == "已有译文"
    assert not report.passed
    assert not repaired


def test_global_request_budget_shares_slots(tmp_path):
    from types import SimpleNamespace

    from src.infrastructure.llm.request_budget import RequestBudget

    config = SimpleNamespace(
        root_dir=tmp_path,
        api_base_url="fake",
        api_key="fake",
        max_concurrent=1,
        rate_limit=10,
        get=lambda key, default: default,
    )
    first, second = RequestBudget(config), RequestBudget(config)
    assert first._try("one")
    assert not second._try("two")
    first._release("one")
    assert second._try("two")
    second._release("two")


def test_structured_export_preserves_quotes_and_code(tmp_path):
    from src.core.output_manager import OutputManager
    from src.domain.models.translation_models import TranslationResult
    from src.infrastructure.filesystem.task_files import TaskFiles

    directory = tmp_path
    result = TranslationResult(0, "source\n\n---\n\n```python\nprint(1)\n```", "译文\n\n> 引文", True)
    path = directory / "demo.bilingual.md"
    OutputManager.write_document(path, "demo", [result], True)
    TaskFiles.save_records(directory, "demo", [result])
    html_path = ExportService.export_bilingual_html(str(path))
    html = Path(html_path).read_text()
    assert "print(1)" in html
    assert "<blockquote>" in html
    assert html.count("print(1)") == 1


def test_asset_resolution_rejects_symlink_and_html_escapes(tmp_path):
    allowed = tmp_path / "assets"
    allowed.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"fixture")
    (allowed / "image.png").symlink_to(outside)
    assert ExportService._resolve_image_source(allowed / "image.png", allowed / "out.md", [allowed]) is None
    html = ExportService.markdown_to_html("[click](javascript:alert%281%29)\n\n<script>alert(1)</script>")
    assert "<script>" not in html
    assert 'href="javascript:' not in html


def test_health_check_is_not_shadowed_by_frontend():
    from fastapi.testclient import TestClient

    from src.api.app import app

    # 不进入 lifespan，不启动真实任务 worker。
    assert TestClient(app).get("/api/health").status_code == 200


def test_api_validates_upload_and_duplicate_glossary(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.app import app
    from src.api.routes import glossary, translation
    from src.application.use_cases.translation_tasks import TranslationService
    from src.infrastructure.filesystem.task_files import TaskFiles
    from src.infrastructure.persistence.task_repository import TaskRepository

    db = Database(str(tmp_path / "api.db"))
    asyncio.run(db.initialize())
    service = TranslationService(repository=TaskRepository(db), files=TaskFiles(tmp_path / "data"))
    monkeypatch.setattr(translation, "service", service)
    terms = GlossaryService.__new__(GlossaryService)
    terms.glossary_dir = tmp_path / "glossaries"
    terms.glossary_dir.mkdir()
    monkeypatch.setattr(glossary, "service", terms)
    client = TestClient(app)
    assert client.post("/api/translation/tasks", files={"file": ("bad.pdf", b"plain text")}).status_code == 400
    assert client.post("/api/translation/tasks", files={"file": ("empty.md", b"")}).status_code == 400
    assert client.post("/api/glossary/", json={"name": "demo", "terms": {"A": "甲"}}).status_code == 200
    assert client.post("/api/glossary/", json={"name": "demo", "terms": {"B": "乙"}}).status_code == 409
    assert client.post("/api/glossary/", json={"name": "../outside", "terms": {}}).status_code == 400


def test_edit_resolves_quality_state_and_error(tmp_path):
    async def run():
        db = Database(str(tmp_path / "review.db"))
        await db.initialize()
        await db.save_task(
            TranslationTask(
                "review", "review.md", TaskStatus.PARTIAL_SUCCESS, artifact_dir="old", error="1 个块需人工复核"
            )
        )
        assert await db.replace_artifacts("review", "old", "new", {"total": 1, "failed": 0, "needs_review": 0})
        task = await db.get_task("review")
        assert task.status == TaskStatus.COMPLETED
        assert task.error is None
        assert task.result_url

    asyncio.run(run())


def test_cache_write_failure_keeps_translation_and_counts_progress_once(tmp_path):
    import copy

    from langchain_core.runnables import RunnableLambda

    from src.domain.rules.chunk_planning import TextChunk
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.utils.config_loader import Config

    class Factory:
        def create_analyst(self):
            return RunnableLambda(lambda _: "{}")

        def create_translator(self):
            return RunnableLambda(lambda _: "有效译文")

        def create_checker(self):
            return RunnableLambda(lambda _: "有效译文")

    class UnwritableCache(TranslationCache):
        async def set(self, **kwargs):
            raise OSError("disk unavailable")

    async def run():
        cfg = Config()
        cfg.config = copy.deepcopy(cfg.config)
        cfg.config["quality"]["enable_qa_check"] = False
        engine = TranslationEngine(config=cfg, model_factory=Factory(), cache=UnwritableCache(tmp_path / "cache.db"))
        events = []

        async def progress(event):
            events.append(event["status"])

        result = await engine.translate_chunk(TextChunk(0, "demo", "source", [], "", ""), progress)
        assert result.success and result.translation == "有效译文"
        assert events == ["completed"]

    asyncio.run(run())


def test_modern_result_is_not_reinterpreted_as_legacy_bilingual(tmp_path):
    from src.api.routes.files import _ensure_markdown_variants
    from src.core.output_manager import OutputManager
    from src.domain.models.translation_models import TranslationResult
    from src.infrastructure.filesystem.task_files import TaskFiles

    result = TranslationResult(0, "source", "> 保留真实引用\n\n---\n\n正文", True)
    mono, bilingual = tmp_path / "demo.md", tmp_path / "demo.bilingual.md"
    OutputManager.write_document(mono, "demo", [result])
    OutputManager.write_document(bilingual, "demo", [result], True)
    TaskFiles.save_records(tmp_path, "demo", [result])
    before = mono.read_text(encoding="utf-8")
    _ensure_markdown_variants("demo", True, tmp_path)
    assert mono.read_text(encoding="utf-8") == before
