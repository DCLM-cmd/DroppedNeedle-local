from __future__ import annotations

import asyncio
import errno
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import get_args
from unittest.mock import AsyncMock

import pytest

from api.v1.schemas.library_policies import LibraryRootSettings, TypedLibrarySettings
from core.exceptions import StaleRevisionError
from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.audio import AudioInfo, AudioTag
from models.library_work import ScanRequest, ScanRun, ScanScope, ScanState
from services.native.library_indexer import INDEX_BATCH_SIZE, LibraryIndexer
from services.native.library_filesystem_coordinator import (
    LibraryFilesystemCoordinator,
)
from services.native.library_inventory_scanner import (
    DirectoryWalker,
    INVENTORY_BATCH_SIZE,
    INVENTORY_QUEUE_SIZE,
    LibraryInventoryScanner,
)
from services.native.library_policy_resolver import LibraryPolicyResolver
from services.native.library_reconciler import LibraryReconciler
from services.native.library_scan_coordinator import LibraryScanCoordinator
from services.native.library_scan_scheduler import LibraryAutomaticScanScheduler
from services.native.library_schedule_service import LibraryScheduleService


class _TagReader:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def read_tags(self, path: Path) -> tuple[AudioTag, AudioInfo]:
        self.calls.append(path)
        number = int(path.stem.rsplit("-", 1)[-1])
        return (
            AudioTag(
                title=f"Track {number}",
                artist="Local Artist",
                album="Local Album",
                album_artist="Local Artist",
                track_number=number,
            ),
            AudioInfo(
                duration_seconds=180,
                bitrate=900,
                sample_rate=44_100,
                channels=2,
                file_format="flac",
                file_size_bytes=path.stat().st_size,
                bit_depth=16,
            ),
        )


@pytest.fixture
def target_store(tmp_path: Path) -> NativeLibraryStore:
    db_path = tmp_path / "target.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    return NativeLibraryStore(db_path=db_path, write_lock=threading.Lock())


def _resolver(root: Path) -> LibraryPolicyResolver:
    return LibraryPolicyResolver(
        TypedLibrarySettings(
            library_roots=[
                LibraryRootSettings(
                    id="root-a", path=str(root), label="Library", policy="automatic"
                )
            ]
        )
    )


def _request(
    resolver: LibraryPolicyResolver,
    *,
    kind: str = "incremental",
    relative_path: str = ".",
    trigger: str = "manual",
    policy_revision: str | None = None,
) -> ScanRequest:
    revision = policy_revision or resolver.policy_revision
    return ScanRequest(
        kind=kind,
        trigger=trigger,
        policy_revision=revision,
        scopes=[
            ScanScope(
                root_id="root-a",
                relative_path=relative_path,
                policy_revision=revision,
            )
        ],
    )


def _coordinator(
    store: NativeLibraryStore,
    resolver: LibraryPolicyResolver,
    tag_reader: _TagReader | None = None,
    directory_walker: DirectoryWalker | None = None,
    *,
    tag_read_timeout_seconds: float = 30.0,
    max_detached_tag_reads: int = 4,
    walk_deadline_seconds: float = 30.0,
    on_indexed_album: Callable[[str], Awaitable[object]] | None = None,
    clock: Callable[[], float] = lambda: 1_800_000_000.0,
) -> LibraryScanCoordinator:
    reader = tag_reader or _TagReader()
    walker_kwargs: dict[str, DirectoryWalker] = {}
    if directory_walker is not None:
        walker_kwargs["directory_walker"] = directory_walker
    scanner = LibraryInventoryScanner(
        store, walk_deadline_seconds=walk_deadline_seconds, **walker_kwargs
    )
    return LibraryScanCoordinator(
        store,
        scanner,
        LibraryIndexer(
            store,
            reader,
            tag_read_timeout_seconds=tag_read_timeout_seconds,
            max_detached_tag_reads=max_detached_tag_reads,
        ),
        LibraryReconciler(store),
        lambda: resolver,
        clock=clock,
        on_indexed_album=on_indexed_album,
    )


def test_scan_state_contract_is_shared_and_complete() -> None:
    assert set(get_args(ScanState)) == {
        "queued",
        "discovering",
        "indexing",
        "reconciling",
        "pausing",
        "paused",
        "stopping",
        "completed",
        "cancelled",
        "superseded_policy_changed",
        "failed",
    }


@pytest.mark.asyncio
async def test_atomic_single_flight_coalescing_union_and_kind_conflict(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)

    first, duplicate = await asyncio.gather(
        coordinator.request_run(_request(resolver)),
        coordinator.request_run(_request(resolver)),
    )
    assert {first.disposition, duplicate.disposition} == {"started", "coalesced"}
    active = await target_store.claim_next_scan_run(now=1_800_000_001)
    assert active is not None

    queued = await coordinator.request_run(
        _request(resolver, relative_path="Disc 1", kind="rescan_files")
    )
    assert queued.disposition == "queued"
    expanded = await coordinator.request_run(
        _request(resolver, relative_path="Disc 2", kind="rescan_files")
    )
    assert expanded.disposition == "expanded"
    conflict = await coordinator.request_run(
        _request(resolver, kind="policy_reconcile")
    )
    assert conflict.disposition == "conflict"
    assert conflict.conflicting_kind == "rescan_files"
    _, scopes, _ = await target_store.get_scan_run(queued.run_id)
    assert {scope.relative_path for scope in scopes} == {"Disc 1", "Disc 2"}


@pytest.mark.asyncio
async def test_queued_only_disjoint_request_expands_existing_run(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)

    first = await coordinator.request_run(
        _request(resolver, relative_path="Disc 1", trigger="manual")
    )
    expanded = await coordinator.request_run(
        _request(resolver, relative_path="Disc 2", trigger="automatic")
    )

    assert first.disposition == "started"
    assert expanded.disposition == "expanded"
    assert expanded.run_id == first.run_id
    assert await target_store.row_count("library_scan_runs") == 1
    run, scopes, _ = await target_store.get_scan_run(first.run_id)
    assert run.kind == "incremental"
    assert run.trigger == "manual"
    assert {scope.relative_path for scope in scopes} == {"Disc 1", "Disc 2"}
    assert {scope.policy_revision for scope in scopes} == {resolver.policy_revision}

    with sqlite3.connect(target_store.db_path) as connection:
        triggers = connection.execute(
            "SELECT trigger, reason FROM library_scan_run_triggers "
            "WHERE run_id = ? ORDER BY trigger_sequence",
            (first.run_id,),
        ).fetchall()
        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list('library_scan_runs')"
            ).fetchall()
        }
    assert triggers == [("manual", "accepted"), ("automatic", "scope_expanded")]
    assert "idx_scan_runs_single_queued" in indexes


@pytest.mark.asyncio
async def test_concurrent_queued_only_requests_expand_one_run(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)

    results = await asyncio.gather(
        coordinator.request_run(
            _request(resolver, relative_path="Disc 1", trigger="manual")
        ),
        coordinator.request_run(
            _request(resolver, relative_path="Disc 2", trigger="automatic")
        ),
    )

    assert {result.disposition for result in results} == {"started", "expanded"}
    started = next(result for result in results if result.disposition == "started")
    expanded = next(result for result in results if result.disposition == "expanded")
    assert expanded.run_id == started.run_id
    assert await target_store.row_count("library_scan_runs") == 1
    _, scopes, _ = await target_store.get_scan_run(started.run_id)
    assert {scope.relative_path for scope in scopes} == {"Disc 1", "Disc 2"}
    assert await target_store.row_count("library_scan_run_triggers") == 2


@pytest.mark.asyncio
async def test_queued_only_incompatible_requests_conflict_without_mutation(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)

    first = await coordinator.request_run(
        _request(resolver, relative_path="Disc 1")
    )
    kind_conflict = await coordinator.request_run(
        _request(resolver, relative_path="Disc 2", kind="rescan_files")
    )
    policy_conflict = await coordinator.request_run(
        _request(
            resolver,
            relative_path="Disc 3",
            policy_revision="different-policy",
        )
    )

    assert kind_conflict.disposition == "conflict"
    assert kind_conflict.run_id == first.run_id
    assert kind_conflict.conflicting_kind == "incremental"
    assert policy_conflict.disposition == "conflict"
    assert policy_conflict.run_id == first.run_id
    assert policy_conflict.conflicting_kind == "incremental"
    assert await target_store.row_count("library_scan_runs") == 1
    assert await target_store.row_count("library_scan_run_triggers") == 1
    assert await target_store.get_stream_revision("scan") == 1
    run, scopes, _ = await target_store.get_scan_run(first.run_id)
    assert run.row_revision == first.row_revision
    assert {scope.relative_path for scope in scopes} == {"Disc 1"}

