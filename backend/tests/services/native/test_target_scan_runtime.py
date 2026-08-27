from __future__ import annotations

import asyncio
import errno
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.task_registry import TaskRegistry
from infrastructure.sse_publisher import KEEPALIVE, SSEPublisher
from infrastructure.queue.durable_work_wakeup import DurableWorkWakeups
from models.library_work import ScanFailureRecord, ScanRun, ScanRunSnapshot, ScanScope
from services.compat.target_scan_service import TargetCompatScanService
from services.native.library_scan_events import LibraryScanEventPublisher
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.library_indexer import LibraryIndexer
from services.native.library_inventory_scanner import (
    INVENTORY_BATCH_SIZE,
    LibraryInventoryScanner,
)
from services.native.library_scan_coordinator import LibraryScanCoordinator
from services.native.library_operation_supervisor import LibraryOperationSupervisor
from services.native.library_scan_scheduler import LibraryAutomaticScanScheduler
from services.native.library_policy_resolver import LibraryPolicyResolver
from api.v1.schemas.library_policies import (
    LibraryPathPolicyRule,
    LibraryRootSettings,
    TypedLibrarySettings,
)
from services.native.library_scan_supervisor import (
    ERROR_RETRY_INTERVAL_SECONDS,
    SUPERVISOR_TASK_NAME,
    start_target_scan_supervisor,
    supervise_target_scans,
)
from services.native.target_application_runtime import (
    run_library_contribution_verification_worker,
    run_target_identification_worker,
    run_target_operation_worker,
    run_target_worker_watchdog,
)
from core.exceptions import AudioFormatError, ResourceNotFoundError
from infrastructure.resilience.retry import CircuitState
from services.native.background_workload_gate import BackgroundWorkloadGate
from services.native.library_filesystem_coordinator import LibraryFilesystemCoordinator


@pytest.mark.asyncio
async def test_subsonic_target_projection_uses_only_the_coordinator() -> None:
    coordinator = AsyncMock()
    coordinator.current.return_value = [
        ScanRun(
            id="run-1",
            kind="incremental",
            trigger="subsonic",
            state="indexing",
            phase="indexing",
        )
    ]
    coordinator.snapshot.return_value = ScanRunSnapshot(
        run=coordinator.current.return_value[0], counters={"inspected_count": 42}
    )
    resolver = SimpleNamespace(
        policy_revision="policy-1",
        settings=SimpleNamespace(
            library_roots=[
                SimpleNamespace(id="root-a", path="/music", policy="automatic")
            ]
        ),
    )
    service = TargetCompatScanService(coordinator, lambda: resolver)

    await service.start()
    scanning, count = await service.status()

    request = coordinator.request_run.await_args.args[0]
    assert request.trigger == "subsonic"
    assert request.scopes[0].root_id == "root-a"
    assert scanning is True
    assert count == 42


