import asyncio
import tempfile
from pathlib import Path

from src.api.database.db import Database
from src.api.services import translation_service as translation_service_module
from src.core.translator import TranslationResult
from src.domain.rules.chunk_planning import TextChunk


class FakeConverter:
    def convert(self, input_path, output_dir):
        return input_path


class FailingConverter:
    def convert(self, input_path, output_dir):
        raise AssertionError("不应再次触发转换")


class FakeEngine:
    def __init__(self, glossary=None):
        self.glossary = glossary or {}

    def plan_chunks(self, text):
        return [
            TextChunk(
                index=0,
                chunk_id="chunk-0",
                text="chunk-0",
                section_path=["Section A"],
                section_title="Section A",
                context_text="",
            ),
            TextChunk(
                index=1,
                chunk_id="chunk-1",
                text="chunk-1",
                section_path=["Section A"],
                section_title="Section A",
                context_text="上一块末尾",
            ),
        ]

    def split_text(self, text):
        return ["chunk-0", "chunk-1"]

    async def translate_batch(self, text, output_path, progress_callback=None, bilingual=False, prepared_chunks=None):
        if progress_callback:
            await progress_callback({"chunk_index": 0, "status": "completed", "translation": "译文 1"})
            await progress_callback({"chunk_index": 1, "status": "failed", "error": "mock failure"})

        return [
            TranslationResult(0, "chunk-0", "译文 1", True),
            TranslationResult(1, "chunk-1", "[翻译失败]", False),
        ]


def test_partial_success_status_is_persisted():
    async def scenario():
        original_engine = translation_service_module.TranslationEngine
        original_converter = translation_service_module.DocumentConverter
        temp_dir = tempfile.TemporaryDirectory()
        test_db_path = Path(temp_dir.name) / "translation.db"
        task = None

        translation_service_module.TranslationEngine = FakeEngine
        translation_service_module.DocumentConverter = FakeConverter

        service = translation_service_module.TranslationService()
        service.db = Database(str(test_db_path))
        await service.db.initialize()

        try:
            task = await service.create_task(
                file_content=b"# title\n\nmock body\n",
                filename="service_case.md",
                bilingual=True,
            )
            await service.start_translation(task.task_id)
            saved = await service.get_task(task.task_id)
            mono_content = service.files.result(saved).read_text(encoding="utf-8")
            bilingual_content = service.files.result(saved, True).read_text(encoding="utf-8")
            return task.task_id, saved, mono_content, bilingual_content
        finally:
            translation_service_module.TranslationEngine = original_engine
            translation_service_module.DocumentConverter = original_converter

            if task is not None:
                import shutil

                shutil.rmtree(Path("data/results/runs") / task.task_id, ignore_errors=True)
                upload_path = Path("data/uploads") / f"{task.task_id}_service_case.md"
                result_path = Path("data/results") / f"{task.task_id}.md"
                bilingual_result_path = Path("data/results") / f"{task.task_id}.bilingual.md"
                if upload_path.exists():
                    upload_path.unlink()
                if result_path.exists():
                    result_path.unlink()
                if bilingual_result_path.exists():
                    bilingual_result_path.unlink()
            temp_dir.cleanup()

    task_id, saved, mono_content, bilingual_content = asyncio.run(scenario())

    assert saved.status.value == "partial_success"
    assert saved.bilingual
    assert saved.progress.current == 2
    assert saved.result_url == f"/api/files/results/{task_id}"
    assert "失败块索引" in saved.error
    assert "译文 1" in mono_content
    assert "> chunk-0" in bilingual_content


def test_reuses_existing_epub_markdown_after_interruption():
    async def scenario():
        original_engine = translation_service_module.TranslationEngine
        original_converter = translation_service_module.DocumentConverter
        temp_dir = tempfile.TemporaryDirectory()
        test_db_path = Path(temp_dir.name) / "translation.db"
        task = None

        translation_service_module.TranslationEngine = FakeEngine
        translation_service_module.DocumentConverter = FailingConverter

        service = translation_service_module.TranslationService()
        service.db = Database(str(test_db_path))
        await service.db.initialize()

        try:
            task = await service.create_task(
                file_content=b"PK placeholder epub bytes",
                filename="resume_case.epub",
                bilingual=False,
            )

            upload_path = Path("data/uploads") / f"{task.task_id}_resume_case.epub"
            temp_markdown = Path("data/temp") / task.task_id / upload_path.with_suffix(".md").name
            temp_markdown.parent.mkdir(parents=True, exist_ok=True)
            temp_markdown.write_text("# Title\n\nbody\n", encoding="utf-8")

            await service.start_translation(task.task_id)
            saved = await service.get_task(task.task_id)
            return task.task_id, saved
        finally:
            translation_service_module.TranslationEngine = original_engine
            translation_service_module.DocumentConverter = original_converter

            if task is not None:
                import shutil

                shutil.rmtree(Path("data/results/runs") / task.task_id, ignore_errors=True)
                upload_path = Path("data/uploads") / f"{task.task_id}_resume_case.epub"
                result_path = Path("data/results") / f"{task.task_id}.md"
                temp_task_dir = Path("data/temp") / task.task_id
                if upload_path.exists():
                    upload_path.unlink()
                if result_path.exists():
                    result_path.unlink()
                if temp_task_dir.exists():
                    for child in sorted(temp_task_dir.rglob("*"), reverse=True):
                        if child.is_file():
                            child.unlink()
                        elif child.is_dir():
                            child.rmdir()
                    temp_task_dir.rmdir()
            temp_dir.cleanup()

    task_id, saved = asyncio.run(scenario())

    assert saved.status.value == "partial_success"
    assert saved.result_url == f"/api/files/results/{task_id}"
