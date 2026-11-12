"""检索工具：按关键词查段、按意思找原话。全部只读。"""

from __future__ import annotations

from .base import ToolContext, shared_tool


@shared_tool("search_book")
async def search_book(
    ctx: ToolContext, task_id: str, query: str, field: str = "any", skip: int = 0, limit: int = 20
) -> dict:
    """按关键词在全书原文和/或译文里查段，返回段号、原文、译文。

    Args:
        task_id: 结构化翻译的任务 ID。
        query: 关键词（英文或中文）。
        field: 查哪一侧：source / translation / any。
        skip: 分页偏移。
        limit: 每页条数（1~100）。
    """
    if field not in {"source", "translation", "any"}:
        raise ValueError("field 只能是 source、translation 或 any")
    return await ctx.service.search_book(task_id, query, field, skip, max(1, min(limit, 100)))


@shared_tool("search_book_by_meaning")
async def search_book_by_meaning(ctx: ToolContext, task_id: str, query: str, limit: int = 10) -> dict:
    """按意思检索一本已翻译的书：用一句大概的话（中文或英文）找到意思相近的段落。

    原文和译文都参与匹配；返回每段的原文、译文、seg_id（对照阅读器段号，可用于跳转）与排序信号。
    只读，不修改任何数据。未配置向量模型时为仅关键词排序，mode 为 "lexical" 并在 hint 中说明。

    Args:
        task_id: 已完成的结构化翻译任务 ID。
        query: 查询句子，中文或英文均可。
        limit: 返回条数（1~50）。
    """
    if not query.strip():
        raise ValueError("query 不能为空")
    return await ctx.service.search_meaning(task_id, query, max(1, min(limit, 50)))
