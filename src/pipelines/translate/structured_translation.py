"""
结构化块翻译（Document IR 模式）

一次 LLM 调用翻译一个块内的所有段：输入/输出都是 `<seg id="N">…</seg>` 序列，
段内结构以行内标签表示。随后逐段做确定性的结构校验 + 规则质检，
只把不合格的段交给审校员定点修复；仍不合格的段回退为原文并标记，绝不丢结构。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from ...core.validator import QualityIssue, QualityReport, TranslationValidator
from ...domain.document import document_ir, inline_codec
from ...domain.models.translation_models import DocumentProfile
from ...domain.rules.chunk_planning import TextChunk
from ...domain.rules.glossary_selection import respect_case as _respect_case
from ...domain.rules.glossary_selection import select_relevant_glossary
from ...infrastructure.document import pandoc
from ...infrastructure.llm.rate_limits import QuotaExhaustedError, classify
from .langchain_compat import StrOutputParser
from .prompt_builder import TranslationPromptBuilder

logger = logging.getLogger(__name__)

_SEG_RE = re.compile(r'<seg\s+id="?(\d+)"?\s*>(.*?)</seg>', re.DOTALL)
_SEVERE = {"high", "medium"}


def parse_segments(raw: str) -> dict[int, str]:
    """解析 LLM 返回的 seg 序列；同一 id 出现多次时取第一次。"""
    found: dict[int, str] = {}
    for match in _SEG_RE.finditer(raw):
        found.setdefault(int(match.group(1)), match.group(2).strip())
    return found


@dataclass
class SegmentOutcome:
    tagged: str | None
    inlines: list[dict] | None
    issues: list[QualityIssue]

    @property
    def needs_repair(self) -> bool:
        return any(issue.severity in _SEVERE for issue in self.issues)


class SegmentValidator:
    """段级质检：结构骨架（确定性）+ 复用规则质检（术语、残留英文、数字……）。"""

    def __init__(self, validator: TranslationValidator, glossary: dict[str, str]):
        self.validator = validator
        self.glossary = glossary

    def check(self, segment: document_ir.Segment, source_inlines: list[dict], tagged: str | None) -> SegmentOutcome:
        if tagged is None:
            return SegmentOutcome(None, None, [QualityIssue("missing", "high", "模型输出缺少该段", segment.seg_id)])
        decoded = inline_codec.decode(tagged, source_inlines)
        # 模型自创的标签（常见：给术语加 <b1>）与多余闭合标签在解码时已被丢弃，文字不受影响，
        # 只作轻微提示、不触发修复；真正写错的原子标签会在下面的骨架比对中报"缺少公式"等。
        # 未闭合标签会让格式延续到段尾，仍需修复。
        issues = [
            QualityIssue(
                "structure", "medium" if "未闭合" in problem else "low", f"标签错误: {problem}", segment.seg_id
            )
            for problem in decoded.problems
        ]
        severe, minor = inline_codec.compare_skeletons(source_inlines, decoded.inlines)
        issues += [QualityIssue("structure", "high", message, segment.seg_id) for message in severe]
        issues += [QualityIssue("format", "low", message, segment.seg_id) for message in minor]
        report = self.validator.validate(segment.plain, inline_codec.plain_text(decoded.inlines), self.glossary)
        for issue in report.issues:
            issue.segment = segment.seg_id
        issues += report.issues
        return SegmentOutcome(tagged, decoded.inlines, issues)


class StructuredChunkTranslator:
    """结构化模式下的块翻译 + 段级质检 + 定点修复。"""

    def __init__(
        self,
        llm_translator,
        llm_checker,
        config,
        prompt_builder: TranslationPromptBuilder,
        rate_limiter,
        clean_output,
        glossary: dict[str, str],
        validator: TranslationValidator,
    ):
        self.llm_translator = llm_translator
        self.llm_checker = llm_checker
        self.config = config
        self.prompt_builder = prompt_builder
        self.rate_limiter = rate_limiter
        self.clean_output = clean_output
        self.glossary = glossary
        self.segment_validator = SegmentValidator(validator, glossary)

    async def translate(
        self, chunk: TextChunk, document_profile: DocumentProfile
    ) -> tuple[str, QualityReport, bool, int, float]:
        """返回 (块译文 Markdown, 质检报告, 是否修复过, 重试次数, 耗时)。"""
        structured: document_ir.StructuredChunk = chunk.structured
        if not structured.segments:
            return chunk.text, QualityReport(passed=True), False, 0, 0.0

        glossary_text = self._glossary_text(chunk)
        common = {
            "document_profile": document_profile.to_prompt_text(),
            "glossary": glossary_text,
            "section_title": " > ".join(chunk.section_path) if chunk.section_path else chunk.section_title,
        }
        prompt = self.prompt_builder.build_structured_translation_prompt()
        raw, retry, duration = await self._invoke(
            prompt | self.llm_translator | StrOutputParser(),
            {**common, "context": chunk.context_text or "无", "segments": structured.tagged_payload()},
        )
        outputs = parse_segments(raw)
        if not outputs:
            raise ValueError("模型未按 <seg> 协议输出")

        candidates = list(document_ir.iter_candidates(structured.blocks))
        outcomes = {
            seg.local_id: self._check(seg, candidates, outputs.get(seg.local_id)) for seg in structured.segments
        }

        repaired = False
        if getattr(self.config, "enable_qa_check", True):
            for _ in range(self.config.max_fix_attempts):
                failing = [seg for seg in structured.segments if outcomes[seg.local_id].needs_repair]
                if not failing:
                    break
                try:
                    fixes = await self._repair(failing, outcomes, common)
                except Exception as exc:  # 审校失败不影响初译
                    logger.warning("段级审校失败，保留初译并等待人工复核: %s", exc)
                    break
                for seg in failing:
                    if seg.local_id not in fixes:
                        continue
                    candidate = self._check(seg, candidates, fixes[seg.local_id])
                    if _score(candidate.issues) < _score(outcomes[seg.local_id].issues):
                        outcomes[seg.local_id] = candidate
                        repaired = True

        # 传带标签文本而非已解码节点：translate_blocks 逆序回填，段落里的 <fn/> 才能取到已译脚注
        replacements: dict[int, str] = {}
        issues: list[QualityIssue] = []
        for seg in structured.segments:
            outcome = outcomes[seg.local_id]
            unusable = outcome.inlines is None or any(
                issue.kind in {"structure", "missing"} and issue.severity == "high" for issue in outcome.issues
            )
            if unusable:
                issues += [issue for issue in outcome.issues if issue.kind in {"structure", "missing"}]
                issues.append(QualityIssue("structure", "high", "结构校验未通过，该段保留原文待人工处理", seg.seg_id))
                continue
            replacements[seg.candidate_index] = outcome.tagged
            issues += outcome.issues

        translated_blocks = document_ir.translate_blocks(structured.blocks, replacements)
        markdown = await asyncio.to_thread(
            pandoc.blocks_to_markdown, {"pandoc-api-version": structured.pandoc_api}, translated_blocks
        )
        report = QualityReport(
            passed=not any(issue.severity in _SEVERE for issue in issues),
            issues=[_prefixed(issue, structured) for issue in issues],
        )
        return markdown, report, repaired, retry, duration

    def _check(self, seg, candidates, tagged):
        if tagged is not None:
            tagged = self.clean_output(tagged)
        return self.segment_validator.check(seg, candidates[seg.candidate_index], tagged)

    def _glossary_text(self, chunk: TextChunk) -> str:
        relevant = select_relevant_glossary(
            self.glossary, chunk.text, chunk.context_text, respect_case=_respect_case(self.config)
        )
        return "\n".join(f"- {en}: {zh}" for en, zh in relevant.items()) or "无"

    async def _repair(self, failing, outcomes, common) -> dict[int, str]:
        parts = []
        for seg in failing:
            outcome = outcomes[seg.local_id]
            problems = "；".join(issue.message for issue in outcome.issues if issue.severity != "low")
            parts.append(
                f"【段 {seg.local_id}】\n原文：{seg.tagged}\n当前译文：{outcome.tagged or '（缺失）'}\n问题：{problems}"
            )
        prompt = self.prompt_builder.build_structured_repair_prompt()
        raw, _, _ = await self._invoke(
            prompt | self.llm_checker | StrOutputParser(), {**common, "segments": "\n".join(parts)}
        )
        return {
            local_id: re.sub(r"^(?:修正后的?译文|当前译文|译文)[:：]\s*", "", text)
            for local_id, text in parse_segments(raw).items()
        }

    async def _invoke(self, chain, payload) -> tuple[str, int, float]:
        max_retries = self.config.get("api.translator.max_retries", 3)
        for retry in range(max_retries + 1):
            try:
                await self.rate_limiter.acquire()
                start = time.monotonic()
                raw = await chain.ainvoke(payload)
                return raw, retry, time.monotonic() - start
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
                if retry >= max_retries:
                    raise
                await asyncio.sleep(2**retry)
        raise RuntimeError("unreachable")


def _score(issues: list[QualityIssue]) -> int:
    weights = {"high": 100, "medium": 10, "low": 1}
    return sum(weights.get(issue.severity, 1) for issue in issues)


def _prefixed(issue: QualityIssue, structured: document_ir.StructuredChunk) -> QualityIssue:
    if issue.segment is None or issue.message.startswith("[段"):
        return issue
    local = issue.segment - structured.first_seg_id + 1
    return QualityIssue(issue.kind, issue.severity, f"[段 {local}] {issue.message}", issue.segment)
