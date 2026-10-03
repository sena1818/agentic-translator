"""翻译任务用例：状态迁移、执行租约、块结果和人工审校。"""

from __future__ import annotations

import asyncio
import copy
import logging
import shutil
import uuid
from pathlib import Path

from ...converters.document_converter import DocumentConverter
from ...core.output_manager import OutputManager
from ...core.translator import TranslationEngine
from ...core.validator import TranslationValidator
from ...domain.models.task_models import TaskProgress, TaskStatus, TranslationTask
from ...domain.models.translation_models import TranslationResult
from ...infrastructure.filesystem.task_files import TaskFiles, safe_id, validate_upload
from ...infrastructure.persistence.database import Database
from ...infrastructure.persistence.glossary_repository import GlossaryService
from ...infrastructure.persistence.task_repository import TaskRepository
from ...pipelines.postprocess.result_postprocess_pipeline import ResultPostprocessPipeline
from ...utils.config_loader import get_config
from .run_translation_pipeline import RunTranslationPipeline

logger = logging.getLogger(__name__)


class TranslationService:
    def __init__(self, repository=None, converter=None, glossary_service=None, files=None, config=None):
        self.tasks = repository or TaskRepository()
        self.converter = converter or DocumentConverter()
        self.glossary_service = glossary_service or GlossaryService()
        self.files = files or TaskFiles()
        self.config = config or get_config()
        self.postprocess_pipeline = ResultPostprocessPipeline()

    @property
    def db(self):
        return self.tasks.db

    @db.setter
    def db(self, value: Database):
        self.tasks = TaskRepository(value)

    async def create_task(self, file_content: bytes, filename: str, glossary_id=None, bilingual=False):
        filename = validate_upload(file_content, filename)
        glossary = {}
        if glossary_id:
            selected = await self.glossary_service.get_glossary(glossary_id)
            if selected is None:
                raise ValueError("所选术语表不存在")
            glossary = selected["terms"]
        task = TranslationTask(
            str(uuid.uuid4()),
            filename,
            TaskStatus.PENDING,
            glossary_id=glossary_id,
            bilingual=bilingual,
            config_snapshot=copy.deepcopy(self.config.config),
            glossary_snapshot=glossary,
        )
        path = self.files.upload(task)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, file_content)
        try:
            await self.tasks.save(task)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return task

    async def claim_next_pending_task(self, worker_id="direct", lease_seconds=900):
        return await self.tasks.claim_next_pending(worker_id=worker_id, lease_seconds=lease_seconds)

    async def requeue_stale_tasks(self, stale_after_seconds=900):
        return await self.tasks.requeue_stale_processing(stale_after_seconds)

    def _task_config(self, task):
        config = copy.copy(self.config)
        config.config = copy.deepcopy(task.config_snapshot or self.config.config)
        return config

    async def start_translation(self, task_id, already_claimed=False, execution_id=None):
        task = (
            await self.tasks.get(task_id) if already_claimed else await self.db.claim_next_pending_task(task_id=task_id)
        )
        if not task or task.status != TaskStatus.PROCESSING:
            return
        if execution_id and task.execution_id != execution_id:
            return
        work = asyncio.create_task(self._execute(task))

        async def monitor():
            while True:
                await asyncio.sleep(1)
                if not await self.db.heartbeat_task(task.task_id, task.execution_id, self.config.worker_stale_after):
                    return

        watch = asyncio.create_task(monitor())
        try:
            done, _ = await asyncio.wait({work, watch}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
            else:
                logger.info("停止已取消或失去租约的任务: %s", task_id)
        finally:
            work.cancel()
            watch.cancel()
            await asyncio.gather(work, watch, return_exceptions=True)

    async def _stage(self, task, stage):
        task.stage = stage
        if not await self.tasks.update(task):
            raise asyncio.CancelledError("执行版本已失效")

    async def _execute(self, task):
        task_id = task.task_id
        if not task.config_snapshot:
            task.config_snapshot = copy.deepcopy(self.config.config)
            if task.glossary_id:
                selected = await self.glossary_service.get_glossary(task.glossary_id)
                if selected is None:
                    task.status = TaskStatus.FAILED
                    task.error = "历史任务的术语表不存在"
                    await self.tasks.update(task)
                    return
                task.glossary_snapshot = selected["terms"]
        config = self._task_config(task)
        # 所有转换和结果写入都在执行版本私有目录；最后通过 DB CAS 发布。
        directory = self.files.attempt(task)
        directory.mkdir(parents=True, exist_ok=True)
        temp_dir = self.files.root / "temp" / task_id / task.execution_id
        try:
            file_path = self.files.upload(task)
            await self._stage(task, "converting")
            if file_path.suffix.lower() in {".pdf", ".epub", ".mobi"}:
                temp_dir.mkdir(parents=True, exist_ok=True)
                markdown = self._find_reusable_markdown(file_path, self.files.root / "temp" / task_id)
                if markdown is None:
                    if hasattr(self.converter, "convert_async"):
                        markdown = await self.converter.convert_async(file_path, temp_dir)
                    else:
                        markdown = await asyncio.to_thread(self.converter.convert, file_path, temp_dir)
                if markdown is None:
                    raise RuntimeError("文档转换失败: 未能生成 Markdown 文件")
                if Path(markdown).resolve().is_relative_to(temp_dir.resolve()):
                    (temp_dir / ".conversion-complete").write_text(str(Path(markdown).resolve()), encoding="utf-8")
            else:
                markdown = file_path
            text = await asyncio.to_thread(Path(markdown).read_text, encoding="utf-8-sig")
            if not text.strip():
                raise ValueError("转换后的文档为空")
            use_case = RunTranslationPipeline(task.glossary_snapshot, engine_cls=TranslationEngine, config=config)
            chunks = use_case.plan_chunks(text)
            old = self.files.records(task)
            if hasattr(use_case.engine, "seed_results"):
                use_case.engine.seed_results = {
                    item["chunk_index"]: TranslationResult(**item) for item in old if item["success"]
                }
                use_case.engine.force_indices = set(task.retry_indices)
            task.progress = TaskProgress(total=len(chunks))
            await self._stage(task, "analyzing")
            stream_path = directory / "stream.md"
            stream_path.write_text("", encoding="utf-8")
            started = asyncio.get_running_loop().time()
            lock = asyncio.Lock()

            async def progress(event):
                async with lock:
                    if event.get("status") in {"completed", "failed"}:
                        task.progress.current += 1
                        task.progress.elapsed = asyncio.get_running_loop().time() - started
                        task.progress.percentage = 100 * task.progress.current / max(1, task.progress.total)
                        task.progress.speed = task.progress.current * 60 / max(0.001, task.progress.elapsed)
                    await self._stage(task, "translating")

            output = await use_case.execute(text, stream_path, task.bilingual, progress, chunks)
            results = output.results
            await self._stage(task, "exporting")
            await asyncio.to_thread(self._render, task, directory, results, [Path(markdown).parent, temp_dir])
            failures = [r for r in results if not r.success]
            warnings = [r for r in results if r.success and r.quality_report.get("passed") is False]
            task.quality_summary = self._summary(results)
            task.status = (
                TaskStatus.FAILED
                if len(failures) == len(results) and failures
                else (TaskStatus.PARTIAL_SUCCESS if failures or warnings else TaskStatus.COMPLETED)
            )
            task.stage = "finished"
            task.artifact_dir = str(directory)
            task.result_url = f"/api/files/results/{task_id}" if task.status != TaskStatus.FAILED else None
            task.error = (
                (
                    f"{len(failures)}/{len(results)} 个文本块翻译失败；失败块索引: "
                    + ", ".join(str(r.chunk_index) for r in failures)
                )
                if failures
                else (f"{len(warnings)} 个块需人工复核" if warnings else None)
            )
            await self.tasks.update(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("任务失败: %s", task_id)
            task.status = TaskStatus.FAILED
            task.stage = "failed"
            task.error = str(exc)
            await self.tasks.update(task)

    @staticmethod
    def _summary(results):
        return {
            "total": len(results),
            "failed": sum(not r.success for r in results),
            "needs_review": sum(r.success and r.quality_report.get("passed") is False for r in results),
            "repaired": sum(r.repaired for r in results),
            "cached": sum(r.cached for r in results),
        }

    def _render(self, task, directory, results, asset_sources):
        formatter = self.postprocess_pipeline.formatter
        for result in results:
            if result.success:
                result.translation = formatter.format(
                    result.translation, source_type=Path(task.filename).suffix.lstrip(".")
                )
        paths = [directory / f"{task.task_id}.md"]
        OutputManager.write_document(paths[0], task.filename, results)
        if task.bilingual:
            paths.append(directory / f"{task.task_id}.bilingual.md")
            OutputManager.write_document(paths[1], task.filename, results, True)
        self.postprocess_pipeline.sync_assets(paths, asset_sources, task.task_id)
        # 结构化记录中的图片路径也保持在已发布的任务资源目录。
        from ...pipelines.postprocess.export_service import ExportService

        for result in results:
            temporary = directory / "record.md"
            temporary.write_text(result.translation, encoding="utf-8")
            ExportService.sync_result_assets(
                temporary, [*asset_sources, directory / "assets" / task.task_id], task.task_id
            )
            result.translation = temporary.read_text(encoding="utf-8")
            temporary.unlink()
        self.files.save_records(directory, task.task_id, results)

    def _find_reusable_markdown(self, file_path, temp_dir):
        expected = temp_dir / f"{file_path.stem}.md"
        if expected.exists() and expected.stat().st_size:
            return expected
        for marker in sorted(temp_dir.rglob(".conversion-complete")) if temp_dir.exists() else []:
            candidate = Path(marker.read_text(encoding="utf-8"))
            if (
                candidate.resolve().is_relative_to(temp_dir.resolve())
                and candidate.is_file()
                and candidate.stat().st_size
            ):
                return candidate
        return None

    async def get_task(self, task_id):
        return await self.tasks.get(task_id)

    async def list_tasks(self, skip=0, limit=20, **filters):
        return await self.tasks.list(skip, limit, **filters)

    async def cancel_task(self, task_id):
        return await self.tasks.cancel(task_id) if await self.tasks.get(task_id) else None

    async def retry_task(self, task_id, indices=None):
        task = await self.tasks.get(task_id)
        if task is None:
            raise ValueError("任务不存在")
        if task.status in {TaskStatus.PENDING, TaskStatus.PROCESSING}:
            raise ValueError("正在执行的任务无法重试")
        records = self.files.records(task)
        if indices is None:
            indices = [item["chunk_index"] for item in records if not item["success"]]
        valid = {item["chunk_index"] for item in records}
        if indices and not set(indices).issubset(valid):
            raise ValueError("块索引无效")
        if not await self.db.retry_task(task_id, indices):
            raise ValueError("任务状态已变化，请刷新")
        return await self.get_task(task_id)

    async def get_chunks(self, task_id, skip=0, limit=20):
        task = await self.tasks.get(task_id)
        if not task:
            raise ValueError("任务不存在")
        records = self.files.records(task)
        return {"chunks": records[skip : skip + limit], "total": len(records), "revision": task.artifact_dir}

    async def edit_chunk(self, task_id, index, translation, revision):
        task = await self.tasks.get(task_id)
        if not task or task.status not in {TaskStatus.COMPLETED, TaskStatus.PARTIAL_SUCCESS, TaskStatus.FAILED}:
            raise ValueError("任务执行结束后才能审校")
        if not translation.strip():
            raise ValueError("译文不能为空")
        if revision != task.artifact_dir:
            raise ValueError("结果已更新，请刷新后再编辑")
        records = self.files.records(task)
        selected = next((item for item in records if item["chunk_index"] == index), None)
        if selected is None:
            raise ValueError("块不存在")
        selected["translation"] = translation
        selected["success"] = True
        selected["quality_report"] = (
            TranslationValidator().validate(selected["original"], translation, task.glossary_snapshot).to_dict()
        )
        selected["quality_report"]["reviewed"] = True
        results = [TranslationResult(**item) for item in records]
        new_dir = self.files.root / "results" / "runs" / task_id / f"review-{uuid.uuid4().hex}"
        new_dir.mkdir(parents=True)
        await asyncio.to_thread(self._render, task, new_dir, results, [self.files.artifacts(task) / "assets" / task_id])
        if not await self.db.replace_artifacts(task_id, task.artifact_dir, str(new_dir), self._summary(results)):
            raise ValueError("结果已更新，请刷新后再编辑")
        return await self.get_chunks(task_id)

    async def export_result(self, task_id, variant="mono"):
        task = await self.tasks.get(task_id)
        if task is None:
            return None
        if variant not in {"mono", "bilingual"}:
            raise ValueError("结果类型无效")
        path = self.files.result(task, variant == "bilingual")
        available = (
            path.exists()
            and task.status in {TaskStatus.COMPLETED, TaskStatus.PARTIAL_SUCCESS}
            and (variant != "bilingual" or task.bilingual)
        )
        return {
            "task_id": task_id,
            "status": task.status.value,
            "variant": variant,
            "available": available,
            "content": path.read_text(encoding="utf-8") if available else None,
            "path": str(path) if available else None,
        }

    async def translate_text(self, text, glossary_id=None, bilingual=False):
        if not text.strip():
            raise ValueError("文本不能为空")
        selected = await self.glossary_service.get_glossary(glossary_id) if glossary_id else None
        if glossary_id and selected is None:
            raise ValueError("术语表不存在")
        use_case = RunTranslationPipeline(selected["terms"] if selected else {}, engine_cls=TranslationEngine)
        path = self.files.root / "temp" / f"inline_{uuid.uuid4().hex}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            output = await use_case.execute(text, path, bilingual)
        finally:
            path.unlink(missing_ok=True)
        segments = [{"source": r.original, "translation": r.translation} for r in output.results if r.success]
        return {
            "translation": "\n\n".join(s["translation"] for s in segments),
            "segments": segments,
            "chunk_count": len(output.results),
            "failed_chunks": [r.chunk_index for r in output.results if not r.success],
            "glossary_id": glossary_id,
            "bilingual": bilingual,
        }

    async def delete_task(self, task_id):
        task = await self.tasks.get(task_id)
        if task is None or task.status in {TaskStatus.PENDING, TaskStatus.PROCESSING}:
            return False
        if not await self.tasks.delete(task_id):
            return False
        self.files.upload(task).unlink(missing_ok=True)
        for suffix in (".md", ".bilingual.md", ".html", ".json"):
            (self.files.root / "results" / f"{safe_id(task_id)}{suffix}").unlink(missing_ok=True)
        for directory in (
            self.files.root / "results" / "runs" / task_id,
            self.files.root / "temp" / task_id,
            self.files.root / "results" / "assets" / task_id,
        ):
            if directory.exists():
                shutil.rmtree(directory)
        (self.files.root / "results" / "downloads" / f"{task_id}.assets.zip").unlink(missing_ok=True)
        return True
