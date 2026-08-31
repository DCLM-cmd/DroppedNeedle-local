"""A crashing operation job must not wedge the worker queue forever.

Before this guard, a handler that raised anything its own ``except`` clauses did
not name (a ValueError out of artwork projection, say) escaped to the durable
worker loop. The loop logged it and carried on, but the job stayed 'running'
until its lease expired, recovery put it back to 'queued', and the next
iteration claimed and crashed on the very same row - forever. One album with
unusable data was enough to stop the Organizer processing anything at all.
"""

import asyncio
import sqlite3
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.library_operation_supervisor import (
    MAX_OPERATION_ATTEMPTS,
    OPERATION_RETRY_DELAY_SECONDS,
    LibraryOperationSupervisor,
)


def _claimed_job(kind: str = "library_management") -> dict:
    return {"id": "job-1", "kind": kind, "requested_by_user_id": "user-1"}


def _supervisor(store, management) -> LibraryOperationSupervisor:
    return LibraryOperationSupervisor(
        store,
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        management=management,
    )


@pytest.mark.asyncio
async def test_unexpected_handler_error_is_charged_to_the_job() -> None:
    store = AsyncMock()
    store.claim_operation_job.side_effect = [_claimed_job(), None, None, None]
    store.record_operation_job_attempt_failure.return_value = None
    management = AsyncMock()
    management.run_claimed.side_effect = ValueError("Invalid release MBID")

    result = await _supervisor(store, management).run_once("worker-1", now=10.0)

    assert result is None
    store.record_operation_job_attempt_failure.assert_awaited_once_with(
        "job-1",
        "worker-1",
        now=10.0,
        max_attempts=MAX_OPERATION_ATTEMPTS,
        retry_delay_seconds=OPERATION_RETRY_DELAY_SECONDS,
    )


@pytest.mark.asyncio
async def test_worker_loop_is_not_asked_to_swallow_the_error() -> None:
    """run_once returns normally, so the loop keeps draining the queue."""
    store = AsyncMock()
    store.claim_operation_job.side_effect = [_claimed_job(), None, None, None]
    terminal = MagicMock()
    store.record_operation_job_attempt_failure.return_value = {"id": "job-1"}
    operations = AsyncMock()
    operations._response = MagicMock(return_value=terminal)
    management = AsyncMock()
    management.run_claimed.side_effect = RuntimeError("boom")
    supervisor = LibraryOperationSupervisor(
        store, operations, AsyncMock(), AsyncMock(), management=management
    )

    assert await supervisor.run_once("worker-1", now=10.0) is terminal


@pytest.mark.asyncio
async def test_bookkeeping_failure_still_does_not_escape() -> None:
    store = AsyncMock()
    store.claim_operation_job.side_effect = [_claimed_job(), None, None, None]
    store.record_operation_job_attempt_failure.side_effect = RuntimeError("db down")
    management = AsyncMock()
    management.run_claimed.side_effect = ValueError("boom")

    assert await _supervisor(store, management).run_once("worker-1", now=10.0) is None


@pytest.mark.asyncio
async def test_cancellation_is_never_treated_as_a_job_failure() -> None:
    """Shutdown must not mark in-flight jobs as broken."""
    store = AsyncMock()
    store.claim_operation_job.side_effect = [_claimed_job(), None, None, None]
    management = AsyncMock()
    management.run_claimed.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _supervisor(store, management).run_once("worker-1", now=10.0)

    store.record_operation_job_attempt_failure.assert_not_called()


@pytest.fixture
def store(tmp_path):
    from infrastructure.persistence._database import close_pooled_connections

    path = tmp_path / "library.db"
    # library_operation_jobs has an FK to auth_users, which a different store owns.
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users VALUES ('user-1')")
    made = NativeLibraryStore(path, threading.Lock())
    yield made
    close_pooled_connections()


async def _queue_job(store: NativeLibraryStore) -> str:
    def operation(connection):
        connection.execute(
            "INSERT INTO library_operation_jobs "
            "(id, kind, state, created_at, updated_at) "
            "VALUES ('job-1', 'library_management', 'queued', 1.0, 1.0)"
        )

    await store._write(operation)
    return "job-1"


@pytest.mark.asyncio
async def test_attempts_are_bounded_and_the_job_finally_fails(store) -> None:
    await _queue_job(store)

    for attempt in range(1, MAX_OPERATION_ATTEMPTS):
        claimed = await store.claim_operation_job(
            "worker-1", now=100.0 * attempt, lease_seconds=60.0, kind="library_management"
        )
        assert claimed is not None, f"job should still be retryable on attempt {attempt}"
        outcome = await store.record_operation_job_attempt_failure(
            "job-1",
            "worker-1",
            now=100.0 * attempt,
            max_attempts=MAX_OPERATION_ATTEMPTS,
            retry_delay_seconds=OPERATION_RETRY_DELAY_SECONDS,
        )
        assert outcome is None, "retries remaining, so the job is not terminal yet"

    claimed = await store.claim_operation_job(
        "worker-1", now=1000.0, lease_seconds=60.0, kind="library_management"
    )
    assert claimed is not None
    outcome = await store.record_operation_job_attempt_failure(
        "job-1",
        "worker-1",
        now=1000.0,
        max_attempts=MAX_OPERATION_ATTEMPTS,
        retry_delay_seconds=OPERATION_RETRY_DELAY_SECONDS,
    )

    assert outcome is not None
    assert outcome["state"] == "failed"
    assert outcome["terminal_code"] == "WORKER_ERROR"

    assert (
        await store.claim_operation_job(
            "worker-1", now=2000.0, lease_seconds=60.0, kind="library_management"
        )
        is None
    ), "a terminally failed job must never be claimed again"


@pytest.mark.asyncio
async def test_retry_is_held_off_so_it_cannot_spin(store) -> None:
    await _queue_job(store)
    await store.claim_operation_job(
        "worker-1", now=100.0, lease_seconds=60.0, kind="library_management"
    )
    await store.record_operation_job_attempt_failure(
        "job-1",
        "worker-1",
        now=100.0,
        max_attempts=MAX_OPERATION_ATTEMPTS,
        retry_delay_seconds=OPERATION_RETRY_DELAY_SECONDS,
    )

    immediate = await store.claim_operation_job(
        "worker-1", now=101.0, lease_seconds=60.0, kind="library_management"
    )
    assert immediate is None, "the retry must be held off, not claimable straight away"

    later = await store.claim_operation_job(
        "worker-1",
        now=100.0 + OPERATION_RETRY_DELAY_SECONDS + 1,
        lease_seconds=60.0,
        kind="library_management",
    )
    assert later is not None


@pytest.mark.asyncio
async def test_a_lease_we_no_longer_own_is_left_alone(store) -> None:
    await _queue_job(store)
    await store.claim_operation_job(
        "worker-1", now=100.0, lease_seconds=60.0, kind="library_management"
    )

    outcome = await store.record_operation_job_attempt_failure(
        "job-1",
        "someone-else",
        now=100.0,
        max_attempts=MAX_OPERATION_ATTEMPTS,
        retry_delay_seconds=OPERATION_RETRY_DELAY_SECONDS,
    )

    assert outcome is None
