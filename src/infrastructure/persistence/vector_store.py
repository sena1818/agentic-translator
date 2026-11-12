"""段级向量缓存：每本书一个 SQLite 文件 data/index/<task_id>.vectors.sqlite。

每段的原文、译文各存一条（side = source | translation），附文本哈希：
文本没变就不重算，人工编辑或重译后只重算改动过的段。换嵌入模型时整表作废。
"""

from __future__ import annotations

import sqlite3
from array import array
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path


class VectorStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        db.execute(
            """CREATE TABLE IF NOT EXISTS vectors (
                position INTEGER NOT NULL, side TEXT NOT NULL, text_hash TEXT NOT NULL, vector BLOB NOT NULL,
                PRIMARY KEY (position, side))"""
        )
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        return db

    def model(self) -> str | None:
        if not self.path.exists():
            return None
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM meta WHERE key='model'").fetchone()
        return row[0] if row else None

    def reset(self, model: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM vectors")
            db.execute(
                "INSERT INTO meta(key, value) VALUES('model', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (model,),
            )

    def hashes(self) -> dict[tuple[int, str], str]:
        if not self.path.exists():
            return {}
        with closing(self._connect()) as db:
            return {(row[0], row[1]): row[2] for row in db.execute("SELECT position, side, text_hash FROM vectors")}

    def upsert(self, rows: Iterable[tuple[int, str, str, list[float]]]) -> int:
        payload = [
            (position, side, text_hash, array("f", vector).tobytes()) for position, side, text_hash, vector in rows
        ]
        with closing(self._connect()) as db, db:
            db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?, ?)", payload)
        return len(payload)

    def remove(self, keys: Iterable[tuple[int, str]]) -> None:
        with closing(self._connect()) as db, db:
            db.executemany("DELETE FROM vectors WHERE position=? AND side=?", list(keys))

    def load(self) -> dict[int, list[list[float]]]:
        """{段位置: [向量, ...]}（原文、译文各至多一条）。"""
        if not self.path.exists():
            return {}
        vectors: dict[int, list[list[float]]] = {}
        with closing(self._connect()) as db:
            for position, blob in db.execute("SELECT position, vector FROM vectors ORDER BY position, side"):
                values = array("f")
                values.frombytes(blob)
                vectors.setdefault(position, []).append(values.tolist())
        return vectors

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)
