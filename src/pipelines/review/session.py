"""
审校会话：当前待分诊的问题与工作区。

审校 agent 的工具（src/application/tools/triage.py）都作用在 session.current 上；
try_edits 是"试编辑并重新质检"，check_fix 工具和提交时的结论校验共用它，
所以 agent 看到的"会生效"与最终生效的判断完全一致。
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.review.triage import FlaggedIssue, apply_review_edits, judge_fix, parse_edits
from .workspace import ReviewWorkspace


@dataclass
class ReviewSession:
    workspace: ReviewWorkspace
    current: FlaggedIssue | None = None

    def issue(self) -> FlaggedIssue:
        if self.current is None:
            raise RuntimeError("当前没有待分诊的问题")
        return self.current

    def try_edits(self, raw_edits) -> dict:
        """应用编辑并重新质检；返回是否生效、原因与新译文。check_fix 与提交时的校验共用。"""
        issue = self.issue()
        edits, problem = parse_edits(raw_edits)
        current = self.workspace.translation(issue.seg_id)
        if problem:
            return {"accepted": False, "reason": problem, "translation": current, "edits": []}
        text, problems = apply_review_edits(current, edits)
        if problems:
            return {
                "accepted": False,
                "reason": "编辑无效：" + "；".join(problems),
                "translation": current,
                "edits": [],
            }
        before = self.workspace.check(issue.seg_id, current)
        after = self.workspace.check(issue.seg_id, text)
        accepted, reason = judge_fix(issue, before, after)
        return {
            "accepted": accepted,
            "reason": reason,
            "translation": text,
            "edits": edits,
            "issues_after": [f"{i.kind}/{i.severity}: {i.message}" for i in after if i.severity in {"high", "medium"}],
        }
