import asyncio
import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import msgspec
import pytest

from core.exceptions import (
    AudioWriteError,
    ConflictError,
    LibraryManagementDestinationConflictError,
    StaleRevisionError,
)
from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_management import LibraryManagementJobSnapshot
from services.native.library_management_planner import LibraryManagementPlanner
from services.native.library_management_publisher import LibraryManagementPublisher
from services.native.library_management_worker import LibraryManagementWorker
from services.native.library_management_undo_service import LibraryManagementUndoService
from services.native.library_management_baseline_service import (
    LibraryManagementBaselineService,
)
from services.native.library_management_duplicate_service import (
    LibraryManagementDuplicateService,
)


def _snapshot() -> LibraryManagementJobSnapshot:
    return LibraryManagementJobSnapshot(
        job_id="management-1",
        mode="apply",
        origin="manual",
        phase="applying",
        selection_json="{}",
        profile_revision="profile",
        settings_revision="settings",
        naming_revision="naming",
        policy_revision="policy",
        catalog_revision=1,
        profile_snapshot_json="{}",
    )


def _worker() -> tuple[LibraryManagementWorker, AsyncMock, AsyncMock]:
    store = AsyncMock(spec=NativeLibraryStore)
    publisher = AsyncMock(spec=LibraryManagementPublisher)
    worker = LibraryManagementWorker(
        store,
        AsyncMock(spec=LibraryManagementPlanner),
        publisher,
        AsyncMock(spec=LibraryManagementUndoService),
        AsyncMock(spec=LibraryManagementBaselineService),
        AsyncMock(spec=LibraryManagementDuplicateService),
    )
    store.get_library_management_job_snapshot.return_value = _snapshot()
    store.checkpoint_operation_control.return_value = None
    store.finish_library_management_apply.return_value = {
        "id": "management-1",
        "state": "succeeded",
    }
    return worker, store, publisher


@pytest.mark.asyncio
async def test_apply_worker_publishes_each_bundle_then_finishes() -> None:
    worker, store, publisher = _worker()
    store.claim_operation_work.side_effect = [
        {"ordinal": 0, "row_revision": 2, "state": "running"},
        None,
    ]

    result = await worker.run_claimed({"id": "management-1"}, "management-worker")

    assert result["state"] == "succeeded"
    publisher.publish_bundle.assert_awaited_once_with(
        "management-1", 0, "management-worker"
    )
    store.finish_library_management_apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_worker_honours_control_only_outside_critical_publish() -> None:
    worker, store, publisher = _worker()
    entered_publish = asyncio.Event()
    release_publish = asyncio.Event()

    async def publish(*_args) -> None:
        entered_publish.set()
        await release_publish.wait()

    publisher.publish_bundle.side_effect = publish
    store.claim_operation_work.side_effect = [
        {"ordinal": 0, "row_revision": 2, "state": "running"},
        None,
    ]
    store.checkpoint_operation_control.side_effect = [
        None,
        {
            "id": "management-1",
            "state": "paused",
            "control_request": "none",
        },
    ]

    task = asyncio.create_task(
        worker.run_claimed({"id": "management-1"}, "management-worker")
    )
    await entered_publish.wait()
    assert store.checkpoint_operation_control.await_count == 1
    release_publish.set()
    result = await task

    assert result["state"] == "paused"
    assert store.checkpoint_operation_control.await_count == 2
    store.finish_library_management_apply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "failure_code"),
    [
        (
            LibraryManagementDestinationConflictError(
                "A destination was created after preview."
            ),
            "STALE_DESTINATION",
        ),
        (
            ConflictError("The durable management snapshot does not match its retry."),
            "PUBLICATION_CONFLICT",
        ),
        (
            StaleRevisionError("A managed file changed after preview."),
            "STALE_INPUT",
        ),
    ],
)
async def test_apply_worker_preserves_publication_conflict_classification(
    error: ConflictError | StaleRevisionError,
    failure_code: str,
) -> None:
    worker, store, publisher = _worker()
    store.claim_operation_work.side_effect = [
        {"ordinal": 0, "row_revision": 2, "state": "running"},
        None,
    ]
    store.get_operation_work_item.return_value = {
        "ordinal": 0,
        "state": "running",
    }
    publisher.publish_bundle.side_effect = error

    result = await worker.run_claimed({"id": "management-1"}, "management-worker")

    assert result["state"] == "succeeded"
    values = store.complete_operation_work.await_args.kwargs
    assert values["state"] == "skipped"
    assert values["failure_code"] == failure_code
    assert msgspec.json.decode(values["result_json"])["reason"] == str(error)


