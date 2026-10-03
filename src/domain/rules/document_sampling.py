"""在固定输入预算内覆盖文档开头、中部与结尾。"""


def sample_document(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    labels = ("文档开头", "文档中部", "文档结尾")
    overhead = sum(len(label) + 5 for label in labels) + 4
    size = max(1, (budget - overhead) // 3)
    starts = (0, max(0, len(text) // 2 - size // 2), len(text) - size)
    return "\n\n".join(
        f"【{label}】\n{text[start : start + size]}" for label, start in zip(labels, starts, strict=False)
    )[:budget]
