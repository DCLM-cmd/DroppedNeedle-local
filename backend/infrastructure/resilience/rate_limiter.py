import asyncio
import heapq
import itertools
import math
import time
from collections import OrderedDict
from typing import Callable, Generic, Optional, TypeVar

EPSILON = 1e-9

_T = TypeVar("_T")


class BoundedTTLMap(Generic[_T]):
    """Per-client state keyed by identity, bounded in both age and count.

    A rate limiter that keys by client needs somewhere to keep one bucket per
    client, and that map is itself an attack surface - unbounded, a stream of
    fresh identities grows it forever. Entries expire after ``ttl_seconds`` idle
    and the oldest is evicted past ``max_entries``; evicting a bucket only
    refills that client's allowance, so the failure mode is leniency, never a
    wrongly-rejected request."""

    def __init__(
        self,
        *,
        max_entries: int,
        ttl_seconds: float,
        factory: Callable[[], _T],
        clock: Callable[[], float],
    ) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._factory = factory
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, _T]] = OrderedDict()

    def get(self, key: str) -> _T:
        now = self._clock()
        self._evict_expired(now)
        current = self._items.pop(key, None)
        value = current[1] if current else self._factory()
        self._items[key] = (now, value)
        if len(self._items) > self._max_entries:
            self._items.popitem(last=False)
        return value

    def discard(self, key: str) -> None:
        self._items.pop(key, None)

    def clear(self) -> None:
        self._items.clear()

    def _evict_expired(self, now: float) -> None:
        cutoff = now - self._ttl_seconds
        while self._items:
            _, (last_seen, _) = next(iter(self._items.items()))
            if last_seen > cutoff:
                break
            self._items.popitem(last=False)

    def __len__(self) -> int:
        self._evict_expired(self._clock())
        return len(self._items)

    def keys(self) -> tuple[str, ...]:
        self._evict_expired(self._clock())
        return tuple(self._items)


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
