"""
术语表引导生成

翻译前先让「术语专家」agent 扫描全文，产出候选术语 + 建议译名清单，
供用户审阅、修改后作为术语表使用。让「自定义术语表」从「必须手写」
变为「一键起草」，是新定位（多领域、自带术语表）的关键一环。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from ...domain.rules.document_sampling import sample_document
from ..translate.langchain_compat import StrOutputParser
from ..translate.prompt_builder import TranslationPromptBuilder

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 16000
DEFAULT_MAX_TERMS = 60


@dataclass
class GlossaryCandidate:
    """单个候选术语。"""

    term: str
    translation: str
    note: str = ""


class GlossaryExtractor:
    """扫描文档，产出候选术语表。"""

    def __init__(self, llm, config, prompt_builder: TranslationPromptBuilder):
        self.llm = llm
        self.config = config
        self.prompt_builder = prompt_builder

    @property
    def max_chars(self) -> int:
        return int(getattr(self.config, "glossary_extraction_max_chars", DEFAULT_MAX_CHARS))

    @property
    def max_terms(self) -> int:
        return int(getattr(self.config, "glossary_extraction_max_terms", DEFAULT_MAX_TERMS))

    async def extract(
        self,
        text: str,
        existing_glossary: dict[str, str] | None = None,
    ) -> list[GlossaryCandidate]:
        """从文档中抽取候选术语。

        Args:
            text: 文档全文（内部会截取前 max_chars 字符采样）
            existing_glossary: 已有术语表，其中的术语会提示模型跳过

        Returns:
            候选术语列表；失败时返回空列表（不抛异常，便于上层降级）。
        """
        existing = existing_glossary or {}
        excerpt = sample_document(text, self.max_chars)
        prompt = self.prompt_builder.build_glossary_extraction_prompt()
        chain = prompt | self.llm | StrOutputParser()

        try:
            raw = await chain.ainvoke(
                {
                    "max_terms": self.max_terms,
                    "existing_terms": "\n".join(f"- {term}" for term in existing) or "（无）",
                    "excerpt": excerpt,
                }
            )
        except Exception as exc:
            logger.warning("术语抽取 agent 调用失败: %s", exc)
            return []

        return self.parse(raw, existing_glossary=existing, max_terms=self.max_terms)

    @staticmethod
    def parse(
        raw: str,
        existing_glossary: dict[str, str] | None = None,
        max_terms: int | None = None,
    ) -> list[GlossaryCandidate]:
        """解析模型返回的 JSON，去重并过滤已有术语。"""
        existing_lower = {term.lower() for term in (existing_glossary or {})}

        try:
            payload_text = raw.strip()
            if not payload_text.startswith("{"):
                match = re.search(r"\{[\s\S]*\}", payload_text)
                if match:
                    payload_text = match.group(0)
            data = json.loads(payload_text)
        except Exception as exc:
            logger.warning("术语抽取结果解析失败: %s", exc)
            return []

        candidates: list[GlossaryCandidate] = []
        seen: set[str] = set()
        for item in data.get("terms", []):
            if not isinstance(item, dict):
                continue
            term = str(item.get("term", "")).strip()
            translation = str(item.get("translation", "")).strip()
            if not term or not translation:
                continue
            key = term.lower()
            if key in existing_lower or key in seen:
                continue
            seen.add(key)
            candidates.append(
                GlossaryCandidate(
                    term=term,
                    translation=translation,
                    note=str(item.get("note", "")).strip(),
                )
            )

        if max_terms is not None:
            candidates = candidates[:max_terms]
        return candidates


def candidates_to_glossary(candidates: list[GlossaryCandidate]) -> dict[str, str]:
    """把候选术语转成术语表 dict（英文术语 -> 中文译名）。"""
    return {candidate.term: candidate.translation for candidate in candidates}
