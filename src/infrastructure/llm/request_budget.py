"""跨 worker 的账号请求预算。SQLite 共享并发租约和滚动 RPM 窗口。"""

import asyncio
import hashlib
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, closing


class RequestBudget:
    def __init__(self, config):
        self.path = config.root_dir / "data" / "request_budget.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.account = hashlib.sha256((config.api_base_url + (config.api_key or "")).encode()).hexdigest()
        self.limit = max(1, config.max_concurrent)
        self.rate = max(1, config.rate_limit)
        self.timeout = float(config.get("concurrency.request_timeout", 60))

    def _try(self, token):
        now = time.time()
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests (token TEXT PRIMARY KEY, account TEXT, started REAL, expires REAL, active INTEGER)"
            )
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM requests WHERE started<? AND expires<?", (now - 60, now))
            active = db.execute(
                "SELECT COUNT(*) FROM requests WHERE account=? AND active=1 AND expires>?", (self.account, now)
            ).fetchone()[0]
            recent = db.execute(
                "SELECT COUNT(*) FROM requests WHERE account=? AND started>?", (self.account, now - 60)
            ).fetchone()[0]
            if active >= self.limit or recent >= self.rate:
                return False
            db.execute("INSERT INTO requests VALUES (?,?,?,?,1)", (token, self.account, now, now + self.timeout + 5))
            return True

    def _release(self, token):
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("UPDATE requests SET active=0 WHERE token=?", (token,))

    @asynccontextmanager
    async def request(self):
        token = uuid.uuid4().hex
        acquired = False
        try:
            while not acquired:
                attempt = asyncio.create_task(asyncio.to_thread(self._try, token))
                try:
                    acquired = await asyncio.shield(attempt)
                except asyncio.CancelledError:
                    acquired = await attempt
                    raise
                if not acquired:
                    await asyncio.sleep(0.1)
            yield
        finally:
            if acquired:
                await asyncio.shield(asyncio.to_thread(self._release, token))