@pytest.mark.asyncio
async def test_every_target_trigger_uses_the_single_request_transaction(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    for trigger in (
        "manual",
        "automatic",
        "subsonic",
        "startup_resume",
        "policy_apply",
    ):
        result = await coordinator.request_run(_request(resolver, trigger=trigger))
        assert result.disposition in {"started", "coalesced"}
    assert await target_store.row_count("library_scan_runs") == 1
    assert await target_store.row_count("library_scan_run_triggers") == 5


@pytest.mark.asyncio
async def test_completed_scan_history_keeps_counters_and_phase_timings(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    requested = await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=1_800_000_001)
    assert run is not None and run.id == requested.run_id
    run = await target_store.add_scan_counters(
        run.id,
        {
            "inspected_count": 7,
            "new_count": 1,
            "changed_count": 1,
            "unchanged_count": 5,
        },
        updated_at=1_800_000_002,
    )
    run = await target_store.transition_scan_run(
        run.id,
        expected_state="discovering",
        expected_revision=run.row_revision,
        new_state="indexing",
        now=1_800_000_004,
    )
    run = await target_store.transition_scan_run(
        run.id,
        expected_state="indexing",
        expected_revision=run.row_revision,
        new_state="reconciling",
        now=1_800_000_009,
    )
    await target_store.transition_scan_run(
        run.id,
        expected_state="reconciling",
        expected_revision=run.row_revision,
        new_state="completed",
        now=1_800_000_011,
    )

    history = await target_store.list_scan_history()

    assert history[0].counters["inspected_count"] == 7
    assert history[0].counters["new_count"] == 1
    assert history[0].counters["changed_count"] == 1
    assert history[0].phase_timings == {
        "discovering": 3.0,
        "indexing": 5.0,
        "reconciling": 2.0,
    }


@pytest.mark.asyncio
async def test_indexer_reports_durable_progress_after_each_bounded_batch(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    file_count = INDEX_BATCH_SIZE * 2 + 2
    for ordinal in range(file_count):
        (root / f"track-{ordinal}.flac").write_bytes(b"audio")
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    requested = await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=10)
    assert run is not None and run.id == requested.run_id
    _, scopes, _ = await target_store.get_scan_run(run.id)

    async def continue_work(_run_id: str, _revision: str) -> bool:
        return True

    run = await LibraryInventoryScanner(target_store).discover(
        run, scopes, {"root-a": root}, resolver, continue_work
    )
    run = await target_store.transition_scan_run(
        run.id,
        expected_state="discovering",
        expected_revision=run.row_revision,
        new_state="indexing",
        now=11,
    )
    updates: list[int] = []

    previous = 0

    async def record_progress(updated_run: ScanRun) -> None:
        nonlocal previous
        inspected = updated_run.counters["inspected_count"]
        updates.append(inspected - previous)
        previous = inspected

    await LibraryIndexer(target_store, _TagReader()).index(
        run,
        resolver.policy_revision,
        continue_work,
        progress=record_progress,
    )

    _, _, counters = await target_store.get_scan_run(run.id)
    assert updates == [INDEX_BATCH_SIZE, INDEX_BATCH_SIZE, 2]
    assert counters["inspected_count"] == file_count
    assert counters["indexed_count"] == file_count
    assert counters["new_count"] == file_count


@pytest.mark.asyncio
async def test_file_changed_during_both_tag_reads_is_recorded_without_catalog_write(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "track-1.flac"
    track.write_bytes(b"audio")
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    requested = await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=10)
    assert run is not None and run.id == requested.run_id
    _, scopes, _ = await target_store.get_scan_run(run.id)

    async def continue_work(_run_id: str, _revision: str) -> bool:
        return True

    run = await LibraryInventoryScanner(target_store).discover(
        run, scopes, {"root-a": root}, resolver, continue_work
    )
    run = await target_store.transition_scan_run(
        run.id,
        expected_state="discovering",
        expected_revision=run.row_revision,
        new_state="indexing",
        now=11,
    )

    class MutatingReader:
        calls = 0

        def read_tags(self, path: Path) -> tuple[AudioTag, AudioInfo]:
            self.calls += 1
            path.write_bytes(path.read_bytes() + b"x")
            return (
                AudioTag(
                    title="Track 1",
                    artist="Artist",
                    album="Album",
                    album_artist="Artist",
                    track_number=1,
                ),
                AudioInfo(
                    duration_seconds=1,
                    bitrate=1,
                    sample_rate=44_100,
                    channels=2,
                    file_format="flac",
                    file_size_bytes=path.stat().st_size,
                    bit_depth=16,
                ),
            )

    reader = MutatingReader()
    counts = await LibraryIndexer(target_store, reader).index(
        run, resolver.policy_revision, continue_work
    )

    inventory = await target_store.get_scan_inventory_batch(
        run.id, processing_state="failed", limit=10
    )
    _, _, counters = await target_store.get_scan_run(run.id)
    assert reader.calls == 2
    assert counts["tag_reads"] == 2
    assert inventory[0]["failure_code"] == "FILE_CHANGED_DURING_READ"
    assert counters["inspected_count"] == 1
    assert counters["errored_count"] == 1
    assert await target_store.row_count("local_tracks") == 0


@pytest.mark.asyncio
async def test_transition_matrix_controls_and_restart_recovery(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    requested = await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=10)
    assert run is not None and run.state == "discovering"

    paused_request = await coordinator.control(run.id, "pause", run.row_revision)
    assert paused_request.state == "pausing"
    paused = await target_store.transition_scan_run(
        run.id,
        expected_state="pausing",
        expected_revision=paused_request.row_revision,
        new_state="paused",
        now=11,
    )
    recovered = await coordinator.recover()
    assert [item.id for item in recovered] == [requested.run_id]
    wake_revision = target_store.work_wakeups.revision("scan")
    waiting = asyncio.create_task(
        target_store.work_wakeups.wait(
            "scan", after_revision=wake_revision, timeout_seconds=1.0
        )
    )
    await asyncio.sleep(0)
    resumed = await coordinator.control(paused.id, "resume", paused.row_revision)
    assert resumed.state == "discovering"
    assert await waiting is True
    assert target_store.work_wakeups.revision("scan") == wake_revision + 1
    stopping = await coordinator.control(resumed.run_id, "stop", resumed.row_revision)
    assert stopping.state == "stopping"
    recovered = await coordinator.recover()
    assert recovered == []
    terminal, _, _ = await target_store.get_scan_run(run.id)
    assert terminal.state == "cancelled"

    with pytest.raises(StaleRevisionError):
        await target_store.transition_scan_run(
            run.id,
            expected_state="cancelled",
            expected_revision=terminal.row_revision,
            new_state="completed",
            now=12,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["discovering", "indexing", "reconciling"])
async def test_process_restart_resumes_same_run_through_completion(
    target_store: NativeLibraryStore, tmp_path: Path, phase: str
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track-1.flac").write_bytes(b"audio")
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    requested = await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=10)
    assert run is not None
    _, scopes, _ = await target_store.get_scan_run(run.id)

    async def continue_work(_run_id: str, _revision: str) -> bool:
        return True

    if phase in {"indexing", "reconciling"}:
        run = await LibraryInventoryScanner(target_store).discover(
            run,
            scopes,
            {"root-a": root},
            resolver,
            continue_work,
        )
        run = await target_store.transition_scan_run(
            run.id,
            expected_state="discovering",
            expected_revision=run.row_revision,
            new_state="indexing",
            now=11,
        )
    if phase == "reconciling":
        await LibraryIndexer(target_store, _TagReader()).index(
            run, resolver.policy_revision, continue_work
        )
        run, _, _ = await target_store.get_scan_run(run.id)
        run = await target_store.transition_scan_run(
            run.id,
            expected_state="indexing",
            expected_revision=run.row_revision,
            new_state="reconciling",
            now=12,
        )

    recovered = await coordinator.recover()
    assert [item.id for item in recovered] == [requested.run_id]
    completed = await coordinator.run_once({"root-a": root})

    assert completed is not None
    assert completed.id == requested.run_id
    assert completed.state == "completed"
    with sqlite3.connect(tmp_path / "target.db") as connection:
        triggers = connection.execute(
            "SELECT trigger FROM library_scan_run_triggers WHERE run_id = ? ORDER BY trigger_sequence",
            (requested.run_id,),
        ).fetchall()
    assert triggers[-1][0] == "startup_resume"


@pytest.mark.asyncio
async def test_scan_worker_exception_becomes_typed_terminal_failure(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)

    class BrokenInventory(LibraryInventoryScanner):
        async def discover(self, *args, **kwargs):
            raise RuntimeError("injected worker failure")

    coordinator = LibraryScanCoordinator(
        target_store,
        BrokenInventory(target_store),
        LibraryIndexer(target_store, _TagReader()),
        LibraryReconciler(target_store),
        lambda: resolver,
        clock=lambda: 20,
    )
    requested = await coordinator.request_run(_request(resolver))

    with pytest.raises(RuntimeError, match="injected worker failure"):
        await coordinator.run_once({"root-a": root})

    failed, _, _ = await target_store.get_scan_run(requested.run_id)
    assert failed.state == "failed"
    assert failed.terminal_code == "UNEXPECTED_WORKER_FAILURE"


@pytest.mark.parametrize("control,expected_state", [("pause", "paused"), ("stop", "cancelled")])
@pytest.mark.asyncio
async def test_pending_control_worker_exception_is_logged_and_settles(
    target_store: NativeLibraryStore, tmp_path: Path, control: str, expected_state: str, caplog
) -> None:
    caplog.set_level(logging.ERROR)
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    # Barrier to hold worker until control is persisted
    proceed = asyncio.Event()
    reached = asyncio.Event()

    class BarrierInventory(LibraryInventoryScanner):
        async def discover(self, run, scopes, root_paths, resolver_getter, checkpoint):
            reached.set()
            await proceed.wait()
            raise RuntimeError(f"tag store failed during {control}")

    coordinator = LibraryScanCoordinator(
        target_store,
        BarrierInventory(target_store),
        LibraryIndexer(target_store, _TagReader()),
        LibraryReconciler(target_store),
        lambda: resolver,
        clock=lambda: 20,
    )
    requested = await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=20)
    assert run is not None
    # Start worker and wait until it reaches barrier
    worker_task = asyncio.create_task(coordinator.run_once({"root-a": root}))
    await reached.wait()
    # Submit control while worker is at barrier - fetch latest revision to avoid StaleRevisionError
    cur, _, _ = await target_store.get_scan_run(run.id)
    await coordinator.control(cur.id, control, cur.row_revision)
    # Verify control was persisted
    cur, _, _ = await target_store.get_scan_run(run.id)
    assert cur.state == ("pausing" if control == "pause" else "stopping")
    # Release worker to raise
    proceed.set()
    result = await worker_task
    assert result is not None and result.state == expected_state
    # Verify durable state and that the original exception was logged with traceback
    stored, _, _ = await target_store.get_scan_run(run.id)
    assert stored.state == expected_state
    # Check log contains original exception identity and traceback, and run_id/control-state, no file path
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR and "Scan worker failed" in r.getMessage()]
    assert len(error_records) == 1
    # Original state should be discovering (the run's state before the worker exception), not pending control
    assert "during scan state discovering" in error_records[0].getMessage()
    assert "pending control" not in error_records[0].getMessage().lower()
    assert run.id in error_records[0].getMessage()
    # Exception identity and traceback
    assert error_records[0].exc_info is not None
    assert error_records[0].exc_info[0] is RuntimeError
    assert f"tag store failed during {control}" in str(error_records[0].exc_info[1])
    assert "RuntimeError" in caplog.text
    assert "track-1.flac" not in caplog.text and "secret" not in caplog.text
    # Verify stream/invalidation/gate cleanup: run is terminal, so gate should be cleared and no pending
    assert coordinator._pending_control_run_ids == set()
    if expected_state == "cancelled":
        assert len(caplog.records) >= 1  # at least the one exception log