@pytest.mark.asyncio
async def test_apply_worker_logs_staged_write_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker, store, publisher = _worker()
    store.claim_operation_work.side_effect = [
        {"ordinal": 4, "row_revision": 2, "state": "running"},
        None,
    ]
    store.get_operation_work_item.return_value = {
        "ordinal": 4,
        "state": "running",
    }
    publisher.publish_bundle.side_effect = AudioWriteError(
        "Staged artwork validation did not match the plan."
    )

    with caplog.at_level(
        logging.ERROR,
        logger="services.native.library_management_worker",
    ):
        await worker.run_claimed({"id": "management-1"}, "management-worker")

    values = store.complete_operation_work.await_args.kwargs
    assert values["state"] == "failed"
    assert values["failure_code"] == "PUBLICATION_FAILED"
    assert "failure_type=AudioWriteError" in caplog.text
    assert "Staged artwork validation did not match the plan." in caplog.text


@pytest.mark.asyncio
async def test_baseline_restore_preview_dispatches_to_baseline_planner() -> None:
    worker, store, _publisher = _worker()
    snapshot = _snapshot()
    snapshot.mode = "baseline_restore"
    snapshot.phase = "planning"
    store.get_library_management_job_snapshot.return_value = snapshot
    store.get_operation_job.return_value = {
        "id": "management-1",
        "state": "ready",
    }

    result = await worker.run_claimed({"id": "management-1"}, "management-worker")

    assert result["state"] == "ready"
    worker._baseline.run_claimed_preview.assert_awaited_once_with(
        {"id": "management-1"}, "management-worker"
    )


@pytest.mark.asyncio
async def test_duplicate_preview_dispatches_to_duplicate_planner() -> None:
    worker, store, _publisher = _worker()
    snapshot = _snapshot()
    snapshot.mode = "duplicate_resolution"
    snapshot.phase = "planning"
    store.get_library_management_job_snapshot.return_value = snapshot
    store.get_operation_job.return_value = {
        "id": "management-1",
        "state": "ready",
    }

    result = await worker.run_claimed({"id": "management-1"}, "management-worker")

    assert result["state"] == "ready"
    worker._duplicates.run_claimed_preview.assert_awaited_once_with(
        {"id": "management-1"}, "management-worker"
    )


@pytest.mark.asyncio
async def test_preview_conflict_finishes_instead_of_repeating_after_lease_expiry() -> (
    None
):
    worker, store, _publisher = _worker()
    snapshot = _snapshot()
    snapshot.mode = "preview"
    snapshot.phase = "planning"
    store.get_library_management_job_snapshot.return_value = snapshot
    worker._planner.run_claimed_preview.side_effect = ConflictError(
        "The content hash is already registered with different metadata."
    )
    store.finish_operation_job.return_value = {
        "id": "management-1",
        "state": "failed",
        "terminal_code": "PLANNING_FAILED",
    }

    result = await worker.run_claimed({"id": "management-1"}, "management-worker")

    assert result["state"] == "failed"
    store.finish_operation_job.assert_awaited_once()
    assert store.finish_operation_job.await_args.kwargs["terminal_code"] == (
        "PLANNING_FAILED"
    )


