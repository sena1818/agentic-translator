"""
核心翻译引擎
重构自 scripts/async_translator.py
"""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from ..domain.models.translation_models import DocumentProfile, TranslationResult
from ..domain.rules.chunk_planning import ChunkPlanner, TextChunk
from ..domain.rules.glossary_selection import respect_case as _respect_case
from ..domain.rules.glossary_selection import select_relevant_glossary
from ..infrastructure.cache.translation_cache import TranslationCache
from ..infrastructure.document.pandoc import pandoc_available
from ..infrastructure.llm.chat_model_factory import ChatModelFactory
from ..infrastructure.llm.rate_limiter import RateLimiter
from ..pipelines.translate.batch_orchestrator import TranslationBatchOrchestrator
from ..pipelines.translate.chunk_processing import build_cached_result, cached_progress_event
from ..pipelines.translate.document_analyzer import DocumentAnalyzer
from ..pipelines.translate.prompt_builder import TranslationPromptBuilder
from ..pipelines.translate.quality_pipeline import TranslationQualityPipeline
from ..pipelines.translate.structured_translation import StructuredChunkTranslator
from ..pipelines.translate.translation_client import TranslationClient
from ..utils.config_loader import get_config
from .output_manager import OutputManager
from .validator import QualityReport, TranslationValidator

logger = logging.getLogger(__name__)

# 账本候选规则或提示词变化时递增，避免断点续跑复用旧版账本
LEDGER_VERSION = "ledger-v2"


