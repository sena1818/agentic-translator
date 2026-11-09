"""
翻译质量检查与选择性修复组件
"""

from __future__ import annotations

import logging

from ...core.chunk_planner import TextChunk
from ...core.validator import QualityReport, TranslationValidator
from ...domain.models.translation_models import DocumentProfile
from ...domain.rules.glossary_selection import respect_case as _respect_case
from ...domain.rules.glossary_selection import select_relevant_glossary
from .langchain_compat import StrOutputParser
from .prompt_builder import TranslationPromptBuilder

logger = logging.getLogger(__name__)


class TranslationQualityPipeline:
    """封装 validate -> repair -> revalidate。"""

    def __init__(
        self,
        validator: TranslationValidator,
        config,
        llm_checker,
        prompt_builder: TranslationPromptBuilder,
        rate_limiter,
        clean_output,
        glossary,
    ):
        self.validator = validator
        self.config = config
        self.llm_checker = llm_checker
        self.prompt_builder = prompt_builder
        self.rate_limiter = rate_limiter
        self.clean_output = clean_output
        self.glossary = glossary

    async def run(
        self,
        chunk: TextChunk,
        translation: str,
        document_profile: DocumentProfile,
    ) -> tuple[str, QualityReport, bool]:
        if not self.config.enable_qa_check:
            return translation, QualityReport(passed=True), False

        baseline = self.validator.validate(chunk.text, translation, self.glossary)
        if baseline.passed or not self.validator.should_repair(baseline):
            return translation, baseline, False

        best_translation = translation
        best_report = baseline
        repaired = False

        for _ in range(self.config.max_fix_attempts):
            try:
                candidate_translation = await self.repair(
                    chunk=chunk,
                    translation=best_translation,
                    report=best_report,
                    document_profile=document_profile,
                )
            except Exception as exc:
                logger.warning("审校失败，保留初译并等待人工复核: %s", exc)
                break
            candidate_translation = self.clean_output(candidate_translation)
            candidate_report = self.validator.validate(chunk.text, candidate_translation, self.glossary)

            if self.validator.is_better(candidate_report, best_report):
                best_translation = candidate_translation
                best_report = candidate_report
                repaired = True

            if best_report.passed:
                break

        return best_translation, best_report, repaired

    async def repair(
        self,
        chunk: TextChunk,
        translation: str,
        report: QualityReport,
        document_profile: DocumentProfile,
    ) -> str:
        await self.rate_limiter.acquire()

        issues_text = "\n".join(f"- {issue.message}" for issue in report.issues)
        prompt = self.prompt_builder.build_repair_prompt()
        chain = prompt | self.llm_checker | StrOutputParser()

        relevant_glossary = select_relevant_glossary(
            self.glossary, chunk.text, chunk.context_text, respect_case=_respect_case(self.config)
        )

        return await chain.ainvoke(
            {
                "section_title": " > ".join(chunk.section_path) if chunk.section_path else chunk.section_title,
                "original": chunk.text,
                "translation": translation,
                "issues": issues_text,
                "document_profile": document_profile.to_prompt_text(),
                "glossary": "\n".join([f"- {en}: {zh}" for en, zh in relevant_glossary.items()]),
            }
        )
