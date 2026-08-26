"""Process-wide, coalesced, cardinality-free public bootstrap demand signal.

(GH-293) ``GET /api/v1/auth/setup/status`` is the first database read on the
unauthenticated SPA bootstrap path. Sustained background identity writes can
starve that read (WAL pressure + lock contention). This signal lets the setup
route record public bootstrap demand without per-request or per-client state:

- one process-wide counter plus a monotonic last-demand timestamp (coalesced,
  cardinality-free, no client identity)
- background identity producers wait at most ``PUBLIC_DEMAND_MAX_HOLD_SECONDS``
  (5 s, owner calibration) for demand to clear, then proceed regardless
- the 5 s hard hold means new requests cannot extend a background wait, so the
  forced-fairness progress floor (at least one subject per 120 s) is guaranteed
  by construction: each background unit costs unit time plus at most 5 s of
  waiting
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable

from infrastructure.persistence.gh293_calibration import (
    PUBLIC_DEMAND_MAX_HOLD_SECONDS,
)


class BootstrapDemandSignal:
    """Coalesced public bootstrap demand + bounded latency/error telemetry.

    The latency ring is fixed-size (no unbounded retention) and stores only
    durations, never client identity or paths.
    """

    def __init__(
        self,
        *,
        max_hold_seconds: float = PUBLIC_DEMAND_MAX_HOLD_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        latency_ring_size: int = 512,
    ) -> None:
        self._max_hold_seconds = max_hold_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._active_requests = 0
        self._last_demand_at: float | None = None
        self._latency_ring: list[float] = []
        self._latency_ring_size = max(1, latency_ring_size)
        self._request_count = 0
        self._error_count = 0

    def begin(self) -> None:
        """Record that one public bootstrap read is in flight."""
        with self._lock:
            self._active_requests += 1
            self._last_demand_at = self._monotonic()

    def end(self) -> None:
        """Release a completed (or failed/timed-out) bootstrap read."""
        with self._lock:
            if self._active_requests <= 0:
                raise RuntimeError("No public bootstrap demand is active")
            self._active_requests -= 1
            if self._active_requests == 0:
                self._last_demand_at = self._monotonic()

    def observe_latency(self, seconds: float, *, error: bool = False) -> None:
        """Record one bounded setup-status latency sample (and error status)."""
        with self._lock:
            self._request_count += 1
            if error:
                self._error_count += 1
            self._latency_ring.append(max(0.0, seconds))
            if len(self._latency_ring) > self._latency_ring_size:
                del self._latency_ring[: len(self._latency_ring) - self._latency_ring_size]

    def latency_snapshot(self) -> dict[str, object]:
        """Bounded summary: count, errors, min/max/p50/p95/p99 over the ring."""
        with self._lock:
            count = self._request_count
            errors = self._error_count
            values = list(self._latency_ring)
        if not values:
            return {"count": count, "errors": errors, "samples": 0}
        ordered = sorted(values)

        def percentile(rank: float) -> float:
            position = max(0, min(len(ordered) - 1, int(rank * len(ordered)) - 1))
            return ordered[position]

        return {
            "count": count,
            "errors": errors,
            "samples": len(ordered),
            "minimum": ordered[0],
            "maximum": ordered[-1],
            "p50": percentile(0.50),
            "p95": percentile(0.95),
            "p99": percentile(0.99),
        }

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active_requests > 0

    @property
    def last_demand_at(self) -> float | None:
        with self._lock:
            return self._last_demand_at

    async def wait_until_idle(self) -> None:
        """Wait for public bootstrap demand to clear, capped at the max hold.

        Never blocks longer than ``max_hold_seconds``: under sustained demand a
        background producer proceeds after the hold, which is the forced
        fairness slice. New requests cannot extend an in-progress wait.
        """
        deadline = self._monotonic() + self._max_hold_seconds
        while self.active:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.05, remaining))