class TranslationEngine:
    """异步翻译引擎"""

    def __init__(
        self,
        glossary: dict[str, str] = None,
        model_factory=None,
        engine: str | None = None,
        cache: TranslationCache | None = None,
        config=None,
    ):
        """
        初始化翻译引擎

        Args:
            glossary: 术语表字典
            model_factory: 聊天模型工厂，默认真实 ChatModelFactory；测试可注入符合 Runnable 协议的假 LLM
            engine: 编排引擎（langgraph | native），默认取配置 multi_agent.engine
            cache: chunk 翻译缓存，默认指向 data/translation_cache.db；测试可注入隔离缓存
        """
        self.config = config or get_config()
        # 复制一份：术语账本会原地并入本次运行的术语表，不能污染调用方（如任务的术语表快照）
        self.glossary = dict(glossary or {})

        model_factory = model_factory or ChatModelFactory(self.config)
        self.llm_translator = model_factory.create_translator()
        self.llm_checker = model_factory.create_checker()
        self.llm_analyst = model_factory.create_analyst()

        # 初始化组件
        self.rate_limiter = RateLimiter(rate=self.config.rate_limit, per=60)

        self.semaphore = asyncio.Semaphore(self.config.max_concurrent)
        self.chunk_planner = ChunkPlanner(
            chunk_size=self.config.chunk_size,
            context_window=self.config.context_window,
            target_chunk_size=self.config.target_chunk_size,
            min_chunk_size=self.config.min_chunk_size,
        )
        self.validator = TranslationValidator(
            untranslated_word_span=self.config.untranslated_word_span,
            max_glossary_checks=self.config.max_glossary_checks,
            glossary_respect_case=_respect_case(self.config),
        )
        self.cache = cache or TranslationCache(self.config.root_dir / "data" / "translation_cache.db")
        self.document_profile = DocumentProfile.empty()
        self.prompt_version = "v5"
        self.seed_results = {}
        self.force_indices = set()
        self.prompt_builder = TranslationPromptBuilder(self.config)
        self.document_analyzer = DocumentAnalyzer(
            llm_analyst=self.llm_analyst,
            config=self.config,
            prompt_builder=self.prompt_builder,
        )
        self.quality_pipeline = TranslationQualityPipeline(
            validator=self.validator,
            config=self.config,
            llm_checker=self.llm_checker,
            prompt_builder=self.prompt_builder,
            rate_limiter=self.rate_limiter,
            clean_output=self.clean_output,
            glossary=self.glossary,
        )
        self.translation_client = TranslationClient(
            llm_translator=self.llm_translator,
            config=self.config,
            prompt_builder=self.prompt_builder,
            rate_limiter=self.rate_limiter,
            clean_output=self.clean_output,
            glossary=self.glossary,
        )
        self.structured_translator = StructuredChunkTranslator(
            llm_translator=self.llm_translator,
            llm_checker=self.llm_checker,
            config=self.config,
            prompt_builder=self.prompt_builder,
            rate_limiter=self.rate_limiter,
            clean_output=self.clean_output,
            glossary=self.glossary,
            validator=self.validator,
        )
        # 结构化模式（Document IR）：结构由程序持有，LLM 只翻译带内联标签的段。
        # 需要 pandoc；不可用或解析失败时自动回落到传统 Markdown 分块。
        self.structured_enabled = bool(self.config.get("document_ir.enabled", False)) and pandoc_available()
        self.document_plan = None
        self.batch_orchestrator = TranslationBatchOrchestrator(
            cache=self.cache,
            build_cache_key=self._build_cache_key,
        )
        self.engine_name = (engine or self.config.engine or "native").lower()

    def plan_chunks(self, text: str) -> list[TextChunk]:
        """结构化切块；启用 Document IR 时按 AST 单元切块。"""
        if self.structured_enabled:
            from ..pipelines.structure.planner import StructuredDocumentPlanner

            try:
                self.document_plan = StructuredDocumentPlanner.from_config(self.config).plan(text)
                return self.document_plan.chunks
            except Exception:
                logger.warning("Document IR 规划失败，回落到传统 Markdown 分块", exc_info=True)
                self.document_plan = None
        return self.chunk_planner.plan(text)

    def split_text(self, text: str) -> list[str]:
        """
        智能文本分块

        Args:
            text: 待分块文本

        Returns:
            分块后的文本列表
        """
        return [chunk.text for chunk in self.plan_chunks(text)]

    def clean_output(self, text: str) -> str:
        """
        清理 LLM 输出的废话

        Args:
            text: 原始输出

        Returns:
            清理后的文本
        """
        # 删除中文注释
        text = re.sub(r"（注：[^）]*）", "", text)
        text = re.sub(r"\(注：[^)]*\)", "", text)

        # 删除修正标记
        text = re.sub(r"【修正后的译文】[:：]?\s*", "", text)
        text = re.sub(r"\[修正后的译文\][:：]?\s*", "", text)

        # 删除英文提示
        text = re.sub(r"^Here is the translation[:：]?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^Translation[:：]?\s*", "", text, flags=re.IGNORECASE)

        # 删除 markdown 代码块标记
        wrapper = re.fullmatch(r"```markdown[ \t]*\n([\s\S]*)\n```[ \t]*", text.strip())
        if wrapper and not re.search(r"^```", wrapper.group(1), re.MULTILINE):
            text = wrapper.group(1)

        # 清理重复短语 (LLM 循环问题)
        text = self._remove_repetitions(text)

        return text.strip()

    # 判定为 LLM 退化循环所需的最小连续重复次数。取较高阈值是刻意的：
    # 专业文本（尤其后现代哲学）常有刻意的排比与咒语式重复，低阈值会误伤正文；
    # 只有远超正常修辞的失控循环才折叠，且每次折叠都打日志便于事后核查。
    _REPETITION_MIN_REPEATS = 10

    def _remove_repetitions(self, text: str) -> str:
        """折叠疑似 LLM 退化循环的连续重复，命中即记日志。

        仅处理远超正常修辞的失控重复（见 _REPETITION_MIN_REPEATS），
        以免误伤刻意的重复表达。标点重复无修辞价值，阈值单独放低。

        Args:
            text: 待处理文本

        Returns:
            清理后的文本
        """
        extra = self._REPETITION_MIN_REPEATS - 1  # 正则 \1{n,} 中的 n（首次出现之外的次数）
        rules = [
            (rf"(.{{5,50}}?)\1{{{extra},}}", r"\1", 0, "短语"),
            (rf"([一-龥]{{2,10}})\1{{{extra},}}", r"\1", 0, "中文词组"),
            (rf"\b(\w{{3,}})\s+(\1\s+){{{extra},}}", r"\1 ", re.IGNORECASE, "英文单词"),
        ]
        for pattern, repl, flags, label in rules:
            new_text = re.sub(pattern, repl, text, flags=flags)
            if new_text != text:
                logger.warning("clean_output 折叠疑似 LLM 循环重复(%s)", label)
                text = new_text

        # 连续重复标点(如 。。。。。。)保留 3 个；这类几乎不会是刻意排版
        text = re.sub(r"([。，！？；：])\1{3,}", r"\1\1\1", text)

        return text

    def build_prompt(self, chunk: TextChunk, document_profile: DocumentProfile | None = None):
        """
        构建翻译 Prompt

        Args:
            chunk: 待翻译文本块
            document_profile: 文档级分析信息

        Returns:
            Prompt 模板
        """
        return self.prompt_builder.build_translation_prompt(chunk, document_profile)

    async def analyze_document(
        self,
        text: str,
        chunks: list[TextChunk],
    ) -> DocumentProfile:
        """文档分析 agent：提炼整本文档的风格与术语上下文"""
        return await self.document_analyzer.analyze(text, chunks)

    def _parse_document_profile(self, raw: str) -> DocumentProfile:
        """解析文档分析 agent 返回的 JSON"""
        return DocumentAnalyzer.parse(raw, self.config)

    def _build_cache_key(self, chunk: TextChunk) -> str:
        """构造缓存键，避免不同模型/术语表之间串用。

        术语表只纳入本块实际注入的子集（与 translation_client 口径一致），
        这样向术语表新增无关术语时不会使全书缓存失效。
        """
        relevant_glossary = select_relevant_glossary(
            self.glossary, chunk.text, chunk.context_text, respect_case=_respect_case(self.config)
        )
        glossary_payload = "\n".join(f"{en}:{zh}" for en, zh in sorted(relevant_glossary.items()))
        payload = "|".join(
            [
                self.prompt_version + ("-ir1" if getattr(chunk, "structured", None) is not None else ""),
                self.config.prompt_fingerprint,
                self.config.model_name or "",
                getattr(self.config, "api_base_url", ""),
                chunk.chunk_id,
                chunk.context_text,
                json.dumps(self.config.get("quality", {}) if hasattr(self.config, "get") else {}, sort_keys=True),
                self.document_profile.fingerprint,
                glossary_payload,
            ]
        ).encode("utf-8")
        return hashlib.sha1(payload).hexdigest()

    async def translate_chunk(self, chunk: TextChunk, progress_callback: Callable | None = None) -> TranslationResult:
        """
        翻译单个文本块

        Args:
            chunk: 结构化文本块
            progress_callback: 进度回调函数

        Returns:
            翻译结果
        """
        try:
            if getattr(chunk, "structured", None) is not None:
                translation, quality_report, repaired, retry, duration = await self.structured_translator.translate(
                    chunk, self.document_profile
                )
            else:
                translation, retry, duration = await self.translation_client.translate(
                    chunk=chunk,
                    document_profile=self.document_profile,
                )
                translation, quality_report, repaired = await self._run_quality_pipeline(
                    chunk=chunk,
                    translation=translation,
                )

            if not translation.strip():
                raise ValueError("模型返回空译文")

            # 缓存是恢复加速层；落盘失败不能丢弃有效译文或重复计入进度。
            try:
                await self.cache.set(
                    cache_key=self._build_cache_key(chunk),
                    chunk_id=chunk.chunk_id,
                    translation=translation,
                    quality_report=quality_report.to_dict(),
                    repaired=repaired,
                )
            except Exception:
                logger.warning("块缓存写入失败: %s", chunk.chunk_id, exc_info=True)

            # 成功回调
            if progress_callback:
                await progress_callback(
                    {
                        "chunk_index": chunk.index,
                        "chunk_id": chunk.chunk_id,
                        "status": "completed",
                        "translation": translation,
                        "duration": duration,
                        "quality_report": quality_report.to_dict(),
                        "repaired": repaired,
                        "cached": False,
                    }
                )

            return TranslationResult(
                chunk_index=chunk.index,
                original=chunk.text,
                translation=translation,
                success=True,
                retry_count=retry,
                duration=duration,
                chunk_id=chunk.chunk_id,
                quality_report=quality_report.to_dict(),
                repaired=repaired,
                cached=False,
            )
        except Exception as e:
            if progress_callback:
                await progress_callback(
                    {"chunk_index": chunk.index, "chunk_id": chunk.chunk_id, "status": "failed", "error": str(e)}
                )

            return TranslationResult(
                chunk_index=chunk.index,
                original=chunk.text,
                translation=f"[翻译失败: {str(e)}]",
                success=False,
                retry_count=self.config.get("api.translator.max_retries", 3),
                chunk_id=chunk.chunk_id,
                quality_report={"passed": False, "issue_count": 1, "issues": []},
            )

    async def _run_quality_pipeline(
        self,
        chunk: TextChunk,
        translation: str,
    ) -> tuple[str, QualityReport, bool]:
        """质量检查与选择性修复"""
        return await self.quality_pipeline.run(
            chunk=chunk,
            translation=translation,
            document_profile=self.document_profile,
        )

    async def _repair_translation(
        self,
        chunk: TextChunk,
        translation: str,
        report: QualityReport,
    ) -> str:
        """对高风险 chunk 触发一次修复"""
        return await self.quality_pipeline.repair(
            chunk=chunk,
            translation=translation,
            report=report,
            document_profile=self.document_profile,
        )

    async def translate_batch(
        self,
        text: str,
        output_path: Path,
        progress_callback: Callable | None = None,
        bilingual: bool = False,
        prepared_chunks: list[TextChunk] | None = None,
    ) -> list[TranslationResult]:
        """
        批量翻译完整文本

        Args:
            text: 完整文本
            output_path: 输出文件路径
            progress_callback: 进度回调
            bilingual: 是否启用双语对照模式

        Returns:
            翻译结果列表
        """
        chunks = prepared_chunks or self.plan_chunks(text)

        if getattr(self, "engine_name", "native") == "langgraph":
            from ..pipelines.translate.graph_engine import LangGraphTranslationOrchestrator

            return await LangGraphTranslationOrchestrator(self).run(
                text=text,
                chunks=chunks,
                output_path=output_path,
                bilingual=bilingual,
                progress_callback=progress_callback,
            )

        # native：手写 asyncio 编排（分析员 → 并发 fan-out → 顺序写盘）
        await self._prepare_document(text, chunks, progress_callback)
        return await self.batch_orchestrator.run(
            chunks=chunks,
            output_path=output_path,
            bilingual=bilingual,
            progress_callback=progress_callback,
            process_chunk=self._process_one_chunk,
            skip_cache_indices=set(getattr(self, "seed_results", {})) | getattr(self, "force_indices", set()),
        )

    async def _prepare_document(
        self,
        text: str,
        chunks: list[TextChunk],
        progress_callback: Callable | None,
    ) -> None:
        """文档级准备：运行分析员、初始化缓存、上报分块完成事件。两引擎共用。"""
        await self.cache.initialize()
        profile_key = (
            hashlib.sha256(
                (text + self.config.prompt_fingerprint + str(self.config.get("multi_agent", {}))).encode()
            ).hexdigest()
            if hasattr(self.config, "prompt_fingerprint")
            else None
        )
        saved = (
            await self.cache.get_profile(profile_key) if profile_key and hasattr(self.cache, "get_profile") else None
        )
        if saved is not None:
            self.document_profile = DocumentProfile(**saved)
        elif hasattr(self, "analyze_document"):
            self.document_profile = await self.analyze_document(text, chunks)
            # 空画像通常是临时 API 错误的降级，不把它永久固定。
            if profile_key and self.document_profile != DocumentProfile.empty():
                await self.cache.set_profile(profile_key, asdict(self.document_profile))
        else:
            self.document_profile = DocumentProfile.empty()

        await self._prepare_term_ledger(text)

        if progress_callback:
            await progress_callback(
                {
                    "event": "split_completed",
                    "total_chunks": len(chunks),
                }
            )

    async def _prepare_term_ledger(self, text: str) -> None:
        """术语账本：为全书反复出现、术语表未覆盖的专名统一译名，并入本次运行的术语表。

        结果按 (源文, 术语表, Prompt 指纹) 缓存，断点续跑时译名不变、块缓存仍然命中。
        用户术语表优先，账本只补充缺失条目；各组件持有同一 dict，原地合并即刻生效。
        """
        self.term_ledger = {}
        getter = getattr(self.config, "get", None)
        if not (getter and getter("multi_agent.enabled", True) and getter("multi_agent.term_ledger.enabled")):
            return
        from ..pipelines.glossary.term_ledger import TermLedgerBuilder

        key = "ledger:" + hashlib.sha256(
            (
                LEDGER_VERSION
                + text
                + json.dumps(self.glossary, ensure_ascii=False, sort_keys=True)
                + str(getattr(self.config, "prompt_fingerprint", ""))
                + str(self.config.get("multi_agent.term_ledger", {}))
            ).encode()
        ).hexdigest()
        saved = await self.cache.get_profile(key) if hasattr(self.cache, "get_profile") else None
        if saved is not None:
            ledger = saved.get("terms", {})
        else:
            ledger = await TermLedgerBuilder(self.llm_analyst, self.config, self.prompt_builder).build(
                text, self.glossary, self.document_profile.summary
            )
            if ledger and hasattr(self.cache, "set_profile"):
                await self.cache.set_profile(key, {"terms": ledger})
        self.term_ledger = {term: zh for term, zh in ledger.items() if term not in self.glossary}
        self.glossary.update(self.term_ledger)
        if self.term_ledger:
            logger.info("术语账本：新增 %s 条统一译名", len(self.term_ledger))

    async def _process_one_chunk(
        self, chunk: TextChunk, output_manager: OutputManager, callback: Callable | None
    ) -> TranslationResult:
        """单块处理(信号量控制)"""
        async with self.semaphore:
            return await self._translate_and_emit(chunk, output_manager, callback)

    async def _translate_and_emit(
        self,
        chunk: TextChunk,
        output_manager: OutputManager,
        progress_callback: Callable | None,
    ) -> TranslationResult:
        """单块完整处理：缓存命中或实时翻译，随后按序写盘。

        与 native 的「batch_orchestrator 缓存分支 + _process_one_chunk 实时分支」
        逐字段等价；langgraph 引擎的每个 Send 分支复用它，保证两引擎产出一致。
        并发上限由调用方控制（native 用 Semaphore，langgraph 用图的 max_concurrency）。
        """
        seed = getattr(self, "seed_results", {}).get(chunk.index)
        if (
            seed is not None
            and seed.original == chunk.text
            and chunk.index not in getattr(self, "force_indices", set())
        ):
            await output_manager.add_result(seed.chunk_index, seed.translation, seed.success, seed.original)
            if progress_callback:
                await progress_callback(
                    {"status": "completed" if seed.success else "failed", "chunk_index": chunk.index, "cached": True}
                )
            return seed
        cache_entry = (
            None
            if chunk.index in getattr(self, "force_indices", set())
            else await self.cache.get(self._build_cache_key(chunk))
        )
        if cache_entry is not None:
            result = build_cached_result(chunk, cache_entry)
            await output_manager.add_result(
                index=result.chunk_index,
                content=result.translation,
                success=True,
                original_text=chunk.text,
            )
            if progress_callback:
                await progress_callback(cached_progress_event(chunk, cache_entry))
            return result

        result = await self.translate_chunk(chunk, progress_callback)
        await output_manager.add_result(
            index=result.chunk_index,
            content=result.translation,
            success=result.success,
            original_text=result.original,
        )
        return result
