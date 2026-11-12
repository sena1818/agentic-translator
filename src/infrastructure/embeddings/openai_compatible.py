"""OpenAI 兼容的 /v1/embeddings 服务（OpenAI、SiliconFlow 的 BAAI/bge-m3 等）。"""

from __future__ import annotations


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        model: str,
        base_url: str | None,
        api_key: str,
        languages: frozenset[str] = frozenset({"en", "zh"}),
        batch_size: int = 32,
    ):
        from openai import OpenAI

        if not model:
            raise ValueError("semantic_search.model 未设置")
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.name = f"openai:{model}"
        self.languages = languages
        self.batch_size = batch_size

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            response = self.client.embeddings.create(model=self.model, input=batch)
            vectors += [item.embedding for item in sorted(response.data, key=lambda item: item.index)]
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]
