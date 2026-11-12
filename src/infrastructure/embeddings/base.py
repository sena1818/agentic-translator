"""嵌入器接口与按配置构造。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

NOT_CONFIGURED_HINT = (
    "未配置向量模型，当前为仅关键词检索（按词语重叠排序，改写较多的句子可能找不到）；"
    "在 config.yaml 的 semantic_search.embedder 配置向量模型后可按意思检索。"
)


class Embedder(Protocol):
    """把文本变成向量。name 写入向量缓存，换模型时旧向量作废。"""

    name: str
    languages: frozenset[str]  # 能处理的语言：{"en"} 或 {"en", "zh"}

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class EmbedderSetup:
    embedder: Embedder | None
    hint: str  # embedder 为 None 时说明原因，供界面与 MCP 显示


def build_embedder(config) -> EmbedderSetup:
    """按 semantic_search 配置构造嵌入器；未配置或构造失败时返回 None 并附说明，不抛错。"""
    kind = (config.get("semantic_search.embedder") or "none").lower()
    if kind == "none":
        return EmbedderSetup(None, NOT_CONFIGURED_HINT)
    model = config.get("semantic_search.model")
    languages = frozenset(config.get("semantic_search.languages") or (["en", "zh"] if kind == "openai" else ["en"]))
    try:
        if kind == "openai":
            from .openai_compatible import OpenAICompatibleEmbedder

            key_env = config.get("semantic_search.api_key_env") or "OPENAI_API_KEY"
            api_key = os.getenv(key_env)
            if not api_key:
                return EmbedderSetup(None, f"向量模型需要环境变量 {key_env}，当前未设置；已退回仅关键词检索。")
            return EmbedderSetup(
                OpenAICompatibleEmbedder(
                    model=model,
                    base_url=config.get("semantic_search.base_url"),
                    api_key=api_key,
                    languages=languages,
                    batch_size=int(config.get("semantic_search.batch_size") or 32),
                ),
                "",
            )
        if kind == "local":
            from .local import TransformersEmbedder

            return EmbedderSetup(
                TransformersEmbedder(
                    model or "BAAI/bge-small-en-v1.5",
                    languages=languages,
                    query_prefix=config.get("semantic_search.query_prefix"),
                    pooling=config.get("semantic_search.pooling"),
                ),
                "",
            )
    except Exception as exc:  # 依赖缺失、模型不在本地缓存等
        logger.warning("向量模型不可用，退回仅关键词检索: %s", exc)
        return EmbedderSetup(None, f"向量模型不可用（{exc}）；已退回仅关键词检索。")
    return EmbedderSetup(None, f"未知的 semantic_search.embedder: {kind}；已退回仅关键词检索。")
