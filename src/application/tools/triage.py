"""审校 agent（#21）的工具：作用在"当前待分诊的问题"上，只给内部 agent 用，不进 DEFAULT_MODULES。

当前问题由 ctx.extras["review_session"]（pipelines/review/session.ReviewSession）提供，
所以这些工具不需要 task_id、也不需要问题编号；查全书的部分复用已有的共享工具
（term_renderings、term_occurrences、read_segment、search_book），见 review_registry。

- get_issue        当前问题、所在段的原文与当前译文、该段的其他问题
- get_context      前后文（前后各 radius 段）
- glossary_lookup  查术语表里包含该词的条目
- nontext_objects  对照原文与译文里的数字、链接、行内代码、公式、图表编号
- check_fix        试一组约束式编辑：应用后重新质检，告诉你会不会生效（不真正修改）
- book_renderings  某词在全书各段的译法分布（没有任务服务时的替代，如评测）

提交修改、驳回误报用 agent 输出协议里的 submit；拿不准用 handoff。真正写回译文由审校用例统一完成
（一次分诊只发布一次），因此这里没有写入类工具。
"""

from __future__ import annotations

from ...infrastructure.agent import ToolRegistry
from ...pipelines.review.session import ReviewSession
from ...pipelines.review.workspace import nontext_objects as _objects
from .base import ToolContext, shared_tool

SESSION = "review_session"
SESSION_TOOLS = ["get_issue", "get_context", "glossary_lookup", "nontext_objects", "check_fix"]
TASK_TOOLS = ["term_renderings", "term_occurrences", "read_segment", "search_book"]


def _session(ctx: ToolContext) -> ReviewSession:
    session = ctx.extras.get(SESSION)
    if session is None:
        raise RuntimeError("没有审校会话（ctx.extras['review_session']）")
    return session


@shared_tool("get_issue")
async def get_issue(ctx: ToolContext) -> dict:
    """读取当前问题：问题类型与描述、所在段的原文与当前译文、该段的其他问题。"""
    session = _session(ctx)
    issue = session.issue()
    workspace = session.workspace
    segment = workspace.segment(issue.seg_id)
    return {
        "issue": {"kind": issue.kind, "severity": issue.severity, "message": issue.message},
        "segment": {
            "seg_id": issue.seg_id,
            "section": " > ".join(segment.section_path) if segment else "",
            "source": segment.source if segment else "",
            "translation": workspace.translation(issue.seg_id),
        },
        "other_issues_on_segment": [
            f"{other.kind}: {other.message}"
            for other in workspace.issues_on(issue.seg_id)
            if other.issue_id != issue.issue_id
        ],
    }


@shared_tool("get_context")
async def get_context(ctx: ToolContext, radius: int = 1) -> list:
    """查看当前段的前后文：前后各 radius 段（1~3）的原文与译文。"""
    session = _session(ctx)
    workspace = session.workspace
    return [
        {
            "seg_id": segment.seg_id,
            "source": segment.source,
            "translation": workspace.translation(segment.seg_id) if segment.seg_id else segment.translation,
        }
        for segment in workspace.neighbors(session.issue().seg_id, max(1, min(radius, 3)))
    ]


@shared_tool("glossary_lookup")
async def glossary_lookup(ctx: ToolContext, term: str) -> dict:
    """查术语表里包含该英文词的条目及规定译名（不区分大小写）。"""
    found = _session(ctx).workspace.glossary_lookup(term)
    return found or {"note": f"术语表里没有包含 {term!r} 的条目"}


@shared_tool("nontext_objects")
async def nontext_objects(ctx: ToolContext) -> dict:
    """列出当前段原文与译文里的数字、链接、行内代码、公式与图表编号引用，便于逐项对照。"""
    session = _session(ctx)
    issue = session.issue()
    segment = session.workspace.segment(issue.seg_id)
    return {
        "source": _objects(segment.source if segment else ""),
        "translation": _objects(session.workspace.translation(issue.seg_id)),
    }


@shared_tool("check_fix")
async def check_fix(ctx: ToolContext, edits: list[dict]) -> dict:
    """试一组约束式编辑，不真正修改：返回应用后的译文、重新质检的结果，以及是否会生效。

    每条编辑形如 {"from": 译文里恰好出现一次的文字, "to": 新文字}，改动片段不超过 40 字、不能碰标签。
    生效标准是逐类型不退步：目标问题消失，且没有新增其他高/中严重度的问题。
    """
    result = _session(ctx).try_edits(edits)
    result.pop("edits", None)
    return result


@shared_tool("book_renderings")
async def book_renderings(ctx: ToolContext, term: str) -> dict:
    """查一个英文词或短语在全书各段的中文译法分布与次数。"""
    return _session(ctx).workspace.renderings(term)


def review_registry(session: ReviewSession, service=None) -> ToolRegistry:
    """审校 agent 的工具子集。有任务服务时复用共享的全书查询工具，否则用工作区里的索引。"""
    from . import build_tool_suite, review, search, terms, triage

    ctx = ToolContext(service=service, extras={SESSION: session})
    if service is None:
        suite = build_tool_suite(ctx, modules=[triage])
        return suite.subset([*SESSION_TOOLS, "book_renderings"])
    suite = build_tool_suite(ctx, modules=[triage, terms, review, search])
    return suite.subset([*SESSION_TOOLS, *TASK_TOOLS])
