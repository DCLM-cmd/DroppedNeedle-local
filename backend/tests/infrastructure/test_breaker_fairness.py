"""QW11 Part 1: lock the breaker-fairness contract - ``with_retry`` reports
ONE breaker outcome per LOGICAL call (decided after the final attempt), never
one per retry attempt. The historical fix landed in 491947e; these tests pin
the contract so it cannot regress silently.

Threshold stays 5 = "5 bad calls"; open-after-threshold timing unchanged;
HALF_OPEN deliberately conservative (single probe failure reopens).
"""

import pytest

from infrastructure.resilience.retry import (
    CircuitBreaker,
    CircuitState,
    with_retry,
)


class AlwaysFails:
    def __init__(self):
        self.attempts = 0


def _flaky(breaker: CircuitBreaker, state: AlwaysFails, max_attempts: int = 3):
    @with_retry(
        max_attempts=max_attempts,
        base_delay=0.001,
        max_delay=0.002,
        jitter=False,
        circuit_breaker=breaker,
        retriable_exceptions=(RuntimeError,),
    )
    async def fail():
        state.attempts += 1
        raise RuntimeError("boom")

    return fail


class TestOneFailurePerLogicalCall:
    @pytest.mark.asyncio
    async def test_three_attempts_of_one_call_increment_once(self):
        breaker = CircuitBreaker(failure_threshold=5, name="fair-1")
        state = AlwaysFails()
        fail = _flaky(breaker, state, max_attempts=3)

        with pytest.raises(RuntimeError):
            await fail()

        assert state.attempts == 3  # all attempts ran
        assert breaker.failure_count == 1  # but ONE breaker failure

    @pytest.mark.asyncio
    async def test_threshold_five_means_five_bad_calls(self):
        breaker = CircuitBreaker(failure_threshold=5, timeout=60.0, name="fair-2")
        state = AlwaysFails()
        fail = _flaky(breaker, state, max_attempts=3)

        # 4 logical calls x 3 attempts each: still CLOSED.
        for _ in range(4):
            with pytest.raises(RuntimeError):
                await fail()
            assert breaker.state == CircuitState.CLOSED
        assert state.attempts == 12

        # 5th bad logical call crosses the threshold -> OPEN.
        with pytest.raises(RuntimeError):
            await fail()
        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_breaker_rejects_before_any_attempt(self):
        breaker = CircuitBreaker(failure_threshold=5, timeout=60.0, name="fair-3")
        state = AlwaysFails()
        fail = _flaky(breaker, state, max_attempts=3)
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await fail()
        assert breaker.state == CircuitState.OPEN

        from infrastructure.resilience.retry import CircuitOpenError

        state.attempts = 0
        with pytest.raises(CircuitOpenError):
            await fail()
        assert state.attempts == 0  # fail-fast, body never ran

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        breaker = CircuitBreaker(failure_threshold=5, name="fair-4")
        state = AlwaysFails()
        fail = _flaky(breaker, state)

        with pytest.raises(RuntimeError):
            await fail()
        with pytest.raises(RuntimeError):
            await fail()
        assert breaker.failure_count == 2

        @with_retry(max_attempts=3, circuit_breaker=breaker)
        async def ok():
            return "fine"

        assert await ok() == "fine"
        assert breaker.failure_count == 0
        assert breaker.state == CircuitState.CLOSED


class TestHalfOpenConservatism:
    @pytest.mark.asyncio
    async def test_single_probe_failure_reopens_immediately(self):
        """Deliberate policy: one failed probe reopens - no second chance."""
        breaker = CircuitBreaker(
            failure_threshold=5, timeout=0.0, success_threshold=2, name="halfopen-1"
        )
        state = AlwaysFails()
        fail = _flaky(breaker, state)

        for _ in range(5):
            with pytest.raises(RuntimeError):
                await fail()
        assert breaker.state == CircuitState.OPEN

        # timeout=0 -> OPEN transitions to HALF_OPEN on next entry...
        await breaker.atry_transition()
        assert breaker.state == CircuitState.HALF_OPEN

        # ...and ONE failed probe reopens instantly.
        with pytest.raises(RuntimeError):
            await fail()
        assert breaker.state == CircuitState.OPEN
        assert breaker.failure_count == 0  # reopened counters reset

    @pytest.mark.asyncio
    async def test_probe_successes_close_after_success_threshold(self):
        breaker = CircuitBreaker(
            failure_threshold=5, timeout=0.0, success_threshold=2, name="halfopen-2"
        )
        state = AlwaysFails()
        fail = _flaky(breaker, state)
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await fail()
        await breaker.atry_transition()
        assert breaker.state == CircuitState.HALF_OPEN

        @with_retry(max_attempts=1, circuit_breaker=breaker)
        async def ok():
            return "fine"

        await ok()  # first successful probe: still HALF_OPEN
        assert breaker.state == CircuitState.HALF_OPEN
        await ok()  # second: success_threshold reached -> CLOSED
        assert breaker.state == CircuitState.CLOSED


class TestOpenTimingUnchanged:
    @pytest.mark.asyncio
    async def test_open_window_matches_timeout(self):
        breaker = CircuitBreaker(failure_threshold=2, timeout=30.0, name="timing-1")
        state = AlwaysFails()
        fail = _flaky(breaker, state, max_attempts=1)

        with pytest.raises(RuntimeError):
            await fail()
        with pytest.raises(RuntimeError):
            await fail()
        assert breaker.state == CircuitState.OPEN
        remaining = breaker.remaining_open_seconds()
        assert 29.0 < remaining <= 30.0
