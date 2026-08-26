from __future__ import annotations

import asyncio

import pytest

from infrastructure.queue.durable_work_wakeup import DurableWorkWakeups


@pytest.mark.asyncio
async def test_notification_after_empty_check_cannot_be_cleared_as_stale() -> None:
    wakeups = DurableWorkWakeups()
    empty_check_revision = wakeups.revision("identification")

    wakeups.notify("identification")

    assert await wakeups.wait(
        "identification",
        after_revision=empty_check_revision,
        timeout_seconds=1,
    )


@pytest.mark.asyncio
async def test_wait_has_a_bounded_missed_wake_recovery_deadline() -> None:
    wakeups = DurableWorkWakeups()

    woke = await wakeups.wait(
        "operation",
        after_revision=wakeups.revision("operation"),
        timeout_seconds=0.001,
    )

    assert woke is False


@pytest.mark.asyncio
async def test_wait_propagates_cancellation() -> None:
    wakeups = DurableWorkWakeups()
    task = asyncio.create_task(
        wakeups.wait(
            "scan",
            after_revision=wakeups.revision("scan"),
            timeout_seconds=30,
        )
    )
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_future_deadline_wakes_queue_when_retry_becomes_due() -> None:
    wakeups = DurableWorkWakeups()
    revision = wakeups.revision("scan")

    wakeups.notify_after("scan", 0.01)

    assert await wakeups.wait("scan", after_revision=revision, timeout_seconds=0.1)
    assert wakeups.revision("scan") == revision + 1


@pytest.mark.asyncio
async def test_later_deadline_survives_an_earlier_scheduled_wake() -> None:
    wakeups = DurableWorkWakeups()
    wakeups.notify_after("contribution", 0.01)
    wakeups.notify_after("contribution", 0.2)

    first_revision = wakeups.revision("contribution")
    assert await wakeups.wait(
        "contribution", after_revision=first_revision, timeout_seconds=0.1
    )
    second_revision = wakeups.revision("contribution")
    assert await wakeups.wait(
        "contribution", after_revision=second_revision, timeout_seconds=0.5
    )
    assert wakeups.revision("contribution") == second_revision + 1
