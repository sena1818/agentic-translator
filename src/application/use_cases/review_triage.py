"""
审校 agent 用例：对一个已完成的翻译任务，把质检后仍被标记的问题交给审校 agent 分诊，
把生效的修改写回块译文，把结局写回各块的质检报告（对照阅读器与审校页据此显示），
并保存本次分诊的结果与成本。

默认关闭（review_agent.enabled）。只在结构化翻译、且有全书索引的任务上可用：
问题要定位到段，agent 的工具要查全书索引。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from ...domain.review.triage import FIXED, HANDOFF, TriageResult, annotate_report, flagged_from_records
from ...infrastructure.agent import ChatModel, JsonlTracer
from ...pipelines.review.review_agent import ReviewAgent, ReviewAgentConfig, TriageRun
from ...pipelines.review.workspace import ReviewWorkspace, recheck_for_config
from ..tools.triage import review_registry
from .book_index import BookIndexService

logger = logging.getLogger(__name__)

ModelBuilder = Callable[[object], ChatModel]


def default_model(config) -> ChatModel:
    """审校 agent 用审校员（checker）模型；token 用量随 LangChain 回调进入现有统计。"""
    from ...infrastructure.agent import RunnableChatModel
    from ...infrastructure.llm.chat_model_factory import ChatModelFactory

    return RunnableChatModel(ChatModelFactory(config).create_checker(), run_name="review-agent")


def apply_fixes_to_records(records: Sequence[dict], results: Sequence[TriageResult]) -> list[int]:
    """把已修的编辑写回块译文（Markdown）；返回改动过的块号。

    编辑的 from 必须在整块译文里恰好出现一次才写回；做不到（例如被加粗等格式标记隔开、
    或同一块里另有相同文字）就把结局改为待人工，绝不猜位置。
    """
    by_index = {int(record["chunk_index"]): record for record in records}
    changed: list[int] = []
    for result in results:
        if result.verdict != FIXED:
            continue
        record = by_index.get(result.issue.chunk_index)
        text = (record or {}).get("translation") or ""
        if record is None or any(text.count(edit.old) != 1 for edit in result.edits):
            result.verdict = HANDOFF
            result.reason = "修改在块译文中无法唯一定位（可能被格式标记隔开），请人工修改：" + "；".join(
                f"{edit.old}→{edit.new}" for edit in result.edits
            )
            continue
        for edit in result.edits:
            text = text.replace(edit.old, edit.new, 1)
        record["translation"] = text
        if result.issue.chunk_index not in changed:
            changed.append(result.issue.chunk_index)
    return changed


class ReviewTriageService:
    def __init__(self, root: str | Path, book_index: BookIndexService, model_builder: ModelBuilder | None = None):
        self.root = Path(root)
        self.book_index = book_index
        self.model_builder = model_builder or default_model

    def _dir(self) -> Path:
        return self.root / "review"

    def path(self, task_id: str) -> Path:
        return self._dir() / f"{task_id}.json"

    def load(self, task_id: str) -> dict | None:
        path = self.path(task_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def delete(self, task_id: str) -> None:
        self.path(task_id).unlink(missing_ok=True)
        (self._dir() / f"{task_id}.trace.jsonl").unlink(missing_ok=True)

    async def triage(
        self,
        task_id: str,
        config,
        records: list[dict],
        glossary: dict[str, str],
        audit: dict | None,
        service=None,
    ) -> tuple[TriageRun, list[int]]:
        """分诊并就地更新 records（译文与质检报告）；返回 (分诊结果, 改动过译文的块号)。"""
        agent_config = ReviewAgentConfig.from_config(config)
        index = self.book_index.load(task_id)
        if index is None:
            raise ValueError("该任务没有全书索引（需要结构化翻译），无法运行审校 agent")
        flagged = flagged_from_records(records, audit)
        workspace = ReviewWorkspace(index, glossary, flagged, recheck_for_config(config))
        self._dir().mkdir(parents=True, exist_ok=True)
        tracer = JsonlTracer(self._dir() / f"{task_id}.trace.jsonl")
        agent = ReviewAgent(
            self.model_builder(config), lambda session: review_registry(session, service), agent_config, tracer
        )
        run = await agent.triage(workspace, task_id=task_id if service is not None else None)
        changed = apply_fixes_to_records(records, run.results)
        for record in records:
            mine = [r for r in run.results if r.issue.chunk_index == int(record["chunk_index"])]
            if mine:
                record["quality_report"] = annotate_report(record.get("quality_report") or {}, mine)
        return run, changed

    def save(self, task_id: str, run: TriageRun, revision: str) -> dict:
        payload = {
            "task_id": task_id,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "revision": revision,
            "summary": run.summary,
            "results": [result.to_dict() for result in run.results],
        }
        self._dir().mkdir(parents=True, exist_ok=True)
        self.path(task_id).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
