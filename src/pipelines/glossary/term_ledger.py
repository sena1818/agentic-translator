"""
术语账本（Term Ledger）

翻译前确定性地抽取全书反复出现、术语表未覆盖的候选术语，用一次（或分批的）模型调用定下统一译名，
并入本次运行的术语表：之后每块翻译都会注入相关条目、质检也会核对，从而消除
"Parsani → 帕萨尼 / 帕尔萨尼"这类跨块飘忽。用户术语表优先，账本只补充缺失条目。

候选来自三路（见 domain/rules/term_sources）：大写专名、关键度（作者自造的小写概念词）、
文档结构（强调、引号、标题）。每个候选附全书多处上下文和确定性歧义信号；
defer_ambiguous 打开时，有歧义的候选不进账本，留给术语 agent 查证。
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from ...domain.index.book_index import BookIndex
from ...domain.rules.term_candidates import TermCandidate
from ...domain.rules.term_sources import (
    ALL_SOURCES,
    AMBIGUITY_LABELS,
    PROPER,
    SOURCE_LABELS,
    SourceConfig,
    gather_candidates,
    paragraph_index,
    structure_cues,
)
from ...infrastructure.llm.retry import ainvoke_with_retries
from ..translate.langchain_compat import StrOutputParser
from ..translate.prompt_builder import TranslationPromptBuilder
from .glossary_extractor import GlossaryExtractor

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """无用量回报时的粗估：汉字约 1 token/字，其余约 4 字符/token。"""
    cjk = sum(1 for char in text if "一" <= char <= "鿿")
    return cjk + (len(text) - cjk + 3) // 4


@dataclass
class LedgerUsage:
    calls: int = 0
    prompt_chars: int = 0
    estimated_prompt_tokens: int = 0
    candidates: int = 0
    deferred: int = 0
    by_source: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_chars": self.prompt_chars,
            "estimated_prompt_tokens": self.estimated_prompt_tokens,
            "candidates": self.candidates,
            "deferred": self.deferred,
            "by_source": self.by_source,
        }


def prepare_sources(text: str, chunks: Sequence | None = None) -> tuple[BookIndex, Counter | None]:
    """候选抽取所需的全书索引与结构线索。

    有 Document IR 分块时直接用它（与全书索引同一套段，结构线索从 AST 读，不再调 pandoc）；
    否则按空行拆段，结构线索在 pandoc 可用时解析一次全文得到，不可用时跳过这一路。
    """
    structured = [getattr(chunk, "structured", None) for chunk in chunks or []]
    if structured and all(item is not None for item in structured):
        from ..structure.indexing import source_segments

        blocks = [block for item in structured for block in item.blocks]
        return BookIndex(source_segments(structured)), structure_cues(blocks)
    index = paragraph_index(text)
    try:
        from ...infrastructure.document import pandoc

        if pandoc.pandoc_available():
            return index, structure_cues(pandoc.markdown_to_ast(text)["blocks"])
    except Exception:  # 结构线索只是补充来源，解析失败不影响其他两路
        logger.warning("术语候选：解析文档结构失败，跳过强调/引号/标题线索", exc_info=True)
    return index, None


LEGACY_SOURCES = (PROPER,)
LEGACY_MAX_TERMS = 120
LEGACY_BATCH_SIZE = 120


def source_config(config) -> SourceConfig:
    """读取术语候选配置；没写的项取旧版行为（只抽专名、120 条、每个候选 1 处上下文）。

    多路候选会显著增加账本调用的 token，只在配置里明确开启时使用（见 docs/cost-switches.md）。
    """
    getter = getattr(config, "get", None) or (lambda _key, default=None: default)
    sources = getter("multi_agent.term_ledger.sources", None) or LEGACY_SOURCES
    defaults = SourceConfig(max_terms=LEGACY_MAX_TERMS, contexts=1)
    return SourceConfig(
        max_proper=int(getter("multi_agent.term_ledger.max_proper", defaults.max_proper)),
        max_keyness=int(getter("multi_agent.term_ledger.max_keyness", defaults.max_keyness)),
        max_structure=int(getter("multi_agent.term_ledger.max_structure", defaults.max_structure)),
        max_terms=int(getter("multi_agent.term_ledger.max_terms", defaults.max_terms)),
        contexts=int(getter("multi_agent.term_ledger.contexts", defaults.contexts)),
        sources=tuple(source for source in ALL_SOURCES if source in sources),
    )


def format_candidate(candidate: TermCandidate) -> str:
    sources = "、".join(SOURCE_LABELS.get(source, source) for source in candidate.sources)
    head = f"- {candidate.term}（{candidate.count} 次；来源：{sources}"
    if candidate.ambiguity:
        head += "；可能有歧义：" + "、".join(AMBIGUITY_LABELS.get(signal, signal) for signal in candidate.ambiguity)
    head += "）"
    contexts = candidate.contexts or [candidate.context]
    return "\n".join([head, *(f"    {i}. {context}" for i, context in enumerate(contexts, 1))])


class TermLedgerBuilder:
    def __init__(self, llm, config, prompt_builder: TranslationPromptBuilder):
        self.llm = llm
        self.config = config
        self.prompt_builder = prompt_builder
        self.candidates: list[TermCandidate] = []
        self.deferred: list[TermCandidate] = []  # 有歧义、留给术语 agent 的候选
        self.reading_terms: list[tuple[str, int]] | None = None  # 阅读遍记下的概念（开启阅读遍时由引擎设置）
        self.usage = LedgerUsage()

    def _get(self, key: str, default=None):
        getter = getattr(self.config, "get", None)
        return getter(key, default) if getter else default

    @property
    def max_terms(self) -> int:
        return source_config(self.config).max_terms

    @property
    def batch_size(self) -> int:
        return max(1, int(self._get("multi_agent.term_ledger.batch_size", LEGACY_BATCH_SIZE)))

    def collect(self, text: str, glossary: dict[str, str], chunks: Sequence | None = None) -> list[TermCandidate]:
        index, cues = prepare_sources(text, chunks)
        return gather_candidates(
            index, glossary, text=text, cues=cues, config=source_config(self.config), reading=self.reading_terms
        )

    async def build(
        self, text: str, glossary: dict[str, str], summary: str = "", chunks: Sequence | None = None
    ) -> dict[str, str]:
        """返回 {原文: 统一译名}；无候选或调用失败时返回空表（翻译照常进行）。"""
        candidates = self.collect(text, glossary, chunks)
        self.candidates = candidates
        if self._get("multi_agent.term_ledger.defer_ambiguous", False):
            self.deferred = [candidate for candidate in candidates if candidate.ambiguous]
            candidates = [candidate for candidate in candidates if not candidate.ambiguous]
        self.usage = LedgerUsage(
            candidates=len(self.candidates),
            deferred=len(self.deferred),
            by_source=dict(Counter(source for candidate in self.candidates for source in candidate.sources)),
        )
        if not candidates:
            return {}
        prompt = self.prompt_builder.build_term_ledger_prompt()
        ledger: dict[str, str] = {}
        for start in range(0, len(candidates), self.batch_size):
            batch = candidates[start : start + self.batch_size]
            names = {candidate.term for candidate in batch}
            related = {
                term: translation
                for term, translation in {**glossary, **ledger}.items()
                if term in names or any(part in names for part in term.split())
            }
            payload = {
                "summary": summary or "（无）",
                "existing_terms": "\n".join(f"- {en}: {zh}" for en, zh in related.items()) or "（无）",
                "candidates": "\n".join(format_candidate(candidate) for candidate in batch),
            }
            rendered = prompt.format(**payload)
            self.usage.calls += 1
            self.usage.prompt_chars += len(rendered)
            self.usage.estimated_prompt_tokens += estimate_tokens(rendered)
            try:
                raw, _ = await ainvoke_with_retries(
                    prompt | self.llm | StrOutputParser(), payload, self._get("api.translator.max_retries", 3)
                )
            except Exception as exc:
                logger.warning("术语账本第 %s 批生成失败，这一批按原术语表继续: %s", start // self.batch_size + 1, exc)
                continue
            for term, translation in self.parse(raw, names).items():
                if term.lower() not in {key.lower() for key in ledger}:
                    ledger[term] = translation
        logger.info(
            "术语账本：候选 %s 条（%s），留给术语 agent %s 条，调用 %s 次，提示约 %s token",
            self.usage.candidates,
            self.usage.by_source,
            self.usage.deferred,
            self.usage.calls,
            self.usage.estimated_prompt_tokens,
        )
        return ledger

    @staticmethod
    def parse(raw: str, names: set[str]) -> dict[str, str]:
        import json
        import re

        try:
            payload = raw.strip()
            if not payload.startswith("{"):
                match = re.search(r"\{[\s\S]*\}", payload)
                payload = match.group(0) if match else payload
            data = json.loads(payload)
        except Exception as exc:
            logger.warning("术语账本解析失败: %s", exc)
            return {}
        ledger: dict[str, str] = {}
        for item in data.get("terms", []):
            if not isinstance(item, dict) or item.get("keep") is False:
                continue
            term = str(item.get("term", "")).strip()
            translation = str(item.get("translation", "")).strip()
            if term in names and translation and term.lower() not in {key.lower() for key in ledger}:
                ledger[term] = translation  # 大小写变体（AxSys / Axsys）只保留第一条
        return ledger


__all__ = ["TermLedgerBuilder", "GlossaryExtractor", "prepare_sources", "format_candidate", "LedgerUsage"]
