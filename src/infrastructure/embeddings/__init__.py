"""段级向量的嵌入器：可配置，未配置时语义检索退回仅关键词排序。

配置在 config.yaml 的 `semantic_search`：

- `embedder: none`（默认）：不算向量；
- `embedder: openai`：任何 OpenAI 兼容的 /v1/embeddings 服务（如 SiliconFlow 上的 BAAI/bge-m3），
  多语言模型可同时给原文、译文和中英文查询算向量；
- `embedder: local`：本机 transformers 模型，只从本地缓存加载、不联网下载（需要 torch 与 transformers）。

`languages` 声明模型能处理哪些语言：只含 en 时只给英文原文和英文查询算向量，中文查询走关键词排序。
"""

from .base import Embedder, EmbedderSetup, build_embedder

__all__ = ["Embedder", "EmbedderSetup", "build_embedder"]
