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
from ...domain.memory.corrections import suggestions_prompt
from ...domain.models.translation_models import DocumentProfile
from ...domain.rules.chunk_planning import TextChunk
from ...domain.rules.glossary_selection import respect_case as _respect_case
from ...domain.rules.glossary_selection import select_relevant_glossary
from ...infrastructure.document import pandoc
from ...infrastructure.llm.retry import ainvoke_with_retries
from .langchain_compat import StrOutputParser
from .prompt_builder import TranslationPromptBuilder
from .semantic_checker import SegmentPair, findings_to_issues
from .typed_repair import RepairStats, TypedRepairer

logger = logging.getLogger(__name__)

_SEG_RE = re.compile(r'<seg\s+id="?(\d+)"?\s*>(.*?)</seg>', re.DOTALL)
_SEVERE = {"high", "medium"}
_UNUSABLE_KINDS = {"structure", "missing", "truncated"}
_UNCLOSED_TAIL_RE = re.compile(r'<seg\s+id="?\d+"?\s*>(?:(?!</seg>).)*(?:(?!</seg>)\S)(?:(?!</seg>).)*$', re.DOTALL)
_LENGTH_STOPS = {"length", "max_tokens", "MAX_TOKENS"}
_MAX_ROUNDS = 3


def response_text(result) -> tuple[str, str | None]:
    """取出模型回复的文本与结束原因（OpenAI 的 finish_reason、Anthropic 的 stop_reason、Gemini 的 MAX_TOKENS）。"""
    if isinstance(result, str):
        return result, None
    text = StrOutputParser().invoke(result)
    metadata = getattr(result, "response_metadata", None) or {}
    reason = metadata.get("finish_reason") or metadata.get("stop_reason")
    return text, str(reason) if reason is not None else None


def looks_truncated(raw: str, finish_reason: str | None) -> bool:
    """服务端报告达到输出上限，或输出以一个没有闭合的 <seg> 结尾。"""
    return finish_reason in _LENGTH_STOPS or bool(_UNCLOSED_TAIL_RE.search(raw))


def protocol_feedback(raw: str) -> str:
    """模型没按 <seg> 协议输出时，告诉它上次错在哪里，而不是原样重发。"""
    stripped = raw.strip()
    if not stripped:
        problem = "上次回复是空的"
    elif "<seg" not in stripped:
        excerpt = stripped[:80].replace("\n", " ")
        problem = f'上次回复没有用 <seg id="N">…</seg> 包裹译文（开头是：{excerpt}）'
    else:
        problem = "上次回复里的 <seg> 标签写法无法解析"
    return (
        f'\n【输出格式提醒】{problem}。这一次请只输出 <seg id="N">译文</seg> 序列：'
        "每个待翻译段落对应一个，id 与原文相同，不要输出解释、Markdown 代码块或其他内容。\n"
    )


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
        translated_plain = inline_codec.plain_text(decoded.inlines)
        report = self.validator.validate(segment.plain, translated_plain, self.glossary)
        residue = _residual_english_words(translated_plain, self.glossary)
        if residue:
            severity = "medium" if _HAS_WORDFREQ else "low"
            issues.append(
                QualityIssue("untranslated", severity, "残留英文单词: " + ", ".join(residue[:6]), segment.seg_id)
            )
        if self._left_untranslated(segment.plain, translated_plain):
            issues.append(QualityIssue("untranslated", "medium", f"该段未翻译: {segment.plain[:60]}", segment.seg_id))
        for issue in report.issues:
            issue.segment = segment.seg_id
        issues += report.issues
        return SegmentOutcome(tagged, decoded.inlines, issues)

    def _left_untranslated(self, source: str, translation: str) -> bool:
        """短段落被原样照抄（如标题 "Part-2"）——长段落由残留英文检查覆盖。

        以下保留原文是正当的，不算未翻译：网址/邮箱/文件名、参考文献条目、
        全部由大写开头专名组成的段（Kulili、Leamington Spa）、术语表中保留原文的条目与缩写。
        """
        source = source.strip()
        if translation.strip() != source or re.search(r"[\u3400-\u9fff]", source):
            return False
        if _STRUCTURAL_LABEL.match(source):
            return True
        if re.search(r"@|https?://|www\.|\w\.(?:com|net|org|edu|html?)\b", source) or (
            " " not in source and re.search(r"[_./]", source)
        ):
            return False
        if "," in source and re.search(r"\b(?:1[5-9]|20)\d{2}\b", source):  # 参考文献
            return False
        kept = {
            token.lower()
            for term, zh in self.glossary.items()
            if zh.split(" (", 1)[0].strip().lower() == term.lower()
            for token in re.findall(r"[A-Za-z]+", term)
        }
        words = re.findall(r"[A-Za-z][A-Za-z'’]{2,}", source)
        return any(word[0].islower() and word.lower() not in kept for word in words)


