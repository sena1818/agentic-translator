"""
术语 agent 结果的持久化与审阅清单。

- 每个任务一份 `data/term_agent/<task_id>.json`：全部决定（含证据、步数、token）与用量汇总；
- 单一译名的决定写进纠正记忆的术语条目（来源记为术语 agent，带义项与证据段），人工条目不会被覆盖；
- 转交人工的条目汇总成审阅清单，在 Web 术语表页显示。
"""

from __future__ import annotations

import json
from pathlib import Path

from ...domain.memory.corrections import SCOPE_BOOK, SOURCE_AGENT, TermEntry
from ...domain.terminology.term_decisions import HANDOFF
from ...pipelines.glossary.term_agent import TermAgentReport
from .correction_memory import book_name

DIRECTORY = "term_agent"


def report_path(root: str | Path, task_id: str) -> Path:
    return Path(root) / DIRECTORY / f"{task_id}.json"


def save_report(root: str | Path, task, report: TermAgentReport) -> Path:
    path = report_path(root, task.task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"task_id": task.task_id, "book": book_name(task.filename), **report.to_dict()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def delete_report(root: str | Path, task_id: str) -> None:
    report_path(root, task_id).unlink(missing_ok=True)


def agent_entries(task, report: TermAgentReport) -> list[TermEntry]:
    """单一译名的决定转成术语条目；作者 / 领域范围只在任务填了对应信息时生效，否则按本书。"""
    entries = []
    for decision in report.decisions:
        rendering = decision.single_rendering
        if not rendering:
            continue
        sense = decision.senses[0]
        scope = sense.scope
        if (scope == "author" and not (task.author or "").strip()) or (
            scope == "domain" and not (task.domain or "").strip()
        ):
            scope = SCOPE_BOOK
        entries.append(
            TermEntry(
                task.task_id,
                book_name(task.filename),
                decision.term,
                rendering,
                scope,
                SOURCE_AGENT,
                task.author,
                task.domain,
                sense=sense.sense,
                evidence=tuple(decision.evidence_positions[:50]),
            )
        )
    return entries


def pending_reviews(root: str | Path, task_id: str | None = None) -> list[dict]:
    """转交人工的条目（可按任务过滤），按书名、术语排序。"""
    directory = Path(root) / DIRECTORY
    if not directory.exists():
        return []
    paths = [report_path(root, task_id)] if task_id else sorted(directory.glob("*.json"))
    items = []
    for path in paths:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for decision in data.get("decisions", []):
            if decision.get("status") != HANDOFF:
                continue
            items.append(
                {
                    "task_id": data.get("task_id", path.stem),
                    "book": data.get("book", ""),
                    "term": decision["term"],
                    "reason": decision.get("reason", ""),
                    "evidence": [
                        {"tool": item.get("tool", ""), "excerpt": item.get("excerpt", "")}
                        for item in decision.get("evidence", [])
                    ],
                    "contexts": decision.get("contexts", []),
                    "steps": decision.get("steps", 0),
                }
            )
    return sorted(items, key=lambda item: (item["book"], item["term"].casefold()))


__all__ = ["agent_entries", "delete_report", "pending_reviews", "report_path", "save_report"]
