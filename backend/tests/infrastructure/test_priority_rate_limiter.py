import asyncio

import pytest

import infrastructure.resilience.rate_limiter as rate_limiter_module
from infrastructure.resilience.rate_limiter import TokenBucketRateLimiter


@pytest.mark.asyncio
async def test_higher_priority_waiter_receives_next_token(monkeypatch) -> None:
    original_sleep = asyncio.sleep
    now = 0.0
    sleepers: list[asyncio.Future[None]] = []

    async def controlled_sleep(_delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        sleepers.append(future)
        await future

    monkeypatch.setattr(rate_limiter_module.time, "monotonic", lambda: now)
    monkeypatch.setattr(rate_limiter_module.asyncio, "sleep", controlled_sleep)

    limiter = TokenBucketRateLimiter(rate=1.0, capacity=1)
    await limiter.acquire()
    order: list[str] = []

    async def wait(name: str, priority: int) -> None:
        await limiter.acquire(priority=priority)
        order.append(name)

    background = asyncio.create_task(wait("background", 3))
    await original_sleep(0)
    interactive = asyncio.create_task(wait("interactive", 0))
    await original_sleep(0)
    assert len(sleepers) == 2

    now = 1.0
    for sleeper in list(sleepers):
        sleeper.set_result(None)
    await original_sleep(0)
    await original_sleep(0)

    assert order == ["interactive"]
    assert not background.done()

    now = 2.0
    for sleeper in sleepers:
        if not sleeper.done():
            sleeper.set_result(None)
    await background
    await interactive
    assert order == ["interactive", "background"]


@pytest.mark.asyncio
async def test_equal_priority_waiters_are_fifo(monkeypatch) -> None:
    original_sleep = asyncio.sleep
    now = 0.0
    sleepers: list[asyncio.Future[None]] = []

    async def controlled_sleep(_delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        sleepers.append(future)
        await future

    monkeypatch.setattr(rate_limiter_module.time, "monotonic", lambda: now)
    monkeypatch.setattr(rate_limiter_module.asyncio, "sleep", controlled_sleep)

    limiter = TokenBucketRateLimiter(rate=1.0, capacity=1)
    await limiter.acquire()
    order: list[str] = []

    async def wait(name: str) -> None:
        await limiter.acquire()
        order.append(name)

    first = asyncio.create_task(wait("first"))
    await original_sleep(0)
    second = asyncio.create_task(wait("second"))
    await original_sleep(0)

    now = 1.0
    for sleeper in list(sleepers):
        sleeper.set_result(None)
    await original_sleep(0)
    await original_sleep(0)
    assert order == ["first"]

    now = 2.0
    for sleeper in sleepers:
        if not sleeper.done():
            sleeper.set_result(None)
    await first
    await second
    assert order == ["first", "second"]


@pytest.mark.asyncio
async def test_try_acquire_does_not_bypass_queued_waiter() -> None:
    limiter = TokenBucketRateLimiter(rate=0.001, capacity=1)
    await limiter.acquire()
    waiting = asyncio.create_task(limiter.acquire(priority=3))
    await asyncio.sleep(0)
    limiter._tokens = 1.0

    assert await limiter.try_acquire() is False

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting


@pytest.mark.asyncio
async def test_cancelled_waiter_is_removed() -> None:
    limiter = TokenBucketRateLimiter(rate=0.001, capacity=1)
    await limiter.acquire()

    waiting = asyncio.create_task(limiter.acquire(priority=3))
    await asyncio.sleep(0)
    waiting.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert limiter._waiters == []