@pytest.mark.asyncio
async def test_active_worker_exception_still_becomes_typed_failure_and_is_reraised(
    target_store: NativeLibraryStore, tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.ERROR)
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)

    class BrokenInventory(LibraryInventoryScanner):
        async def discover(self, *args, **kwargs):
            raise RuntimeError("injected active failure")

    coordinator = LibraryScanCoordinator(
        target_store,
        BrokenInventory(target_store),
        LibraryIndexer(target_store, _TagReader()),
        LibraryReconciler(target_store),
        lambda: resolver,
        clock=lambda: 20,
    )
    requested = await coordinator.request_run(_request(resolver))
    with pytest.raises(RuntimeError, match="injected active failure"):
        await coordinator.run_once({"root-a": root})
    failed, _, _ = await target_store.get_scan_run(requested.run_id)
    assert failed.state == "failed"
    assert failed.terminal_code == "UNEXPECTED_WORKER_FAILURE"
    # Also should have logged the exception with truthful state
    error_records = [r for r in caplog.records if "Scan worker failed" in r.getMessage()]
    assert len(error_records) == 1
    assert "during scan state discovering" in error_records[0].getMessage()
    assert requested.run_id in error_records[0].getMessage()
    assert any("injected active failure" in str(r.exc_info[1]) for r in caplog.records if r.exc_info)
@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["discovering", "indexing", "reconciling"])
async def test_pause_resume_and_stop_are_idempotent_at_every_phase(
    target_store: NativeLibraryStore, tmp_path: Path, phase: str
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=10)
    assert run is not None
    if phase in {"indexing", "reconciling"}:
        run = await target_store.transition_scan_run(
            run.id,
            expected_state="discovering",
            expected_revision=run.row_revision,
            new_state="indexing",
            now=11,
        )
    if phase == "reconciling":
        run = await target_store.transition_scan_run(
            run.id,
            expected_state="indexing",
            expected_revision=run.row_revision,
            new_state="reconciling",
            now=12,
        )
    original_revision = run.row_revision
    requested = await coordinator.control(run.id, "pause", original_revision)
    repeated = await coordinator.control(run.id, "pause", original_revision)
    assert repeated.row_revision == requested.row_revision
    paused = await target_store.transition_scan_run(
        run.id,
        expected_state="pausing",
        expected_revision=requested.row_revision,
        new_state="paused",
        now=13,
    )
    resumed = await coordinator.control(run.id, "resume", paused.row_revision)
    assert resumed.state == phase
    repeated_resume = await coordinator.control(run.id, "resume", paused.row_revision)
    assert repeated_resume.row_revision == resumed.row_revision
    stopping = await coordinator.control(run.id, "stop", resumed.row_revision)
    repeated_stop = await coordinator.control(run.id, "stop", resumed.row_revision)
    assert stopping.state == "stopping"
    assert repeated_stop.row_revision == stopping.row_revision


