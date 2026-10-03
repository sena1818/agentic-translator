import asyncio

from src.application.use_cases import translation_tasks
from src.domain.models.task_models import TaskStatus
from src.domain.models.translation_models import TranslationResult
from src.domain.rules.chunk_planning import TextChunk
from src.infrastructure.filesystem.task_files import TaskFiles
from src.infrastructure.persistence.database import Database
from src.infrastructure.persistence.task_repository import TaskRepository


class Engine:
    calls = []

    def __init__(self, glossary=None):
        self.seed_results = {}
        self.force_indices = set()

    def plan_chunks(self, text):
        return [TextChunk(0, "one", "Term one", [], "", ""), TextChunk(1, "two", "Term two", [], "", "")]

    async def translate_batch(self, text, output_path, bilingual=False, progress_callback=None, prepared_chunks=None):
        results = []
        for chunk in prepared_chunks:
            if chunk.index in self.seed_results and chunk.index not in self.force_indices:
                result = self.seed_results[chunk.index]
            else:
                self.calls.append(chunk.index)
                result = TranslationResult(
                    chunk.index,
                    chunk.text,
                    "译文一" if chunk.index == 0 else "译文二",
                    True,
                    chunk_id=chunk.chunk_id,
                    quality_report={"passed": True},
                )
            results.append(result)
            if progress_callback:
                await progress_callback({"status": "completed", "chunk_index": chunk.index})
        return results


def test_edit_retry_and_export_share_structured_results(tmp_path, monkeypatch):
    monkeypatch.setattr(translation_tasks, "TranslationEngine", Engine)
    Engine.calls = []

    async def run():
        db = Database(str(tmp_path / "tasks.db"))
        await db.initialize()
        service = translation_tasks.TranslationService(
            repository=TaskRepository(db), files=TaskFiles(tmp_path / "data")
        )
        task = await service.create_task(b"Term one\n\nTerm two", "sample.md", bilingual=True)
        await service.start_translation(task.task_id)
        saved = await service.get_task(task.task_id)
        assert saved.status == TaskStatus.COMPLETED
        old_revision = saved.artifact_dir
        await service.edit_chunk(task.task_id, 0, "人工修改译文", old_revision)
        assert "人工修改译文" in (await service.export_result(task.task_id))["content"]
        assert (await service.get_task(task.task_id)).error is None
        await service.retry_task(task.task_id, [1])
        await service.start_translation(task.task_id)
        assert Engine.calls == [0, 1, 1]
        assert (await service.get_chunks(task.task_id))["chunks"][0]["translation"].strip() == "人工修改译文"
        saved = await service.get_task(task.task_id)
        assert saved.artifact_dir != old_revision
        assert service.files.result(saved, True).exists()

    asyncio.run(run())
