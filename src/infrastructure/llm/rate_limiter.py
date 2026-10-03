"""
速率限制器基础设施
"""

import asyncio
import time


class RateLimiter:
    """
    Token Bucket 速率限制器
    """

    def __init__(self, rate: int, per: int = 60):
        self.rate = rate
        self.per = per
        self.allowance = rate
        self.last_check = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            current = time.monotonic()
            time_passed = current - self.last_check
            self.last_check = current

            self.allowance += time_passed * (self.rate / self.per)
            if self.allowance > self.rate:
                self.allowance = self.rate

            if self.allowance < 1.0:
                sleep_time = (1.0 - self.allowance) * (self.per / self.rate)
                await asyncio.sleep(sleep_time)
                self.last_check = time.monotonic()
                self.allowance = 0.0
            else:
                self.allowance -= 1.0
