"""纠正记忆用例：记录人工编辑、识别译名纠正、书内传播（预览 → 应用）与跨书术语建议。

- 纠正记录与术语条目存于 data/memory/memory.sqlite（所有书共用）；
- 书内传播依赖全书索引（#13）找出现处，写回块译文由 pipelines.structure.propagation 完成；
- 人工确定的译名覆盖本书术语表与账本：之后的质检、重译都按新译名；
- 跨书只给建议：同作者（scope=author）或同领域（scope=domain）的条目注入翻译提示的"参考译名"，
  不并入术语表，因此不参与术语一致性校验。

入口供 Web 路由使用，也是 #23 MCP 工具的同一组用例。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ...domain.document import document_ir
from ...domain.memory.corrections import (
    SCOPES,
    SOURCE_GLOSSARY,
    SOURCE_HUMAN,
    SOURCE_LEDGER,
    Correction,
    PropagationPlan,
    TermChange,
    TermEntry,
    TermSuggestion,
    changed_spans,
    default_scope,
    detect_term_changes,
    plan_propagation,
    preferred_rendering,
    rank_suggestions,
    with_rendering,
)
from ...infrastructure.persistence.glossary_repository import GlossaryService
from ...infrastructure.persistence.memory_store import MemoryStore
from ...pipelines.structure import indexing
from ...pipelines.structure.propagation import ChunkEdit, apply_to_chunk
from .book_index import BookIndexService
from .term_ledger_store import ledger_glossary_id

_MAX_FRAGMENT = 400


def book_name(filename: str) -> str:
    return Path(filename).stem


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CorrectionMemoryService:
    def __init__(self, root: str | Path, book_index: BookIndexService, glossary_service: GlossaryService | None = None):
        self.store = MemoryStore(Path(root) / "memory" / "memory.sqlite")
        self.book_index = book_index
        self.glossary_service = glossary_service

    # ------------------------------------------------------------------ #
    # 术语表：本书人工纠正覆盖其他来源
    # ------------------------------------------------------------------ #

    def human_overrides(self, task_id: str) -> dict[str, str]:
        return {
            entry.term: entry.rendering for entry in self.store.entries(task_id=task_id) if entry.source == SOURCE_HUMAN
        }

    def effective_glossary(self, task_id: str, glossary: Mapping[str, str]) -> dict[str, str]:
        """术语表 + 本书的人工纠正（同一术语大小写不敏感地覆盖，保留原条目的括注写法）。"""
        merged = dict(glossary)
        for term, rendering in self.human_overrides(task_id).items():
            key = next((existing for existing in merged if existing.casefold() == term.casefold()), term)
            merged[key] = with_rendering(merged.get(key, rendering), preferred_rendering(rendering))
        return merged

    def record_book_terms(self, task, user_glossary: Mapping[str, str], ledger: Mapping[str, str]) -> int:
        """一本书译完后，把它实际使用的译名记成术语条目，供同作者 / 同领域的下一本书作为建议。"""
        scope = default_scope(task.author, task.domain)
        book = book_name(task.filename)
        user_keys = {term.casefold() for term in user_glossary}
        entries = [
            TermEntry(task.task_id, book, term, value, scope, SOURCE_GLOSSARY, task.author, task.domain)
            for term, value in user_glossary.items()
        ] + [
            TermEntry(task.task_id, book, term, value, scope, SOURCE_LEDGER, task.author, task.domain)
            for term, value in ledger.items()
            if term.casefold() not in user_keys
        ]
        return self.store.upsert_entries(entries)

    # ------------------------------------------------------------------ #
    # 人工编辑
    # ------------------------------------------------------------------ #

    def record_edit(
        self, task, chunk_index: int, source: str, old: str, new: str, glossary: Mapping[str, str]
    ) -> list[TermChange]:
        """记一条编辑纠正，并返回识别出的译名纠正（供前端提示是否传播到全书）。"""
        if old == new:
            return []
        spans = changed_spans(old, new)
        self.store.add_correction(
            Correction(
                task_id=task.task_id,
                book=book_name(task.filename),
                chunk_index=chunk_index,
                kind="edit",
                old_text=" … ".join(span.old_text for span in spans)[:_MAX_FRAGMENT],
                new_text=" … ".join(span.new_text for span in spans)[:_MAX_FRAGMENT],
            )
        )
        return detect_term_changes(source, old, new, glossary)

    # ------------------------------------------------------------------ #
    # 书内传播
    # ------------------------------------------------------------------ #

    def plan(self, task_id: str, term: str, old: str, new: str, glossary: Mapping[str, str]) -> PropagationPlan:
        index = self.book_index._require(task_id)
        renderings = [preferred_rendering(value) for value in glossary.values()]
        return plan_propagation(index.segments, term.strip(), old.strip(), new.strip(), renderings)

    def apply(
        self,
        plan: PropagationPlan,
        chunks: Sequence[document_ir.StructuredChunk],
        records: list[dict],
        positions: Sequence[int] | None = None,
    ) -> tuple[list[ChunkEdit], list[int]]:
        """把选中的段（默认预览里的全部段）写回块译文。返回 (各块写回结果, 实际改动的段)。"""
        wanted = {item.position for item in plan.items} if positions is None else set(positions)
        unknown = wanted - {item.position for item in plan.items}
        if unknown:
            raise ValueError("所选段已不在预览中，请重新预览：" + ", ".join(map(str, sorted(unknown)[:10])))
        by_chunk: dict[int, list[int]] = {}
        for item in plan.items:
            if item.position in wanted:
                by_chunk.setdefault(item.chunk_index, []).append(item.position)
        first = indexing.first_positions(chunks)
        structured = {chunk.index: chunk for chunk in chunks}
        translations = {record["chunk_index"]: record for record in records}
        edits: list[ChunkEdit] = []
        applied: list[int] = []
        for chunk_index, chunk_positions in sorted(by_chunk.items()):
            record = translations.get(chunk_index)
            if record is None or not record.get("success") or chunk_index not in structured:
                continue
            edit = apply_to_chunk(
                structured[chunk_index],
                record["translation"],
                first[chunk_index],
                chunk_positions,
                plan.old_rendering,
                plan.new_rendering,
                plan.protected,
            )
            if edit is not None:
                edits.append(edit)
                applied += chunk_positions
        return edits, sorted(applied)

    def record_term_decision(
        self, task, plan: PropagationPlan, positions: Sequence[int], scope: str | None = None
    ) -> TermEntry:
        """人工确定的译名：记一条传播纠正、写入术语条目，并覆盖本书账本里的该术语。"""
        scope = scope or default_scope(task.author, task.domain)
        if scope not in SCOPES:
            raise ValueError("适用范围只能是 book、author 或 domain")
        book = book_name(task.filename)
        self.store.add_correction(
            Correction(
                task_id=task.task_id,
                book=book,
                chunk_index=None,
                kind="propagation",
                term=plan.term,
                old_text=plan.old_rendering,
                new_text=plan.new_rendering,
                positions=tuple(positions),
            )
        )
        entry = TermEntry(
            task.task_id,
            book,
            plan.term,
            plan.new_rendering,
            scope,
            SOURCE_HUMAN,
            task.author,
            task.domain,
            evidence=tuple(positions[:50]),
            updated_at=_now(),
        )
        self.store.upsert_entries([entry])
        self._update_ledger(task.filename, plan.term, plan.new_rendering)
        return entry

    def decide_term(self, task, term: str, rendering: str, scope: str | None = None) -> TermEntry:
        """只更新术语决定、不改动已有译文：记一条 kind="term" 的纠正，写入人工术语条目并覆盖本书账本。

        之后的质检、重译都按新译名；已有译文里的旧译名需要另行发起书内传播。
        """
        term, rendering = term.strip(), rendering.strip()
        if not term or not rendering:
            raise ValueError("术语和译名都不能为空")
        scope = scope or default_scope(task.author, task.domain)
        if scope not in SCOPES:
            raise ValueError("适用范围只能是 book、author 或 domain")
        book = book_name(task.filename)
        previous = self.human_overrides(task.task_id)
        old = next((value for key, value in previous.items() if key.casefold() == term.casefold()), "")
        self.store.add_correction(
            Correction(
                task_id=task.task_id,
                book=book,
                chunk_index=None,
                kind="term",
                term=term,
                old_text=old,
                new_text=rendering,
            )
        )
        entry = TermEntry(
            task.task_id, book, term, rendering, scope, SOURCE_HUMAN, task.author, task.domain, updated_at=_now()
        )
        self.store.upsert_entries([entry])
        self._update_ledger(task.filename, term, rendering)
        return entry

    def _update_ledger(self, filename: str, term: str, rendering: str) -> None:
        """账本里同名术语改成新译名（人工决定覆盖自动识别）；账本里没有就追加。"""
        if self.glossary_service is None:
            return
        service = self.glossary_service
        glossary_id = ledger_glossary_id(filename)
        path = service._path(glossary_id)
        with service._lock(glossary_id):
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            key = next((existing for existing in current if existing.casefold() == term.casefold()), term)
            current[key] = with_rendering(current.get(key, rendering), rendering)
            service._write(path, current)

    # ------------------------------------------------------------------ #
    # 跨书建议
    # ------------------------------------------------------------------ #

    def suggestions(self, task, known_terms: Sequence[str] = ()) -> list[TermSuggestion]:
        if not ((task.author or "").strip() or (task.domain or "").strip()):
            return []
        entries = self.store.entries(author=task.author, domain=task.domain)
        return rank_suggestions(entries, task.author, task.domain, exclude_task=task.task_id, known_terms=known_terms)

    def corrections(self, task_id: str, limit: int = 100) -> list[dict]:
        return [asdict(item) for item in self.store.corrections(task_id, limit)]

    def delete(self, task_id: str) -> None:
        self.store.delete_task(task_id)


def plan_to_dict(plan: PropagationPlan) -> dict:
    return {
        "term": plan.term,
        "old": plan.old_rendering,
        "new": plan.new_rendering,
        "protected": plan.protected,
        "total_segments": len(plan.items),
        "total_replacements": plan.total_replacements,
        "items": [asdict(item) for item in plan.items],
        "skipped": [asdict(item) for item in plan.skipped],
    }


def suggestion_to_dict(suggestion: TermSuggestion) -> dict:
    return {**asdict(suggestion), "label": suggestion.label}


def term_change_to_dict(change: TermChange) -> dict:
    return asdict(change)
