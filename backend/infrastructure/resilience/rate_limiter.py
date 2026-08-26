import asyncio
import heapq
import itertools
import math
import time
from typing import Optional

EPSILON = 1e-9


class TokenBucketRateLimiter:
    def __init__(self, rate: float, capacity: Optional[int] = None):
        self.rate = rate
        self.capacity = capacity or int(rate * 2)
        self._tokens = float(self.capacity)
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()
        self._waiters: list[tuple[int, int, int, asyncio.Future[None]]] = []
        self._waiter_sequence = itertools.count()

    def _grant_waiters_locked(self) -> None:
        while self._waiters:
            _, _, tokens, future = self._waiters[0]
            if future.done():
                heapq.heappop(self._waiters)
                continue
            if self._tokens < tokens - EPSILON:
                return
            heapq.heappop(self._waiters)
            self._tokens -= tokens
            future.set_result(None)

    async def acquire(self, tokens: int = 1, priority: int = 0) -> None:
        if tokens > self.capacity:
            raise ValueError(
                f"Cannot acquire {tokens} tokens (capacity: {self.capacity}). "
                f"Request would wait indefinitely."
            )

        async with self._lock:
            self._refresh_tokens()
            if not self._waiters and self._tokens >= tokens - EPSILON:
                self._tokens -= tokens
                return
            future = asyncio.get_running_loop().create_future()
            waiter = (int(priority), next(self._waiter_sequence), tokens, future)
            heapq.heappush(self._waiters, waiter)

        try:
            while not future.done():
                async with self._lock:
                    self._refresh_tokens()
                    self._grant_waiters_locked()
                    if future.done():
                        break
                    next_tokens = self._waiters[0][2]
                    wait_time = max(
                        (next_tokens - self._tokens) / self.rate,
                        EPSILON,
                    )
                await asyncio.sleep(wait_time)
            await future
        except asyncio.CancelledError:
            async with self._lock:
                if future.done() and not future.cancelled():
                    self._tokens = min(
                        float(self.capacity),
                        self._tokens + tokens,
                    )
                else:
                    future.cancel()
                    self._waiters = [
                        queued for queued in self._waiters if queued is not waiter
                    ]
                    heapq.heapify(self._waiters)
                self._grant_waiters_locked()
            raise

    async def try_acquire(self, tokens: int = 1) -> bool:
        async with self._lock:
            self._refresh_tokens()
            if self._waiters:
                return False

            if self._tokens >= tokens - EPSILON:
                self._tokens -= tokens
                return True
            return False

    def _refresh_tokens(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_update
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_update = now

    @property
    def remaining(self) -> int:
        self._refresh_tokens()
        return max(0, int(self._tokens))

    def retry_after(self, tokens: int = 1) -> float:
        self._refresh_tokens()
        if self._tokens >= tokens - EPSILON:
            return 0.0
        deficit = tokens - self._tokens
        return math.ceil(deficit / self.rate)

    def reset(self) -> None:
        self._tokens = float(self.capacity)
        self._last_update = time.monotonic()

    def update_capacity(self, new_capacity: int) -> None:
        self.capacity = new_capacity
        self._tokens = min(self._tokens, float(new_capacity))

    def update_rate(self, new_rate: float) -> None:
        """Update the token refill rate in tokens per second."""
        if new_rate <= 0:
            raise ValueError(f"Rate must be positive, got {new_rate}")
        self.rate = new_rate