try:  # 可选依赖：区分常用英语词与自造词/生僻词
    from wordfreq import zipf_frequency

    _HAS_WORDFREQ = True
except ImportError:  # pragma: no cover - 未安装时只作轻微提示
    _HAS_WORDFREQ = False

_RESIDUE_EXCLUDED = re.compile(r"[（(][^（）()]*[）)]|“[^”]*”|‘[^’]*’|\"[^\"]*\"|《[^》]*》|「[^」]*」")


def _residual_english_words(translation: str, glossary: dict[str, str]) -> list[str]:
    """中文为主的译文里夹着未翻译的常用英语单词（实测 DeepSeek 关闭思考时较常见："一部 dated 1987年的文本"）。

    括注、引号、书名号内的英文是有意保留，不算；只认小写开头的词（专名首字母大写）；
    有 wordfreq 时只报常用英语词（zipf ≥ 3），书中自造词（dopesh、eiaoung）与生僻词不报。
    """
    if len(re.findall(r"[\u4e00-\u9fff]", translation)) < 10:
        return []
    kept = {term.lower() for term, zh in glossary.items() if zh.split(" (", 1)[0].strip().lower() == term.lower()}
    stripped = _RESIDUE_EXCLUDED.sub(" ", translation)
    words = re.findall(r"(?<![A-Za-z\-\[\]])[a-z]{3,}(?![A-Za-z\-\[\]])", stripped)
    found: list[str] = []
    for word in words:
        if word in kept or word in found:
            continue
        if _HAS_WORDFREQ and zipf_frequency(word, "en") < 3.0:
            continue
        found.append(word)
    return found


