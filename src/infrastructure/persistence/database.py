"""任务存储：原子认领、租约、执行版本和终态保护。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ...domain.models.task_models import TaskProgress, TaskStatus, TranslationTask

_JSON_FIELDS = {"config_snapshot", "glossary_snapshot", "retry_indices", "quality_summary"}
_EXTRA_COLUMNS = {
    "bilingual": "INTEGER DEFAULT 0",
    "stage": "TEXT DEFAULT 'queued'",
    "execution_id": "TEXT",
    "worker_id": "TEXT",
    "lease_until": "TEXT",
    "artifact_dir": "TEXT",
    "config_snapshot": "TEXT DEFAULT '{}'",
    "glossary_snapshot": "TEXT DEFAULT '{}'",
    "retry_indices": "TEXT DEFAULT '[]'",
    "quality_summary": "TEXT DEFAULT '{}'",
    "author": "TEXT",
    "domain": "TEXT",
}
_TERMINAL = {s.value for s in TaskStatus} - {"pending", "processing"}


class Database:
    def __init__(self, db_path: str = "data/translation.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect_sync(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    async def _run(self, fn):
        def execute():
            with closing(self._connect_sync()) as db, db:
                return fn(db)

        return await asyncio.to_thread(execute)

    async def initialize(self):
        def init(db):
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL,
                glossary_id TEXT, progress_current INTEGER DEFAULT 0, progress_total INTEGER DEFAULT 0,
                progress_percentage REAL DEFAULT 0, progress_speed REAL DEFAULT 0,
                progress_elapsed REAL DEFAULT 0, result_url TEXT, error TEXT,
                created_at TEXT, updated_at TEXT)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
            for name, definition in _EXTRA_COLUMNS.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_created_at ON tasks(status, created_at)")
            db.execute("CREATE TABLE IF NOT EXISTS workers (worker_id TEXT PRIMARY KEY, heartbeat TEXT NOT NULL)")

        await self._run(init)

    def _values(self, task):
        data = {f.name: getattr(task, f.name) for f in fields(task) if f.name != "progress"}
        data["status"] = task.status.value
        data["bilingual"] = int(task.bilingual)
        for name in ("created_at", "updated_at"):
            data[name] = data[name].astimezone(timezone.utc).isoformat() if data[name] else None
        for name in _JSON_FIELDS:
            data[name] = json.dumps(data[name], ensure_ascii=False)
        for name, value in task.progress.to_dict().items():
            data[f"progress_{name}"] = value
        return data

    async def save_task(self, task):
        data = self._values(task)
        names = ",".join(data)
        placeholders = ",".join("?" for _ in data)
        await self._run(
            lambda db: db.execute(f"INSERT INTO tasks ({names}) VALUES ({placeholders})", tuple(data.values()))
        )

    async def update_task(self, task) -> bool:
        task.updated_at = datetime.now(timezone.utc)
        data = self._values(task)
        for key in ("task_id", "created_at", "execution_id", "worker_id", "lease_until"):
            data.pop(key)
        assignments = ",".join(f"{name}=?" for name in data)
        # 唯有当前执行版本可推进 processing，终态不能被旧对象覆盖。
        sql = f"UPDATE tasks SET {assignments} WHERE task_id=? AND status NOT IN ('completed','partial_success','failed','cancelled') AND execution_id IS ?"
        if task.execution_id:
            sql += " AND status='processing' AND lease_until >= ?"
        params = (*data.values(), task.task_id, task.execution_id)
        if task.execution_id:
            params += (datetime.now(timezone.utc).isoformat(),)
        return await self._run(lambda db: db.execute(sql, params).rowcount > 0)

    async def get_task(self, task_id):
        return await self._run(
            lambda db: self._row_to_task(db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
        )

    async def list_tasks(self, skip=0, limit=20, search="", status=None):
        where = "WHERE filename LIKE ?"
        params = [f"%{search}%"]
        if status:
            where += " AND status=?"
            params.append(status)

        def query(db):
            count = db.execute(f"SELECT COUNT(*) FROM tasks {where}", params).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*params, limit, skip)
            ).fetchall()
            return [self._row_to_task(row) for row in rows], count

        return await self._run(query)

    async def task_stats(self):
        return await self._run(
            lambda db: {
                row["status"]: row["n"] for row in db.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
            }
        )

    async def delete_task(self, task_id):
        return await self._run(
            lambda db: (
                db.execute(
                    "DELETE FROM tasks WHERE task_id=? AND status NOT IN ('pending','processing')", (task_id,)
                ).rowcount
                > 0
            )
        )

    async def claim_next_pending_task(self, worker_id="direct", lease_seconds=900, task_id=None):
        def claim(db):
            db.execute("BEGIN IMMEDIATE")
            query = "SELECT * FROM tasks WHERE status='pending'"
            args = ()
            if task_id:
                query += " AND task_id=?"
                args = (task_id,)
            row = db.execute(query + " ORDER BY created_at ASC LIMIT 1", args).fetchone()
            if row is None:
                return None
            now = datetime.now(timezone.utc)
            db.execute(
                """UPDATE tasks SET status='processing', stage='converting', error=NULL,
                execution_id=?, worker_id=?, lease_until=?, updated_at=? WHERE task_id=? AND status='pending'""",
                (
                    uuid.uuid4().hex,
                    worker_id,
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    row["task_id"],
                ),
            )
            return self._row_to_task(db.execute("SELECT * FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone())

        return await self._run(claim)

    async def heartbeat_task(self, task_id, execution_id, lease_seconds=900):
        now = datetime.now(timezone.utc)
        return await self._run(
            lambda db: (
                db.execute(
                    """UPDATE tasks SET lease_until=?
            WHERE task_id=? AND execution_id=? AND status='processing' AND lease_until>=?""",
                    ((now + timedelta(seconds=lease_seconds)).isoformat(), task_id, execution_id, now.isoformat()),
                ).rowcount
                > 0
            )
        )

    async def execution_is_current(self, task_id, execution_id):
        return await self._run(
            lambda db: (
                db.execute(
                    """SELECT 1 FROM tasks
            WHERE task_id=? AND execution_id=? AND status='processing' AND lease_until>=?""",
                    (task_id, execution_id, datetime.now(timezone.utc).isoformat()),
                ).fetchone()
                is not None
            )
        )

    async def requeue_stale_processing_tasks(self, stale_after_seconds=900):
        now = datetime.now(timezone.utc)
        before = (now - timedelta(seconds=stale_after_seconds)).isoformat()
        return await self._run(
            lambda db: (
                db.execute(
                    """UPDATE tasks SET status='pending', stage='queued',
            execution_id=NULL, worker_id=NULL, lease_until=NULL, error='任务因 worker 中断被重新排队', updated_at=?
            WHERE status='processing' AND ((lease_until IS NOT NULL AND lease_until<?)
            OR (lease_until IS NULL AND updated_at<?))""",
                    (now.isoformat(), now.isoformat(), before),
                ).rowcount
            )
        )

    async def cancel_task(self, task_id):
        return await self._run(
            lambda db: (
                db.execute(
                    """UPDATE tasks SET status='cancelled', stage='cancelled',
            error='任务已被取消', updated_at=? WHERE task_id=? AND status IN ('pending','processing')""",
                    (datetime.now(timezone.utc).isoformat(), task_id),
                ).rowcount
                > 0
            )
        )

    async def retry_task(self, task_id, indices=None):
        return await self._run(
            lambda db: (
                db.execute(
                    """UPDATE tasks SET status='pending', stage='queued',
            execution_id=NULL, worker_id=NULL, lease_until=NULL, error=NULL, retry_indices=?, updated_at=?
            WHERE task_id=? AND status IN ('completed','partial_success','failed','cancelled')""",
                    (json.dumps(indices or []), datetime.now(timezone.utc).isoformat(), task_id),
                ).rowcount
                > 0
            )
        )

    async def replace_artifacts(self, task_id, expected_dir, artifact_dir, quality_summary):
        failed = quality_summary.get("failed", 0)
        review = quality_summary.get("needs_review", 0)
        total = quality_summary.get("total", 0)
        status = "failed" if failed == total and total else ("partial_success" if failed or review else "completed")
        error = f"{failed} 个块失败，{review} 个块待复核" if failed or review else None
        return await self._run(
            lambda db: (
                db.execute(
                    """UPDATE tasks SET artifact_dir=?, quality_summary=?, updated_at=?, status=?, error=?, result_url=?
            WHERE task_id=? AND artifact_dir IS ? AND status IN ('completed','partial_success','failed')""",
                    (
                        artifact_dir,
                        json.dumps(quality_summary),
                        datetime.now(timezone.utc).isoformat(),
                        status,
                        error,
                        f"/api/files/results/{task_id}" if status != "failed" else None,
                        task_id,
                        expected_dir,
                    ),
                ).rowcount
                > 0
            )
        )

    async def worker_heartbeat(self, worker_id):
        await self._run(
            lambda db: db.execute(
                "INSERT OR REPLACE INTO workers VALUES (?,?)", (worker_id, datetime.now(timezone.utc).isoformat())
            )
        )

    async def online_workers(self, age_seconds=30):
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
        return await self._run(
            lambda db: db.execute("SELECT COUNT(*) FROM workers WHERE heartbeat>=?", (cutoff,)).fetchone()[0]
        )

    def _row_to_task(self, row):
        if row is None:
            return None
        data = {f.name: row[f.name] for f in fields(TranslationTask) if f.name != "progress" and f.name in row.keys()}
        data["status"] = TaskStatus(data["status"])
        data["bilingual"] = bool(data.get("bilingual"))
        for name in ("created_at", "updated_at"):
            data[name] = datetime.fromisoformat(data[name]) if data.get(name) else None
        for name in _JSON_FIELDS:
            data[name] = json.loads(data.get(name) or ("[]" if name == "retry_indices" else "{}"))
        data["progress"] = TaskProgress(**{name: row[f"progress_{name}"] for name in TaskProgress.__dataclass_fields__})
        return TranslationTask(**data)