@pytest.mark.asyncio
async def test_scan_preview_begins_automatic_apply_without_browser_confirmation() -> (
    None
):
    worker, store, _publisher = _worker()
    snapshot = _snapshot()
    snapshot.mode = "preview"
    snapshot.origin = "scan_discovered"
    snapshot.phase = "planning"
    snapshot.preview_token_hash = "proof"
    ready = msgspec.structs.replace(
        snapshot,
        phase="ready",
        summary_json='{"blocked_count":0,"stale_count":0}',
    )
    store.get_library_management_job_snapshot.return_value = snapshot
    store.get_operation_job.return_value = {
        "id": "management-1",
        "state": "ready",
        "row_revision": 4,
    }
    store.begin_library_management_apply.return_value = {
        "id": "management-1",
        "state": "queued",
    }
    worker._planner.run_claimed_preview.return_value = ready

    result = await worker.run_claimed({"id": "management-1"}, "management-worker")

    assert result["state"] == "queued"
    store.begin_library_management_apply.assert_awaited_once()
    call = store.begin_library_management_apply.await_args
    assert call.args == ("management-1",)
    assert call.kwargs["preview_token_hash"] == "proof"
    assert call.kwargs["expected_job_revision"] == 4
    assert call.kwargs["idempotency_key"] == "automatic-scan-apply:management-1"
    assert call.kwargs["now"] > 0


@pytest.mark.asyncio
async def test_scan_preview_with_blockers_remains_held_and_inert() -> None:
    worker, store, _publisher = _worker()
    snapshot = _snapshot()
    snapshot.mode = "preview"
    snapshot.origin = "scan_discovered"
    snapshot.phase = "planning"
    snapshot.preview_token_hash = "proof"
    ready = msgspec.structs.replace(
        snapshot,
        phase="ready",
        summary_json='{"blocked_count":1,"stale_count":0}',
    )
    store.get_library_management_job_snapshot.return_value = snapshot
    store.get_operation_job.return_value = {
        "id": "management-1",
        "state": "ready",
        "row_revision": 4,
    }
    worker._planner.run_claimed_preview.return_value = ready

    result = await worker.run_claimed({"id": "management-1"}, "management-worker")

    assert result["state"] == "ready"
    store.begin_library_management_apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_keep_existing_duplicate_is_durable_without_filesystem_publish() -> None:
    worker, store, publisher = _worker()
    snapshot = _snapshot()
    snapshot.mode = "duplicate_resolution"
    store.get_library_management_job_snapshot.return_value = snapshot
    store.claim_operation_work.side_effect = [
        {"ordinal": 0, "row_revision": 2, "state": "running"},
        None,
    ]
    store.get_library_management_bundle_plan_items.return_value = [
        SimpleNamespace(
            diff_json=(
                '{"duplicate_resolution":{"action":"keep_existing"},'
                '"requires_write":false}'
            )
        )
    ]

    result = await worker.run_claimed({"id": "management-1"}, "management-worker")

    assert result["state"] == "succeeded"
    publisher.publish_bundle.assert_not_awaited()
    store.complete_operation_work.assert_awaited_once()
    values = store.complete_operation_work.await_args.kwargs
    assert values["state"] == "succeeded"
    assert values["result_json"] == (
        '{"filesystem_writes":0,"resolution":"kept_existing"}'
    )


# F-210: concurrent-applier drill on one operation job


@pytest.fixture
def real_store(tmp_path):
    import sqlite3
    import threading

    from infrastructure.persistence.native_library_store import (
        NativeLibraryStore as _Store,
    )

    database = tmp_path / "library.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users VALUES ('admin')")
    return _Store(database, threading.Lock())


