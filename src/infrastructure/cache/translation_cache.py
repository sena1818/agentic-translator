"""
翻译缓存基础设施
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import aiosqlite
except ImportError:  # pragma: no cover - 允许轻量测试环境导入模块
    aiosqlite = None


@dataclass
class CacheEntry:
    cache_key: str
    chunk_id: str
    translation: str
    quality_report: dict
    repaired: bool


class TranslationCache:
    """基于 SQLite 的 chunk 翻译缓存"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False

    def _ensure_backend(self):
        if aiosqlite is None:
            raise ImportError("aiosqlite 未安装，无法使用翻译缓存")

    async def initialize(self):
        if self._initialized:
            return
        self._ensure_backend()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS translation_cache (
                    cache_key TEXT PRIMARY KEY,
                    chunk_id TEXT NOT NULL,
                    translation TEXT NOT NULL,
                    quality_report TEXT,
                    repaired INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.commit()

            await db.execute(
                "CREATE TABLE IF NOT EXISTS document_profiles (profile_key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            await db.commit()

        self._initialized = True

    async def get(self, cache_key: str) -> CacheEntry | None:
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM translation_cache WHERE cache_key = ?",
                (cache_key,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            return None

        return CacheEntry(
            cache_key=row["cache_key"],
            chunk_id=row["chunk_id"],
            translation=row["translation"],
            quality_report=json.loads(row["quality_report"] or "{}"),
            repaired=bool(row["repaired"]),
        )

    async def set(
        self,
        cache_key: str,
        chunk_id: str,
        translation: str,
        quality_report: dict,
        repaired: bool,
    ):
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO translation_cache (
                    cache_key,
                    chunk_id,
                    translation,
                    quality_report,
                    repaired,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    chunk_id,
                    translation,
                    json.dumps(quality_report, ensure_ascii=False),
                    int(repaired),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()

    async def get_profile(self, key):
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT payload FROM document_profiles WHERE profile_key=?", (key,))
            row = await cursor.fetchone()
        return json.loads(row[0]) if row else None

    async def set_profile(self, key, payload):
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO document_profiles VALUES (?,?)", (key, json.dumps(payload, ensure_ascii=False))
            )
            await db.commit()
