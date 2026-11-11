"""全书索引的持久化：每本书（任务）一个 SQLite 文件，按段存原文、译文与位置。

查询在内存里做（一本书几千段，整表载入是毫秒级），存储只负责增量写入与整表读出。
source_fingerprint 记录建索引时源文的指纹：源文或分块参数变了，旧索引整表作废重建。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from ...domain.index.book_index import IndexedSegment

_COLUMNS = (
    "position",
    "chunk_index",
    "seg_id",
    "section_path",
    "in_note",
    "verbatim",
    "status",
    "source",
    "translation",
)


class BookIndexStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        db.execute(
            """CREATE TABLE IF NOT EXISTS segments (
                position INTEGER PRIMARY KEY, chunk_index INTEGER NOT NULL, seg_id INTEGER,
                section_path TEXT NOT NULL DEFAULT '[]', in_note INTEGER NOT NULL DEFAULT 0,
                verbatim INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                source TEXT NOT NULL, translation TEXT)"""
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_segments_chunk ON segments(chunk_index)")
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        return db

    def exists(self) -> bool:
        return self.path.exists()

    def fingerprint(self) -> str | None:
        if not self.exists():
            return None
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM meta WHERE key='source_fingerprint'").fetchone()
        return row[0] if row else None

    def replace(self, segments: Iterable[IndexedSegment], fingerprint: str) -> None:
        """整表重建（翻译前建索引、源文变化时）。"""
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM segments")
            db.executemany(_insert_sql(), [_row(seg) for seg in segments])
            db.execute(
                "INSERT INTO meta(key, value) VALUES('source_fingerprint', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (fingerprint,),
            )

    def update(self, segments: Iterable[IndexedSegment]) -> int:
        """按段位置覆盖写入（块译完、人工编辑后）；返回写入段数。"""
        rows = [_row(seg) for seg in segments]
        with closing(self._connect()) as db, db:
            db.executemany(_insert_sql(), rows)
        return len(rows)

    def load(self) -> list[IndexedSegment]:
        if not self.exists():
            return []
        with closing(self._connect()) as db:
            rows = db.execute(f"SELECT {','.join(_COLUMNS)} FROM segments ORDER BY position").fetchall()
        return [
            IndexedSegment(
                position=row[0],
                chunk_index=row[1],
                seg_id=row[2],
                section_path=tuple(json.loads(row[3])),
                in_note=bool(row[4]),
                verbatim=bool(row[5]),
                status=row[6],
                source=row[7],
                translation=row[8],
            )
            for row in rows
        ]

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


def _insert_sql() -> str:
    placeholders = ",".join("?" for _ in _COLUMNS)
    return f"INSERT OR REPLACE INTO segments ({','.join(_COLUMNS)}) VALUES ({placeholders})"


def _row(seg: IndexedSegment) -> tuple:
    return (
        seg.position,
        seg.chunk_index,
        seg.seg_id,
        json.dumps(list(seg.section_path), ensure_ascii=False),
        int(seg.in_note),
        int(seg.verbatim),
        seg.status,
        seg.source,
        seg.translation,
    )
