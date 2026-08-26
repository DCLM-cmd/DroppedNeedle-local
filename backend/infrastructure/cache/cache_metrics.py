"""Cache hit-ratio instrumentation (QW9 Part 2).

Windowed, per-prefix counters for the shared in-memory cache plus a thin
delegating wrapper that records ``get``/``set``/``delete`` traffic.

Semantics (deliberate, per the QW9 plan):
- Counters are process-local state. They are correct only under the production
  single-worker invariant (``uvicorn --workers 1``, findings.md §1); additional
  workers would each report private numbers.
- Counters reset on process restart.
- Cache clears deliberately do NOT reset counters: ratios measure the
  observation window, not cache contents.
- No locks: at one worker the only concurrency is event-loop interleaving and
  every mutation below runs synchronously between awaits.
- Zero new cache prefixes are introduced here; the prefix registry is derived
  from ``cache_keys.py`` so new prefixes join automatically.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from infrastructure.cache import cache_keys as _cache_keys
from infrastructure.cache.memory_cache import CacheInterface

CounterKey = tuple[str, ...]

_FIELD_BY_OP = {"hit": "hits", "miss": "misses", "set": "sets"}


def build_prefix_registry() -> tuple[tuple[str, str], ...]:
    """Build the prefix registry once from ``cache_keys.py`` module attributes
    ending in ``_PREFIX`` (single source of truth), longest first so
    longest-match wins during lookup."""
    entries = [
        (value, name)
        for name, value in vars(_cache_keys).items()
        if name.endswith("_PREFIX") and isinstance(value, str) and value
    ]
    entries.sort(key=lambda entry: len(entry[0]), reverse=True)
    return tuple(entries)


_PREFIX_REGISTRY = build_prefix_registry()


def prefix_label(key: str) -> str:
    """Longest registered ``*_PREFIX`` match for *key*; unmatched keys fall
    back to their first ``:`` segment, or ``"other"`` without a colon."""
    for prefix, _name in _PREFIX_REGISTRY:
        if key.startswith(prefix):
            return prefix
    head, sep, _rest = key.partition(":")
    return head if sep else "other"


class WindowedCounterMap:
    """Fixed-ring windowed counters keyed by a hashable label tuple.

    Slots are indexed by monotonic-clock bucket epoch; each slot remembers the
    absolute epoch it was last written for, so stale slots are zeroed lazily on
    ring reuse instead of via a background sweeper. Cumulative totals live
    beside the ring so snapshots can pair windowed counts with lifetime sums.
    """

    __slots__ = (
        "_buckets",
        "_bucket_seconds",
        "_clock",
        "_stamps",
        "_counts",
        "_totals",
    )

    def __init__(
        self,
        buckets: int = 360,
        bucket_seconds: float = 10.0,
        clock: Callable[[], float] | None = None,
    ):
        self._buckets = max(1, int(buckets))
        self._bucket_seconds = max(0.001, float(bucket_seconds))
        self._clock = clock or time.monotonic
        self._stamps: list[int] = [-1] * self._buckets
        self._counts: dict[CounterKey, list[int]] = {}
        self._totals: dict[CounterKey, int] = {}

    @property
    def window_seconds(self) -> int:
        return int(self._buckets * self._bucket_seconds)

    @property
    def bucket_seconds(self) -> float:
        return self._bucket_seconds

    def increment(self, key: CounterKey, amount: int = 1) -> None:
        epoch = int(self._clock() // self._bucket_seconds)
        idx = epoch % self._buckets
        if self._stamps[idx] != epoch:
            # Ring slot reuse: evict whatever older epoch still sits there.
            self._stamps[idx] = epoch
            for counts in self._counts.values():
                counts[idx] = 0
        counts = self._counts.get(key)
        if counts is None:
            counts = self._counts[key] = [0] * self._buckets
        counts[idx] += amount
        self._totals[key] = self._totals.get(key, 0) + amount

    def totals(self) -> dict[CounterKey, int]:
        return dict(self._totals)

    def snapshot(self, window_seconds: float | None = None) -> dict[CounterKey, int]:
        """Sum of each key's counts over the trailing *window_seconds* (the
        full ring when omitted). Slots from epochs no longer covered by the
        window contribute nothing."""
        now_epoch = int(self._clock() // self._bucket_seconds)
        if window_seconds is None:
            span = self._buckets
        else:
            span = min(
                self._buckets,
                max(1, math.ceil(window_seconds / self._bucket_seconds)),
            )
        result: dict[CounterKey, int] = {}
        for key, counts in self._counts.items():
            total = 0
            for epoch in range(now_epoch - span + 1, now_epoch + 1):
                idx = epoch % self._buckets
                if self._stamps[idx] == epoch:
                    total += counts[idx]
            result[key] = total
        return result


class InstrumentedCache(CacheInterface):
    """Thin delegating wrapper recording hit/miss/set/delete per cache prefix.

    ``expired`` reads count as misses because the inner cache returns ``None``
    for expired entries (matching ``memory_cache.py`` semantics). Every other
    method passes straight through untouched, so ``size()`` /
    ``estimate_memory_bytes()`` / ``get_stats()`` keep their exact behavior for
    ``CacheService``.
    """

    def __init__(self, inner: CacheInterface):
        self._inner = inner
        self._counters = WindowedCounterMap()
        self._since = int(time.time())

    async def get(self, key: str) -> Any | None:
        value = await self._inner.get(key)
        self._counters.increment(
            (prefix_label(key), "hit" if value is not None else "miss")
        )
        return value

    async def set(self, key: str, value: Any, ttl_seconds: int = 60) -> None:
        self._counters.increment((prefix_label(key), "set"))
        await self._inner.set(key, value, ttl_seconds)

    async def delete(self, key: str) -> None:
        self._counters.increment((prefix_label(key), "delete"))
        await self._inner.delete(key)

    async def clear(self) -> None:
        await self._inner.clear()

    async def clear_prefix(self, prefix: str) -> int:
        return await self._inner.clear_prefix(prefix)

    async def cleanup_expired(self) -> int:
        return await self._inner.cleanup_expired()

    async def peek(self, key: str) -> Any | None:
        # Pass-through with NO counter recording: peeks are the stale-serving
        # escape hatch (QW11 Part 3) and must not perturb hit/miss ratios.
        return await self._inner.peek(key)

    def size(self) -> int:
        return self._inner.size()

    def estimate_memory_bytes(self) -> int:
        return self._inner.estimate_memory_bytes()

    def get_stats(self) -> dict[str, Any]:
        return self._inner.get_stats()

    @property
    def counters_since(self) -> int:
        """Wall-clock epoch seconds when this process started counting."""
        return self._since

    def op_totals(self) -> dict[CounterKey, int]:
        """Lifetime operation totals keyed ``(prefix, op)``; test/introspection seam."""
        return self._counters.totals()

    def per_prefix_rows(
        self, window_seconds: float | None = None
    ) -> list[dict[str, Any]]:
        """Window-scoped per-prefix rows for the ``/cache/stats`` payload."""
        window = (
            self._counters.window_seconds
            if window_seconds is None
            else int(window_seconds)
        )
        grouped: dict[str, dict[str, int]] = {}
        for (prefix, op), count in self._counters.snapshot(window).items():
            field = _FIELD_BY_OP.get(op)
            if field is not None:
                grouped.setdefault(prefix, {"hits": 0, "misses": 0, "sets": 0})[
                    field
                ] = count
        rows: list[dict[str, Any]] = []
        for prefix in sorted(grouped):
            ops = grouped[prefix]
            attempts = ops["hits"] + ops["misses"]
            rate = (ops["hits"] / attempts * 100) if attempts else 0.0
            rows.append(
                {
                    "prefix": prefix,
                    "hits": ops["hits"],
                    "misses": ops["misses"],
                    "sets": ops["sets"],
                    "hit_rate_percent": round(rate, 2),
                    "window_seconds": window,
                }
            )
        return rows

    def observability(self, window_seconds: float | None = None) -> dict[str, Any]:
        """Everything ``CacheService.get_stats()`` needs for the additive ratio
        fields; global numbers reuse the inner cache's own cumulative
        computation rather than duplicating arithmetic."""
        stats = self._inner.get_stats()
        return {
            "memory_hits": stats["hits"],
            "memory_misses": stats["misses"],
            "memory_hit_rate_percent": stats["hit_rate_percent"],
            "per_prefix": self.per_prefix_rows(window_seconds),
            "counters_since": self._since,
        }
