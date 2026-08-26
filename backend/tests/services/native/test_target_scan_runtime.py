from __future__ import annotations

import asyncio
import errno
import logging
import threading
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.task_registry import TaskRegistry
from infrastructure.sse_publisher import KEEPALIVE, SSEPublisher
from infrastructure.queue.durable_work_wakeup import DurableWorkWakeups
from models.library_work import ScanRun, ScanRunSnapshot, ScanScope
from services.compat.target_scan_service import TargetCompatScanService
from services.native.library_scan_events import LibraryScanEventPublisher
from services.native.library_inventory_scanner import LibraryInventoryScanner
from services.native.library_operation_supervisor import LibraryOperationSupervisor
from services.native.library_scan_scheduler import LibraryAutomaticScanScheduler
from services.native.library_policy_resolver import LibraryPolicyResolver
from api.v1.schemas.library_policies import (
    LibraryPathPolicyRule,
    LibraryRootSettings,
    TypedLibrarySettings,
)
from services.native.library_scan_supervisor import (
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
from infrastructure.resilience.retry import CircuitState
from services.native.background_workload_gate import BackgroundWorkloadGate


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
    scheduler.tick.assert_not_awaited()
    coordinator.run_once.assert_not_awaited()
    wakeups.wait.assert_awaited_once()


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

    assert completed is False
    assert failure_code == "ROOT_PERMISSION_DENIED"
    records = store.record_scan_failures.await_args.args[1]
    assert [
        (record.failure_code, record.relative_path, record.phase)
        for record in records
    ] == [("WALK_EACCES", "secret", "discovering")]
    store.complete_scan_scope_discovery.assert_awaited_once_with(
        "run-1",
        "root",
        ".",
        state="partially_read",
        error_code="ROOT_PERMISSION_DENIED",
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
