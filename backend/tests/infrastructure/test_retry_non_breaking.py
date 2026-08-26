"""Tests that non_breaking_exceptions bypass circuit breaker failure recording."""

import asyncio
import logging

import pytest

from infrastructure.resilience.retry import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    with_retry,
)


class _RateLimited(Exception):
    def __init__(self, retry_after: float = 1.0):
        super().__init__("rate limited")
        self.retry_after_seconds = retry_after


class _ServiceDown(Exception):
    pass


@pytest.mark.asyncio
async def test_non_breaking_exception_does_not_trip_circuit():
    cb = CircuitBreaker(failure_threshold=3, name="test-non-breaking")
    call_count = 0

    @with_retry(
        max_attempts=4,
        base_delay=0.01,
        max_delay=0.05,
        circuit_breaker=cb,
        retriable_exceptions=(_RateLimited, _ServiceDown),
        non_breaking_exceptions=(_RateLimited,),
    )
    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 4:
            raise _RateLimited(retry_after=0.01)
        return "ok"

    result = await flaky()

    assert result == "ok"
    assert call_count == 4
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_breaking_exception_still_trips_circuit():
    cb = CircuitBreaker(failure_threshold=2, name="test-breaking")
    call_count = 0

    @with_retry(
        max_attempts=3,
        base_delay=0.01,
        max_delay=0.05,
        circuit_breaker=cb,
        retriable_exceptions=(_RateLimited, _ServiceDown),
        non_breaking_exceptions=(_RateLimited,),
    )
    async def fail():
        nonlocal call_count
        call_count += 1
        raise _ServiceDown("down")

    # Each call records one failure after all retries exhausted (not per retry)
    with pytest.raises(_ServiceDown):
        await fail()
    assert cb.failure_count == 1
    assert cb.state == CircuitState.CLOSED

    call_count = 0
    with pytest.raises(_ServiceDown):
        await fail()
    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_non_breaking_uses_retry_after_for_delay():
    cb = CircuitBreaker(failure_threshold=5, name="test-retry-after")
    call_count = 0

    @with_retry(
        max_attempts=2,
        base_delay=100.0,
        max_delay=100.0,
        circuit_breaker=cb,
        retriable_exceptions=(_RateLimited,),
        non_breaking_exceptions=(_RateLimited,),
    )
    async def rate_limited_then_ok():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _RateLimited(retry_after=0.01)
        return "ok"

    result = await rate_limited_then_ok()

    assert result == "ok"
    assert call_count == 2
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_circuit_still_opens_for_real_errors_amid_rate_limits():
    cb = CircuitBreaker(failure_threshold=2, name="test-mixed")

    @with_retry(
        max_attempts=1,
        base_delay=0.01,
        max_delay=0.05,
        circuit_breaker=cb,
        retriable_exceptions=(_RateLimited, _ServiceDown),
        non_breaking_exceptions=(_RateLimited,),
    )
    async def real_failure():
        raise _ServiceDown("down")

    for _ in range(2):
        with pytest.raises(_ServiceDown):
            await real_failure()

    assert cb.state == CircuitState.OPEN

    @with_retry(
        max_attempts=1,
        base_delay=0.01,
        max_delay=0.05,
        circuit_breaker=cb,
        retriable_exceptions=(_RateLimited, _ServiceDown),
        non_breaking_exceptions=(_RateLimited,),
    )
    async def subsequent_call():
        return "should not reach"

    with pytest.raises(CircuitOpenError):
        await subsequent_call()


@pytest.mark.asyncio
async def test_non_breaking_in_half_open_stays_half_open():
    """Non-breaking exceptions in HALF_OPEN keep the circuit HALF_OPEN (service is reachable)."""
    cb = CircuitBreaker(failure_threshold=2, success_threshold=2, timeout=0.01, name="test-half-open")

    for _ in range(2):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN

    await asyncio.sleep(0.02)
    await cb.atry_transition()
    assert cb.state == CircuitState.HALF_OPEN

    @with_retry(
        max_attempts=1,
        base_delay=0.01,
        max_delay=0.05,
        circuit_breaker=cb,
        retriable_exceptions=(_RateLimited,),
        non_breaking_exceptions=(_RateLimited,),
    )
    async def rate_limited_in_half_open():
        raise _RateLimited(retry_after=0.01)

    with pytest.raises(_RateLimited):
        await rate_limited_in_half_open()

    assert cb.state == CircuitState.HALF_OPEN


@pytest.mark.asyncio
async def test_retry_after_not_clamped_by_max_delay():
    """Server-provided Retry-After should not be clamped by max_delay."""
    cb = CircuitBreaker(failure_threshold=10, name="test-retry-after-clamp")
    call_count = 0
    observed_gap = 0.0

    @with_retry(
        max_attempts=2,
        base_delay=0.01,
        max_delay=0.05,
        circuit_breaker=cb,
        retriable_exceptions=(_RateLimited,),
        non_breaking_exceptions=(_RateLimited,),
    )
    async def rate_limited_then_ok():
        nonlocal call_count, observed_gap
        call_count += 1
        if call_count == 1:
            raise _RateLimited(retry_after=0.3)
        return "ok"

    import time
    start = time.monotonic()
    result = await rate_limited_then_ok()
    elapsed = time.monotonic() - start

    assert result == "ok"
    assert elapsed >= 0.25, f"Expected >=0.25s delay from retry_after=0.3, got {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_open_warning_is_rate_limited(caplog):
    """A hot loop against an open breaker must not flood the logs: at most one
    OPEN warning per interval, while every call still raises CircuitOpenError."""
    cb = CircuitBreaker(failure_threshold=1, name="test-open-warning-flood")
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    @with_retry(max_attempts=1, circuit_breaker=cb)
    async def blocked():
        return "unreachable"

    with caplog.at_level(logging.WARNING, logger="infrastructure.resilience.retry"):
        for _ in range(5):
            with pytest.raises(CircuitOpenError):
                await blocked()

    warnings = [r for r in caplog.records if "is OPEN" in r.getMessage()]
    assert len(warnings) == 1

    caplog.clear()
    cb._last_open_warning = 0.0
    with caplog.at_level(logging.WARNING, logger="infrastructure.resilience.retry"):
        with pytest.raises(CircuitOpenError):
            await blocked()
    warnings = [r for r in caplog.records if "is OPEN" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_non_retriable_exception_fails_after_single_attempt():
    """Deterministic failures skip the retry loop entirely (while non-breaking
    exceptions without non_retriable are still retried - see above)."""
    cb = CircuitBreaker(failure_threshold=2, name="test-non-retriable")
    call_count = 0

    @with_retry(
        max_attempts=5,
        base_delay=0.01,
        max_delay=0.05,
        circuit_breaker=cb,
        retriable_exceptions=(_RateLimited, _ServiceDown),
        non_breaking_exceptions=(_ServiceDown,),
        non_retriable_exceptions=(_ServiceDown,),
    )
    async def doomed():
        nonlocal call_count
        call_count += 1
        raise _ServiceDown("down")

    with pytest.raises(_ServiceDown):
        await doomed()

    assert call_count == 1
    assert cb.failure_count == 0
    assert cb.state == CircuitState.CLOSED