def _seed_claimed_apply_job(real_store, *, job_id: str, lease_owner: str) -> None:
    import sqlite3

    with sqlite3.connect(real_store.db_path) as connection:
        connection.execute(
            "INSERT INTO library_operation_jobs "
            "(id, kind, state, lease_owner, lease_expires_at, heartbeat_at, "
            "expected_work_count, completed_count, succeeded_count, failed_count, "
            "skipped_count, control_request, reidentification_attempt_count, "
            "created_at, phase_timings_json, updated_at, row_revision, "
            "event_revision) VALUES (?, 'library_management', 'running', ?, 1000, "
            "100, 1, 0, 0, 0, 0, 'none', 0, 100, '{}', 100, 1, 0)",
            (job_id, lease_owner),
        )
        connection.execute(
            "INSERT INTO library_management_job_snapshots "
            "(job_id, mode, origin, phase, selection_json, profile_revision, "
            "settings_revision, naming_revision, policy_revision, catalog_revision, "
            "profile_snapshot_json, intent_json, summary_json, warnings_json, "
            "created_at, updated_at, row_revision) VALUES "
            "(?, 'apply', 'manual', 'applying', '{}', 'profile', 'settings', "
            "'naming', 'policy', 1, '{}', '{}', '{}', '[]', 100, 100, 1)",
            (job_id,),
        )
        connection.execute(
            "INSERT INTO library_operation_work "
            "(job_id, ordinal, local_album_id, expected_subject_revision, "
            "expected_input_revision, "
            "action, idempotency_key, state, row_revision, updated_at) VALUES "
            "(?, 0, 'album-1', 1, 1, 'library_management', ?, 'pending', 1, 100)",
            (job_id, f"{job_id}:bundle:0"),
        )


def _real_store_worker(store) -> tuple[LibraryManagementWorker, AsyncMock]:
    publisher = AsyncMock(spec=LibraryManagementPublisher)
    worker = LibraryManagementWorker(
        store,
        AsyncMock(spec=LibraryManagementPlanner),
        publisher,
        AsyncMock(spec=LibraryManagementUndoService),
        AsyncMock(spec=LibraryManagementBaselineService),
        AsyncMock(spec=LibraryManagementDuplicateService),
    )
    return worker, publisher


@pytest.mark.asyncio
async def test_concurrent_appliers_publish_one_bundle_without_duplicate_work(
    real_store,
) -> None:
    """Two workers drive run_claimed on the SAME claimed job concurrently. The
    store's lease claim is the only mutual exclusion: exactly one may publish
    the bundle; the loser must exit without publishing or duplicating work."""
    _seed_claimed_apply_job(real_store, job_id="management-race", lease_owner="worker-a")
    worker_a, publisher_a = _real_store_worker(real_store)
    worker_b, publisher_b = _real_store_worker(real_store)
    job = {"id": "management-race"}

    # The real publisher's catalog commit settles its bundle's work row
    # (commit_library_management_bundle); the stand-in mirrors exactly that
    # store contract so finish_library_management_apply sees a terminal item.
    import sqlite3 as _sqlite3

    async def _settle_work(job_id: str, ordinal: int, worker_id: str) -> None:
        def run() -> None:
            with _sqlite3.connect(real_store.db_path) as connection:
                connection.execute(
                    "UPDATE library_operation_work SET state='succeeded', "
                    "row_revision=row_revision+1 WHERE job_id=? AND ordinal=? "
                    "AND state='running'",
                    (job_id, ordinal),
                )
                connection.execute(
                    "UPDATE library_operation_jobs SET completed_count="
                    "completed_count+1, succeeded_count=succeeded_count+1 "
                    "WHERE id=? AND state='running' AND lease_owner=?",
                    (job_id, worker_id),
                )

        await asyncio.to_thread(run)

    for publisher in (publisher_a, publisher_b):
        publisher.publish_bundle.side_effect = _settle_work

    results = await asyncio.gather(
        worker_a.run_claimed(job, "worker-a"),
        worker_b.run_claimed(job, "worker-b"),
        return_exceptions=True,
    )

    winner_results = [
        result
        for result in results
        if not isinstance(result, BaseException)
        and result.get("state") == "succeeded"
    ]
    # exactly one worker published the single bundle
    total_publishes = publisher_a.publish_bundle.await_count + (
        publisher_b.publish_bundle.await_count
    )
    assert total_publishes == 1
    assert len(winner_results) == 1
    # the work item reached a terminal state through ONE CAS transition
    with __import__("sqlite3").connect(real_store.db_path) as connection:
        states = [
            row[0]
            for row in connection.execute(
                "SELECT state FROM library_operation_work WHERE job_id=?",
                ("management-race",),
            ).fetchall()
        ]
        attempts = connection.execute(
            "SELECT succeeded_count FROM library_operation_jobs WHERE id=?",
            ("management-race",),
        ).fetchone()[0]
    assert states == ["succeeded"]
    assert attempts == 1
    # the non-owner can never terminalize the job it does not hold
    losers = [
        result
        for result in results
        if isinstance(result, StaleRevisionError)
    ]
    assert len(losers) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        msgspec.DecodeError("corrupt profile snapshot"),
        RuntimeError("unexpected blob-store explosion"),
        sqlite3.OperationalError("disk I/O error"),
    ],
)
async def test_apply_worker_marks_unknown_failures_as_durable_failures(error):
    """F-107: an unclassified exception must terminate the work row durably
    instead of leaving the job retrying forever with no visible outcome."""
    worker, store, publisher = _worker()
    store.claim_operation_work.side_effect = [
        {"ordinal": 0, "row_revision": 2, "state": "running"},
        None,
    ]
    store.get_operation_work_item.return_value = {
        "ordinal": 0,
        "state": "running",
    }
    publisher.publish_bundle.side_effect = error

    result = await worker.run_claimed({"id": "management-1"}, "management-worker")

    assert result["state"] == "succeeded"
    values = store.complete_operation_work.await_args.kwargs
    assert values["state"] == "failed"
    assert values["failure_code"] == "PUBLICATION_FAILED"
    payload = msgspec.json.decode(values["result_json"])
    assert payload["failure_type"] == type(error).__name__
    assert payload["reason"] == str(error)


