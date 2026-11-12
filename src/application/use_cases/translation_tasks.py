"""翻译任务用例：状态迁移、执行租约、块结果和人工审校。"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
import uuid
from dataclasses import asdict
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
from .book_index import BookIndexService
from .correction_memory import CorrectionMemoryService, plan_to_dict, suggestion_to_dict, term_change_to_dict
from .run_translation_pipeline import RunTranslationPipeline
from .term_ledger_store import load_ledger, merged_glossary, save_ledger

logger = logging.getLogger(__name__)

SOURCE_NAME = "source.md"


class TranslationService:
    def __init__(self, repository=None, converter=None, glossary_service=None, files=None, config=None):
        self.tasks = repository or TaskRepository()
        self.converter = converter or DocumentConverter()
        self.glossary_service = glossary_service or GlossaryService()
        self.files = files or TaskFiles()
        self.config = config or get_config()
        self.postprocess_pipeline = ResultPostprocessPipeline()
        self.book_index = BookIndexService(self.files.root)
        self.memory = CorrectionMemoryService(self.files.root, self.book_index, self.glossary_service)

    @property
    def db(self):
        return self.tasks.db

    @db.setter
    def db(self, value: Database):
        self.tasks = TaskRepository(value)

    async def create_task(
        self, file_content: bytes, filename: str, glossary_id=None, bilingual=False, author=None, domain=None
    ):
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
            author=(author or "").strip() or None,
            domain=(domain or "").strip() or None,
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
            # 已保存的术语账本（可能经人工审阅）补充到用户术语表之后，保证同一本书多次运行译名一致
            saved_ledger = await load_ledger(self.glossary_service, task.filename)
            glossary = self.memory.effective_glossary(task_id, merged_glossary(task.glossary_snapshot or {}, saved_ledger))
            use_case = RunTranslationPipeline(glossary, engine_cls=TranslationEngine, config=config)
            # 同作者、同领域其他书的术语只作为提示里的参考译名，不并入术语表、不参与术语一致性校验
            suggestions = self.memory.suggestions(task, known_terms=list(glossary))
            translator = getattr(use_case.engine, "structured_translator", None)
            if suggestions and translator is not None:
                translator.term_suggestions = suggestions
            chunks = use_case.plan_chunks(text)
            structured_chunks = _structured_chunks(chunks)
            if structured_chunks:
                # 翻译前就建好只含原文的全书索引，之后每译完一块写入该块译文
                await self._update_index(task_id, lambda: self.book_index.build_source(task_id, structured_chunks))
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
                if structured_chunks and event.get("status") in {"completed", "failed"} and "chunk_index" in event:
                    translations = {event["chunk_index"]: event.get("translation") if event["status"] == "completed" else None}
                    await self._update_index(
                        task_id, lambda: self.book_index.update_chunks(task_id, structured_chunks, translations)
                    )

            output = await use_case.execute(text, stream_path, task.bilingual, progress, chunks)
            results = output.results
            await self._stage(task, "exporting")
            # 源文随产物保存：结构化导出与人工审校后的重渲染都从它重新规划
            await asyncio.to_thread((directory / SOURCE_NAME).write_text, text, encoding="utf-8")
            await asyncio.to_thread(self._render, task, directory, results, [Path(markdown).parent, temp_dir])
            if structured_chunks:
                # 以导出后的最终记录为准（含缓存命中、未触发进度事件的块，图片路径也已改写）
                records = [asdict(result) for result in results]
                await self._update_index(
                    task_id, lambda: self.book_index.update_records(task_id, structured_chunks, records)
                )
            failures = [r for r in results if not r.success]
            warnings = [r for r in results if r.success and r.quality_report.get("passed") is False]
            task.quality_summary = self._summary(results, directory, task_id)
            new_terms = getattr(use_case.engine, "term_ledger", None) or {}
            if new_terms or saved_ledger:
                _, added = await save_ledger(self.glossary_service, task.filename, new_terms)
                task.quality_summary["ledger_terms"] = len(saved_ledger) + added
                task.quality_summary["ledger_terms_added"] = added
            if suggestions:
                task.quality_summary["term_suggestions"] = len(suggestions)
            try:
                ledger = merged_glossary(saved_ledger, new_terms)
                await asyncio.to_thread(self.memory.record_book_terms, task, task.glossary_snapshot or {}, ledger)
            except Exception:
                logger.warning("术语条目记录失败（不影响翻译结果）: %s", task_id, exc_info=True)
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

    async def _update_index(self, task_id, update):
        """索引只是查询辅助：写入失败只记日志，不影响翻译与导出。"""
        try:
            await asyncio.to_thread(update)
        except Exception:
            logger.warning("全书索引更新失败（不影响翻译结果）: %s", task_id, exc_info=True)

    def _plan_from_artifacts(self, task, directory):
        from ...infrastructure.document.pandoc import pandoc_available
        from ...pipelines.structure.planner import StructuredDocumentPlanner

        config = self._task_config(task)
        source = Path(directory) / SOURCE_NAME
        if not (config.get("document_ir.enabled", False) and pandoc_available() and source.exists()):
            return None
        return StructuredDocumentPlanner.from_config(config).plan(source.read_text(encoding="utf-8")).structured_chunks

    @staticmethod
    def _summary(results, directory=None, task_id=None):
        summary = {
            "total": len(results),
            "failed": sum(not r.success for r in results),
            "needs_review": sum(r.success and r.quality_report.get("passed") is False for r in results),
            "repaired": sum(r.repaired for r in results),
            "cached": sum(r.cached for r in results),
        }
        structure = Path(directory) / f"{task_id}.structure.json" if directory and task_id else None
        if structure is not None and structure.exists():
            # 结构化导出可用：前端据此提供对照阅读器入口
            stats = json.loads(structure.read_text(encoding="utf-8"))
            summary["structured"] = 1
            summary["segments"] = stats.get("segments", 0)
            summary["segments_with_issues"] = stats.get("segments_with_issues", 0)
            summary["dead_links"] = len(stats.get("dead_links", []))
            summary["missing_images"] = len(stats.get("missing_images", []))
            # EPUB 变体：1 = 仅译文，2 = 仅译文 + 双语
            summary["epub"] = len(stats.get("epub", []))
            audit = stats.get("nontext_audit") or {}
            summary["nontext_issues"] = audit.get("translation_issues", 0)
            summary["nontext_source_issues"] = audit.get("source_issues", 0)
        return summary

    def _render(self, task, directory, results, asset_sources):
        if self._render_structured(task, directory, results, asset_sources):
            return
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
        self._sync_record_assets(task, directory, results, asset_sources)
        self.files.save_records(directory, task.task_id, results)

    def _sync_record_assets(self, task, directory, results, asset_sources):
        from ...pipelines.postprocess.export_service import ExportService

        for result in results:
            temporary = directory / "record.md"
            temporary.write_text(result.translation, encoding="utf-8")
            ExportService.sync_result_assets(
                temporary, [*asset_sources, directory / "assets" / task.task_id], task.task_id
            )
            result.translation = temporary.read_text(encoding="utf-8")
            temporary.unlink()

    def _render_structured(self, task, directory, results, asset_sources) -> bool:
        """Document IR 导出：单语/段级双语 Markdown + 双栏联动 HTML。不适用时返回 False 走旧导出。"""
        from dataclasses import asdict

        from ...infrastructure.document.pandoc import pandoc_available
        from ...pipelines.postprocess.export_service import ExportService
        from ...pipelines.structure.audit import image_checker
        from ...pipelines.structure.exporter import StructuredExporter

        config = self._task_config(task)
        source_path = directory / SOURCE_NAME
        if not source_path.exists() and task.artifact_dir:
            previous = self.files.artifacts(task) / SOURCE_NAME
            if previous.exists():
                shutil.copy2(previous, source_path)
        if not (config.get("document_ir.enabled", False) and pandoc_available() and source_path.exists()):
            return False
        try:
            stats = StructuredExporter.from_config(config).export_all(
                source_path.read_text(encoding="utf-8"),
                [asdict(result) for result in results],
                directory,
                task.task_id,
                title=Path(task.filename).stem,
                bilingual=task.bilingual,
                image_locator=ExportService.image_locator([*asset_sources, directory / "assets" / task.task_id]),
                image_exists=image_checker([*asset_sources, directory / "assets" / task.task_id]),
            )
        except Exception:
            logger.warning("结构化导出失败，回落到旧导出: %s", task.task_id, exc_info=True)
            return False
        if stats is None:
            return False
        paths = [directory / f"{task.task_id}.md"]
        if task.bilingual:
            paths.append(directory / f"{task.task_id}.bilingual.md")
        self.postprocess_pipeline.sync_assets(paths, asset_sources, task.task_id)
        self._sync_record_assets(task, directory, results, asset_sources)
        self.files.save_records(directory, task.task_id, results)
        return True

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
        previous = selected.get("translation") or ""
        glossary = await self._review_glossary(task)
        selected["translation"] = translation
        selected["success"] = True
        selected["quality_report"] = TranslationValidator().validate(selected["original"], translation, glossary).to_dict()
        # 人工编辑过 ≠ 已审校：审校是另一次显式确认（mark_reviewed）
        selected["quality_report"]["edited"] = True
        await self._republish(task, records, [index])
        changes = []
        if previous and previous != translation:
            try:
                changes = await asyncio.to_thread(
                    self.memory.record_edit, task, index, selected["original"], previous, translation, glossary
                )
            except Exception:
                logger.warning("纠正记录写入失败（不影响审校结果）: %s", task_id, exc_info=True)
        return {**(await self.get_chunks(task_id)), "term_changes": [term_change_to_dict(c) for c in changes]}

    async def _review_glossary(self, task):
        """审校时的质检术语表：任务术语表 + 本书账本 + 本书人工纠正（与重译时一致）。"""
        saved_ledger = await load_ledger(self.glossary_service, task.filename)
        base = merged_glossary(task.glossary_snapshot or {}, saved_ledger)
        return await asyncio.to_thread(self.memory.effective_glossary, task.task_id, base)

    async def _republish(self, task, records, changed):
        """把改过的记录渲染到新的产物目录并以 CAS 发布，再更新改动块的全书索引。"""
        task_id = task.task_id
        results = [TranslationResult(**item) for item in records]
        new_dir = self.files.root / "results" / "runs" / task_id / f"review-{uuid.uuid4().hex}"
        new_dir.mkdir(parents=True)
        await asyncio.to_thread(self._render, task, new_dir, results, [self.files.artifacts(task) / "assets" / task_id])
        summary = self._summary(results, new_dir, task_id)
        if not await self.db.replace_artifacts(task_id, task.artifact_dir, str(new_dir), summary):
            raise ValueError("结果已更新，请刷新后再编辑")
        task.artifact_dir = str(new_dir)
        translations = {result.chunk_index: result.translation for result in results if result.chunk_index in changed}

        def reindex():
            chunks = self._plan_from_artifacts(task, new_dir)
            if chunks and translations:
                self.book_index.update_chunks(task_id, chunks, translations)

        await self._update_index(task_id, reindex)
        return new_dir

    async def _reviewable_task(self, task_id, revision):
        task = await self.tasks.get(task_id)
        if not task or task.status not in {TaskStatus.COMPLETED, TaskStatus.PARTIAL_SUCCESS, TaskStatus.FAILED}:
            raise ValueError("任务执行结束后才能审校")
        if revision != task.artifact_dir:
            raise ValueError("结果已更新，请刷新后再编辑")
        return task

    async def mark_reviewed(self, task_id, index, revision, reviewed=True):
        """标记某块已审校（与"人工编辑过"分开）。"""
        task = await self._reviewable_task(task_id, revision)
        records = self.files.records(task)
        selected = next((item for item in records if item["chunk_index"] == index), None)
        if selected is None:
            raise ValueError("块不存在")
        selected.setdefault("quality_report", {})["reviewed"] = bool(reviewed)
        await self._republish(task, records, [])
        return await self.get_chunks(task_id)

    # ------------------------------------------------------------------ #
    # 纠正记忆：书内传播与跨书建议
    # ------------------------------------------------------------------ #

    async def preview_term_correction(self, task_id, term, old, new):
        """改一个译名前先看：全书哪些段的原文含该术语、译文还在用旧译名，替换后是什么样。"""
        task = await self._indexed_task(task_id)
        glossary = await self._review_glossary(task)
        plan = await asyncio.to_thread(self.memory.plan, task_id, term, old, new, glossary)
        return {**plan_to_dict(plan), "revision": task.artifact_dir}

    async def apply_term_correction(self, task_id, term, old, new, positions=None, revision=None, scope=None):
        """把预览中选中的段批量改成新译名，重新质检改动的块并发布；同时记下纠正与术语条目。"""
        task = await self._reviewable_task(task_id, revision)
        glossary = await self._review_glossary(task)
        plan = await asyncio.to_thread(self.memory.plan, task_id, term, old, new, glossary)
        chunks = await asyncio.to_thread(self._plan_from_artifacts, task, Path(task.artifact_dir))
        if not chunks:
            raise ValueError("该任务没有结构化结果，无法定位到段")
        records = self.files.records(task)
        edits, applied = await asyncio.to_thread(self.memory.apply, plan, chunks, records, positions)
        if not edits:
            raise ValueError("没有可替换的段")
        entry = await asyncio.to_thread(self.memory.record_term_decision, task, plan, applied, scope)
        # 新译名记入本书术语后再质检：改动的块按新译名检查
        glossary = await self._review_glossary(task)
        validator = TranslationValidator()
        by_index = {record["chunk_index"]: record for record in records}
        for edit in edits:
            record = by_index[edit.chunk_index]
            record["translation"] = edit.translation
            report = validator.validate(record["original"], edit.translation, glossary).to_dict()
            report["edited"] = True
            report["propagated"] = plan.term
            record["quality_report"] = report
        await self._republish(task, records, [edit.chunk_index for edit in edits])
        return {
            "term": plan.term,
            "old": plan.old_rendering,
            "new": plan.new_rendering,
            "scope": entry.scope,
            "applied_segments": applied,
            "replacements": sum(edit.replacements for edit in edits),
            "chunks": [
                {
                    "chunk_index": edit.chunk_index,
                    "replacements": edit.replacements,
                    "method": edit.method,
                    "passed": by_index[edit.chunk_index]["quality_report"]["passed"],
                }
                for edit in edits
            ],
        }

    async def term_suggestions(self, task_id):
        """这本书能看到的跨书术语建议（同作者、同领域的其他书），每条标明来源。"""
        task = await self.tasks.get(task_id)
        if task is None:
            raise ValueError("任务不存在")
        glossary = await self._review_glossary(task)
        suggestions = await asyncio.to_thread(self.memory.suggestions, task, list(glossary))
        return {"author": task.author, "domain": task.domain, "items": [suggestion_to_dict(s) for s in suggestions]}

    async def task_corrections(self, task_id, limit=100):
        if await self.tasks.get(task_id) is None:
            raise ValueError("任务不存在")
        return {"items": await asyncio.to_thread(self.memory.corrections, task_id, limit)}

    # ------------------------------------------------------------------ #
    # 全书索引查询
    # ------------------------------------------------------------------ #

    async def _indexed_task(self, task_id):
        task = await self.tasks.get(task_id)
        if task is None:
            raise ValueError("任务不存在")
        return task

    async def search_book(self, task_id, query, field="any", skip=0, limit=20):
        await self._indexed_task(task_id)
        return await asyncio.to_thread(self.book_index.search, task_id, query, field, skip, limit)

    async def term_occurrences(self, task_id, term, case_sensitive=False, variants=True, skip=0, limit=20):
        await self._indexed_task(task_id)
        return await asyncio.to_thread(
            self.book_index.occurrences,
            task_id,
            term,
            case_sensitive=case_sensitive,
            variants=variants,
            offset=skip,
            limit=limit,
        )

    async def term_renderings(self, task_id, term, case_sensitive=False, variants=True):
        task = await self._indexed_task(task_id)
        # 术语表里的译名作为已知译法优先比对；其余译法从译文中统计
        known = [
            zh.split(" (", 1)[0].strip()
            for en, zh in (task.glossary_snapshot or {}).items()
            if en.casefold() == term.strip().casefold()
        ]
        return await asyncio.to_thread(
            self.book_index.renderings, task_id, term, known, case_sensitive=case_sensitive, variants=variants
        )

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
        self.book_index.delete(task_id)
        self.memory.delete(task_id)
        return True


def _structured_chunks(chunks):
    """结构化规划的块载荷；回落到传统分块时返回 None（没有段位置，不建索引）。"""
    items = [getattr(chunk, "structured", None) for chunk in chunks]
    return items if items and all(item is not None for item in items) else None
