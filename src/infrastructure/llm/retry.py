"""
LLM 调用重试

免费/共享资源池常见 502/503 与短期限流，这类错误重试即可恢复；
4xx 请求错误与每日额度耗尽重试无意义，直接抛出。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .rate_limits import QuotaExhaustedError, classify

_RETRYABLE_CLIENT_STATUS = {408, 409, 429}


async def ainvoke_with_retries(
    chain,
    payload: Any,
    max_retries: int,
    before_attempt: Callable[[], Awaitable[None]] | None = None,
) -> tuple[Any, int]:
    """调用 chain，返回 (结果, 实际重试次数)。"""
    for attempt in range(max_retries + 1):
        try:
            if before_attempt is not None:
                await before_attempt()
            return await chain.ainvoke(payload), attempt
        except QuotaExhaustedError:
            raise
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status is not None and 400 <= status < 500 and status not in _RETRYABLE_CLIENT_STATUS:
                raise
            if attempt >= max_retries:
                raise
            kind, wait = classify(exc)
            if kind == "rate":
                # 服务端给出的等待时间比固定指数退避更可靠；没有提示时按分钟级窗口退避（上限 2 分钟）
                await asyncio.sleep(min((wait + 1) if wait else 15 * (attempt + 1), 120))
            else:
                await asyncio.sleep(2**attempt)
    raise RuntimeError("unreachable")
