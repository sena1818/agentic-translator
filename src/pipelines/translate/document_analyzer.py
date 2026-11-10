"""
文档分析组件
"""

from __future__ import annotations

import json
import logging
import re

from ...core.chunk_planner import TextChunk
from ...domain.models.translation_models import DocumentProfile
from ...domain.rules.document_sampling import sample_document
from ...infrastructure.llm.retry import ainvoke_with_retries
from .langchain_compat import StrOutputParser
from .prompt_builder import TranslationPromptBuilder

logger = logging.getLogger(__name__)


class DocumentAnalyzer:
    """负责文档级分析与结果解析。"""

    def __init__(self, llm_analyst, config, prompt_builder: TranslationPromptBuilder):
        self.llm_analyst = llm_analyst
        self.config = config
        self.prompt_builder = prompt_builder

    async def analyze(self, text: str, chunks: list[TextChunk]) -> DocumentProfile:
        if not getattr(self.config, "multi_agent_enabled", False):
            return DocumentProfile.empty()

        unique_sections = []
        for chunk in chunks:
            title = " > ".join(chunk.section_path) if chunk.section_path else chunk.section_title
            if title and title not in unique_sections:
                unique_sections.append(title)
            if len(unique_sections) >= self.config.analyst_max_sections:
                break

        excerpt = sample_document(text, self.config.analyst_max_chars)
        analyst_prompt = self.prompt_builder.build_analyst_prompt()
        chain = analyst_prompt | self.llm_analyst | StrOutputParser()

        try:
            raw, _ = await ainvoke_with_retries(
                chain,
                {
                    "max_term_hints": getattr(self.config, "analyst_max_term_hints", 12),
                    "sections": "\n".join(f"- {item}" for item in unique_sections) or "- Document Root",
                    "excerpt": excerpt,
                },
                self.config.get("api.translator.max_retries", 3),
            )
            return self.parse(raw, self.config)
        except Exception as exc:
            logger.warning("文档分析 agent 失败，退化为空 profile: %s", exc)
            return DocumentProfile.empty()

    @staticmethod
    def parse(raw: str, config) -> DocumentProfile:
        """解析文档分析 agent 返回的 JSON"""
        try:
            payload_text = raw.strip()
            if not payload_text.startswith("{"):
                match = re.search(r"\{[\s\S]*\}", payload_text)
                if match:
                    payload_text = match.group(0)
            data = json.loads(payload_text)
        except Exception as exc:
            logger.warning("文档分析结果解析失败，退化为空 profile: %s", exc)
            return DocumentProfile.empty()

        return DocumentProfile(
            summary=str(data.get("summary", "")).strip(),
            style_notes=[str(item).strip() for item in data.get("style_notes", []) if str(item).strip()][:4],
            terminology_hints=[str(item).strip() for item in data.get("terminology_hints", []) if str(item).strip()][
                : getattr(config, "analyst_max_term_hints", 12)
            ],
            section_overview=[str(item).strip() for item in data.get("section_overview", []) if str(item).strip()][
                : getattr(config, "analyst_max_sections", 12)
            ],
        )
