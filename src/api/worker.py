"""
翻译任务 worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable
from contextlib import suppress

from ..utils.config_loader import get_config
from .services.translation_service import TranslationService


class TaskWorker:
    """轮询数据库队列并执行翻译任务"""

    def __init__(
        self,
        poll_interval: float | None = None,
        stale_after_seconds: int | None = None,
        max_parallel_tasks: int | None = None,
        worker_name: str | None = None,
    ):
        config = get_config()
        self.service = TranslationService()
        self.poll_interval = poll_interval if poll_interval is not None else config.worker_poll_interval
        self.stale_after_seconds = stale_after_seconds if stale_after_seconds is not None else config.worker_stale_after
        self.max_parallel_tasks = (
            max_parallel_tasks if max_parallel_tasks is not None else config.worker_max_parallel_tasks
        )
        self.worker_name = worker_name or f"{socket.gethostname()}-{os.getpid()}"
        self._stop_event = asyncio.Event()
        self._active_tasks: set[asyncio.Task] = set()
        self.logger = logging.getLogger(__name__)

    async def run_forever(self):
        """持续轮询并处理任务"""
        await self._initialize_service()

        self.logger.info("🎯 Worker 已启动: %s", self.worker_name)

        idle_rounds = 0
        heartbeat_at = 0.0
        while not self._stop_event.is_set():
            now = asyncio.get_running_loop().time()
            if now - heartbeat_at >= 5:
                await self.service.db.worker_heartbeat(self.worker_name)
                heartbeat_at = now
            if idle_rounds % 30 == 0:
                reclaimed = await self.service.requeue_stale_tasks(self.stale_after_seconds)
                if reclaimed:
                    self.logger.warning("♻️ 运行时重新排队了 %s 个陈旧任务", reclaimed)

            started = await self._fill_capacity()
            if started == 0 and not self._active_tasks:
                idle_rounds += 1
                await self._wait_or_timeout(self.poll_interval)
                continue

            if started == 0:
                await self._wait_for_completion_or_timeout(self.poll_interval)
                continue

            idle_rounds = 0

    async def stop(self):
        """停止 worker"""
        self._stop_event.set()
        if self._active_tasks:
            for task in self._active_tasks:
                task.cancel()
            await asyncio.gather(*self._active_tasks, return_exceptions=True)

    async def _wait_or_timeout(self, timeout: float):
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)

    async def run_until_idle(self):
        """处理直到当前队列被消费完，便于测试和批处理模式"""
        await self._initialize_service()
        while not self._stop_event.is_set():
            started = await self._fill_capacity()
            if started == 0 and not self._active_tasks:
                break
            await self._wait_for_completion_or_timeout(self.poll_interval)

    async def _initialize_service(self):
        if hasattr(self.service, "db") and hasattr(self.service.db, "initialize"):
            await self.service.db.initialize()
        if hasattr(self.service, "requeue_stale_tasks"):
            reclaimed = await self.service.requeue_stale_tasks(self.stale_after_seconds)
            if reclaimed:
                self.logger.warning("♻️ 重新排队了 %s 个中断任务", reclaimed)

    async def _fill_capacity(self) -> int:
        started = 0
        while not self._stop_event.is_set() and len(self._active_tasks) < self.max_parallel_tasks:
            task = (
                await self.service.claim_next_pending_task(
                    worker_id=self.worker_name, lease_seconds=self.stale_after_seconds
                )
                if isinstance(self.service, TranslationService)
                else await self.service.claim_next_pending_task()
            )
            if not task:
                break

            self.logger.info("📥 Worker 认领任务: %s (%s)", task.task_id, task.filename)
            options = {"already_claimed": True}
            if hasattr(task, "execution_id"):
                options["execution_id"] = task.execution_id
            self._spawn_task(self.service.start_translation(task.task_id, **options))
            started += 1

        return started

    def _spawn_task(self, coroutine: Awaitable):
        task = asyncio.create_task(coroutine)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        task.add_done_callback(self._log_task_exception)

    def _log_task_exception(self, task: asyncio.Task):
        with suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc:
                self.logger.exception("❌ Worker 子任务异常", exc_info=exc)

    async def _wait_for_completion_or_timeout(self, timeout: float):
        if not self._active_tasks:
            await self._wait_or_timeout(timeout)
            return

        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                self._active_tasks | {stop_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not stop_task.done():
                stop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_task

        for task in done:
            if task in self._active_tasks:
                with suppress(Exception):
                    await task