_STRUCTURAL_LABEL = re.compile(r"^(?:Part|Chapter|Section|Appendix|Book|Volume|Vol\.)[\s\-]*[\dIVXLC]+\b", re.I)


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
        semantic_checker=None,
    ):
        self.llm_translator = llm_translator
        self.llm_checker = llm_checker
        self.config = config
        self.prompt_builder = prompt_builder
        self.rate_limiter = rate_limiter
        self.clean_output = clean_output
        self.glossary = glossary
        self.segment_validator = SegmentValidator(validator, glossary)
        # 语义质检（quality.semantic_check）：None 表示关闭，行为与未接入时一致
        self.semantic_checker = semantic_checker
        self.repairer = TypedRepairer(llm_checker, prompt_builder, self._invoke)
        self.repair_stats = RepairStats()  # 本次运行累计，供评测与日志对比

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
        outputs, truncated, retry, duration = await self._translate_segments(chunk, structured, common)

        candidates = list(document_ir.iter_candidates(structured.blocks))
        outcomes = {
            seg.local_id: self._check(seg, candidates, outputs.get(seg.local_id)) for seg in structured.segments
        }
        for local_id in truncated:
            outcomes[local_id] = SegmentOutcome(
                None,
                None,
                [
                    QualityIssue(
                        "truncated", "high", "模型输出达到长度上限被截断，该段未译出", _seg_id(structured, local_id)
                    )
                ],
            )

        repaired = False
        rejections: dict[int, list[str]] = {}
        if getattr(self.config, "enable_qa_check", True):
            result = await self.repairer.repair(
                structured.segments,
                outcomes,
                candidates,
                lambda seg, tagged: self._check(seg, candidates, tagged),
                common,
                self.config.max_fix_attempts,
            )
            repaired, rejections = result.repaired, result.rejections
            self.repair_stats.merge(result.stats)

        # 传带标签文本而非已解码节点：translate_blocks 逆序回填，段落里的 <fn/> 才能取到已译脚注
        replacements: dict[int, str] = {}
        issues: list[QualityIssue] = []
        for seg in structured.segments:
            outcome = outcomes[seg.local_id]
            # 修复被拒绝、问题仍在的段：写明拒绝原因，标记待人工处理
            notes = [QualityIssue("repair", "low", note, seg.seg_id) for note in rejections.get(seg.local_id, [])]
            unusable = outcome.inlines is None or any(
                issue.kind in _UNUSABLE_KINDS and issue.severity == "high" for issue in outcome.issues
            )
            if unusable:
                issues += [issue for issue in outcome.issues if issue.kind in _UNUSABLE_KINDS]
                issues.append(QualityIssue("structure", "high", "结构校验未通过，该段保留原文待人工处理", seg.seg_id))
                issues += notes
                continue
            replacements[seg.candidate_index] = outcome.tagged
            issues += outcome.issues
            if outcome.needs_repair:
                issues += notes
        if self.semantic_checker is not None:
            issues += await self._semantic_issues(structured, outcomes, replacements)

        translated_blocks = document_ir.translate_blocks(structured.blocks, replacements)
        markdown = await asyncio.to_thread(
            pandoc.blocks_to_markdown, {"pandoc-api-version": structured.pandoc_api}, translated_blocks
        )
        report = QualityReport(
            # 语义提示只报告、不决定是否通过（它不是确定性结论，交给人工或审校 agent 分诊）
            passed=not any(issue.severity in _SEVERE and issue.kind != "semantic" for issue in issues),
            issues=[_prefixed(issue, structured) for issue in issues],
        )
        return markdown, report, repaired, retry, duration

    async def _translate_segments(self, chunk, structured, common) -> tuple[dict[int, str], set[int], int, float]:
        """初译整块；返回 (各段输出, 因截断而缺失的段, 重试次数, 耗时)。

        - 模型不按 <seg> 协议输出（全书实测 185 块中 1 块）：带上错误说明再请求一次，仍失败才判块失败。
        - 输出被长度上限截断：日志写明是截断，只对缺失的段补译，最多补到第 _MAX_ROUNDS 轮。
        """
        prompt = self.prompt_builder.build_structured_translation_prompt()
        outputs: dict[int, str] = {}
        note = ""
        protocol_retried = False
        truncated_ids: set[int] = set()
        retries, duration = 0, 0.0
        raw = ""
        for _ in range(_MAX_ROUNDS):
            pending = [seg for seg in structured.segments if seg.local_id not in outputs]
            payload = {
                **common,
                "context": chunk.context_text or "无",
                "retry_note": note,
                "segments": "\n".join(f'<seg id="{seg.local_id}">{seg.tagged}</seg>' for seg in pending),
            }
            result, retry, elapsed = await self._invoke(prompt | self.llm_translator, payload)
            retries += retry
            duration += elapsed
            raw, finish_reason = response_text(result)
            pending_ids = {seg.local_id for seg in pending}
            got = {local_id: text for local_id, text in parse_segments(raw).items() if local_id in pending_ids}
            outputs.update(got)
            missing = pending_ids - got.keys()
            if not missing:
                truncated_ids = set()
                break
            if looks_truncated(raw, finish_reason):
                truncated_ids = missing
                logger.warning(
                    "块 %s 输出被截断（finish_reason=%s，%d 字符），缺 %d 段，只补译缺失段",
                    chunk.chunk_id,
                    finish_reason,
                    len(raw),
                    len(missing),
                )
                if not got:
                    break  # 第一段本身就超出输出上限，原样补译不会有进展
                note = ""
                continue
            truncated_ids = set()
            if got or protocol_retried:
                break  # 只缺个别段且不是截断：交给段级质检报"缺失"并修复
            protocol_retried = True
            note = protocol_feedback(raw)
            logger.warning("块 %s 未按 <seg> 协议输出，带说明重试一次；输出开头: %s", chunk.chunk_id, raw.strip()[:120])

        if not outputs:
            if truncated_ids:
                raise ValueError(
                    f"模型输出被长度上限截断，未得到任何完整段（共 {len(raw)} 字符），可调大输出上限或减小块大小"
                )
            if not raw.strip():
                raise ValueError(
                    "模型返回的正文为空（已重试；推理模型可能把输出上限全部耗在思考上，可调低 api.reasoning_effort，如 none）"
                )
            excerpt = raw.strip()[:200].replace("\n", " ")
            raise ValueError(f"模型未按 <seg> 协议输出（已重试；共 {len(raw)} 字符，开头：{excerpt}）")
        return outputs, truncated_ids, retries, duration

    async def _semantic_issues(self, structured, outcomes, replacements) -> list[QualityIssue]:
        """对最终采用的译文做语义检查（在段级修复之后，只报告不修复）。"""
        checked = [
            seg
            for seg in structured.segments
            if seg.candidate_index in replacements and outcomes[seg.local_id].inlines is not None
        ]
        if not checked:
            return []
        # 送模型用块内段号（与翻译提示一致，从 1 开始），报告里换回全书段号
        pairs = [
            SegmentPair(seg.local_id, seg.plain, inline_codec.plain_text(outcomes[seg.local_id].inlines))
            for seg in checked
        ]
        try:
            result = await self.semantic_checker.check(pairs)
        except Exception as exc:  # 语义检查失败不影响译文
            logger.warning("语义检查失败，跳过: %s", exc)
            return []
        issues = []
        for seg in checked:
            issues += findings_to_issues(result.findings.get(seg.local_id, []), seg.seg_id)
        return issues

    def _check(self, seg, candidates, tagged):
        if tagged is not None:
            tagged = self.clean_output(tagged)
        return self.segment_validator.check(seg, candidates[seg.candidate_index], tagged)

    def _glossary_text(self, chunk: TextChunk) -> str:
        relevant = select_relevant_glossary(
            self.glossary, chunk.text, chunk.context_text, respect_case=_respect_case(self.config)
        )
        text = "\n".join(f"- {en}: {zh}" for en, zh in relevant.items()) or "无"
        # 跨书术语建议（同作者、同领域的其他书）：只进提示，不进 self.glossary，因此不参与术语一致性校验
        suggestions = getattr(self, "term_suggestions", None)
        if suggestions:
            hinted = select_relevant_glossary(
                {s.term: s.rendering for s in suggestions},
                chunk.text,
                chunk.context_text,
                respect_case=_respect_case(self.config),
            )
            text += suggestions_prompt(suggestions, hinted)
        return text

    async def _invoke(self, chain, payload) -> tuple[str, int, float]:
        started = {"at": time.monotonic()}

        async def before_attempt():
            await self.rate_limiter.acquire()
            started["at"] = time.monotonic()

        raw, retry = await ainvoke_with_retries(
            chain, payload, self.config.get("api.translator.max_retries", 3), before_attempt
        )
        return raw, retry, time.monotonic() - started["at"]


def _seg_id(structured: document_ir.StructuredChunk, local_id: int) -> int:
    return next(seg.seg_id for seg in structured.segments if seg.local_id == local_id)


def _prefixed(issue: QualityIssue, structured: document_ir.StructuredChunk) -> QualityIssue:
    if issue.segment is None or issue.message.startswith("[段"):
        return issue
    local = issue.segment - structured.first_seg_id + 1
    return QualityIssue(issue.kind, issue.severity, f"[段 {local}] {issue.message}", issue.segment)
