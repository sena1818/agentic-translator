"""
LLM 调用重试

免费/共享资源池常见 502/503 与短期限流，这类错误重试即可恢复；
4xx 请求错误与每日额度耗尽重试无意义，直接抛出。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

from .rate_limits import QuotaExhaustedError, classify

_RETRYABLE_CLIENT_STATUS = {408, 409, 429}


def backoff_delay(attempt: int, cap: float = 60.0) -> float:
    """指数退避加抖动：在 [d/2, d] 内随机取值，并发失败的请求不会在同一时刻一起重试。"""
    ceiling = min(cap, 2.0**attempt)
    return ceiling / 2 + random.uniform(0, ceiling / 2)


def rate_limit_delay(attempt: int, hint: float | None) -> float:
    """限流退避：服务端给出的等待时间比固定指数退避更可靠；没有提示时按分钟级窗口退避（上限 2 分钟）。

    抖动只往后加，不会早于服务端要求的时间重试。
    """
    base = min((hint + 1) if hint else 15 * (attempt + 1), 120)
    return base + random.uniform(0, max(1.0, base * 0.2))


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, QuotaExhaustedError):
        return False
    status = getattr(exc, "status_code", None)
    return not (status is not None and 400 <= status < 500 and status not in _RETRYABLE_CLIENT_STATUS)


async def wait_before_retry(exc: Exception, attempt: int) -> None:
    kind, hint = classify(exc)
    await asyncio.sleep(rate_limit_delay(attempt, hint) if kind == "rate" else backoff_delay(attempt))


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
        except Exception as exc:
            if not is_retryable(exc) or attempt >= max_retries:
                raise
            await wait_before_retry(exc, attempt)
    raise RuntimeError("unreachable")
