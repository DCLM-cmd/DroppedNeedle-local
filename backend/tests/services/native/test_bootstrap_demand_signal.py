"""GH-293 public bootstrap demand signal tests.

The signal is process-wide, coalesced, cardinality-free, and its wait is capped
by an absolute maximum hold that new requests cannot extend; latency telemetry
is bounded.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from services.native.bootstrap_demand_signal import BootstrapDemandSignal


def test_begin_end_balance_and_active_state() -> None:
    signal = BootstrapDemandSignal()
    assert signal.active is False
    signal.begin()
    assert signal.active is True
    assert signal.last_demand_at is not None
    signal.begin()  # two concurrent bootstrap reads
    signal.end()
    assert signal.active is True
    signal.end()
    assert signal.active is False


def test_end_without_begin_raises() -> None:
    signal = BootstrapDemandSignal()
    with pytest.raises(RuntimeError):
        signal.end()


@pytest.mark.asyncio
async def test_wait_returns_immediately_when_idle() -> None:
    signal = BootstrapDemandSignal()
    started = time.monotonic()
    await signal.wait_until_idle()
    assert time.monotonic() - started < 0.1


@pytest.mark.asyncio
async def test_wait_is_bounded_and_cannot_be_extended_by_new_requests() -> None:
    signal = BootstrapDemandSignal(max_hold_seconds=0.2)
    signal.begin()
    started = time.monotonic()

    async def flood() -> None:
        # Sustained demand: new requests keep arriving while the waiter runs.
        while time.monotonic() - started < 0.35:
            signal.begin()
            await asyncio.sleep(0.05)
            signal.end()

    waiter = asyncio.create_task(signal.wait_until_idle())
    flooder = asyncio.create_task(flood())
    await waiter
    await flooder
    elapsed = time.monotonic() - started
    # The waiter returned at the absolute hold (0.2 s) even though demand never
    # cleared; new requests did not extend the wait. Bounds include scheduling
    # slack but stay far below an extendable hold.
    assert elapsed >= 0.15
    assert elapsed < 0.45
    signal.end()


def test_latency_observations_are_bounded_and_summarized() -> None:
    signal = BootstrapDemandSignal(latency_ring_size=8)
    for index in range(50):
        signal.observe_latency(0.001 * index, error=(index % 5 == 0))
    snapshot = signal.latency_snapshot()
    assert snapshot["count"] == 50
    assert snapshot["errors"] == 10
    assert snapshot["samples"] == 8  # ring is bounded to the newest samples
    assert snapshot["minimum"] == 0.042
    assert snapshot["maximum"] == 0.049
    assert 0.042 <= snapshot["p50"] <= 0.049
    assert 0.042 <= snapshot["p95"] <= 0.049
    assert 0.042 <= snapshot["p99"] <= 0.049
