"""纠正记忆的持久化：全部书共用一个 SQLite 文件 data/memory/memory.sqlite。

两张表：
- corrections：每次人工编辑、译名纠正与书内传播一条，只追加；
- term_entries：结构化术语条目，每本书（任务）每个术语一条，人工纠正会覆盖同书同术语的旧条目。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from ...domain.memory.corrections import Correction, TermEntry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    book TEXT NOT NULL,
    chunk_index INTEGER,
    kind TEXT NOT NULL,
    term TEXT,
    old_text TEXT NOT NULL,
    new_text TEXT NOT NULL,
    positions TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_task ON corrections(task_id, created_at);
CREATE TABLE IF NOT EXISTS term_entries (
    task_id TEXT NOT NULL,
    term_key TEXT NOT NULL,
    term TEXT NOT NULL,
    book TEXT NOT NULL,
    rendering TEXT NOT NULL,
    sense TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL,
    source TEXT NOT NULL,
    author TEXT,
    domain TEXT,
    evidence TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (task_id, term_key)
);
CREATE INDEX IF NOT EXISTS idx_term_entries_scope ON term_entries(scope, author, domain);
"""

# 人工纠正不会被自动来源（术语表、账本、agent）覆盖
_UPSERT = """
INSERT INTO term_entries (task_id, term_key, term, book, rendering, sense, scope, source, author, domain, evidence, updated_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(task_id, term_key) DO UPDATE SET
    term=excluded.term, book=excluded.book, rendering=excluded.rendering, sense=excluded.sense,
    scope=excluded.scope, source=excluded.source, author=excluded.author, domain=excluded.domain,
    evidence=excluded.evidence, updated_at=excluded.updated_at
WHERE excluded.source = 'human' OR term_entries.source != 'human'
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        return db

    # ------------------------------------------------------------------ #
    # 纠正记录
    # ------------------------------------------------------------------ #

    def add_correction(self, correction: Correction) -> int:
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                """INSERT INTO corrections (task_id, book, chunk_index, kind, term, old_text, new_text, positions, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    correction.task_id,
                    correction.book,
                    correction.chunk_index,
                    correction.kind,
                    correction.term,
                    correction.old_text,
                    correction.new_text,
                    json.dumps(list(correction.positions)),
                    correction.created_at or _now(),
                ),
            )
            return int(cursor.lastrowid)

    def corrections(self, task_id: str, limit: int = 100) -> list[Correction]:
        if not self.path.exists():  # 只读查询不建库
            return []
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM corrections WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)
            ).fetchall()
        return [
            Correction(
                task_id=row["task_id"],
                book=row["book"],
                chunk_index=row["chunk_index"],
                kind=row["kind"],
                old_text=row["old_text"],
                new_text=row["new_text"],
                term=row["term"],
                positions=tuple(json.loads(row["positions"])),
                created_at=row["created_at"],
                id=row["id"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # 术语条目
    # ------------------------------------------------------------------ #

    def upsert_entries(self, entries: Iterable[TermEntry]) -> int:
        rows = [
            (
                entry.task_id,
                entry.term.casefold(),
                entry.term,
                entry.book,
                entry.rendering,
                entry.sense,
                entry.scope,
                entry.source,
                entry.author,
                entry.domain,
                json.dumps(list(entry.evidence)),
                entry.updated_at or _now(),
            )
            for entry in entries
        ]
        if not rows:
            return 0
        with closing(self._connect()) as db, db:
            db.executemany(_UPSERT, rows)
        return len(rows)

    def entries(
        self, *, task_id: str | None = None, author: str | None = None, domain: str | None = None
    ) -> list[TermEntry]:
        """按任务取本书条目；或按作者、领域取可共享的条目（大小写与首尾空白不敏感）。"""
        if not self.path.exists():  # 只读查询不建库
            return []
        sql, params = "SELECT * FROM term_entries", []
        if task_id is not None:
            sql += " WHERE task_id=?"
            params.append(task_id)
        else:
            clauses = []
            if author and author.strip():
                clauses.append("(scope='author' AND lower(trim(author))=lower(trim(?)))")
                params.append(author)
            if domain and domain.strip():
                clauses.append("(scope='domain' AND lower(trim(domain))=lower(trim(?)))")
                params.append(domain)
            if not clauses:
                return []
            sql += " WHERE " + " OR ".join(clauses)
        with closing(self._connect()) as db:
            rows = db.execute(sql, params).fetchall()
        return [_entry(row) for row in rows]

    def delete_task(self, task_id: str) -> None:
        if not self.path.exists():
            return
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM term_entries WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM corrections WHERE task_id=?", (task_id,))


def _entry(row: sqlite3.Row) -> TermEntry:
    return TermEntry(
        task_id=row["task_id"],
        book=row["book"],
        term=row["term"],
        rendering=row["rendering"],
        scope=row["scope"],
        source=row["source"],
        author=row["author"],
        domain=row["domain"],
        sense=row["sense"],
        evidence=tuple(json.loads(row["evidence"])),
        updated_at=row["updated_at"],
    )
