"""本机 transformers 嵌入模型：只从本地缓存加载（local_files_only），不会在检索时联网下载。

默认 BAAI/bge-small-en-v1.5（只支持英文，CLS 池化，查询加检索指令前缀）；
sentence-transformers 系列（如 all-MiniLM-L6-v2）用均值池化。
需要可选依赖：pip install -e ".[semantic-local]"。
"""

from __future__ import annotations

_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class TransformersEmbedder:
    def __init__(
        self,
        model_name: str,
        languages: frozenset[str] = frozenset({"en"}),
        query_prefix: str | None = None,
        pooling: str | None = None,
        batch_size: int = 32,
        max_length: int = 512,
    ):
        import torch
        from transformers import AutoModel, AutoTokenizer

        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_name, local_files_only=True)
        self.model = AutoModel.from_pretrained(model_name, local_files_only=True)
        self.model.eval()
        is_bge = "bge" in model_name.lower()
        self.pooling = pooling or ("cls" if is_bge else "mean")
        self.query_prefix = query_prefix if query_prefix is not None else (_BGE_QUERY_PREFIX if is_bge else "")
        self.name = f"local:{model_name}"
        self.languages = languages
        self.batch_size = batch_size
        self.max_length = max_length

    def _encode(self, texts: list[str]) -> list[list[float]]:
        torch = self._torch
        vectors: list[list[float]] = []
        with torch.no_grad():
            for start in range(0, len(texts), self.batch_size):
                batch = self.tokenizer(
                    texts[start : start + self.batch_size],
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                    return_tensors="pt",
                )
                hidden = self.model(**batch).last_hidden_state
                if self.pooling == "cls":
                    pooled = hidden[:, 0]
                else:
                    mask = batch["attention_mask"].unsqueeze(-1).to(hidden.dtype)
                    pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
                pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
                vectors += pooled.tolist()
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._encode([self.query_prefix + text])[0]
