"""
上下文管理：证据编号、观察分页、已处理观察的压缩。

- 每次工具调用的完整输出存为一条证据（E1、E2……），模型看到的只是第一页，可用 read_page 翻页。
- 只有最近一步保留完整的回复和观察；更早的步骤压缩成一行：动作摘要 + "[E3] 模型给出的一行结论"。
  结论来自模型下一步回复里的 note 字段；模型没写时取观察的第一行截断。
- 因此提示长度 ≈ 固定部分 + 步数 × 一行 + 一页，不随工具输出长度增长。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .model import Message


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Evidence:
    evidence_id: str
    tool: str
    args: dict
    output: str
    ok: bool

    def excerpt(self, limit: int = 160) -> str:
        return _clip(self.output, limit)


@dataclass
class EvidenceStore:
    page_size: int = 1500
    items: dict[str, Evidence] = field(default_factory=dict)

    def add(self, tool: str, args: dict, output: str, ok: bool) -> Evidence:
        evidence = Evidence(f"E{len(self.items) + 1}", tool, args, output, ok)
        self.items[evidence.evidence_id] = evidence
        return evidence

    def pages(self, evidence_id: str) -> int:
        return max(1, -(-len(self.items[evidence_id].output) // self.page_size))

    def render(self, evidence_id: str, page: int = 1) -> str:
        evidence = self.items[evidence_id]
        total = self.pages(evidence_id)
        start = (page - 1) * self.page_size
        body = evidence.output[start : start + self.page_size]
        label = f"[{evidence_id}]" if total == 1 else f"[{evidence_id} 第 {page}/{total} 页]"
        prefix = "" if evidence.ok else "工具出错："
        footer = (
            f'\n（输出已截断，用 {{"action": "tool", "tool": "read_page", '
            f'"args": {{"evidence_id": "{evidence_id}", "page": {page + 1}}}}} 看下一页）'
            if page < total
            else ""
        )
        return f"{label} {prefix}{body}{footer}"


@dataclass
class Turn:
    reply: str
    summary: str  # 动作的一行摘要
    observation: str
    evidence_id: str | None = None
    note: str = ""  # 模型在下一步对这条观察写的一行结论


@dataclass
class ContextWindow:
    """单个条目的上下文：系统说明 + 条目任务 + 逐步轨迹（旧步骤压缩）。"""

    system: str
    task: str
    max_reply_chars: int = 2000
    summary_chars: int = 200
    note_chars: int = 160
    turns: list[Turn] = field(default_factory=list)

    def add(self, reply: str, summary: str, observation: str, evidence_id: str | None = None) -> None:
        self.turns.append(Turn(reply, summary, observation, evidence_id))

    def annotate_last(self, note: str) -> None:
        if self.turns and note:
            self.turns[-1].note = note

    def messages(self) -> list[Message]:
        messages: list[Message] = [{"role": "system", "content": self.system}, {"role": "user", "content": self.task}]
        for index, turn in enumerate(self.turns):
            if index == len(self.turns) - 1:
                messages.append({"role": "assistant", "content": _clip(turn.reply, self.max_reply_chars)})
                messages.append({"role": "user", "content": turn.observation})
                continue
            messages.append({"role": "assistant", "content": _clip(turn.summary, self.summary_chars)})
            gist = turn.note or turn.observation.split("\n", 1)[0]
            label = f"[{turn.evidence_id}] " if turn.evidence_id and not gist.startswith("[") else ""
            messages.append({"role": "user", "content": f"（已处理）{label}{_clip(gist, self.note_chars)}"})
        return messages

    def size(self) -> int:
        return sum(len(message["content"]) for message in self.messages())


def action_summary(kind: str, **fields) -> str:
    return json.dumps({"action": kind, **fields}, ensure_ascii=False, default=str)