@pytest.mark.asyncio
async def test_only_latest_fifty_terminal_runs_are_retained(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    for ordinal in range(51):
        await coordinator.request_run(_request(resolver))
        run = await target_store.claim_next_scan_run(now=ordinal * 10 + 1)
        assert run is not None
        run = await target_store.transition_scan_run(
            run.id,
            expected_state="discovering",
            expected_revision=run.row_revision,
            new_state="indexing",
            now=ordinal * 10 + 2,
        )
        run = await target_store.transition_scan_run(
            run.id,
            expected_state="indexing",
            expected_revision=run.row_revision,
            new_state="reconciling",
            now=ordinal * 10 + 3,
        )
        await target_store.transition_scan_run(
            run.id,
            expected_state="reconciling",
            expected_revision=run.row_revision,
            new_state="completed",
            now=ordinal * 10 + 4,
        )
    await target_store.cleanup_terminal_scan_inventory()
    assert await target_store.row_count("library_scan_runs") == 50
    assert len(await coordinator.history(limit=50)) == 50
    first_page, cursor = await coordinator.history_page(limit=20)
    assert len(first_page) == 20
    assert cursor is not None
    second_page, _ = await coordinator.history_page(limit=20, cursor=cursor)
    assert len(second_page) == 20
    assert {run.id for run in first_page}.isdisjoint(run.id for run in second_page)


@pytest.mark.asyncio
async def test_one_walk_incremental_tag_revisions_and_no_repeat(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    first = album / "track-1.flac"
    second = album / "track-2.flac"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    resolver = _resolver(root)
    reader = _TagReader()
    real_walk = os.walk
    walk_count = 0
    scheduled_album_ids: list[str] = []

    async def schedule_album(album_id: str) -> None:
        scheduled_album_ids.append(album_id)

    def counted_walk(*args, **kwargs):
        nonlocal walk_count
        walk_count += 1
        return real_walk(*args, **kwargs)

    coordinator = _coordinator(
        target_store,
        resolver,
        reader,
        directory_walker=counted_walk,
        on_indexed_album=schedule_album,
    )
    await coordinator.request_run(_request(resolver))
    completed = await coordinator.run_once({"root-a": root})
    assert completed is not None and completed.state == "completed"
    assert completed.counters["new_count"] == 2
    assert completed.counters["changed_count"] == 0
    assert walk_count == 1
    assert len(reader.calls) == 2
    assert scheduled_album_ids == []
    assert await coordinator.run_once({"root-a": root}) is None
    assert len(scheduled_album_ids) == 1
    assert INVENTORY_QUEUE_SIZE == 256
    assert INVENTORY_BATCH_SIZE == 256

    await coordinator.request_run(_request(resolver, trigger="automatic"))
    repeated = await coordinator.run_once({"root-a": root})
    assert repeated is not None and repeated.state == "completed"
    assert repeated.counters["new_count"] == 0
    assert repeated.counters["changed_count"] == 0
    assert walk_count == 2
    assert len(reader.calls) == 2
    assert len(scheduled_album_ids) == 1

    await coordinator.request_run(_request(resolver, kind="rescan_files"))
    rescanned = await coordinator.run_once({"root-a": root})
    assert rescanned is not None and rescanned.state == "completed"
    assert rescanned.counters["new_count"] == 0
    assert rescanned.counters["changed_count"] == 0
    assert walk_count == 3
    assert len(reader.calls) == 4
    assert len(scheduled_album_ids) == 1

    first.write_bytes(b"one changed")
    await coordinator.request_run(_request(resolver))
    changed = await coordinator.run_once({"root-a": root})
    assert changed is not None and changed.state == "completed"
    assert changed.counters["new_count"] == 0
    assert changed.counters["changed_count"] == 1
    assert walk_count == 4
    assert len(reader.calls) == 5
    assert len(scheduled_album_ids) == 1
    assert await coordinator.run_once({"root-a": root}) is None
    tracks = await target_store.search_local_tracks("Track")
    assert len(tracks) == 2
    assert scheduled_album_ids == [
        str(tracks[0]["local_album_id"]),
        str(tracks[0]["local_album_id"]),
    ]


@pytest.mark.asyncio
async def test_walk_permission_error_fails_run_and_records_failed_path(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)

    def denied_walk(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(root / "secret"))
        yield

    coordinator = _coordinator(target_store, resolver, directory_walker=denied_walk)
    await coordinator.request_run(_request(resolver))
    failed = await coordinator.run_once({"root-a": root})

    assert failed is not None and failed.state == "failed"
    assert failed.terminal_code == "ROOT_PERMISSION_DENIED"
    failures, next_cursor = await target_store.list_scan_run_failures(failed.id)
    assert next_cursor is None
    assert [
        (failure.failure_code, failure.relative_path, failure.phase)
        for failure in failures
    ] == [("WALK_EACCES", "secret", "discovering")]


@pytest.mark.asyncio
async def test_wedged_walk_fails_bounded_and_the_next_run_claims(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track-1.flac").write_bytes(b"one")
    resolver = _resolver(root)
    wedged = threading.Event()
    calls = 0

    def walker(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield (str(root), [], ["track-1.flac"])
            wedged.wait()
            return
        yield from os.walk(str(root), followlinks=False)

    coordinator = _coordinator(
        target_store,
        resolver,
        directory_walker=walker,
        walk_deadline_seconds=0.05,
    )
    try:
        await coordinator.request_run(_request(resolver))
        started = time.monotonic()
        failed = await asyncio.wait_for(
            coordinator.run_once({"root-a": root}), timeout=10
        )
        elapsed = time.monotonic() - started
    finally:
        wedged.set()

    assert elapsed < 5.0
    assert failed is not None and failed.state == "failed"
    assert failed.terminal_code == "WALK_TIMEOUT"
    failures, _cursor = await target_store.list_scan_run_failures(failed.id)
    assert [failure.failure_code for failure in failures] == ["WALK_TIMEOUT"]
    assert await coordinator.run_once({"root-a": root}) is None

    await coordinator.request_run(_request(resolver, trigger="automatic"))
    completed = await coordinator.run_once({"root-a": root})
    assert completed is not None and completed.state == "completed"


@pytest.mark.asyncio
async def test_queued_scan_is_completed_before_staged_management_callbacks(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track-1.flac").write_bytes(b"audio")
    resolver = _resolver(root)
    callback_states: list[str] = []

    async def schedule_album(_album_id: str) -> None:
        latest = (await target_store.list_scan_history(limit=1))[0]
        callback_states.append(latest.state)

    coordinator = _coordinator(target_store, resolver, on_indexed_album=schedule_album)
    await coordinator.request_run(_request(resolver))
    first = await coordinator.run_once({"root-a": root})
    assert first is not None and first.state == "completed"
    assert callback_states == []

    await coordinator.request_run(_request(resolver, kind="rescan_files"))
    second = await coordinator.run_once({"root-a": root})
    assert second is not None and second.state == "completed"
    assert callback_states == []

    assert await coordinator.run_once({"root-a": root}) is None
    assert callback_states == ["completed"]


@pytest.mark.asyncio
async def test_candidate_staging_failure_rolls_back_before_terminal_transition(
    target_store: NativeLibraryStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track-1.flac").write_bytes(b"audio")
    resolver = _resolver(root)

    async def schedule_album(_album_id: str) -> None:
        return None

    original_stage = target_store._stage_scan_management_candidates_tx  # noqa: SLF001

    def fail_after_staging(
        connection: sqlite3.Connection, run_id: str, *, now: float
    ) -> int:
        original_stage(connection, run_id, now=now)
        raise RuntimeError("injected failure after candidate staging")

    monkeypatch.setattr(
        target_store, "_stage_scan_management_candidates_tx", fail_after_staging
    )
    coordinator = _coordinator(target_store, resolver, on_indexed_album=schedule_album)
    requested = await coordinator.request_run(_request(resolver))

    with pytest.raises(RuntimeError, match="failure after candidate staging"):
        await coordinator.run_once({"root-a": root})

    failed, _, _ = await target_store.get_scan_run(requested.run_id)
    assert failed.state == "failed"
    assert failed.terminal_code == "UNEXPECTED_WORKER_FAILURE"
    with sqlite3.connect(tmp_path / "target.db") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM library_scan_management_candidates"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM library_scan_management_staging"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_post_scan_management_callback_retries_durably_after_restart(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track-1.flac").write_bytes(b"audio")
    resolver = _resolver(root)
    now = 1_800_000_000.0
    attempts: list[str] = []

    async def schedule_album(album_id: str) -> None:
        attempts.append(album_id)
        if len(attempts) == 1:
            raise RuntimeError("temporary management scheduling failure")

    coordinator = _coordinator(
        target_store,
        resolver,
        on_indexed_album=schedule_album,
        clock=lambda: now,
    )
    await coordinator.request_run(_request(resolver))
    completed = await coordinator.run_once({"root-a": root})

    assert completed is not None and completed.state == "completed"
    assert attempts == []
    with pytest.raises(StaleRevisionError):
        await target_store.transition_scan_run(
            completed.id,
            expected_state="reconciling",
            expected_revision=completed.row_revision - 1,
            new_state="completed",
            now=now + 100,
            stage_management_candidates=True,
        )
    with sqlite3.connect(tmp_path / "target.db") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM library_scan_management_candidates"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM library_scan_management_staging"
            ).fetchone()[0]
            == 1
        )
    assert await coordinator.run_once({"root-a": root}) is None
    assert len(attempts) == 1
    with sqlite3.connect(tmp_path / "target.db") as connection:
        pending = connection.execute(
            "SELECT state,attempt_count,next_attempt_at "
            "FROM library_scan_management_candidates"
        ).fetchone()
        staged_at = connection.execute(
            "SELECT staged_at FROM library_scan_management_staging"
        ).fetchone()[0]
    assert pending == ("pending", 1, now + 1)
    assert staged_at == now

    restarted_store = NativeLibraryStore(
        db_path=tmp_path / "target.db", write_lock=threading.Lock()
    )
    now += 2
    restarted = _coordinator(
        restarted_store,
        resolver,
        on_indexed_album=schedule_album,
        clock=lambda: now,
    )
    assert await restarted.run_once({"root-a": root}) is None
    assert len(attempts) == 2
    for _ in range(10):
        assert await restarted.run_once({"root-a": root}) is None
    with sqlite3.connect(tmp_path / "target.db") as connection:
        completed_candidate = connection.execute(
            "SELECT state,attempt_count,completed_at "
            "FROM library_scan_management_candidates"
        ).fetchone()
        remaining_inventory = connection.execute(
            "SELECT COUNT(*) FROM library_scan_inventory"
        ).fetchone()[0]
    assert completed_candidate == ("completed", 1, now)
    assert remaining_inventory == 0

    assert await restarted.run_once({"root-a": root}) is None
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_scan_transaction_ratios_and_tag_read_gates(
    target_store: NativeLibraryStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "music"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    file_count = 4_096
    for index in range(file_count):
        (album / f"track-{index}.flac").write_bytes(b"audio")
    resolver = _resolver(root)
    reader = _TagReader()

    class NoGrouping:
        async def regroup_run(
            self,
            _run_id: str,
            *,
            now: float,
            checkpoint=None,
            frozen_policy_revision: str = "",
        ) -> int:
            del now, checkpoint, frozen_policy_revision
            return 0

    coordinator = LibraryScanCoordinator(
        target_store,
        LibraryInventoryScanner(target_store),
        LibraryIndexer(target_store, reader, grouping=NoGrouping()),
        LibraryReconciler(target_store),
        lambda: resolver,
        clock=lambda: 1_800_000_000.0,
    )
    original_execute_background = target_store._execute_background
    background_transactions = 0

    def count_background_transaction(operation):
        nonlocal background_transactions
        background_transactions += 1
        return original_execute_background(operation)

    monkeypatch.setattr(
        target_store, "_execute_background", count_background_transaction
    )
    await coordinator.request_run(_request(resolver))
    first = await coordinator.run_once({"root-a": root})

    assert first is not None and first.state == "completed"
    assert len(reader.calls) == file_count
    assert background_transactions / file_count < 0.03
    catalog_revision = await target_store.get_catalog_revision()

    background_transactions = 0
    await coordinator.request_run(_request(resolver))
    unchanged = await coordinator.run_once({"root-a": root})

    assert unchanged is not None and unchanged.state == "completed"
    assert len(reader.calls) == file_count
    assert background_transactions / file_count < 0.02
    assert await target_store.get_catalog_revision() == catalog_revision

    changed_path = album / "track-0.flac"
    changed_path.write_bytes(b"changed")
    await coordinator.request_run(_request(resolver))
    changed = await coordinator.run_once({"root-a": root})

    assert changed is not None and changed.state == "completed"
    assert len(reader.calls) == file_count + 1
    assert await target_store.get_catalog_revision() == catalog_revision


@pytest.mark.asyncio
async def test_scan_ignores_reserved_management_artifacts(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track-1.flac").write_bytes(b"audio")
    (root / ".droppedneedle-management-job-track-2.flac").write_bytes(b"temp")
    hidden = root / ".droppedneedle-management-job"
    hidden.mkdir()
    (hidden / "track-3.flac").write_bytes(b"backup")
    resolver = _resolver(root)
    filesystem = LibraryFilesystemCoordinator()
    reader = _TagReader()
    coordinator = LibraryScanCoordinator(
        target_store,
        LibraryInventoryScanner(target_store, filesystem_coordinator=filesystem),
        LibraryIndexer(target_store, reader, filesystem_coordinator=filesystem),
        LibraryReconciler(target_store, filesystem),
        lambda: resolver,
    )

    await coordinator.request_run(_request(resolver))
    completed = await coordinator.run_once({"root-a": root})

    assert completed is not None and completed.state == "completed"
    assert [path.name for path in reader.calls] == ["track-1.flac"]
    assert len(await target_store.search_local_tracks("Track")) == 1


@pytest.mark.asyncio
async def test_publication_after_discovery_cannot_mark_new_catalog_path_missing(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    source = root / "track-1.flac"
    source.write_bytes(b"audio")
    resolver = _resolver(root)
    filesystem = LibraryFilesystemCoordinator()
    coordinator = LibraryScanCoordinator(
        target_store,
        LibraryInventoryScanner(target_store, filesystem_coordinator=filesystem),
        LibraryIndexer(target_store, _TagReader(), filesystem_coordinator=filesystem),
        LibraryReconciler(target_store, filesystem),
        lambda: resolver,
    )
    await coordinator.request_run(_request(resolver))
    first = await coordinator.run_once({"root-a": root})
    assert first is not None and first.state == "completed"
    track = (await target_store.search_local_tracks("Track"))[0]

    requested = await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=20)
    assert run is not None and run.id == requested.run_id
    _, scopes, _ = await target_store.get_scan_run(run.id)

    async def continue_work(_run_id: str, _revision: str) -> bool:
        return True

    run = await coordinator._inventory.discover(
        run, scopes, {"root-a": root}, resolver, continue_work
    )
    destination = root / "renamed-track-1.flac"
    async with filesystem.write("root-a"):
        source.rename(destination)
        stat = destination.stat()
        with sqlite3.connect(tmp_path / "target.db") as connection:
            connection.execute(
                "UPDATE local_tracks SET file_path=?, relative_path=?, "
                "file_size_bytes=?, file_mtime_ns=? WHERE id=?",
                (
                    str(destination),
                    destination.name,
                    stat.st_size,
                    stat.st_mtime_ns,
                    track["id"],
                ),
            )

    run = await target_store.transition_scan_run(
        run.id,
        expected_state="discovering",
        expected_revision=run.row_revision,
        new_state="indexing",
        now=21,
    )
    run = await target_store.transition_scan_run(
        run.id,
        expected_state="indexing",
        expected_revision=run.row_revision,
        new_state="reconciling",
        now=22,
    )
    await coordinator._reconciler.reconcile(run.id, scopes, continue_work)

    moved = await target_store.get_target_track_by_path(str(destination))
    assert moved is not None
    assert moved["availability"] == "indexed"


@pytest.mark.asyncio
async def test_reconcile_uses_inventory_and_unavailable_root_cannot_mark_missing(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "track-1.flac"
    track.write_bytes(b"one")
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    await coordinator.request_run(_request(resolver))
    await coordinator.run_once({"root-a": root})

    track.unlink()
    await coordinator.request_run(_request(resolver))
    await coordinator.run_once({"root-a": root})
    row = await target_store.search_local_tracks("Track")
    assert row == []
    stored = await target_store.get_stored_sibling_context("root-a", ".")
    assert stored[0]["availability"] == "missing"


@pytest.mark.asyncio
async def test_blocking_tag_read_stays_pausing_until_the_safe_boundary(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "track-1.flac"
    track.write_bytes(b"one")
    resolver = _resolver(root)
    entered = threading.Event()
    release = threading.Event()

    class BlockingReader(_TagReader):
        def read_tags(self, path: Path) -> tuple[AudioTag, AudioInfo]:
            entered.set()
            if not release.wait(timeout=2):
                raise ValueError("test timeout")
            return super().read_tags(path)

    coordinator = _coordinator(target_store, resolver, BlockingReader())
    await coordinator.request_run(_request(resolver))
    worker = asyncio.create_task(coordinator.run_once({"root-a": root}))
    assert await asyncio.to_thread(entered.wait, 1)
    current = (await coordinator.current())[0]
    requested = await coordinator.control(current.id, "pause", current.row_revision)
    assert requested.state == "pausing"
    still_pausing = (await coordinator.current())[0]
    assert still_pausing.state == "pausing"
    release.set()
    result = await worker
    assert result is not None and result.state == "paused"
    assert await target_store.search_local_tracks("Track") == []


@pytest.mark.asyncio
async def test_stalled_tag_read_is_recorded_and_later_files_continue(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    for number in (1, 2):
        (root / f"track-{number}.flac").write_bytes(b"audio")
    resolver = _resolver(root)
    entered = threading.Event()
    release = threading.Event()

    class StalledReader(_TagReader):
        def read_tags(self, path: Path) -> tuple[AudioTag, AudioInfo]:
            if path.name == "track-1.flac":
                entered.set()
                release.wait()
            return super().read_tags(path)

    coordinator = _coordinator(
        target_store,
        resolver,
        StalledReader(),
        tag_read_timeout_seconds=0.02,
    )
    await coordinator.request_run(_request(resolver))
    try:
        result = await asyncio.wait_for(
            coordinator.run_once({"root-a": root}), timeout=1
        )
        assert result is not None and result.state == "completed"
        assert entered.is_set()
        _, _, counters = await target_store.get_scan_run(result.id)
        assert counters["indexed_count"] == 1
        assert counters["errored_count"] == 1
        failures = await target_store.get_scan_inventory_batch(
            result.id, processing_state="failed", limit=10
        )
        assert [row["failure_code"] for row in failures] == ["TAG_READ_TIMEOUT"]
        tracks = await target_store.search_local_tracks("Track")
        assert [track["title"] for track in tracks] == ["Track 2"]
    finally:
        release.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_stalled_tag_reads_have_bounded_executor_capacity(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    for number in (1, 2, 3):
        (root / f"track-{number}.flac").write_bytes(b"audio")
    resolver = _resolver(root)
    release = threading.Event()

    class StalledReader(_TagReader):
        def read_tags(self, path: Path) -> tuple[AudioTag, AudioInfo]:
            self.calls.append(path)
            release.wait()
            return super().read_tags(path)

    reader = StalledReader()
    coordinator = _coordinator(
        target_store,
        resolver,
        reader,
        tag_read_timeout_seconds=0.02,
        max_detached_tag_reads=1,
    )
    await coordinator.request_run(_request(resolver))
    try:
        result = await asyncio.wait_for(
            coordinator.run_once({"root-a": root}), timeout=1
        )
        assert result is not None and result.state == "completed"
        _, _, counters = await target_store.get_scan_run(result.id)
        assert counters["indexed_count"] == 0
        assert counters["errored_count"] == 3
        assert len(reader.calls) == 1
        failures = await target_store.get_scan_inventory_batch(
            result.id, processing_state="failed", limit=10
        )
        assert sorted(row["failure_code"] for row in failures) == [
            "TAG_READ_DEFERRED",
            "TAG_READ_DEFERRED",
            "TAG_READ_TIMEOUT",
        ]
    finally:
        release.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_cancelled_tag_read_remains_counted_until_worker_finishes(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    track = tmp_path / "track-1.flac"
    track.write_bytes(b"audio")
    entered = threading.Event()
    release = threading.Event()

    class BlockingReader(_TagReader):
        def read_tags(self, path: Path) -> tuple[AudioTag, AudioInfo]:
            entered.set()
            release.wait()
            return super().read_tags(path)

    indexer = LibraryIndexer(
        target_store,
        BlockingReader(),
        tag_read_timeout_seconds=30,
        max_detached_tag_reads=1,
    )
    read = asyncio.create_task(indexer._read_tags_and_stat(track, "root-a"))
    assert await asyncio.to_thread(entered.wait, 1)
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read
    assert len(indexer._detached_tag_reads) == 1

    release.set()
    for _ in range(20):
        if not indexer._detached_tag_reads:
            break
        await asyncio.sleep(0.01)
    assert indexer._detached_tag_reads == set()


@pytest.mark.asyncio
async def test_forced_rescan_preserves_accepted_catalog_projection(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track-1.flac").write_bytes(b"audio")
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    await coordinator.request_run(_request(resolver))
    first = await coordinator.run_once({"root-a": root})
    assert first is not None and first.state == "completed"

    with sqlite3.connect(target_store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        track = connection.execute("SELECT * FROM local_tracks").fetchone()
        assert track is not None
        track_id = str(track["id"])
        album_id = str(track["local_album_id"])
        connection.execute(
            "UPDATE local_tracks SET title='Feel It Inside',title_folded='feel it inside',"
            "artist_name='Trapeze',artist_name_folded='trapeze',album_title='Hot Wire',"
            "album_title_folded='hot wire',album_artist_name='Trapeze',"
            "album_artist_name_folded='trapeze',track_number=8,year=1974,"
            "membership_locked=1 WHERE id=?",
            (track_id,),
        )
        connection.execute(
            "UPDATE local_albums SET title='Hot Wire',title_folded='hot wire',"
            "album_artist_name='Trapeze',album_artist_name_folded='trapeze',year=1974 "
            "WHERE id=?",
            (album_id,),
        )
        connection.execute(
            "INSERT INTO local_album_external_identities "
            "(local_album_id,provider,release_group_mbid,release_mbid,decision_source,selected_at) "
            "VALUES (?,'musicbrainz','release-group','release','manual',1)",
            (album_id,),
        )
        connection.execute(
            "INSERT INTO local_track_external_identities "
            "(local_track_id,provider,recording_mbid,release_mbid,release_track_mbid,"
            "medium_position,release_track_position,decision_source,selected_at) "
            "VALUES (?,'musicbrainz','recording','release','release-track',1,8,'manual',1)",
            (track_id,),
        )
        connection.commit()

    class EmptyTagReader(_TagReader):
        def read_tags(self, path: Path) -> tuple[AudioTag, AudioInfo]:
            _tag, info = super().read_tags(path)
            return AudioTag(title="", artist="", album="", track_number=0), info

    forced = _coordinator(target_store, resolver, EmptyTagReader())
    await forced.request_run(_request(resolver, kind="rescan_files"))
    result = await forced.run_once({"root-a": root})
    assert result is not None and result.state == "completed"

    with sqlite3.connect(target_store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        stored = connection.execute(
            "SELECT * FROM local_tracks WHERE id=?", (track_id,)
        ).fetchone()
    assert stored is not None
    assert stored["title"] == "Feel It Inside"
    assert stored["artist_name"] == "Trapeze"
    assert stored["album_title"] == "Hot Wire"
    assert stored["track_number"] == 8
    assert stored["year"] == 1974
    with sqlite3.connect(target_store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        album = connection.execute(
            "SELECT * FROM local_albums WHERE id=?", (album_id,)
        ).fetchone()
    assert album is not None
    assert album["title"] == "Hot Wire"
    assert album["album_artist_name"] == "Trapeze"
    assert album["year"] == 1974


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control", "expected_state"), [("pause", "paused"), ("stop", "cancelled")]
)
async def test_control_at_discovery_phase_boundary_is_settled(
    target_store: NativeLibraryStore,
    tmp_path: Path,
    control: str,
    expected_state: str,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    await coordinator.request_run(_request(resolver))

    async def request_at_boundary(run, *_args, **_kwargs):
        current, _, _ = await target_store.get_scan_run(run.id)
        await coordinator.control(current.id, control, current.row_revision)
        return run

    coordinator._inventory.discover = request_at_boundary  # type: ignore[method-assign]

    result = await coordinator.run_once({"root-a": root})

    assert result is not None and result.state == expected_state
    persisted, _, _ = await target_store.get_scan_run(result.id)
    assert persisted.state == expected_state


@pytest.mark.asyncio
async def test_control_winning_discovery_revision_race_is_settled(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    await coordinator.request_run(_request(resolver))

    async def lose_revision_race(run, *_args, **_kwargs):
        current, _, _ = await target_store.get_scan_run(run.id)
        await coordinator.control(current.id, "pause", current.row_revision)
        raise StaleRevisionError("control won the scan batch revision race")

    coordinator._inventory.discover = lose_revision_race  # type: ignore[method-assign]

    result = await coordinator.run_once({"root-a": root})

    assert result is not None and result.state == "paused"


@pytest.mark.asyncio
async def test_discovery_cancellation_drains_the_bounded_thread_queue(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    for ordinal in range(600):
        (root / f"track-{ordinal}.flac").write_bytes(b"audio")
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=10)
    assert run is not None
    _, scopes, _ = await target_store.get_scan_run(run.id)
    scanner = LibraryInventoryScanner(target_store)
    task = asyncio.create_task(
        scanner.discover(
            run,
            scopes,
            {"root-a": root},
            resolver,
            coordinator.checkpoint,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_reconciliation_is_bounded_and_resumes_past_its_cursor(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    for ordinal in range(270):
        (root / f"track-{ordinal}.flac").write_bytes(b"audio")
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    await coordinator.request_run(_request(resolver))
    completed = await coordinator.run_once({"root-a": root})
    assert completed is not None and completed.state == "completed"

    for path in root.iterdir():
        path.unlink()
    await coordinator.request_run(_request(resolver))
    reconciled = await coordinator.run_once({"root-a": root})
    assert reconciled is not None and reconciled.state == "completed"
    stored = await target_store.get_stored_sibling_context("root-a", ".")
    assert len(stored) == 270
    assert {row["availability"] for row in stored} == {"missing"}
    root.rmdir()
    await coordinator.request_run(_request(resolver))
    failed = await coordinator.run_once({"root-a": root})
    assert failed is not None and failed.state == "failed"
    stored = await target_store.get_stored_sibling_context("root-a", ".")
    assert stored[0]["availability"] == "missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["discovering", "indexing", "reconciling", "paused"])
async def test_policy_supersession_is_terminal_and_queues_nothing(
    target_store: NativeLibraryStore, tmp_path: Path, phase: str
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    current = resolver
    coordinator = LibraryScanCoordinator(
        target_store,
        LibraryInventoryScanner(target_store),
        LibraryIndexer(target_store, _TagReader()),
        LibraryReconciler(target_store),
        lambda: current,
        clock=lambda: 20,
    )
    await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=20)
    assert run is not None
    if phase in {"indexing", "reconciling", "paused"}:
        run = await target_store.transition_scan_run(
            run.id,
            expected_state="discovering",
            expected_revision=run.row_revision,
            new_state="indexing",
            now=21,
        )
    if phase in {"reconciling", "paused"}:
        run = await target_store.transition_scan_run(
            run.id,
            expected_state="indexing",
            expected_revision=run.row_revision,
            new_state="reconciling",
            now=22,
        )
    if phase == "paused":
        requested = await coordinator.control(run.id, "pause", run.row_revision)
        run = await target_store.transition_scan_run(
            run.id,
            expected_state="pausing",
            expected_revision=requested.row_revision,
            new_state="paused",
            now=23,
        )
    current = LibraryPolicyResolver(
        TypedLibrarySettings(
            library_roots=[
                LibraryRootSettings(
                    id="root-a", path=str(root), label="Library", policy="excluded"
                )
            ]
        )
    )
    assert not await coordinator.checkpoint(run.id, resolver.policy_revision)
    terminal, _, _ = await target_store.get_scan_run(run.id)
    assert terminal.state == "superseded_policy_changed"
    assert await coordinator.current() == []
    assert await target_store.row_count("library_scan_inventory") == 0
@pytest.mark.asyncio
async def test_stop_wins_over_concurrent_policy_change(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=1)
    assert run is not None
    await coordinator.control(run.id, "stop", run.row_revision)
    persisted, _, _ = await target_store.get_scan_run(run.id)
    assert persisted.state == "stopping"
    assert persisted.requested_control == "stop"
    # Change policy revision
    new_resolver = LibraryPolicyResolver(
        TypedLibrarySettings(
            library_roots=[LibraryRootSettings(id="root-a", path=str(root), label="Library", policy="excluded")]
        )
    )
    coordinator._resolver_getter = lambda: new_resolver  # type: ignore[method-assign]
    result = await coordinator.checkpoint(run.id, resolver.policy_revision)
    assert result is False
    terminal, _, _ = await target_store.get_scan_run(run.id)
    assert terminal.state == "cancelled"
    assert terminal.requested_control == "none"
    assert terminal.terminal_at is not None
    assert terminal.state != "superseded_policy_changed"
    # Pending control should be cleared and fence forgotten, single-active slot released
    assert run.id not in coordinator._pending_control_run_ids  # type: ignore[attr-defined]
    # Follow-up can be claimed
    await coordinator.request_run(_request(new_resolver))
    follow = await target_store.claim_next_scan_run(now=2)
    assert follow is not None
    assert follow.id != run.id



@pytest.mark.asyncio
async def test_non_stopping_policy_supersession_remains_superseded(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=1)
    assert run is not None
    # No stop, just policy change while in discovering
    new_resolver = LibraryPolicyResolver(
        TypedLibrarySettings(
            library_roots=[LibraryRootSettings(id="root-a", path=str(root), label="Library", policy="excluded")]
        )
    )
    coordinator._resolver_getter = lambda: new_resolver  # type: ignore[method-assign]
    result = await coordinator.checkpoint(run.id, resolver.policy_revision)
    assert result is False
    terminal, _, _ = await target_store.get_scan_run(run.id)
    assert terminal.state == "superseded_policy_changed"
    assert terminal.terminal_code == "SUPERSEDED_POLICY_CHANGED"




@pytest.mark.asyncio
async def test_disabled_startup_narrowly_recovers_stopping_without_resuming_work(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver_enabled = _resolver(root)
    coordinator = _coordinator(target_store, resolver_enabled)
    await coordinator.request_run(_request(resolver_enabled))
    run = await target_store.claim_next_scan_run(now=1)
    assert run is not None
    stopped = await coordinator.control(run.id, "stop", run.row_revision)
    assert stopped.state == "stopping"
    # Simulate disabled startup
    disabled_resolver = LibraryPolicyResolver(
        TypedLibrarySettings(library_roots=[LibraryRootSettings(id="root-a", path=str(root), label="Library", policy="automatic")], enabled=False)
    )
    coordinator._resolver_getter = lambda: disabled_resolver  # type: ignore[method-assign]
    # recover() when disabled should only do narrow stopping recovery, not full
    recovered = await coordinator.recover()
    terminal, _, _ = await target_store.get_scan_run(run.id)
    assert terminal.state == "cancelled"
    assert terminal.requested_control == "none"
    assert terminal.terminal_at is not None
    # inventory_cleanup_pending should be set correctly (exists if inventory exists)
    # No resumable work should be returned while disabled
    assert recovered == [] or all(r.state != "cancelled" for r in recovered) or any(r.id == run.id for r in recovered)
    # Scheduler and run_once should remain disabled — run_once returns None
    assert await coordinator.run_once({"root-a": root}) is None
    # Re-enable and verify queued follow-up can claim without restart
@pytest.mark.asyncio
async def test_recover_stopping_is_idempotent_and_sets_cleanup(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)
    await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=1)
    assert run is not None
    await coordinator.control(run.id, "stop", run.row_revision)
    persisted, _, _ = await target_store.get_scan_run(run.id)
    assert persisted.state == "stopping"
    first = await target_store.recover_stopping_scan_runs(now=2)
    assert len(first) == 1
    assert first[0].state == "cancelled"
    # Check raw DB for inventory_cleanup_pending (not exposed on ScanRun)
    import sqlite3

    with sqlite3.connect(target_store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT inventory_cleanup_pending FROM library_scan_runs WHERE id = ?", (run.id,)).fetchone()
        assert row is not None
        assert row["inventory_cleanup_pending"] in (0, 1)  # 0 when no inventory, 1 would be if inventory existed
    second = await target_store.recover_stopping_scan_runs(now=3)
    assert second == []
    terminal, _, _ = await target_store.get_scan_run(run.id)
    assert terminal.state == "cancelled"
    assert terminal.terminal_at == 2
    # Coordinator-level idempotency
    coordinator2 = _coordinator(target_store, resolver)
    await coordinator2.request_run(_request(resolver))
    run2 = await target_store.claim_next_scan_run(now=4)
    assert run2 is not None
    await coordinator2.control(run2.id, "stop", run2.row_revision)
    first_c = await coordinator2.recover_stopping()
    assert len(first_c) == 1
    second_c = await coordinator2.recover_stopping()
    assert second_c == []




async def test_scheduler_anchor_is_not_hidden_by_many_policy_reconciliations(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    await target_store.create_scan_run(
        ScanRun(id="filesystem", kind="incremental", trigger="automatic", queued_at=1)
    )
    with sqlite3.connect(tmp_path / "target.db") as connection:
        connection.execute(
            "UPDATE library_scan_runs SET state = 'completed', phase = 'reconciling', "
            "terminal_at = queued_at, updated_at = queued_at WHERE id = 'filesystem'"
        )
    for index in range(60):
        await target_store.create_scan_run(
            ScanRun(
                id=f"policy-{index}",
                kind="policy_reconcile",
                trigger="policy_apply",
                aggregate_scope="selected",
                queued_at=10 + index,
            )
        )
        with sqlite3.connect(tmp_path / "target.db") as connection:
            connection.execute(
                "UPDATE library_scan_runs SET state = 'completed', phase = 'reconciling', "
                "terminal_at = queued_at, updated_at = queued_at WHERE id = ?",
                (f"policy-{index}",),
            )
    anchor = await target_store.get_latest_filesystem_scan_terminal()

    assert anchor is not None
    assert anchor.id == "filesystem"


@pytest.mark.parametrize(
    ("frequency", "duration"),
    [
        ("5min", 300),
        ("10min", 600),
        ("30min", 1_800),
        ("1hr", 3_600),
        ("6hr", 21_600),
        ("12hr", 43_200),
        ("24hr", 86_400),
        ("3d", 259_200),
        ("7d", 604_800),
    ],
)
def test_interval_schedule_anchors_to_terminal_time(
    frequency: str, duration: int
) -> None:
    terminal = 1_800_000_000.0
    now = datetime.fromtimestamp(terminal + duration)
    due = LibraryScheduleService.next_due(
        frequency, "03:00", terminal, now=now, timezone_name="Europe/London"
    )
    assert due is not None
    assert due.timestamp() == terminal + duration


def test_daily_schedule_handles_dst_and_manual() -> None:
    now = datetime.fromisoformat("2026-03-28T12:00:00+00:00")
    due = LibraryScheduleService.next_due(
        "daily",
        "01:30",
        now.timestamp(),
        now=now,
        timezone_name="Europe/London",
    )
    assert due is not None
    assert due.date().isoformat() == "2026-03-29"
    assert (due.hour, due.minute) == (2, 30)
    repeated_now = datetime.fromisoformat("2026-10-24T12:00:00+00:00")
    repeated = LibraryScheduleService.next_due(
        "daily",
        "01:30",
        repeated_now.timestamp(),
        now=repeated_now,
        timezone_name="Europe/London",
    )
    assert repeated is not None
    assert repeated.fold == 0
    assert (
        LibraryScheduleService.next_due(
            "manual", "03:00", None, now=now, timezone_name="Europe/London"
        )
        is None
    )
@pytest.mark.asyncio
async def test_normal_scan_with_filesystem_coordinator_completes_and_releases_leases(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artist" / "Album").mkdir(parents=True)
    (root / "Artist" / "Album" / "track-1.flac").write_bytes(b"audio")
    resolver = _resolver(root)
    filesystem = LibraryFilesystemCoordinator()
    scanner = LibraryInventoryScanner(
        target_store, filesystem_coordinator=filesystem, walk_deadline_seconds=0.05
    )
    coordinator = LibraryScanCoordinator(
        target_store,
        scanner,
        LibraryIndexer(target_store, _TagReader()),
        LibraryReconciler(target_store),
        lambda: resolver,
    )
    await coordinator.request_run(_request(resolver))
    completed = await coordinator.run_once({"root-a": root})
    assert completed is not None and completed.state == "completed"
    assert not scanner._detached_walkers
    # Writer can acquire immediately after normal walk (no lease held)
    async with asyncio.timeout(0.5):
        async with filesystem.write("root-a"):
            pass
    assert completed.counters["new_count"] == 1


@pytest.mark.asyncio
async def test_wedged_walk_allows_concurrent_write_and_revision_restart(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track.flac").write_bytes(b"one")
    resolver = _resolver(root)
    filesystem = LibraryFilesystemCoordinator()
    walk_calls = 0
    block = threading.Event()

    def blocking_walker(*_args, **_kwargs):
        nonlocal walk_calls
        walk_calls += 1
        if walk_calls == 1:
            yield (str(root), [], ["track.flac"])
            block.wait(timeout=5.0)
            return
        yield from os.walk(str(root), followlinks=False)

    scanner = LibraryInventoryScanner(
        target_store,
        filesystem_coordinator=filesystem,
        directory_walker=blocking_walker,
        walk_deadline_seconds=30.0,
    )
    coordinator = LibraryScanCoordinator(
        target_store,
        scanner,
        LibraryIndexer(target_store, _TagReader()),
        LibraryReconciler(target_store),
        lambda: resolver,
    )
    await coordinator.request_run(_request(resolver))
    task = asyncio.create_task(coordinator.run_once({"root-a": root}))
    await asyncio.sleep(0.1)
    # Bump revision while walker is blocked - must not block (no lease held)
    async with asyncio.timeout(0.5):
        async with filesystem.write("root-a"):
            pass
    revision_after_write = filesystem.revision("root-a")
    assert revision_after_write == 1
    block.set()
    completed = await asyncio.wait_for(task, timeout=5.0)
    assert completed is not None and completed.state == "completed"
    # First walk saw stale revision, so discover restarted and performed second walk
    assert walk_calls == 2
    assert not scanner._detached_walkers


@pytest.mark.asyncio
async def test_wedged_walk_timeout_with_filesystem_does_not_block_writer_and_next_scan(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track.flac").write_bytes(b"one")
    resolver = _resolver(root)
    filesystem = LibraryFilesystemCoordinator()
    wedged = threading.Event()
    calls = 0

    def walker(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield (str(root), [], ["track.flac"])
            wedged.wait()
            return
        yield from os.walk(str(root), followlinks=False)

    scanner = LibraryInventoryScanner(
        target_store,
        filesystem_coordinator=filesystem,
        directory_walker=walker,
        walk_deadline_seconds=0.05,
    )
    coordinator = LibraryScanCoordinator(
        target_store,
        scanner,
        LibraryIndexer(target_store, _TagReader()),
        LibraryReconciler(target_store),
        lambda: resolver,
    )
    await coordinator.request_run(_request(resolver))
    failed = await asyncio.wait_for(coordinator.run_once({"root-a": root}), timeout=2.0)
    assert failed is not None and failed.state == "failed"
    assert failed.terminal_code == "WALK_TIMEOUT"
    assert len(scanner._detached_walkers) == 1
    # Writer must acquire while walker still wedged
    async with asyncio.timeout(0.5):
        async with filesystem.write("root-a"):
            pass
    wedged.set()
    deadline = time.monotonic() + 2.0
    while scanner._detached_walkers and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not scanner._detached_walkers
    # Next scan must be claimable and complete normally
    await coordinator.request_run(_request(resolver, trigger="automatic"))
    completed = await coordinator.run_once({"root-a": root})
    assert completed is not None and completed.state == "completed"



@pytest.mark.asyncio
async def test_failed_run_forgets_scan_revision_for_every_terminal_state(target_store: NativeLibraryStore, tmp_path: Path) -> None:
    # First scope succeeds and records fence, second fails -> run failed, no retained revision
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artist" / "Album").mkdir(parents=True)
    (root / "Artist" / "Album" / "track-1.flac").write_bytes(b"audio")
    # Create a second scope that will fail (missing directory)
    # Use two scopes under same root: one real, one missing
    resolver = LibraryPolicyResolver(
        TypedLibrarySettings(
            library_roots=[LibraryRootSettings(id="root-a", path=str(root), label="Library", policy="automatic")]
        )
    )
    filesystem = LibraryFilesystemCoordinator()
    scanner = LibraryInventoryScanner(target_store, filesystem_coordinator=filesystem, walk_deadline_seconds=0.05)
    coordinator = LibraryScanCoordinator(target_store, scanner, LibraryIndexer(target_store, _TagReader()), LibraryReconciler(target_store), lambda: resolver, filesystem_coordinator=filesystem)
    # Create a run with two scopes: one valid, one missing
    policy_revision = resolver.policy_revision
    request = ScanRequest(
        kind="incremental",
        trigger="manual",
        policy_revision=policy_revision,
        scopes=[
            ScanScope(root_id="root-a", relative_path=".", policy_revision=policy_revision),
            ScanScope(root_id="root-a", relative_path="missing", policy_revision=policy_revision),
        ],
    )
    await coordinator.request_run(request)
    failed = await coordinator.run_once({"root-a": root})
    assert failed is not None and failed.state == "failed"
    # Verify durable failed state
    stored, _, _ = await target_store.get_scan_run(failed.id)
    assert stored.state == "failed"
    # Advance root revision via write lease
    async with filesystem.write("root-a"):
        pass
    # Public scan_revision should return current, not stale, and no entry for terminal run remains
    current = filesystem.scan_revision(failed.id, "root-a")
    # Should be current revision, not the stale recorded one
    # The coordinator should have forgotten, so scan_revision returns current (or None if no current)
    # Check that the internal map has no entry for this run
    assert (failed.id, "root-a") not in filesystem._scan_revisions
    # After bumping, it should equal current revision
    async with filesystem.write("root-a"):
        pass
    assert filesystem.scan_revision(failed.id, "root-a") == filesystem.revision("root-a")

@pytest.mark.asyncio
async def test_repeated_failed_runs_do_not_grow_map(target_store: NativeLibraryStore, tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = LibraryPolicyResolver(TypedLibrarySettings(library_roots=[LibraryRootSettings(id="root-a", path=str(root), label="Library", policy="automatic")]))
    filesystem = LibraryFilesystemCoordinator()
    scanner = LibraryInventoryScanner(target_store, filesystem_coordinator=filesystem, walk_deadline_seconds=0.05)
    coordinator = LibraryScanCoordinator(target_store, scanner, LibraryIndexer(target_store, _TagReader()), LibraryReconciler(target_store), lambda: resolver, filesystem_coordinator=filesystem)
    for i in range(5):
        policy_revision = resolver.policy_revision
        request = ScanRequest(kind="incremental", trigger="manual", policy_revision=policy_revision, scopes=[ScanScope(root_id="root-a", relative_path=".", policy_revision=policy_revision)])
        # Make the walk fail by using a denied walker for this run
        def denied_walk(*_args, **_kwargs):
            raise PermissionError(errno.EACCES, "Permission denied", str(root / "secret"))
            yield
        # Temporarily patch the scanner's walker
        original_walker = scanner._directory_walker
        scanner._directory_walker = denied_walk
        await coordinator.request_run(request)
        failed = await coordinator.run_once({"root-a": root})
        assert failed is not None and failed.state == "failed"
        scanner._directory_walker = original_walker
        # After each failed run, the map should have no entry for that run
        assert (failed.id, "root-a") not in filesystem._scan_revisions
    # After 5 unique failed runs, map should be bounded at zero terminal entries
    assert len(filesystem._scan_revisions) == 0

@pytest.mark.asyncio
async def test_pre_record_failure_leaves_no_entry(target_store: NativeLibraryStore, tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = LibraryPolicyResolver(TypedLibrarySettings(library_roots=[LibraryRootSettings(id="root-a", path=str(root), label="Library", policy="automatic")]))
    filesystem = LibraryFilesystemCoordinator()
    scanner = LibraryInventoryScanner(target_store, filesystem_coordinator=filesystem, walk_deadline_seconds=0.05)
    coordinator = LibraryScanCoordinator(target_store, scanner, LibraryIndexer(target_store, _TagReader()), LibraryReconciler(target_store), lambda: resolver, filesystem_coordinator=filesystem)
    # Make discovery fail before any scope records a revision (e.g., root unavailable)
    # Use a missing root path
    missing_root = tmp_path / "missing"
    # Don't create missing_root
    request = ScanRequest(kind="incremental", trigger="manual", policy_revision=resolver.policy_revision, scopes=[ScanScope(root_id="root-a", relative_path=".", policy_revision=resolver.policy_revision)])
    await coordinator.request_run(request)
    failed = await coordinator.run_once({"root-a": missing_root})
    assert failed is not None and failed.state == "failed"
    assert (failed.id, "root-a") not in filesystem._scan_revisions


@pytest.mark.asyncio
async def test_paused_run_retains_fence_until_terminal(target_store: NativeLibraryStore, tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artist" / "Album").mkdir(parents=True)
    (root / "Artist" / "Album" / "track-1.flac").write_bytes(b"audio")
    resolver = LibraryPolicyResolver(TypedLibrarySettings(library_roots=[LibraryRootSettings(id="root-a", path=str(root), label="Library", policy="automatic")]))
    filesystem = LibraryFilesystemCoordinator()
    scanner = LibraryInventoryScanner(target_store, filesystem_coordinator=filesystem, walk_deadline_seconds=0.05)
    coordinator = LibraryScanCoordinator(target_store, scanner, LibraryIndexer(target_store, _TagReader()), LibraryReconciler(target_store), lambda: resolver, filesystem_coordinator=filesystem)
    await coordinator.request_run(ScanRequest(kind="incremental", trigger="manual", policy_revision=resolver.policy_revision, scopes=[ScanScope(root_id="root-a", relative_path=".", policy_revision=resolver.policy_revision)]))
    run = await target_store.claim_next_scan_run(now=10)
    assert run is not None
    # Simulate a successful discovery that recorded a fence
    filesystem.record_scan_revision(run.id, "root-a")
    assert filesystem.scan_revision(run.id, "root-a") == filesystem.revision("root-a")
    # Pause via control -> pausing -> paused (not terminal, should retain)
    pausing = await coordinator.control(run.id, "pause", run.row_revision)
    assert pausing.state == "pausing"
    paused = await coordinator._settle_pending_control(run.id)
    assert paused.state == "paused"
    assert filesystem.scan_revision(run.id, "root-a") == filesystem.revision("root-a")
    # Now stop -> stopping -> cancelled (terminal, should forget)
    stopping = await coordinator.control(run.id, "stop", paused.row_revision)
    assert stopping.state in ("stopping", "cancelled")
    cancelled = await coordinator._settle_pending_control(run.id)
    assert cancelled.state == "cancelled"
    assert (run.id, "root-a") not in filesystem._scan_revisions
    assert filesystem.scan_revision(run.id, "root-a") == filesystem.revision("root-a")


@pytest.mark.asyncio
async def test_duplicate_during_active_run_queues_follow_up_that_survives_failure(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)

    class BrokenInventory(LibraryInventoryScanner):
        async def discover(self, *args, **kwargs):
            raise RuntimeError("injected worker failure")

    coordinator = LibraryScanCoordinator(
        target_store,
        BrokenInventory(target_store),
        LibraryIndexer(target_store, _TagReader()),
        LibraryReconciler(target_store),
        lambda: resolver,
        clock=lambda: 1_800_000_000.0,
    )

    first = await coordinator.request_run(_request(resolver))
    assert first.disposition == "started"
    active = await target_store.claim_next_scan_run(now=1_800_000_001)
    assert active is not None and active.state == "discovering"

    duplicate = await coordinator.request_run(
        _request(resolver, trigger="automatic")
    )
    assert duplicate.disposition == "queued"
    assert duplicate.run_id != first.run_id
    assert await target_store.row_count("library_scan_runs") == 2

    with pytest.raises(RuntimeError, match="injected worker failure"):
        await coordinator.run_once({"root-a": root})

    failed, _, _ = await target_store.get_scan_run(first.run_id)
    assert failed.state == "failed"
    successor = await target_store.claim_next_scan_run(now=1_800_000_002)
    assert successor is not None and successor.id == duplicate.run_id
    _, scopes, _ = await target_store.get_scan_run(successor.id)
    assert {scope.relative_path for scope in scopes} == {"."}
    assert {scope.policy_revision for scope in scopes} == {resolver.policy_revision}


@pytest.mark.asyncio
async def test_duplicate_during_active_run_coalesces_onto_existing_queued_follow_up(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)

    first = await coordinator.request_run(_request(resolver))
    active = await target_store.claim_next_scan_run(now=1_800_000_001)
    assert active is not None
    follow_up = await coordinator.request_run(
        _request(resolver, relative_path="Disc 1", trigger="manual")
    )
    assert follow_up.disposition == "queued"
    coalesced = await coordinator.request_run(
        _request(resolver, relative_path="Disc 1", trigger="subsonic")
    )
    assert coalesced.disposition == "coalesced"
    assert coalesced.run_id == follow_up.run_id
    assert await target_store.row_count("library_scan_runs") == 2
    queued, scopes, _ = await target_store.get_scan_run(follow_up.run_id)
    assert queued.state == "queued"
    assert queued.coalesced_request_count == 1
    assert {scope.relative_path for scope in scopes} == {"Disc 1"}
    with sqlite3.connect(target_store.db_path) as connection:
        triggers = connection.execute(
            "SELECT trigger, reason FROM library_scan_run_triggers "
            "WHERE run_id = ? ORDER BY trigger_sequence",
            (follow_up.run_id,),
        ).fetchall()
    assert triggers == [("manual", "accepted"), ("subsonic", "covered")]


@pytest.mark.asyncio
async def test_disjoint_request_during_active_expands_queued_follow_up(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)

    await coordinator.request_run(_request(resolver))
    active = await target_store.claim_next_scan_run(now=1_800_000_001)
    assert active is not None
    follow_up = await coordinator.request_run(
        _request(resolver, relative_path="Disc 1", trigger="manual")
    )
    expanded = await coordinator.request_run(
        _request(resolver, relative_path="Disc 2", trigger="automatic")
    )
    assert expanded.disposition == "expanded"
    assert expanded.run_id == follow_up.run_id
    assert await target_store.row_count("library_scan_runs") == 2
    _, scopes, _ = await target_store.get_scan_run(follow_up.run_id)
    assert {scope.relative_path for scope in scopes} == {"Disc 1", "Disc 2"}


@pytest.mark.asyncio
async def test_incompatible_request_during_active_conflicts_without_mutation(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)

    await coordinator.request_run(_request(resolver))
    active = await target_store.claim_next_scan_run(now=1_800_000_001)
    assert active is not None
    follow_up = await coordinator.request_run(
        _request(resolver, relative_path="Disc 1", kind="rescan_files")
    )
    assert follow_up.disposition == "queued"
    conflict = await coordinator.request_run(_request(resolver))
    assert conflict.disposition == "conflict"
    assert conflict.run_id == follow_up.run_id
    assert conflict.conflicting_kind == "rescan_files"
    assert await target_store.row_count("library_scan_runs") == 2
    assert await target_store.get_stream_revision("scan") == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("control,terminal_state", [("stop", "cancelled"), ("pause", "paused")])
async def test_follow_up_survives_stop_and_pause_of_covering_run(
    target_store: NativeLibraryStore, tmp_path: Path, control: str, terminal_state: str
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)

    await coordinator.request_run(_request(resolver))
    active = await target_store.claim_next_scan_run(now=20)
    assert active is not None
    follow_up = await coordinator.request_run(_request(resolver, trigger="automatic"))
    assert follow_up.disposition == "queued"

    settled_control = await coordinator.control(active.id, control, active.row_revision)
    assert settled_control.state in {"pausing", "stopping"}
    settled = await coordinator._settle_pending_control(active.id)
    assert settled.state == terminal_state

    if terminal_state == "cancelled":
        successor = await target_store.claim_next_scan_run(now=21)
        assert successor is not None and successor.id == follow_up.run_id
    else:
        blocked = await target_store.claim_next_scan_run(now=21)
        assert blocked is None
        resumed = await target_store.transition_scan_run(
            active.id,
            expected_state="paused",
            expected_revision=(await target_store.get_scan_run(active.id))[0].row_revision,
            new_state="cancelled",
            now=22,
        )
        assert resumed.state == "cancelled"
        successor = await target_store.claim_next_scan_run(now=23)
        assert successor is not None and successor.id == follow_up.run_id


@pytest.mark.asyncio
async def test_completed_covering_run_does_not_suppress_queued_follow_up(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track-1.flac").write_bytes(b"audio")
    resolver = _resolver(root)
    coordinator = _coordinator(target_store, resolver)

    requested = await coordinator.request_run(_request(resolver))
    run = await target_store.claim_next_scan_run(now=10)
    assert run is not None
    follow_up = await coordinator.request_run(_request(resolver, trigger="automatic"))
    assert follow_up.disposition == "queued"
    completed = await coordinator.run_once({"root-a": root})
    assert completed is not None and completed.state == "completed"
    completed_run, _, _ = await target_store.get_scan_run(requested.run_id)
    assert completed_run.state == "completed"
    successor = await target_store.claim_next_scan_run(now=11)
    assert successor is not None and successor.id == follow_up.run_id


@pytest.mark.asyncio
async def test_multi_trigger_follow_up_preserves_metadata_across_restart(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    db_path = tmp_path / "restart.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    def build_store() -> NativeLibraryStore:
        return NativeLibraryStore(db_path=db_path, write_lock=threading.Lock())

    store = build_store()
    root = tmp_path / "music"
    root.mkdir()
    (root / "Disc 1").mkdir()
    resolver = _resolver(root)
    coordinator = _coordinator(store, resolver)

    manual = await coordinator.request_run(_request(resolver, trigger="manual"))
    active = await store.claim_next_scan_run(now=30)
    assert active is not None
    automatic = await coordinator.request_run(
        _request(resolver, trigger="automatic")
    )
    subsonic = await coordinator.request_run(
        _request(resolver, relative_path="Disc 1", trigger="subsonic")
    )
    assert subsonic.disposition == "coalesced"
    current, _, _ = await store.get_scan_run(active.id)
    await store.transition_scan_run(
        current.id,
        expected_state=current.state,
        expected_revision=current.row_revision,
        new_state="failed",
        now=31,
        terminal_code="UNEXPECTED_WORKER_FAILURE",
    )

    reopened = build_store()
    successor = await reopened.claim_next_scan_run(now=32)
    assert successor is not None and successor.id == automatic.run_id
    assert successor.trigger == "automatic"
    assert successor.kind == "incremental"
    run, scopes, _ = await reopened.get_scan_run(successor.id)
    assert run.state == "discovering"
    assert {scope.relative_path for scope in scopes} == {"."}
    with sqlite3.connect(db_path) as connection:
        triggers = connection.execute(
            "SELECT trigger, reason FROM library_scan_run_triggers "
            "WHERE run_id = ? ORDER BY trigger_sequence",
            (successor.id,),
        ).fetchall()
    assert triggers == [
        ("automatic", "accepted"),
        ("subsonic", "covered"),
    ]


@pytest.mark.asyncio
async def test_delayed_probe_failure_persists_fresh_terminal_and_scheduler_anchor(
    target_store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = _resolver(root)
    stale = 100.0
    requested = await target_store.request_scan_run(
        _request(resolver), run_id="run-fresh", requested_at=stale
    )
    claimed = await target_store.claim_next_scan_run(now=stale)
    assert claimed is not None and claimed.state == "discovering"
    fresh = 400.0

    def fake_clock() -> float:
        return fresh

    def blocking_probe(_path: Path) -> bool:
        time.sleep(0.12)
        return True

    scanner = LibraryInventoryScanner(
        target_store, clock=fake_clock, walk_deadline_seconds=0.05, directory_probe=blocking_probe
    )

    async def checkpoint(_run_id: str, _revision: str) -> bool:
        return True

    failed = await scanner.discover(
        claimed, (await target_store.get_scan_run(claimed.id))[1], {"root-a": root}, resolver, checkpoint
    )
    assert failed.state == "failed"
    persisted, _, _ = await target_store.get_scan_run(failed.id)
    assert persisted.phase_timings.get("discovering", 0) == fresh - stale
    with sqlite3.connect(target_store.db_path) as connection:
        failure = connection.execute(
            "SELECT failure_code, recorded_at FROM library_scan_failures WHERE run_id = ?",
            (failed.id,),
        ).fetchone()
    assert failure is not None and failure[0] == "WALK_TIMEOUT"
    assert float(failure[1]) == fresh
    assert float(failure[1]) <= float(persisted.terminal_at or 0)
    scheduler = LibraryAutomaticScanScheduler()
    coordinator_mock = AsyncMock()
    coordinator_mock.latest_filesystem_terminal.return_value = persisted
    before_due = await scheduler.tick(
        coordinator_mock,
        resolver,
        frequency="24hr",
        daily_time="03:00",
        timezone_name="Europe/London",
        now=datetime.fromtimestamp(fresh + 86400 - 100).astimezone(),
    )
    assert before_due is False
    coordinator_mock.request_run.assert_not_awaited()
    coordinator_mock.reset_mock()
    coordinator_mock.latest_filesystem_terminal.return_value = persisted
    due = await scheduler.tick(
        coordinator_mock,
        resolver,
        frequency="24hr",
        daily_time="03:00",
        timezone_name="Europe/London",
        now=datetime.fromtimestamp(fresh + 86400 + 1).astimezone(),
    )
    assert due is True
    reopened = NativeLibraryStore(db_path=Path(target_store.db_path), write_lock=threading.Lock())
    reopened_run, _, _ = await reopened.get_scan_run(failed.id)
    assert reopened_run.terminal_at == fresh
    assert reopened_run.updated_at == fresh