@pytest.mark.asyncio
async def test_apply_worker_cancellation_propagates_without_marking() -> None:
    """F-107: CancelledError stays a BaseException - no durable failed marking,
    because durability belongs to the publisher's shielded critical task."""
    worker, store, publisher = _worker()
    store.claim_operation_work.side_effect = [
        {"ordinal": 0, "row_revision": 2, "state": "running"},
        None,
    ]

    async def cancel_during_publish(*_args) -> None:
        raise asyncio.CancelledError

    publisher.publish_bundle.side_effect = cancel_during_publish

    with pytest.raises(asyncio.CancelledError):
        await worker.run_claimed({"id": "management-1"}, "management-worker")

    store.complete_operation_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_worker_renews_the_operation_lease_per_bundle() -> None:
    """F-105: one heartbeat per loop iteration keeps the 60 s lease alive."""
    from services.native.library_operation_service import LEASE_SECONDS

    worker, store, _publisher = _worker()
    store.claim_operation_work.side_effect = [
        {"ordinal": 0, "row_revision": 2, "state": "running"},
        None,
    ]

    result = await worker.run_claimed({"id": "management-1"}, "management-worker")

    assert result["state"] == "succeeded"
    # two renewals inside the bundle iteration (loop top + post-publish) and
    # one at the top of the draining iteration
    assert store.heartbeat_operation_job.await_count == 3
    for call in store.heartbeat_operation_job.await_args_list:
        assert call.args[0] == "management-1"
        assert call.args[1] == "management-worker"
        assert call.kwargs["lease_seconds"] == LEASE_SECONDS


@pytest.mark.asyncio
async def test_apply_worker_stops_when_the_lease_is_lost() -> None:
    """F-105: a failed heartbeat records the bundle outcome and the zombie
    applier exits before claiming any further work."""
    worker, store, _publisher = _worker()
    store.claim_operation_work.side_effect = [
        {"ordinal": 0, "row_revision": 2, "state": "running"},
    ]
    store.get_operation_work_item.return_value = {
        "ordinal": 0,
        "state": "running",
    }
    store.heartbeat_operation_job.side_effect = [True] + [False] * 10

    with pytest.raises(StaleRevisionError, match="lease"):
        await worker.run_claimed({"id": "management-1"}, "management-worker")

    # iteration 1 recorded its bundle as skipped/STALE_INPUT, iteration 2's
    # top-of-loop heartbeat failed before another claim could happen
    assert store.claim_operation_work.await_count == 1
    values = store.complete_operation_work.await_args.kwargs
    assert values["state"] == "skipped"
    assert values["failure_code"] == "STALE_INPUT"