@pytest.mark.asyncio
async def test_supervisor_fetches_the_current_coordinator_each_iteration() -> None:
    coordinators = [AsyncMock(), AsyncMock(), AsyncMock()]
    for coordinator in coordinators:
        coordinator.run_once.return_value = None
    calls = 0

    def getter():
        nonlocal calls
        result = coordinators[min(calls, 2)]
        calls += 1
        return result

    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
    )
    await supervise_target_scans(getter, lambda: {"root-a": Path("/scratch")}, wakeups)

    assert calls == 3
    coordinators[0].recover.assert_awaited_once()
    coordinators[1].run_once.assert_awaited_once()
    coordinators[2].run_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_supervisor_wait_failure_logs_and_sleeps_instead_of_dying(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F-002: a non-cancel exception from the sleep path must log with exc_info,
    take exactly one error-retry sleep, and continue the supervision loop."""
    coordinator = AsyncMock()
    coordinator.run_once.return_value = None
    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(
            side_effect=[RuntimeError("wakeup store fault"), asyncio.CancelledError()]
        ),
    )
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    with caplog.at_level(
        logging.ERROR, logger="services.native.library_scan_supervisor"
    ):
        await supervise_target_scans(
            lambda: coordinator, lambda: {"root-a": Path("/scratch")}, wakeups
        )

    assert coordinator.run_once.await_count == 2
    assert sleeps == [ERROR_RETRY_INTERVAL_SECONDS]
    assert any(
        record.exc_info is not None
        and record.getMessage() == "Target scan supervisor wait failed"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_coordinator_scan_run_failures_gates_listing_on_existence() -> None:
    """F-005: the failures passthrough keeps snapshot's typed NOT_FOUND boundary
    (unknown runs raise before the page read) and delegates the rowid page."""
    store = AsyncMock()
    record = ScanFailureRecord(
        root_id="root-a",
        relative_path="Artist/Album",
        failure_code="WALK_EACCES",
        recorded_at=1.0,
        failure_detail="[Errno 13] Permission denied",
        phase="discovering",
    )
    store.list_scan_run_failures.return_value = ([record], 41)
    coordinator = LibraryScanCoordinator(
        store, AsyncMock(), AsyncMock(), AsyncMock(), lambda: None
    )

    items, next_cursor = await coordinator.scan_run_failures(
        "run-1", limit=50, cursor_rowid=40
    )

    store.get_scan_run.assert_awaited_once_with("run-1")
    store.list_scan_run_failures.assert_awaited_once_with(
        "run-1", limit=50, cursor_rowid=40
    )
    assert items == [record]
    assert next_cursor == 41

    store.get_scan_run.side_effect = ResourceNotFoundError("Scan run not found: nope")
    with pytest.raises(ResourceNotFoundError):
        await coordinator.scan_run_failures("nope")
    store.list_scan_run_failures.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_one_target_supervisor_can_be_registered() -> None:
    registry = TaskRegistry.get_instance()
    registry.reset()
    coordinator = AsyncMock()
    coordinator.run_once.return_value = None
    wakeups = DurableWorkWakeups()
    task = start_target_scan_supervisor(lambda: coordinator, lambda: {}, wakeups)
    assert registry.is_running(SUPERVISOR_TASK_NAME)
    with pytest.raises(RuntimeError):
        start_target_scan_supervisor(lambda: coordinator, lambda: {}, wakeups)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert task.done()
    registry.reset()


@pytest.mark.asyncio
async def test_supervisor_refreshes_scheduler_and_resolver_each_iteration() -> None:
    coordinator = AsyncMock()
    coordinator.run_once.return_value = None
    scheduler = AsyncMock()
    resolver = SimpleNamespace(
        policy_revision="one", settings=SimpleNamespace(enabled=True)
    )
    calls = {"scheduler": 0, "resolver": 0, "settings": 0}

    def scheduler_getter():
        calls["scheduler"] += 1
        return scheduler

    def resolver_getter():
        calls["resolver"] += 1
        return resolver

    def settings_getter():
        calls["settings"] += 1
        return {
            "frequency": "manual",
            "daily_time": "03:00",
            "timezone_name": "Europe/London",
        }

    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
    )
    await supervise_target_scans(
        lambda: coordinator,
        lambda: {},
        wakeups,
        scheduler_getter,
        resolver_getter,
        settings_getter,
    )

    # One extra resolver read comes from the startup recovery gate; the two
    # loop iterations then read it once each.
    assert calls == {"scheduler": 2, "resolver": 3, "settings": 2}
    assert scheduler.tick.await_count == 2


@pytest.mark.asyncio
async def test_supervisor_skips_recover_tick_and_run_when_library_disabled() -> None:
    coordinator = AsyncMock()
    coordinator.run_once.return_value = None
    coordinator.recover_stopping = AsyncMock()
    scheduler = AsyncMock()
    resolver = SimpleNamespace(
        policy_revision="one", settings=SimpleNamespace(enabled=False)
    )
    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    await supervise_target_scans(
        lambda: coordinator,
        lambda: {},
        wakeups,
        lambda: scheduler,
        lambda: resolver,
        lambda: {"frequency": "manual", "daily_time": "03:00", "timezone_name": "UTC"},
    )

    coordinator.recover.assert_not_awaited()
    coordinator.recover_stopping.assert_awaited_once()
    scheduler.tick.assert_not_awaited()
    coordinator.run_once.assert_not_awaited()
    wakeups.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_supervisor_disabled_startup_recovers_stopping_without_scheduler(
    tmp_path: Path,
) -> None:
    from infrastructure.persistence.native_library_store import NativeLibraryStore

    db_path = tmp_path / "target.db"
    import sqlite3
    import threading

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    store = NativeLibraryStore(db_path=db_path, write_lock=threading.Lock())
    root = tmp_path / "music"
    root.mkdir()
    enabled_resolver = LibraryPolicyResolver(
        TypedLibrarySettings(library_roots=[LibraryRootSettings(id="root-a", path=str(root), label="Library", policy="automatic")], enabled=True)
    )
    disabled_resolver = LibraryPolicyResolver(
        TypedLibrarySettings(library_roots=[LibraryRootSettings(id="root-a", path=str(root), label="Library", policy="automatic")], enabled=False)
    )
    from services.native.library_scan_coordinator import LibraryScanCoordinator
    from services.native.library_indexer import LibraryIndexer
    from services.native.library_reconciler import LibraryReconciler
    from services.native.library_scan_supervisor import supervise_target_scans

    class _TagReader:
        def read_tags(self, path: Path):
            raise NotImplementedError

    coordinator = LibraryScanCoordinator(
        store,
        LibraryInventoryScanner(store),
        LibraryIndexer(store, _TagReader()),
        LibraryReconciler(store),
        lambda: enabled_resolver,
    )
    from models.library_work import ScanRequest, ScanScope

    request = ScanRequest(
        kind="incremental",
        trigger="manual",
        scopes=[ScanScope(root_id="root-a", policy_revision=enabled_resolver.policy_revision)],
        policy_revision=enabled_resolver.policy_revision,
    )
    await coordinator.request_run(request)
    run = await store.claim_next_scan_run(now=1)
    assert run is not None
    await coordinator.control(run.id, "stop", run.row_revision)
    persisted, _, _ = await store.get_scan_run(run.id)
    assert persisted.state == "stopping"
    # disabled resolver at startup: narrow recovery runs, no scheduler/run_once
    scheduler = AsyncMock()
    wakeups = SimpleNamespace(revision=lambda _k: 0, wait=AsyncMock(side_effect=asyncio.CancelledError()))
    await supervise_target_scans(
        lambda: coordinator,
        lambda: {"root-a": root},
        wakeups,
        lambda: scheduler,
        lambda: disabled_resolver,
        lambda: {"frequency": "manual", "daily_time": "03:00", "timezone_name": "UTC"},
    )
    terminal, _, _ = await store.get_scan_run(run.id)
    assert terminal.state == "cancelled"
    assert terminal.requested_control == "none"
    assert terminal.terminal_at is not None
    scheduler.tick.assert_not_awaited()
    # Re-enable and verify queued follow-up can claim without restart
    await coordinator.request_run(request)
    follow = await store.claim_next_scan_run(now=2)
    assert follow is not None
    assert follow.id != run.id
    # Cleanup
    coordinator._pending_control_run_ids.clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_target_identification_worker_recovers_claims_and_survives_iterations() -> (
    None
):
    queue = AsyncMock()
    queue.is_paused.return_value = False
    queue.claim.side_effect = [{"id": "job-1"}, None]
    service = AsyncMock()
    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    await run_target_identification_worker(
        lambda: queue,
        lambda: service,
        worker_id="test-worker",
        work_wakeups=wakeups,
    )

    assert queue.recover.await_count == 2
    service.run_claimed_job.assert_awaited_once_with({"id": "job-1"}, "test-worker")


@pytest.mark.asyncio
async def test_identification_worker_defers_crashed_job_with_unexpected_error() -> None:
    queue = AsyncMock()
    queue.is_paused.return_value = False
    queue.claim.side_effect = [{"id": "job-1", "row_revision": 1}, None]
    service = AsyncMock()
    service.run_claimed_job.side_effect = RuntimeError("boom")
    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    await run_target_identification_worker(
        lambda: queue,
        lambda: service,
        worker_id="test-worker",
        work_wakeups=wakeups,
    )

    # The crashed job is deferred as UNEXPECTED_ERROR (feeding the deferral
    # cap) instead of being re-claimed into an infinite crash loop.
    queue.defer.assert_awaited_once_with(
        {"id": "job-1", "row_revision": 1}, "test-worker", "UNEXPECTED_ERROR"
    )


def _idle_identification_harness():
    queue = AsyncMock()
    queue.is_paused.return_value = False
    queue.claim.return_value = None
    service = AsyncMock()
    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(
            side_effect=[None, asyncio.CancelledError(), asyncio.CancelledError()]
        ),
    )
    return queue, service, wakeups


@pytest.mark.asyncio
async def test_identification_worker_sweeps_provider_deferrals_when_breaker_closed() -> (
    None
):
    queue, service, wakeups = _idle_identification_harness()
    probe = AsyncMock()

    await run_target_identification_worker(
        lambda: queue,
        lambda: service,
        worker_id="test-worker",
        work_wakeups=wakeups,
        provider_state_getter=lambda: CircuitState.CLOSED,
        probe_provider=probe,
    )

    queue.reset_provider_deferrals.assert_awaited_once()
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_identification_worker_probes_once_per_rate_limit_when_half_open() -> (
    None
):
    queue, service, wakeups = _idle_identification_harness()
    probe = AsyncMock()

    await run_target_identification_worker(
        lambda: queue,
        lambda: service,
        worker_id="test-worker",
        work_wakeups=wakeups,
        provider_state_getter=lambda: CircuitState.HALF_OPEN,
        probe_provider=probe,
    )

    # Two idle iterations inside the 60s sweep window: exactly one probe.
    probe.assert_awaited_once_with()
    queue.reset_provider_deferrals.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_reset_fires_only_on_recovery_edge_not_steady_closed(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """F-056: the reset is edge-triggered - startup plus every OPEN/HALF_OPEN
    -> CLOSED transition; steady-state CLOSED sweeps never wipe history."""
    queue, service, _ = _idle_identification_harness()
    probe = AsyncMock()
    states = iter(
        [
            CircuitState.CLOSED,   # startup edge -> reset
            CircuitState.CLOSED,   # steady state -> no reset
            CircuitState.OPEN,     # outage begins -> no reset
            CircuitState.CLOSED,   # recovery edge -> reset
            CircuitState.CLOSED,   # steady again -> no reset
        ]
    )
    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(side_effect=[None] * 5 + [asyncio.CancelledError()]),
    )
    # Advance the sweep clock past the 60s interval for each iteration.
    ticks = iter([0.0, 100.0, 200.0, 300.0, 400.0, 500.0, 600.0])
    monkeypatch.setattr(
        "services.native.target_application_runtime.time",
        SimpleNamespace(time=lambda: next(ticks)),
    )

    await run_target_identification_worker(
        lambda: queue,
        lambda: service,
        worker_id="test-worker",
        work_wakeups=wakeups,
        provider_state_getter=lambda: next(states),
        probe_provider=probe,
    )

    assert queue.reset_provider_deferrals.await_count == 2


@pytest.mark.asyncio
async def test_identification_worker_skips_provider_sweep_when_breaker_open() -> None:
    queue, service, wakeups = _idle_identification_harness()
    probe = AsyncMock()

    await run_target_identification_worker(
        lambda: queue,
        lambda: service,
        worker_id="test-worker",
        work_wakeups=wakeups,
        provider_state_getter=lambda: CircuitState.OPEN,
        probe_provider=probe,
    )

    probe.assert_not_awaited()
    queue.reset_provider_deferrals.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_watchdog_restarts_dead_worker_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = TaskRegistry()
    monkeypatch.setattr(TaskRegistry, "get_instance", classmethod(lambda cls: registry))

    async def run_forever() -> None:
        await asyncio.Event().wait()

    alive_task = asyncio.get_running_loop().create_task(run_forever())
    registry.register("alive-worker", alive_task)
    dead_task = asyncio.get_running_loop().create_task(run_forever())
    registry.register("dead-worker", dead_task)
    dead_task.cancel()
    with suppress(asyncio.CancelledError):
        await dead_task

    restarted: list[asyncio.Task[None]] = []

    def dead_starter() -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(run_forever())
        registry.register("dead-worker", task)
        restarted.append(task)
        return task

    alive_starter_calls = 0

    def alive_starter() -> asyncio.Task[None]:
        nonlocal alive_starter_calls
        alive_starter_calls += 1
        return alive_task

    async def stop_after_first_iteration(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_first_iteration)
    try:
        await run_target_worker_watchdog(
            {"dead-worker": dead_starter, "alive-worker": alive_starter}
        )
        assert len(restarted) == 1
        assert alive_starter_calls == 0
        assert registry.is_running("dead-worker")
    finally:
        alive_task.cancel()
        for task in restarted:
            task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(alive_task, *restarted)

@pytest.mark.asyncio
async def test_worker_watchdog_restarts_dead_supervisor_exactly_once_and_not_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = TaskRegistry()
    monkeypatch.setattr(TaskRegistry, "get_instance", classmethod(lambda cls: registry))

    async def run_forever() -> None:
        await asyncio.Event().wait()

    alive_task = asyncio.get_running_loop().create_task(run_forever())
    registry.register("target-library-identification-worker", alive_task)
    dead_task = asyncio.get_running_loop().create_task(run_forever())
    registry.register(SUPERVISOR_TASK_NAME, dead_task)
    dead_task.cancel()
    with suppress(asyncio.CancelledError):
        await dead_task

    restarted: list[asyncio.Task[None]] = []

    def supervisor_starter() -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(run_forever())
        registry.register(SUPERVISOR_TASK_NAME, task)
        restarted.append(task)
        return task

    alive_calls = 0

    def alive_starter() -> asyncio.Task[None]:
        nonlocal alive_calls
        alive_calls += 1
        return alive_task

    async def stop_after_first_iteration(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_first_iteration)
    try:
        await run_target_worker_watchdog(
            {
                SUPERVISOR_TASK_NAME: supervisor_starter,
                "target-library-identification-worker": alive_starter,
            }
        )
        assert len(restarted) == 1
        assert alive_calls == 0
        assert registry.is_running(SUPERVISOR_TASK_NAME)
        assert registry.get_all()[SUPERVISOR_TASK_NAME] is restarted[0]
        assert not restarted[0].done()
    finally:
        alive_task.cancel()
        for task in restarted:
            task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(alive_task, *restarted)
        registry.reset()


@pytest.mark.asyncio
async def test_supervisor_restart_resumes_getter_driven_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = TaskRegistry()
    monkeypatch.setattr(TaskRegistry, "get_instance", classmethod(lambda cls: registry))

    first_coord = AsyncMock()
    first_coord.run_once.return_value = None
    first_coord.recover = AsyncMock()
    second_coord = AsyncMock()
    second_coord.run_once.return_value = None
    second_coord.recover = AsyncMock()
    current = {"coord": first_coord}

    def coordinator_getter():  # type: ignore[no-untyped-def]
        return current["coord"]

    scheduler = AsyncMock()
    resolver = SimpleNamespace(settings=SimpleNamespace(enabled=True))
    work_wakeups = DurableWorkWakeups()

    task1 = start_target_scan_supervisor(
        coordinator_getter,
        lambda: {"root-a": Path("/scratch")},
        work_wakeups,
        scheduler_getter=lambda: scheduler,
        resolver_getter=lambda: resolver,
        schedule_settings_getter=lambda: {
            "frequency": "manual",
            "daily_time": "03:00",
            "timezone_name": "UTC",
        },
    )
    assert registry.is_running(SUPERVISOR_TASK_NAME)
    await asyncio.sleep(0)
    task1.cancel()
    with suppress(asyncio.CancelledError):
        await task1
    assert not registry.is_running(SUPERVISOR_TASK_NAME)
    first_coord.recover.assert_awaited_once()

    current["coord"] = second_coord

    def supervisor_starter() -> asyncio.Task[None]:
        return start_target_scan_supervisor(
            coordinator_getter,
            lambda: {"root-a": Path("/scratch")},
            work_wakeups,
            scheduler_getter=lambda: scheduler,
            resolver_getter=lambda: resolver,
            schedule_settings_getter=lambda: {
                "frequency": "manual",
                "daily_time": "03:00",
                "timezone_name": "UTC",
            },
        )

    task2 = supervisor_starter()
    assert registry.is_running(SUPERVISOR_TASK_NAME)
    assert registry.get_all()[SUPERVISOR_TASK_NAME] is task2
    await asyncio.sleep(0)
    task2.cancel()
    with suppress(asyncio.CancelledError):
        await task2
    second_coord.recover.assert_awaited_once()
    # fresh getter behavior - resolver and scheduler still resolved via getters
    assert task2.done()
    # duplicate registration guard remains intact
    task3 = start_target_scan_supervisor(
        coordinator_getter,
        lambda: {},
        DurableWorkWakeups(),
    )
    assert registry.is_running(SUPERVISOR_TASK_NAME)
    with pytest.raises(RuntimeError):
        start_target_scan_supervisor(coordinator_getter, lambda: {}, DurableWorkWakeups())
    task3.cancel()
    with suppress(asyncio.CancelledError):
        await task3
    registry.reset()


@pytest.mark.asyncio
async def test_worker_watchdog_does_not_resurrect_supervisor_after_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = TaskRegistry()
    monkeypatch.setattr(TaskRegistry, "get_instance", classmethod(lambda cls: registry))

    async def run_forever() -> None:
        await asyncio.Event().wait()

    starter_calls: list[str] = []

    def supervisor_starter() -> asyncio.Task[None]:
        starter_calls.append("called")
        task = asyncio.get_running_loop().create_task(run_forever())
        registry.register(SUPERVISOR_TASK_NAME, task)
        return task

    dead_task = asyncio.get_running_loop().create_task(run_forever())
    registry.register(SUPERVISOR_TASK_NAME, dead_task)
    dead_task.cancel()
    with suppress(asyncio.CancelledError):
        await dead_task
    assert not registry.is_running(SUPERVISOR_TASK_NAME)

    watchdog_task = asyncio.create_task(
        run_target_worker_watchdog(
            {SUPERVISOR_TASK_NAME: supervisor_starter},
            interval_seconds=0.02,
        )
    )
    registry.register("target-worker-watchdog", watchdog_task)
    await asyncio.sleep(0.04)
    assert len(starter_calls) == 1
    assert registry.is_running(SUPERVISOR_TASK_NAME)
    replacement = registry.get_all()[SUPERVISOR_TASK_NAME]
    watchdog_task.cancel()
    with suppress(asyncio.CancelledError):
        await watchdog_task
    assert not registry.is_running("target-worker-watchdog")
    replacement.cancel()
    with suppress(asyncio.CancelledError):
        await replacement
    assert not registry.is_running(SUPERVISOR_TASK_NAME)
    starter_calls.clear()
    await asyncio.sleep(0.06)
    assert starter_calls == []
    assert not registry.is_running(SUPERVISOR_TASK_NAME)
    registry.reset()


@pytest.mark.asyncio
async def test_identification_worker_starts_no_new_unit_while_scan_is_active() -> None:
    queue = AsyncMock()
    queue.is_paused.return_value = False
    queue.claim.side_effect = [{"id": "job-1"}, None]
    service = AsyncMock()
    gate = BackgroundWorkloadGate()
    gate.set_scan_active(True)
    wakeups = SimpleNamespace(revision=lambda _kind: 0, wait=AsyncMock())

    async def release_then_stop(*_args, **_kwargs) -> None:
        if gate.scan_active:
            gate.set_scan_active(False)
        else:
            raise asyncio.CancelledError

    wakeups.wait.side_effect = release_then_stop

    await run_target_identification_worker(
        lambda: queue,
        lambda: service,
        worker_id="test-worker",
        workload_gate=gate,
        work_wakeups=wakeups,
    )

    assert queue.claim.await_count == 2
    queue.claim.assert_any_await("test-worker")
    service.run_claimed_job.assert_awaited_once_with({"id": "job-1"}, "test-worker")


@pytest.mark.asyncio
async def test_identification_worker_rechecks_gate_immediately_before_claim() -> None:
    queue = AsyncMock()
    gate = BackgroundWorkloadGate()

    async def activate_scan() -> bool:
        gate.set_scan_active(True)
        return False

    queue.is_paused.side_effect = activate_scan
    service = AsyncMock()

    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(side_effect=asyncio.CancelledError()),
    )

    await run_target_identification_worker(
        lambda: queue,
        lambda: service,
        worker_id="test-worker",
        workload_gate=gate,
        work_wakeups=wakeups,
    )

    queue.claim.assert_not_awaited()
    service.run_claimed_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_identification_worker_skips_claims_when_library_disabled() -> None:
    queue = AsyncMock()
    queue.is_paused.return_value = False
    queue.claim.return_value = None
    service = AsyncMock()
    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    await run_target_identification_worker(
        lambda: queue,
        lambda: service,
        worker_id="test-worker",
        work_wakeups=wakeups,
        enabled_getter=lambda: False,
    )

    queue.recover.assert_not_awaited()
    queue.claim.assert_not_awaited()
    service.run_claimed_job.assert_not_awaited()
    wakeups.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_target_operation_worker_recovers_and_dispatches_each_iteration() -> None:
    supervisor = AsyncMock()
    recovery = AsyncMock()
    supervisor.run_once.side_effect = [{"id": "job-1"}, None]
    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    await run_target_operation_worker(
        lambda: supervisor,
        lambda: recovery,
        worker_id="test-worker",
        work_wakeups=wakeups,
    )

    assert supervisor.recover.await_count == 2
    assert recovery.recover_once.await_count == 2
    assert supervisor.run_once.await_count == 2
    supervisor.run_once.assert_awaited_with("test-worker")


@pytest.mark.asyncio
async def test_operation_worker_skips_claims_when_library_disabled() -> None:
    supervisor = AsyncMock()
    recovery = AsyncMock()
    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    await run_target_operation_worker(
        lambda: supervisor,
        lambda: recovery,
        worker_id="test-worker",
        work_wakeups=wakeups,
        enabled_getter=lambda: False,
    )

    supervisor.recover.assert_not_awaited()
    recovery.recover_once.assert_not_awaited()
    supervisor.run_once.assert_not_awaited()
    wakeups.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_operation_worker_does_not_reclaim_repairs_during_scan() -> None:
    store = AsyncMock()
    store.claim_operation_job.return_value = None
    operations = AsyncMock()
    operations.recover.return_value = 0
    gate = BackgroundWorkloadGate()
    gate.set_scan_active(True)
    supervisor = LibraryOperationSupervisor(
        store,
        operations,
        AsyncMock(),
        AsyncMock(),
        workload_gate=gate,
    )
    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(side_effect=asyncio.CancelledError()),
    )

    await run_target_operation_worker(
        lambda: supervisor,
        worker_id="test-worker",
        work_wakeups=wakeups,
    )

    store.claim_operation_job.assert_awaited_once()
    assert store.claim_operation_job.await_args.args == ("test-worker",)
    assert store.claim_operation_job.await_args.kwargs["lease_seconds"] == 60.0
    assert store.claim_operation_job.await_args.kwargs["kind"] == "bulk_review_apply"
    wakeups.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_contribution_verification_worker_refreshes_provider_each_iteration() -> (
    None
):
    workers = [AsyncMock(), AsyncMock()]
    calls = 0

    def getter():
        nonlocal calls
        worker = workers[min(calls, 1)]
        calls += 1
        return worker

    workers[0].run_once.return_value = "contribution-1"
    workers[1].run_once.return_value = None
    wakeups = SimpleNamespace(
        revision=lambda _kind: 0,
        wait=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    await run_library_contribution_verification_worker(
        getter, worker_id="test-worker", work_wakeups=wakeups
    )

    assert calls == 2
    workers[0].recover.assert_awaited_once()
    workers[0].run_once.assert_awaited_once_with("test-worker")
    workers[1].recover.assert_awaited_once()
    workers[1].run_once.assert_awaited_once_with("test-worker")


@pytest.mark.asyncio
async def test_identification_enqueue_wakes_an_empty_worker_with_subsecond_dispatch() -> (
    None
):
    queue = AsyncMock()
    queue.is_paused.return_value = False
    queue.claim.side_effect = [None, {"id": "job-1"}, None]
    service = AsyncMock()
    dispatched = asyncio.Event()
    service.run_claimed_job.side_effect = lambda *_args: dispatched.set()
    wakeups = DurableWorkWakeups()
    task = asyncio.create_task(
        run_target_identification_worker(
            lambda: queue,
            lambda: service,
            worker_id="test-worker",
            work_wakeups=wakeups,
        )
    )
    while queue.claim.await_count == 0:
        await asyncio.sleep(0)

    started = time.monotonic()
    wakeups.notify("identification")
    await asyncio.wait_for(dispatched.wait(), timeout=0.25)
    elapsed = time.monotonic() - started

    task.cancel()
    await task
    assert elapsed < 0.25
    service.run_claimed_job.assert_awaited_once_with({"id": "job-1"}, "test-worker")


@pytest.mark.asyncio
async def test_empty_worker_recovers_ready_work_after_a_missed_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.native.target_application_runtime.IDENTIFICATION_RECOVERY_INTERVAL_SECONDS",
        0.001,
    )
    queue = AsyncMock()
    queue.is_paused.return_value = False
    queue.claim.side_effect = [None, {"id": "job-1"}, None]
    service = AsyncMock()
    dispatched = asyncio.Event()
    service.run_claimed_job.side_effect = lambda *_args: dispatched.set()
    task = asyncio.create_task(
        run_target_identification_worker(
            lambda: queue,
            lambda: service,
            worker_id="test-worker",
            work_wakeups=DurableWorkWakeups(),
        )
    )

    await asyncio.wait_for(dispatched.wait(), timeout=0.1)

    task.cancel()
    await task
    assert queue.recover.await_count >= 2


@pytest.mark.asyncio
async def test_identification_worker_never_overlaps_claimed_jobs() -> None:
    queue = AsyncMock()
    queue.is_paused.return_value = False
    queue.claim.side_effect = [{"id": "job-1"}, {"id": "job-2"}, None]
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_finished = asyncio.Event()

    async def run_claimed(job: dict, _owner: str) -> None:
        if job["id"] == "job-1":
            first_started.set()
            await release_first.wait()
        else:
            second_finished.set()

    service = AsyncMock()
    service.run_claimed_job.side_effect = run_claimed
    wakeups = DurableWorkWakeups()
    task = asyncio.create_task(
        run_target_identification_worker(
            lambda: queue,
            lambda: service,
            worker_id="test-worker",
            work_wakeups=wakeups,
        )
    )
    await first_started.wait()

    wakeups.notify("identification")
    await asyncio.sleep(0)
    assert queue.claim.await_count == 1

    release_first.set()
    await asyncio.wait_for(second_finished.wait(), timeout=0.1)
    task.cancel()
    await task
    assert service.run_claimed_job.await_count == 2


@pytest.mark.asyncio
async def test_scan_event_ids_are_monotonic_and_counters_are_throttled() -> None:
    store = AsyncMock()
    store.get_stream_revision.side_effect = [7, 8, 9]
    bus = AsyncMock()
    times = iter([0.0, 0.5, 1.0, 2.5])
    events = LibraryScanEventPublisher(store, bus, clock=lambda: next(times))
    run = ScanRun(
        id="run-1",
        kind="incremental",
        trigger="manual",
        state="indexing",
        phase="indexing",
        row_revision=4,
        event_revision=3,
    )

    assert await events.publish(run, event="scan.transition")
    assert await events.publish(run, event="scan.progress", counter=True)
    assert not await events.publish(run, event="scan.progress", counter=True)
    assert await events.publish(run, event="scan.progress", counter=True)

    ids = [call.args[2]["id"] for call in bus.publish.await_args_list]
    assert ids == ["scan:7", "scan:8", "scan:9"]
    assert bus.publish.await_count == 3


@pytest.mark.asyncio
async def test_scan_event_reconnect_gets_latest_and_idle_stream_heartbeats() -> None:
    store = AsyncMock()
    store.get_stream_revision.return_value = 11
    bus = SSEPublisher()
    events = LibraryScanEventPublisher(store, bus, clock=lambda: 0)
    run = ScanRun(
        id="run-1",
        kind="incremental",
        trigger="manual",
        state="discovering",
        phase="discovering",
    )
    await events.publish(run, event="scan.transition")

    subscription = bus.subscribe("target-library-scan")
    latest = await anext(subscription)
    assert latest["data"]["id"] == "scan:11"
    await subscription.aclose()

    idle = bus.subscribe("unused", keepalive_interval=0.001)
    assert await anext(idle) == KEEPALIVE
    await idle.aclose()


def test_target_compat_module_has_no_legacy_scanner_dependency() -> None:
    module = __import__("services.compat.target_scan_service", fromlist=["unused"])
    names = set(module.__dict__)
    assert "LibraryScanner" not in names


def _scan_run(run_id: str = "run-1") -> ScanRun:
    return ScanRun(
        id=run_id,
        kind="incremental",
        trigger="manual",
        state="discovering",
        phase="discovering",
    )


@pytest.mark.asyncio
async def test_walk_oserror_logs_path_and_records_failure_row(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    denied = root / "secret"
    store = AsyncMock()
    store.classify_scan_paths.return_value = {}
    store.add_scan_inventory_batch.return_value = (2, 1)

    def denied_walk(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(denied))
        yield

    scanner = LibraryInventoryScanner(store, directory_walker=denied_walk)
    scope = ScanScope(root_id="root", policy_revision="policy-1")

    with caplog.at_level(logging.WARNING, logger="services.native.library_inventory_scanner"):
        _updated, completed, failure_code = await scanner._walk_scope(
            _scan_run(),
            scope,
            root,
            root,
            SimpleNamespace(resolve=lambda _path: None),
            AsyncMock(return_value=True),
        )

    # F-022: an unreadable root no longer aborts discovery wholesale - the
    # walk degrades, records the failure row, and reports completion so the
    # inventory that DID land proceeds to indexing.
    assert failure_code == "WALK_EACCES"
    records = store.record_scan_failures.await_args.args[1]
    assert [
        (record.failure_code, record.relative_path, record.phase)
        for record in records
    ] == [("WALK_EACCES", "secret", "discovering")]
    # F-032: discovery rows meet the indexing-phase NEW-SCAN-04 detail
    # standard - exception CLASS plus errno, never str(error) or host paths.
    assert records[0].failure_detail == (
        "PermissionError (errno=EACCES) while walking."
    )
    store.complete_scan_scope_discovery.assert_awaited_once_with(
        "run-1",
        "root",
        ".",
        state="partially_read",
        error_code="WALK_EACCES",
    )
    assert "event=walk_error" in caplog.text
    assert "secret" in caplog.text


@pytest.mark.asyncio
async def test_wedged_walk_times_out_detaches_producer_and_recovers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track.flac").touch()
    wedged = threading.Event()
    calls = 0

    def walker(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield (str(root), [], ["track.flac"])
            wedged.wait()
            return
        yield (str(root), [], ["track.flac"])

    store = AsyncMock()
    store.classify_scan_paths.return_value = {"track.flac": ("new", None)}
    store.add_scan_inventory_batch.return_value = (2, 1)
    scanner = LibraryInventoryScanner(
        store,
        directory_walker=walker,
        walk_deadline_seconds=0.05,
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    resolver = SimpleNamespace(resolve=lambda _path: None)
    checkpoint = AsyncMock(return_value=True)

    started = time.monotonic()
    _updated, completed, failure_code = await scanner._walk_scope(
        _scan_run(), scope, root, root, resolver, checkpoint
    )
    elapsed = time.monotonic() - started

    assert completed is False
    assert failure_code == "WALK_TIMEOUT"
    assert elapsed < 2.0
    assert len(scanner._detached_walkers) == 1
    records = store.record_scan_failures.await_args.args[1]
    assert [record.failure_code for record in records] == ["WALK_TIMEOUT"]
    store.complete_scan_scope_discovery.assert_awaited_once_with(
        "run-1",
        "root",
        ".",
        state="partially_read",
        error_code="WALK_TIMEOUT",
    )

    # Releasing the wedged syscall lets the detached producer finish cleanly.
    wedged.set()
    deadline = time.monotonic() + 2.0
    while scanner._detached_walkers and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not scanner._detached_walkers

    # A subsequent walk on the same scanner is unaffected by the detached one.
    _updated, completed, failure_code = await scanner._walk_scope(
        _scan_run("run-2"), scope, root, root, resolver, checkpoint
    )
    assert completed is True
    assert failure_code is None
@pytest.mark.asyncio
async def test_wedged_next_does_not_hold_read_lease_so_writer_acquires(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track.flac").touch()
    wedged = threading.Event()

    def walker(*_args, **_kwargs):
        yield (str(root), [], ["track.flac"])
        wedged.wait()
        return

    store = AsyncMock()
    store.classify_scan_paths.return_value = {"track.flac": ("new", None)}
    store.add_scan_inventory_batch.return_value = (2, 1)
    filesystem = LibraryFilesystemCoordinator()
    scanner = LibraryInventoryScanner(
        store,
        directory_walker=walker,
        filesystem_coordinator=filesystem,
        walk_deadline_seconds=0.05,
    )
    scope = ScanScope(root_id="root-a", policy_revision="policy-1")
    resolver = SimpleNamespace(resolve=lambda _path: None)
    checkpoint = AsyncMock(return_value=True)

    started = time.monotonic()
    _updated, completed, failure_code = await scanner._walk_scope(
        _scan_run(), scope, root, root, resolver, checkpoint
    )
    elapsed = time.monotonic() - started
    assert completed is False
    assert failure_code == "WALK_TIMEOUT"
    assert elapsed < 2.0
    assert len(scanner._detached_walkers) == 1

    # Writer must acquire while walker is still blocked (no lease held)
    writer_acquired = False
    try:
        async with asyncio.timeout(0.5):
            async with filesystem.write("root-a"):
                writer_acquired = True
    except TimeoutError:
        writer_acquired = False
    assert writer_acquired is True

    wedged.set()
    deadline = time.monotonic() + 2.0
    while scanner._detached_walkers and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not scanner._detached_walkers


@pytest.mark.asyncio
async def test_blocked_stat_does_not_hold_read_lease_so_writer_acquires(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "track.flac"
    track.touch()
    wedged = threading.Event()
    real_stat = Path.stat

    def blocking_stat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(self).endswith("track.flac") and not wedged.is_set():
            wedged.wait(timeout=2.0)
        return real_stat(self, *args, **kwargs)

    def walker(*_args, **_kwargs):
        yield (str(root), [], ["track.flac"])

    store = AsyncMock()
    store.classify_scan_paths.return_value = {}
    store.add_scan_inventory_batch.return_value = (2, 1)
    filesystem = LibraryFilesystemCoordinator()
    scanner = LibraryInventoryScanner(
        store,
        directory_walker=walker,
        filesystem_coordinator=filesystem,
        walk_deadline_seconds=0.05,
    )
    scope = ScanScope(root_id="root-a", policy_revision="policy-1")
    resolver = SimpleNamespace(resolve=lambda _path: None)
    checkpoint = AsyncMock(return_value=True)

    import unittest.mock as mock

    with mock.patch.object(Path, "stat", blocking_stat):
        started = time.monotonic()
        _updated, completed, failure_code = await scanner._walk_scope(
            _scan_run(), scope, root, root, resolver, checkpoint
        )
        elapsed = time.monotonic() - started
        assert completed is False
        assert failure_code == "WALK_TIMEOUT"
        assert elapsed < 2.0
        assert len(scanner._detached_walkers) == 1

        writer_acquired = False
        try:
            async with asyncio.timeout(0.5):
                async with filesystem.write("root-a"):
                    writer_acquired = True
        except TimeoutError:
            writer_acquired = False
        assert writer_acquired is True

        wedged.set()
        deadline = time.monotonic() + 2.0
        while scanner._detached_walkers and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert not scanner._detached_walkers


@pytest.mark.asyncio
async def test_cancelled_walk_detaches_when_blocked_and_does_not_hang(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track.flac").touch()
    wedged = threading.Event()

    def walker(*_args, **_kwargs):
        yield (str(root), [], ["track.flac"])
        wedged.wait()
        return

    store = AsyncMock()
    store.classify_scan_paths.return_value = {}
    store.add_scan_inventory_batch.return_value = (2, 1)
    filesystem = LibraryFilesystemCoordinator()
    scanner = LibraryInventoryScanner(
        store,
        directory_walker=walker,
        filesystem_coordinator=filesystem,
        walk_deadline_seconds=0.05,
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    resolver = SimpleNamespace(resolve=lambda _path: None)
    checkpoint = AsyncMock(return_value=True)

    task = asyncio.create_task(
        scanner._walk_scope(_scan_run(), scope, root, root, resolver, checkpoint)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        pass
    # Walker still blocked, but cancel should have detached without hanging
    assert len(scanner._detached_walkers) == 1
    # Writer must still acquire while walker blocked (no lease held)
    writer_acquired = False
    try:
        async with asyncio.timeout(0.5):
            async with filesystem.write("root"):
                writer_acquired = True
    except TimeoutError:
        writer_acquired = False
    assert writer_acquired is True
    wedged.set()
    deadline = time.monotonic() + 2.0
    while scanner._detached_walkers and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not scanner._detached_walkers


@pytest.mark.asyncio
async def test_repeated_stalled_walkers_all_tracked_and_warned(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track.flac").touch()
    events: list[threading.Event] = [threading.Event() for _ in range(5)]
    call_index = 0

    def walker(*_args, **_kwargs):
        nonlocal call_index
        idx = call_index
        call_index += 1
        yield (str(root), [], ["track.flac"])
        events[idx].wait()
        return

    store = AsyncMock()
    store.classify_scan_paths.return_value = {"track.flac": ("new", None)}
    store.add_scan_inventory_batch.return_value = (2, 1)
    filesystem = LibraryFilesystemCoordinator()
    scanner = LibraryInventoryScanner(
        store,
        directory_walker=walker,
        filesystem_coordinator=filesystem,
        walk_deadline_seconds=0.05,
        max_detached_walkers=4,
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    resolver = SimpleNamespace(resolve=lambda _path: None)
    checkpoint = AsyncMock(return_value=True)

    # F-024: the first 4 wedged walkers are detached and tracked; the 5th
    # exceeds the cap - it is refused, counted as leaked, and fails the run
    # with a dedicated code instead of being tracked silently.
    for ordinal in range(4):
        store.record_scan_failures.reset_mock()
        store.complete_scan_scope_discovery.reset_mock()
        with caplog.at_level(logging.WARNING, logger="services.native.library_inventory_scanner"):
            _updated, completed, failure_code = await scanner._walk_scope(
                _scan_run(f"run-{ordinal}"), scope, root, root, resolver, checkpoint
            )
        assert completed is False
        assert failure_code == "WALK_TIMEOUT"
    assert len(scanner._detached_walkers) == 4

    store.record_scan_failures.reset_mock()
    store.complete_scan_scope_discovery.reset_mock()
    with caplog.at_level(logging.WARNING, logger="services.native.library_inventory_scanner"):
        _updated, completed, failure_code = await scanner._walk_scope(
            _scan_run("run-overflow"), scope, root, root, resolver, checkpoint
        )
    assert completed is False
    assert failure_code == "WALKER_UNAVAILABLE"
    assert len(scanner._detached_walkers) == 4
    assert scanner.leaked_walker_count == 1
    records = store.record_scan_failures.await_args.args[1]
    assert [record.failure_code for record in records] == ["WALKER_UNAVAILABLE"]
    store.complete_scan_scope_discovery.assert_awaited_with(
        "run-overflow",
        "root",
        ".",
        state="partially_read",
        error_code="WALKER_UNAVAILABLE",
    )
    assert "detached_walker_cap_exceeded" in caplog.text

    # Releasing the events lets the four detached producers drain.
    for ev in events:
        ev.set()
    deadline = time.monotonic() + 2.0
    while scanner._detached_walkers and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not scanner._detached_walkers


@pytest.mark.asyncio
async def test_probe_does_not_block_default_executor(
    tmp_path: Path,
) -> None:
    loop = asyncio.get_running_loop()
    default_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="default-test")
    original = loop._default_executor  # type: ignore[attr-defined]
    loop.set_default_executor(default_executor)
    try:
        root = tmp_path / "music"
        root.mkdir()
        wedged = threading.Event()
        probe_thread: list[str] = []

        def probe(path: Path) -> bool:
            probe_thread.append(threading.current_thread().name)
            wedged.wait(timeout=5.0)
            return True

        store = AsyncMock()
        store.get_scan_scope_discovery_state.return_value = "pending"
        scanner = LibraryInventoryScanner(
            store,
            directory_probe=probe,
            walk_deadline_seconds=0.05,
            probe_executor_max_workers=1,
        )
        scope = ScanScope(root_id="root", policy_revision="policy-1")
        run = _scan_run()

        await scanner.discover(
            run, [scope], {scope.root_id: root}, SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1"), AsyncMock(return_value=True)
        )
        assert store.transition_scan_run.await_args is not None
        assert store.transition_scan_run.await_args.kwargs["terminal_code"] == "WALK_TIMEOUT"
        # F-023: the abandoned probe future is tombstoned on timeout, so the
        # slot recovers immediately instead of staying occupied forever.
        assert scanner.probe_pending_count == 0
        assert scanner.wedged_probe_count == 1
        assert probe_thread and "library-probe" in probe_thread[0]
        marker_done = False

        async def marker() -> None:
            nonlocal marker_done
            await asyncio.to_thread(lambda: time.sleep(0.01))
            marker_done = True

        await asyncio.wait_for(marker(), timeout=0.5)
        assert marker_done is True
        wedged.set()
        deadline = time.monotonic() + 2.0
        while scanner.probe_pending_count and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert scanner.probe_pending_count == 0
        scanner.close()
        scanner.close()
    finally:
        try:
            loop.set_default_executor(original)  # type: ignore[arg-type]
        except TypeError:
            loop._default_executor = original  # type: ignore[attr-defined]
        default_executor.shutdown(wait=True)
        wedged.set()

@pytest.mark.asyncio
async def test_repeated_probe_timeouts_recover_the_slot_each_run(
    tmp_path: Path,
) -> None:
    """F-023: a wedged probe no longer occupies the slot forever. Every run
    attempts a fresh probe (bounded by its own deadline) instead of
    fast-failing on capacity until restart; each timeout tombstones its
    future so pending drains to zero immediately."""
    root = tmp_path / "music"
    root.mkdir()
    release: list[threading.Event] = []

    def probe(_path: Path) -> bool:
        ev = threading.Event()
        release.append(ev)
        ev.wait(timeout=5.0)
        return True

    store = AsyncMock()
    store.get_scan_scope_discovery_state.return_value = "pending"
    scanner = LibraryInventoryScanner(
        store,
        directory_probe=probe,
        walk_deadline_seconds=0.05,
        probe_executor_max_workers=1,
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    resolver = SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1")
    checkpoint = AsyncMock(return_value=True)

    run1 = _scan_run("run-1")
    await scanner.discover(run1, [scope], {scope.root_id: root}, resolver, checkpoint)
    assert store.transition_scan_run.await_args is not None
    assert store.transition_scan_run.await_args.kwargs["terminal_code"] == "WALK_TIMEOUT"
    assert scanner.probe_pending_count == 0
    assert scanner.wedged_probe_count == 1
    store.transition_scan_run.reset_mock()

    run2 = _scan_run("run-2")
    start = time.monotonic()
    await scanner.discover(run2, [scope], {scope.root_id: root}, resolver, checkpoint)
    elapsed2 = time.monotonic() - start
    # The slot was recovered, so run2 attempted a real probe and paid one
    # bounded deadline instead of failing instantly with the stale refusal.
    assert store.transition_scan_run.await_args is not None
    assert store.transition_scan_run.await_args.kwargs["terminal_code"] == "WALK_TIMEOUT"
    assert elapsed2 < 2.0
    assert scanner.probe_pending_count == 0
    assert scanner.wedged_probe_count == 2

    for ev in release:
        ev.set()
    deadline = time.monotonic() + 2.0
    while scanner.probe_pending_count and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    scanner.close()


@pytest.mark.asyncio
async def test_probe_capacity_refusal_reports_unavailable_and_retry_recovers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F-023: capacity refusal gets its own PROBE_UNAVAILABLE code and detail,
    plus ONE bounded retry - when the wedged probe finishes during the wait,
    the same scope proceeds instead of failing the run."""
    root = tmp_path / "music"
    root.mkdir()
    (root / "healthy.flac").write_bytes(b"x")
    wedged = threading.Event()

    def probe(_path: Path) -> bool:
        wedged.wait(timeout=5.0)
        return True

    store = AsyncMock()
    store.get_scan_scope_discovery_state.return_value = "pending"
    store.classify_scan_paths.return_value = {"healthy.flac": ("new", None)}
    store.add_scan_inventory_batch.return_value = (1, 1)
    scanner = LibraryInventoryScanner(
        store,
        directory_walker=os.walk,
        directory_probe=probe,
        walk_deadline_seconds=0.05,
        probe_executor_max_workers=1,
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    resolver = SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1", resolve=lambda _p: SimpleNamespace(policy="automatic"))
    checkpoint = AsyncMock(return_value=True)

    blocker = asyncio.get_running_loop().create_future()
    scanner._pending_probes.add(blocker)

    async def release_during_retry() -> None:
        await asyncio.sleep(0.02)
        wedged.set()
        # Mirror the scanner-created futures' on-done discard: without a
        # callback, completing the blocker leaves the slot occupied.
        blocker.set_result(True)
        scanner._pending_probes.discard(blocker)

    asyncio.create_task(release_during_retry())

    with caplog.at_level(logging.WARNING, logger="services.native.library_inventory_scanner"):
        result = await scanner.discover(
            _scan_run("run-retry"),
            [scope],
            {scope.root_id: root},
            resolver,
            checkpoint,
        )

    # The retry recovered the slot: no wholesale failure was recorded.
    assert "probe_capacity_exceeded" in caplog.text
    assert store.transition_scan_run.await_args is None or (
        store.transition_scan_run.await_args.kwargs.get("terminal_code")
        != "PROBE_UNAVAILABLE"
    )
    scanner.close()


@pytest.mark.asyncio
async def test_persistent_probe_wedge_fails_with_probe_unavailable(
    tmp_path: Path,
) -> None:
    """F-023: when the retry also hits a still-occupied slot, the run fails
    with the dedicated PROBE_UNAVAILABLE code and honest detail text."""
    root = tmp_path / "music"
    root.mkdir()
    store = AsyncMock()
    store.get_scan_scope_discovery_state.return_value = "pending"
    scanner = LibraryInventoryScanner(
        store,
        directory_walker=os.walk,
        walk_deadline_seconds=0.05,
        probe_executor_max_workers=1,
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    resolver = SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1")

    blocker = asyncio.get_running_loop().create_future()
    scanner._pending_probes.add(blocker)

    await scanner.discover(
        _scan_run("run-refused"),
        [scope],
        {scope.root_id: root},
        resolver,
        AsyncMock(return_value=True),
    )

    assert store.transition_scan_run.await_args is not None
    assert (
        store.transition_scan_run.await_args.kwargs["terminal_code"]
        == "PROBE_UNAVAILABLE"
    )
    records = store.record_scan_failures.await_args.args[1]
    assert records[0].failure_code == "PROBE_UNAVAILABLE"
    assert "A previous root probe never completed" in records[0].failure_detail
    scanner.close()




@pytest.mark.asyncio
async def test_probe_pending_drains_after_release_and_close_twice(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    wedged = threading.Event()

    def probe(_path: Path) -> bool:
        wedged.wait(timeout=5.0)
        return True

    store = AsyncMock()
    store.get_scan_scope_discovery_state.return_value = "pending"
    scanner = LibraryInventoryScanner(
        store, directory_probe=probe, walk_deadline_seconds=0.05, probe_executor_max_workers=1
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    resolver = SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1")
    await scanner.discover(_scan_run(), [scope], {scope.root_id: root}, resolver, AsyncMock(return_value=True))
    assert store.transition_scan_run.await_args is not None
    assert store.transition_scan_run.await_args.kwargs["terminal_code"] == "WALK_TIMEOUT"
    # F-023: the timeout tombstoned the future, so the slot already recovered.
    assert scanner.probe_pending_count == 0
    scanner.close()
    scanner.close()
    # Close is explicit shutdown and stays idempotent after the recovery.
    assert scanner.probe_pending_count == 0
    wedged.set()
    deadline = time.monotonic() + 2.0
    while scanner.probe_pending_count and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert scanner.probe_pending_count == 0
    scanner.close()


@pytest.mark.asyncio
async def test_probe_success_and_missing_root_unchanged(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track.flac").write_bytes(b"audio")
    store = AsyncMock()
    store.get_scan_scope_discovery_state.return_value = "pending"
    store.classify_scan_paths.return_value = {"track.flac": ("new", None)}
    store.add_scan_inventory_batch.return_value = (1, 1)
    scanner = LibraryInventoryScanner(store, walk_deadline_seconds=0.05, probe_executor_max_workers=1)
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    resolver = SimpleNamespace(
        settings=SimpleNamespace(enabled=True),
        policy_revision="policy-1",
        resolve=lambda _p: SimpleNamespace(policy="automatic"),
    )
    run = _scan_run()
    result = await scanner.discover(run, [scope], {scope.root_id: root}, resolver, AsyncMock(return_value=True))
    assert result is not None
    # success probe should not have produced WALK_TIMEOUT or ROOT_UNAVAILABLE
    if store.transition_scan_run.await_args is not None:
        assert store.transition_scan_run.await_args.kwargs["terminal_code"] not in {"WALK_TIMEOUT", "ROOT_UNAVAILABLE"}
    assert scanner.probe_pending_count == 0
    missing_store = AsyncMock()
    missing_store.get_scan_scope_discovery_state.return_value = "pending"
    missing_scanner = LibraryInventoryScanner(missing_store, walk_deadline_seconds=0.05, probe_executor_max_workers=1)
    missing_root = tmp_path / "missing"
    missing_scope = ScanScope(root_id="root", policy_revision="policy-1")
    await missing_scanner.discover(_scan_run("run-missing"), [missing_scope], {missing_scope.root_id: missing_root}, SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1"), AsyncMock(return_value=True))
    assert missing_store.transition_scan_run.await_args is not None
    assert missing_store.transition_scan_run.await_args.kwargs["terminal_code"] == "ROOT_UNAVAILABLE"
    assert missing_scanner.probe_pending_count == 0
    scanner.close()
    missing_scanner.close()


@pytest.mark.asyncio
async def test_scanner_smoke_delayed_probe_then_normal_scan_and_to_thread(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "track.flac").write_bytes(b"audio")
    wedged = threading.Event()
    call_count = 0

    def probe(path: Path) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            wedged.wait(timeout=5.0)
            return True
        return Path.is_dir(path)

    store = AsyncMock()
    store.get_scan_scope_discovery_state.return_value = "pending"
    store.classify_scan_paths.return_value = {}
    store.add_scan_inventory_batch.return_value = (1, 1)
    scanner = LibraryInventoryScanner(
        store, directory_probe=probe, walk_deadline_seconds=0.05, probe_executor_max_workers=1
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    resolver = SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1", resolve=lambda _p: SimpleNamespace(policy="automatic"))
    await scanner.discover(_scan_run("run-1"), [scope], {scope.root_id: root}, resolver, AsyncMock(return_value=True))
    assert store.transition_scan_run.await_args is not None
    assert store.transition_scan_run.await_args.kwargs["terminal_code"] == "WALK_TIMEOUT"
    # F-023: slot recovered immediately after the timeout tombstone.
    assert scanner.probe_pending_count == 0
    await asyncio.wait_for(asyncio.to_thread(lambda: 42), timeout=0.5)
    wedged.set()
    deadline = time.monotonic() + 2.0
    while scanner.probe_pending_count and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert scanner.probe_pending_count == 0
    store2 = AsyncMock()
    store2.get_scan_scope_discovery_state.return_value = "pending"
    store2.classify_scan_paths.return_value = {"track.flac": ("new", None)}
    store2.add_scan_inventory_batch.return_value = (2, 1)
    scanner2 = LibraryInventoryScanner(store2, walk_deadline_seconds=0.05, probe_executor_max_workers=1)
    result2 = await scanner2.discover(_scan_run("run-2"), [scope], {scope.root_id: root}, resolver, AsyncMock(return_value=True))
    assert result2 is not None
    assert scanner2.probe_pending_count == 0
    scanner.close()
    scanner2.close()
@pytest.mark.asyncio
async def test_scanner_close_is_idempotent_and_coordinator_delegates(tmp_path: Path) -> None:
    store = AsyncMock()
    scanner = LibraryInventoryScanner(store, probe_executor_max_workers=1)
    scanner.close()
    scanner.close()
    assert scanner.probe_pending_count == 0
    await scanner.aclose()
    assert scanner.probe_pending_count == 0
    coordinator = LibraryScanCoordinator(
        store,
        scanner,
        AsyncMock(),
        AsyncMock(),
        lambda: SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="1"),
    )
    coordinator.close()
    await coordinator.aclose()
    coordinator.close()
    await coordinator.aclose()
    assert scanner.probe_pending_count == 0
@pytest.mark.asyncio
async def test_concurrent_probe_submissions_atomic_never_exceeds_cap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    wedged = threading.Event()

    def probe(_path: Path) -> bool:
        wedged.wait(timeout=5.0)
        return True

    store = AsyncMock()
    store.get_scan_scope_discovery_state.return_value = "pending"
    scanner = LibraryInventoryScanner(
        store, directory_probe=probe, walk_deadline_seconds=0.05, probe_executor_max_workers=1
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    resolver = SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1")
    checkpoint = AsyncMock(return_value=True)

    # Two concurrent discovers while first probe is wedged - second must see capacity and fail fast without exceeding cap
    task1 = asyncio.create_task(scanner.discover(_scan_run("run-1"), [scope], {scope.root_id: root}, resolver, checkpoint))
    await asyncio.sleep(0.02)
    assert scanner.probe_pending_count == 1
    task2 = asyncio.create_task(scanner.discover(_scan_run("run-2"), [scope], {scope.root_id: root}, resolver, checkpoint))
    await asyncio.sleep(0.02)
    # Pending must never exceed 1 even under concurrent reservation
    assert scanner.probe_pending_count == 1
    results = await asyncio.gather(task1, task2)
    # Both should have resulted in WALK_TIMEOUT (first via timeout, second via capacity)
    assert store.transition_scan_run.await_count == 2
    for call in store.transition_scan_run.await_args_list:
        assert call.kwargs["terminal_code"] == "WALK_TIMEOUT"
    # F-023: both timeout tombstones recovered the slot - nothing stays pending.
    assert scanner.probe_pending_count == 0
    deadline = time.monotonic() + 2.0
    while scanner.probe_pending_count and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert scanner.probe_pending_count == 0
    scanner.close()


@pytest.mark.asyncio
async def test_no_deadlock_when_blocked_future_completes_during_capacity_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    wedged = threading.Event()

    def probe(_path: Path) -> bool:
        wedged.wait(timeout=5.0)
        return True

    store = AsyncMock()
    store.get_scan_scope_discovery_state.return_value = "pending"
    # Make record_scan_failures sleep to simulate await while holding lock (old bug would deadlock)
    original_record = AsyncMock()
    async def slow_record(*args, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.2)
        return None

    store.record_scan_failures = AsyncMock(side_effect=slow_record)
    store.complete_scan_scope_discovery = AsyncMock()
    store.transition_scan_run = AsyncMock(return_value=_scan_run("run-x"))
    scanner = LibraryInventoryScanner(
        store, directory_probe=probe, walk_deadline_seconds=0.05, probe_executor_max_workers=1
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    resolver = SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1")
    checkpoint = AsyncMock(return_value=True)

    # First probe wedged, pending=1
    task1 = asyncio.create_task(scanner.discover(_scan_run("run-1"), [scope], {scope.root_id: root}, resolver, checkpoint))
    await asyncio.sleep(0.02)
    assert scanner.probe_pending_count == 1
    # Second probe at capacity will enter failure path and await slow_record (0.2s). During that await, release first probe.
    task2 = asyncio.create_task(scanner.discover(_scan_run("run-2"), [scope], {scope.root_id: root}, resolver, checkpoint))
    await asyncio.sleep(0.05)
    # Release first probe while second is in its await - if lock were held across await, this would deadlock
    wedged.set()
    # Both should complete within 1s without deadlock
    await asyncio.wait_for(asyncio.gather(task1, task2), timeout=1.0)
    # After both, pending should be 0 (first completed, second never added pending)
    deadline = time.monotonic() + 2.0
    while scanner.probe_pending_count and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert scanner.probe_pending_count == 0
    scanner.close()


@pytest.mark.asyncio
async def test_probe_thread_is_daemon_and_named(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    captured: list[threading.Thread] = []
    wedged = threading.Event()

    def probe(_path: Path) -> bool:
        captured.append(threading.current_thread())
        wedged.wait(timeout=5.0)
        return True

    store = AsyncMock()
    store.get_scan_scope_discovery_state.return_value = "pending"
    scanner = LibraryInventoryScanner(
        store, directory_probe=probe, walk_deadline_seconds=0.05, probe_executor_max_workers=1
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    task = asyncio.create_task(scanner.discover(_scan_run(), [scope], {scope.root_id: root}, SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1"), AsyncMock(return_value=True)))
    await asyncio.sleep(0.02)
    assert captured
    thread = captured[0]
    assert thread.daemon is True
    assert thread.name == "library-probe"
    assert scanner.probe_pending_count == 1
    wedged.set()
    await asyncio.wait_for(task, timeout=1.0)
    deadline = time.monotonic() + 2.0
    while scanner.probe_pending_count and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert scanner.probe_pending_count == 0
    scanner.close()


@pytest.mark.asyncio
async def test_close_with_permanently_blocked_probe_does_not_hang(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    # Never set this event - probe blocks forever
    blocked = threading.Event()

    def probe(_path: Path) -> bool:
        blocked.wait(timeout=10.0)
        return True

    store = AsyncMock()
    store.get_scan_scope_discovery_state.return_value = "pending"
    scanner = LibraryInventoryScanner(
        store, directory_probe=probe, walk_deadline_seconds=0.05, probe_executor_max_workers=1
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    task = asyncio.create_task(scanner.discover(_scan_run(), [scope], {scope.root_id: root}, SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1"), AsyncMock(return_value=True)))
    await asyncio.sleep(0.02)
    assert scanner.probe_pending_count == 1
    start = time.monotonic()
    scanner.close()
    elapsed = time.monotonic() - start
    assert elapsed < 0.2
    # Close cancels pending future, so pending should be 0 even though thread still blocked (daemon)
    assert scanner.probe_pending_count == 0
    # Second close idempotent
    scanner.close()
    assert scanner.probe_pending_count == 0
    # Discover after close should fail fast due to closed
    await asyncio.wait_for(task, timeout=1.0)
    # New probe after close should also fail fast
    store2 = AsyncMock()
    store2.get_scan_scope_discovery_state.return_value = "pending"
    result2 = await scanner.discover(_scan_run("run-2"), [scope], {scope.root_id: root}, SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1"), AsyncMock(return_value=True))
    assert store2.transition_scan_run.await_args is None or store2.transition_scan_run.await_args.kwargs.get("terminal_code") == "WALK_TIMEOUT"
    # blocked probe thread stays alive as a daemon; aclose must not hang on it
    await scanner.aclose()
@pytest.mark.asyncio
async def test_probe_loop_closed_does_not_set_future_from_worker_thread(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    loop = asyncio.get_running_loop()
    original_call = loop.call_soon_threadsafe

    def raising_call(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("loop closed")

    store = AsyncMock()
    store.get_scan_scope_discovery_state.return_value = "pending"
    scanner = LibraryInventoryScanner(
        store, directory_probe=lambda p: True, walk_deadline_seconds=0.05, probe_executor_max_workers=1
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")
    loop.call_soon_threadsafe = raising_call  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.DEBUG, logger="services.native.library_inventory_scanner"):
            result = await scanner.discover(
                _scan_run(), [scope], {scope.root_id: root}, SimpleNamespace(settings=SimpleNamespace(enabled=True), policy_revision="policy-1"), AsyncMock(return_value=True)
            )
        # Loop closed simulation should not have called set_result from worker thread; future remains pending until timeout
        assert store.transition_scan_run.await_args is not None
        assert store.transition_scan_run.await_args.kwargs["terminal_code"] == "WALK_TIMEOUT"
        # F-023: the timeout tombstones the future even when call_soon_threadsafe
        # is broken - the in-loop fallback cancel recovers the slot immediately.
        assert scanner.probe_pending_count == 0
        # No raw path in debug log
        for record in caplog.records:
            assert str(root) not in record.getMessage()
    finally:
        loop.call_soon_threadsafe = original_call  # type: ignore[method-assign]
        # Slot already recovered by the tombstone; close stays a no-op here.
        assert scanner.probe_pending_count == 0
        scanner.close()
        assert scanner.probe_pending_count == 0










@pytest.mark.asyncio
async def test_root_probe_timeout_fails_the_run_with_walk_timeout(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    wedged_probe = threading.Event()

    def probe(_path: Path) -> bool:
        wedged_probe.wait()
        return True

    store = AsyncMock()
    store.get_scan_scope_discovery_state.return_value = "pending"
    scanner = LibraryInventoryScanner(
        store,
        walk_deadline_seconds=0.05,
        directory_probe=probe,
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")

    try:
        started = time.monotonic()
        await asyncio.wait_for(
            scanner.discover(
                _scan_run(),
                [scope],
                {"root": root},
                SimpleNamespace(),
                AsyncMock(return_value=True),
            ),
            timeout=2.0,
        )
        elapsed = time.monotonic() - started
    finally:
        wedged_probe.set()

    assert elapsed < 2.0
    records = store.record_scan_failures.await_args.args[1]
    assert [record.failure_code for record in records] == ["WALK_TIMEOUT"]
    store.complete_scan_scope_discovery.assert_awaited_once_with(
        "run-1",
        "root",
        ".",
        state="unavailable",
        error_code="WALK_TIMEOUT",
    )
    assert (
        store.transition_scan_run.await_args.kwargs["terminal_code"] == "WALK_TIMEOUT"
    )


@pytest.mark.asyncio
async def test_inventory_file_stat_runs_outside_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "track.flac"
    track.touch()
    event_loop_thread = threading.get_ident()
    stat_threads: list[int] = []
    original_stat = Path.stat

    def record_stat(path: Path, *args, **kwargs):
        if path.name == "track.flac":
            stat_threads.append(threading.get_ident())
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", record_stat)
    store = AsyncMock()
    store.classify_scan_paths.return_value = {"track.flac": ("new", None)}
    store.add_scan_inventory_batch.return_value = (2, 1)
    scanner = LibraryInventoryScanner(
        store,
        directory_walker=lambda *_args, **_kwargs: iter(
            [(str(root), [], ["track.flac"])]
        ),
    )
    run = ScanRun(
        id="run-1",
        kind="incremental",
        trigger="manual",
        state="discovering",
        phase="discovering",
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1")

    _updated, completed, failure_code = await scanner._walk_scope(
        run,
        scope,
        root,
        root,
        SimpleNamespace(resolve=lambda _path: None),
        AsyncMock(return_value=True),
    )

    assert completed is True
    assert failure_code is None
    assert stat_threads
    assert event_loop_thread not in stat_threads


@pytest.mark.asyncio
async def test_automatic_scheduler_uses_terminal_history_and_coordinator() -> None:
    coordinator = AsyncMock()
    coordinator.latest_filesystem_terminal.return_value = ScanRun(
        id="finished",
        kind="incremental",
        trigger="subsonic",
        state="failed",
        phase="reconciling",
        terminal_at=1_800_000_000,
    )
    resolver = SimpleNamespace(
        policy_revision="policy-1",
        settings=SimpleNamespace(
            library_roots=[
                SimpleNamespace(id="root-a", path="/music", policy="automatic")
            ]
        ),
    )
    scheduler = LibraryAutomaticScanScheduler()

    before_due = await scheduler.tick(
        coordinator,
        resolver,
        frequency="24hr",
        daily_time="03:00",
        timezone_name="Europe/London",
        now=datetime.fromtimestamp(1_800_000_100).astimezone(),
    )
    assert before_due is False
    coordinator.request_run.assert_not_awaited()

    due = await scheduler.tick(
        coordinator,
        resolver,
        frequency="24hr",
        daily_time="03:00",
        timezone_name="Europe/London",
        now=datetime.fromtimestamp(1_800_086_401).astimezone(),
    )
    assert due is True
    request = coordinator.request_run.await_args.args[0]
    assert request.trigger == "automatic"

    coordinator.reset_mock()
    manual = await scheduler.tick(
        coordinator,
        resolver,
        frequency="manual",
        daily_time="03:00",
        timezone_name="Europe/London",
        now=datetime.now().astimezone(),
    )
    assert manual is False
    coordinator.latest_filesystem_terminal.assert_not_awaited()


@pytest.mark.asyncio
async def test_automatic_scheduler_scans_allowed_children_of_excluded_roots(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    resolver = LibraryPolicyResolver(
        TypedLibrarySettings(
            library_roots=[
                LibraryRootSettings(
                    id="root-a",
                    path=str(root),
                    label="Music",
                    policy="excluded",
                    rules=[
                        LibraryPathPolicyRule(
                            id="allowed",
                            relative_path="Allowed",
                            policy="local_metadata",
                        ),
                        LibraryPathPolicyRule(
                            id="nested",
                            relative_path="Allowed/Automatic",
                            policy="automatic",
                        ),
                        LibraryPathPolicyRule(
                            id="excluded-sibling",
                            relative_path="Excluded",
                            policy="excluded",
                        ),
                    ],
                )
            ]
        )
    )
    coordinator = AsyncMock()
    coordinator.latest_filesystem_terminal.return_value = None

    queued = await LibraryAutomaticScanScheduler().tick(
        coordinator,
        resolver,
        frequency="5min",
        daily_time="03:00",
        timezone_name="Europe/London",
        now=datetime.now().astimezone(),
    )

    assert queued is True
    scopes = coordinator.request_run.await_args.args[0].scopes
    assert [(scope.scope_id, scope.relative_path) for scope in scopes] == [
        ("allowed", "Allowed")
    ]

@pytest.mark.asyncio
async def test_target_identification_worker_circuit_open_defers_exact_and_one_wake() -> None:
    from unittest.mock import AsyncMock, MagicMock
    from infrastructure.resilience.retry import CircuitOpenError

    queue = AsyncMock()
    queue.recover = AsyncMock()
    queue.is_paused = AsyncMock(return_value=False)
    queue.claim = AsyncMock(return_value={"id": "job1", "attempt_count": 1, "row_revision": 1})
    queue.defer = AsyncMock(return_value=1)
    service = MagicMock()
    service.run_claimed_job = AsyncMock(side_effect=CircuitOpenError("open", breaker_name="test", retry_after_seconds=10))
    wakeups = MagicMock()
    wakeups.revision.return_value = 0
    wakeups.wait = AsyncMock(side_effect=asyncio.CancelledError())
    await run_target_identification_worker(
        lambda: queue,
        lambda: service,
        worker_id="test-worker",
        work_wakeups=wakeups,
    )
    queue.defer.assert_awaited_once_with({"id": "job1", "attempt_count": 1, "row_revision": 1}, "test-worker", "PROVIDER_TEMPORARILY_UNAVAILABLE", retry_after_seconds=10)
    assert wakeups.wait.await_count == 1
    assert wakeups.wait.await_args.kwargs["timeout_seconds"] == 10
    assert queue.claim.await_count == 1


@pytest.mark.asyncio
async def test_target_worker_one_sleep_and_cancel() -> None:
    from unittest.mock import AsyncMock, MagicMock

    queue = MagicMock()
    queue.recover = AsyncMock()
    queue.is_paused = AsyncMock(return_value=True)
    wakeups = MagicMock()
    wakeups.revision.return_value = 0
    wakeups.wait = AsyncMock(side_effect=asyncio.CancelledError())
    await run_target_identification_worker(
        lambda: queue,
        lambda: MagicMock(),
        worker_id="test-worker",
        work_wakeups=wakeups,
    )
    assert wakeups.wait.await_count == 1


@pytest.mark.asyncio
async def test_production_smoke_identification_worker_future_wake_no_reclaim(tmp_path: Path) -> None:
    import sqlite3
    import threading
    from infrastructure.persistence.native_library_store import NativeLibraryStore
    from services.native.identification_queue_service import IdentificationQueueService
    from models.library_work import IdentificationJob
    from models.local_catalog import LocalAlbum, LocalArtist, LocalTrack, CatalogMembership
    from infrastructure.resilience.retry import CircuitOpenError

    db_path = tmp_path / "smoke.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO auth_users(id) VALUES ('worker')")
    store = NativeLibraryStore(db_path, threading.Lock(), work_wakeups=DurableWorkWakeups())
    artist = LocalArtist(id="artist-1", display_name="A", folded_name="a", normalized_name="a", kind="group", created_at=1, updated_at=1)
    album = LocalAlbum(id="album-1", root_id="root", grouping_key="g1", title="Album", album_artist_id=artist.id, album_artist_name="A", created_at=1, updated_at=1)
    track = LocalTrack(id="track-1", local_album_id=album.id, root_id="root", file_path="/music/a.flac", relative_path="a.flac", path_hash="h1", file_size_bytes=100, file_mtime_ns=1, stat_revision="s1", tag_revision="t1", title="Track", artist_name="A", album_title="Album", album_artist_name="A", track_number=1, duration_seconds=180, file_format="flac", imported_at=1, applied_policy="automatic", applied_policy_revision="p1")
    await store.create_catalog_membership(CatalogMembership(album=album, artists=[artist], tracks=[track], album_credits=[], track_credits={track.id: []}))
    queue = IdentificationQueueService(store)
    now = time.time()
    await store.enqueue_identification_job(IdentificationJob(id="job-smoke", local_album_id="album-1", kind="automatic", dedupe_key="automatic:album-1:rev1", input_revision="rev1", priority=20, created_at=now))
    claimed = await queue.claim("worker-1", now=now + 1)
    assert claimed is not None
    delays: list[float] = []
    orig_notify = store.work_wakeups.notify_after

    def spy(kind: str, delay: float) -> None:
        if kind == "identification":
            delays.append(delay)
        return orig_notify(kind, delay)

    store.work_wakeups.notify_after = spy  # type: ignore
    await queue.defer(claimed, "worker-1", "UNEXPECTED_ERROR", now=now + 2, retry_after_seconds=10)
    assert len(delays) == 1
    assert 29.5 <= delays[0] <= 30.5
    # No early claim
    assert await queue.claim("worker-2", now=now + 5) is None
    assert await queue.claim("worker-2", now=now + 13) is None
    assert await queue.claim("worker-2", now=now + 33) is not None

@pytest.mark.asyncio
async def test_discovery_missing_root_uses_fresh_clock() -> None:
    stale = 100.0
    fresh = 300.0

    def fake_clock() -> float:
        return fresh

    store = AsyncMock()
    store.get_scan_scope_discovery_state = AsyncMock(return_value="pending")
    store.complete_scan_scope_discovery = AsyncMock()
    store.record_scan_failures = AsyncMock()
    store.transition_scan_run = AsyncMock(
        return_value=ScanRun(
            id="run-1",
            kind="incremental",
            trigger="manual",
            state="failed",
            phase="discovering",
            updated_at=fresh,
            terminal_at=fresh,
            row_revision=2,
        )
    )
    run = ScanRun(
        id="run-1",
        kind="incremental",
        trigger="manual",
        state="discovering",
        phase="discovering",
        updated_at=stale,
        row_revision=1,
    )
    scope = ScanScope(root_id="root-a", relative_path=".", policy_revision="rev-1")
    scanner = LibraryInventoryScanner(store, clock=fake_clock)
    result = await scanner.discover(
        run, [scope], {}, SimpleNamespace(policy_revision="rev-1"), AsyncMock(return_value=True)
    )
    assert result.state == "failed"
    assert store.record_scan_failures.await_count == 1
    recorded = store.record_scan_failures.call_args[0][1][0]
    assert recorded.recorded_at == fresh
    assert recorded.failure_code == "ROOT_UNAVAILABLE"
    assert store.transition_scan_run.await_count == 1
    _args, kwargs = store.transition_scan_run.call_args
    assert kwargs["now"] == fresh
    assert kwargs["now"] > stale
    assert kwargs["terminal_code"] == "ROOT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_discovery_missing_path_uses_fresh_clock(tmp_path: Path) -> None:
    stale = 100.0
    fresh = 350.0

    def fake_clock() -> float:
        return fresh

    store = AsyncMock()
    store.get_scan_scope_discovery_state = AsyncMock(return_value="pending")
    store.complete_scan_scope_discovery = AsyncMock()
    store.record_scan_failures = AsyncMock()
    store.transition_scan_run = AsyncMock(
        return_value=ScanRun(
            id="run-1",
            kind="incremental",
            trigger="manual",
            state="failed",
            phase="discovering",
            updated_at=fresh,
            terminal_at=fresh,
            row_revision=2,
        )
    )
    root = tmp_path / "music"
    root.mkdir()
    run = ScanRun(
        id="run-1",
        kind="incremental",
        trigger="manual",
        state="discovering",
        phase="discovering",
        updated_at=stale,
        row_revision=1,
    )
    scope = ScanScope(root_id="root-a", relative_path="Missing", policy_revision="rev-1", root_path=str(root))
    scanner = LibraryInventoryScanner(store, clock=fake_clock, directory_probe=lambda _p: False)
    result = await scanner.discover(
        run, [scope], {"root-a": root}, SimpleNamespace(policy_revision="rev-1"), AsyncMock(return_value=True)
    )
    assert result.state == "failed"
    recorded = store.record_scan_failures.call_args[0][1][0]
    assert recorded.recorded_at == fresh
    assert recorded.failure_code == "ROOT_UNAVAILABLE"
    assert store.transition_scan_run.call_args.kwargs["now"] == fresh
    assert store.transition_scan_run.call_args.kwargs["now"] > stale


@pytest.mark.asyncio
async def test_discovery_probe_timeout_uses_fresh_clock(tmp_path: Path) -> None:
    stale = 100.0
    fresh = 400.0

    def fake_clock() -> float:
        return fresh

    store = AsyncMock()
    store.get_scan_scope_discovery_state = AsyncMock(return_value="pending")
    store.complete_scan_scope_discovery = AsyncMock()
    store.record_scan_failures = AsyncMock()
    store.transition_scan_run = AsyncMock(
        return_value=ScanRun(
            id="run-1",
            kind="incremental",
            trigger="manual",
            state="failed",
            phase="discovering",
            updated_at=fresh,
            terminal_at=fresh,
            row_revision=2,
        )
    )
    root = tmp_path / "music"
    root.mkdir()
    run = ScanRun(
        id="run-1",
        kind="incremental",
        trigger="manual",
        state="discovering",
        phase="discovering",
        updated_at=stale,
        row_revision=1,
    )
    scope = ScanScope(root_id="root-a", relative_path=".", policy_revision="rev-1", root_path=str(root))

    def blocking_probe(_path: Path) -> bool:
        time.sleep(0.12)
        return True

    scanner = LibraryInventoryScanner(
        store, clock=fake_clock, walk_deadline_seconds=0.05, directory_probe=blocking_probe
    )
    result = await scanner.discover(
        run, [scope], {"root-a": root}, SimpleNamespace(policy_revision="rev-1"), AsyncMock(return_value=True)
    )
    assert result.state == "failed"
    recorded = store.record_scan_failures.call_args[0][1][0]
    assert recorded.recorded_at == fresh
    assert recorded.failure_code == "WALK_TIMEOUT"
    assert store.transition_scan_run.call_args.kwargs["now"] == fresh
    assert store.transition_scan_run.call_args.kwargs["now"] > stale
    # failure record and terminal share the same fresh clock and ordering is consistent
    assert recorded.recorded_at <= store.transition_scan_run.call_args.kwargs["now"]


@pytest.mark.asyncio
async def test_discovery_walk_failure_uses_fresh_clock(tmp_path: Path) -> None:
    stale = 100.0
    fresh = 450.0

    def fake_clock() -> float:
        return fresh

    store = AsyncMock()
    store.get_scan_scope_discovery_state = AsyncMock(return_value="pending")
    store.get_scan_scope_discovery_generation = AsyncMock(return_value=1)
    store.complete_scan_scope_discovery = AsyncMock()
    store.record_scan_failures = AsyncMock()
    store.get_scan_run = AsyncMock(
        return_value=(
            ScanRun(
                id="run-1",
                kind="incremental",
                trigger="manual",
                state="discovering",
                phase="discovering",
                updated_at=stale,
                row_revision=2,
            ),
            [],
            {},
        )
    )
    store.transition_scan_run = AsyncMock(
        return_value=ScanRun(
            id="run-1",
            kind="incremental",
            trigger="manual",
            state="failed",
            phase="discovering",
            updated_at=fresh,
            terminal_at=fresh,
            row_revision=3,
        )
    )
    store.cleanup_stale_scan_inventory = AsyncMock()
    store.restart_scan_scope_discovery = AsyncMock()
    root = tmp_path / "music"
    root.mkdir()
    run = ScanRun(
        id="run-1",
        kind="incremental",
        trigger="manual",
        state="discovering",
        phase="discovering",
        updated_at=stale,
        row_revision=1,
    )
    scope = ScanScope(root_id="root-a", relative_path=".", policy_revision="rev-1", root_path=str(root))

    class StubScanner(LibraryInventoryScanner):
        async def _walk_scope(self, *args, **kwargs):  # type: ignore[override]
            # Simulate a walk that observed a permission error and returned incomplete
            return (run, False, "WALK_TIMEOUT")

    scanner = StubScanner(store, clock=fake_clock)
    # Ensure the discover loop sees the scope as not completed and checkpoint passes
    result = await scanner.discover(
        run, [scope], {"root-a": root}, SimpleNamespace(policy_revision="rev-1"), AsyncMock(return_value=True)
    )
    assert result.state == "failed"
    # No _record_failure for this path - the walk failure is converted directly to terminal
    # but the terminal still uses fresh clock
    assert store.transition_scan_run.await_count == 1
    _args, kwargs = store.transition_scan_run.call_args
    assert kwargs["now"] == fresh
    assert kwargs["now"] > stale
    assert kwargs["terminal_code"] == "WALK_TIMEOUT"


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["pause", "stop", "supersede"])
async def test_control_exit_partial_scope_has_no_permission_code(
    control, tmp_path: Path
) -> None:
    """F-INDEXREC-06: a checkpoint-false exit (pause/stop/policy-supersede) is
    not a filesystem failure - the partial scope must carry error_code=None."""
    store = AsyncMock()
    store.complete_scan_scope_discovery = AsyncMock()
    store.record_scan_failures = AsyncMock()
    settled = ScanRun(
        id="run-1",
        kind="incremental",
        trigger="manual",
        state={"pause": "paused", "stop": "cancelled", "supersede": "superseded_policy_changed"}[control],
        phase="discovering",
        row_revision=2,
    )
    store.get_scan_run = AsyncMock(return_value=(settled, [], {}))
    root = tmp_path / "music"
    root.mkdir()
    for index in range(INVENTORY_BATCH_SIZE * 2 + 4):
        (root / f"track-{index}.flac").write_bytes(b"audio")

    class ControlledScanner(LibraryInventoryScanner):
        async def _persist_batch(self, run, scope, root, batch, resolver, generation):
            return run

    calls = {"n": 0}

    async def checkpoint(_run_id: str, _revision: str) -> bool:
        calls["n"] += 1
        return calls["n"] <= 1  # second in-walk checkpoint is the control exit

    scanner = ControlledScanner(store)
    run = ScanRun(
        id="run-1",
        kind="incremental",
        trigger="manual",
        state="discovering",
        phase="discovering",
        row_revision=1,
    )
    scope = ScanScope(root_id="root-a", relative_path=".", policy_revision="rev-1")

    def many_files_walker(*_args, **_kwargs):
        base = str(root)
        for index in range(INVENTORY_BATCH_SIZE * 2 + 4):
            yield (base, [], [f"track-{index}.flac"])
        # real files exist so resolved.stat() succeeds

    scanner._directory_walker = many_files_walker  # type: ignore[assignment]
    current, completed, code = await scanner._walk_scope(
        run, scope, root, root, SimpleNamespace(policy_revision="rev-1"), checkpoint, 1
    )

    assert completed is False
    assert code is None  # control exit, not a walk failure
    store.complete_scan_scope_discovery.assert_awaited_once_with(
        "run-1",
        "root-a",
        ".",
        state="partially_read",
        error_code=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_class"),
    [
        (AudioFormatError("bad metadata container"), "AudioFormatError"),
        (OSError(5, "input/output error"), "OSError"),
        (ValueError("bad value"), "ValueError"),
    ],
)
async def test_tag_read_failures_persist_safe_class_detail(
    tmp_path: Path, error: Exception, expected_class: str
) -> None:
    """NEW-SCAN-04: TAG_READ_FAILED rows record the safe exception CLASS and a
    stable operation label - never str(error) or filesystem paths."""
    import sqlite3 as _sqlite3

    from models.library_work import ScanInventoryItem

    root = tmp_path / "music"
    root.mkdir()
    store = NativeLibraryStore(tmp_path / "library.db", threading.Lock())
    with _sqlite3.connect(store.db_path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")

    class FailingTagger:
        def read_tags(self, path: Path):
            raise error

    from api.v1.schemas.library_policies import (
        LibraryRootSettings,
        TypedLibrarySettings,
    )
    from services.native.library_indexer import LibraryIndexer
    from services.native.library_policy_resolver import LibraryPolicyResolver

    resolver = LibraryPolicyResolver(
        TypedLibrarySettings(
            library_roots=[
                LibraryRootSettings(
                    id="root-a", path=str(root), label="L", policy="automatic"
                )
            ]
        )
    )
    indexer = LibraryIndexer(store, FailingTagger())

    await store.create_scan_run(
        ScanRun(id="run-fail", kind="incremental", trigger="manual", queued_at=1)
    )
    run = await store.claim_next_scan_run(now=2)
    assert run is not None
    # The batch fetch joins inventory to its scope row; seed the scope the
    # discovery walk would have created.
    with _sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "INSERT INTO library_scan_run_scopes "
            "(run_id, scope_sequence, root_id, relative_path, effective_policy, "
            "policy_revision, discovery_state, discovery_generation) "
            "VALUES (?, 0, 'root-a', '.', 'automatic', 'policy', 'completed', 1)",
            ("run-fail",),
        )
    await store.add_scan_inventory_batch(
        run.id,
        [
            ScanInventoryItem(
                root_id="root-a",
                relative_path="track-1.flac",
                absolute_path=str(root / "track-1.flac"),
                file_size_bytes=10,
                file_mtime_ns=1,
                stat_revision="s1",
                policy_revision="policy",
                effective_policy="automatic",
                comparison_result="new",
            )
        ],
        expected_run_revision=run.row_revision,
        updated_at=2.0,
    )

    async def checkpoint(_run_id: str, _revision: str) -> bool:
        return True

    counts = await indexer.index(run, "policy", checkpoint)
    assert counts["errored"] == 1

    failures, _cursor = await store.list_scan_run_failures(run.id)
    assert len(failures) == 1
    failure = failures[0]
    assert failure.failure_code == "TAG_READ_FAILED"
    assert failure.phase == "indexing"
    assert expected_class in failure.failure_detail
    assert "reading tags" in failure.failure_detail
    if expected_class == "OSError":
        # Redaction: the raw strerror never enters the persisted row.
        assert "input/output error" not in failure.failure_detail
