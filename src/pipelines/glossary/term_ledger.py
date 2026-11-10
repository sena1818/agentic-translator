"""
术语账本（Term Ledger）

翻译前确定性地抽取全书反复出现、术语表未覆盖的专名，用一次模型调用定下统一译名，
并入本次运行的术语表：之后每块翻译都会注入相关条目、质检也会核对，从而消除
"Parsani → 帕萨尼 / 帕尔萨尼"这类跨块飘忽。用户术语表优先，账本只补充缺失条目。
"""

from __future__ import annotations

import logging

from ...domain.rules.term_candidates import extract_term_candidates
from ..translate.langchain_compat import StrOutputParser
from ..translate.prompt_builder import TranslationPromptBuilder
from .glossary_extractor import GlossaryExtractor

logger = logging.getLogger(__name__)


class TermLedgerBuilder:
    def __init__(self, llm, config, prompt_builder: TranslationPromptBuilder):
        self.llm = llm
        self.config = config
        self.prompt_builder = prompt_builder

    @property
    def max_terms(self) -> int:
        return int(self.config.get("multi_agent.term_ledger.max_terms", 120))

    async def build(self, text: str, glossary: dict[str, str], summary: str = "") -> dict[str, str]:
        """返回 {原文: 统一译名}；无候选或调用失败时返回空表（翻译照常进行）。"""
        candidates = extract_term_candidates(text, glossary, self.max_terms)
        if not candidates:
            return {}
        names = {candidate.term for candidate in candidates}
        related = {
            term: translation
            for term, translation in glossary.items()
            if any(part in names for part in term.split()) or term in names
        }
        prompt = self.prompt_builder.build_term_ledger_prompt()
        try:
            raw = await (prompt | self.llm | StrOutputParser()).ainvoke(
                {
                    "summary": summary or "（无）",
                    "existing_terms": "\n".join(f"- {en}: {zh}" for en, zh in related.items()) or "（无）",
                    "candidates": "\n".join(
                        f"- {candidate.term}（{candidate.count} 次）：…{candidate.context}…" for candidate in candidates
                    ),
                }
            )
        except Exception as exc:
            logger.warning("术语账本生成失败，按原术语表继续翻译: %s", exc)
            return {}
        return self.parse(raw, names)

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
            if term in names and translation:
                ledger[term] = translation
        return ledger


__all__ = ["TermLedgerBuilder", "GlossaryExtractor"]
