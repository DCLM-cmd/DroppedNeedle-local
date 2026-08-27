"""Background workers owned by the target-only application process."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import socket
import time
from collections.abc import Awaitable, Callable

from core.exceptions import StaleRevisionError
from core.task_registry import TaskRegistry
from infrastructure.queue.durable_work_wakeup import DurableWorkWakeups
from infrastructure.resilience.retry import CircuitOpenError, CircuitState

from services.native.album_identification_service import AlbumIdentificationService
from services.native.identification_queue_service import IdentificationQueueService
from services.native.library_operation_supervisor import LibraryOperationSupervisor
from services.native.library_management_recovery_service import (
    LibraryManagementRecoveryService,
)
from services.native.background_workload_gate import BackgroundWorkloadGate
from services.native.library_contribution_verification_worker import (
    LibraryContributionVerificationWorker,
)

logger = logging.getLogger(__name__)
# Different bounded deadlines keep recovery passes from reopening four SQLite
# connections in the same event-loop tick after a quiet startup.
IDENTIFICATION_RECOVERY_INTERVAL_SECONDS = 31.0
OPERATION_RECOVERY_INTERVAL_SECONDS = 37.0
CONTRIBUTION_RECOVERY_INTERVAL_SECONDS = 43.0
ERROR_RETRY_INTERVAL_SECONDS = 1.0
PROVIDER_HEALTH_SWEEP_INTERVAL_SECONDS = 60.0
WATCHDOG_INTERVAL_SECONDS = 30.0

IDENTIFICATION_WORKER_TASK_NAME = "target-library-identification-worker"
OPERATION_WORKER_TASK_NAME = "target-library-operation-worker"
CONTRIBUTION_VERIFICATION_WORKER_TASK_NAME = "library-contribution-verification-worker"
TARGET_WORKER_WATCHDOG_TASK_NAME = "target-worker-watchdog"


def _worker_id(kind: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{kind}"


def _log_worker_error(task: asyncio.Task[None], *, name: str) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error(
            "%s stopped unexpectedly",
            name,
            exc_info=(type(error), error, error.__traceback__),
        )


def _job_log_context(job: dict | None) -> str:
    if job is None:
        return ""
    return f" [job={job.get('id')} album={job.get('local_album_id')}]"


async def run_target_identification_worker(
    queue_getter: Callable[[], IdentificationQueueService],
    service_getter: Callable[[], AlbumIdentificationService],
    *,
    worker_id: str | None = None,
    workload_gate: BackgroundWorkloadGate | None = None,
    work_wakeups: DurableWorkWakeups | None = None,
    provider_state_getter: Callable[[], CircuitState] | None = None,
    probe_provider: Callable[[], Awaitable[None]] | None = None,
    enabled_getter: Callable[[], bool] | None = None,
) -> None:
    owner = worker_id or _worker_id("identification")
    wakeups = work_wakeups or DurableWorkWakeups()
    last_provider_sweep_at = 0.0
    # F-056: the reset fires on the OPEN/HALF_OPEN -> CLOSED recovery edge
    # (plus once at startup), never on every idle tick where state is CLOSED.
    last_provider_state: CircuitState | None = None
    provider_reset_pending = True
    while True:
        revision = wakeups.revision("identification")
        processed = False
        wait_seconds = IDENTIFICATION_RECOVERY_INTERVAL_SECONDS
        queue = queue_getter()
        job: dict | None = None
        try:
            if (enabled_getter is None or enabled_getter()) and (
                workload_gate is None or not workload_gate.scan_active
            ):
                await queue.recover()
                if not await queue.is_paused():
                    if workload_gate is None or not workload_gate.scan_active:
                        job = await queue.claim(owner)
                    if job is not None:
                        await service_getter().run_claimed_job(job, owner)
                        processed = True
        except CircuitOpenError as exc:
            logger.exception(
                "Target identification worker iteration failed%s",
                _job_log_context(job),
            )
            retry_after = getattr(exc, "retry_after_seconds", None)
            try:
                candidate = float(retry_after) if retry_after is not None else 0.0
                if math.isfinite(candidate) and candidate > 0:
                    wait_seconds = max(ERROR_RETRY_INTERVAL_SECONDS, candidate)
                else:
                    wait_seconds = ERROR_RETRY_INTERVAL_SECONDS
            except (TypeError, ValueError):
                wait_seconds = ERROR_RETRY_INTERVAL_SECONDS
            if job is not None:
                try:
                    retry_after_for_defer = None
                    try:
                        ra = float(retry_after) if retry_after is not None else None
                        if ra is not None and math.isfinite(ra) and ra > 0:
                            retry_after_for_defer = ra
                    except (TypeError, ValueError):
                        retry_after_for_defer = None
                    await queue.defer(
                        job,
                        owner,
                        "PROVIDER_TEMPORARILY_UNAVAILABLE",
                        retry_after_seconds=retry_after_for_defer,
                    )
                except Exception:  # noqa: BLE001 - a crashed job must not kill the worker
                    logger.exception("Failed to defer crashed identification job")
        except StaleRevisionError as exc:
            # F-060: files legitimately changing mid-run is an expected race
            # the revision checks exist for - defer under its own code instead
            # of burning the crash budget as UNEXPECTED_ERROR.
            logger.warning(
                "Target identification job hit stale input%s: %s",
                _job_log_context(job),
                exc,
            )
            if job is not None:
                try:
                    await queue.defer(job, owner, "STALE_INPUT")
                except Exception:  # noqa: BLE001 - a crashed job must not kill the worker
                    logger.exception("Failed to defer stale identification job")
            wait_seconds = ERROR_RETRY_INTERVAL_SECONDS
        except Exception:  # noqa: BLE001 - a durable worker must survive one failed item
            logger.exception(
                "Target identification worker iteration failed%s",
                _job_log_context(job),
            )
            if job is not None:
                try:
                    await queue.defer(job, owner, "UNEXPECTED_ERROR")
                except Exception:  # noqa: BLE001 - a crashed job must not kill the worker
                    logger.exception("Failed to defer crashed identification job")
            wait_seconds = ERROR_RETRY_INTERVAL_SECONDS
        if processed:
            continue
        if provider_state_getter is not None:
            now = time.time()
            if now - last_provider_sweep_at >= PROVIDER_HEALTH_SWEEP_INTERVAL_SECONDS:
                last_provider_sweep_at = now
                try:
                    state = provider_state_getter()
                    recovered_edge = (
                        state is CircuitState.CLOSED
                        and (
                            provider_reset_pending
                            or last_provider_state
                            in (CircuitState.OPEN, CircuitState.HALF_OPEN)
                        )
                    )
                    if state is CircuitState.CLOSED and recovered_edge:
                        # Provider recovered (or startup): release provider-
                        # deferred jobs per F-056's bounded-reset policy.
                        await queue_getter().reset_provider_deferrals(now=now)
                        provider_reset_pending = False
                    elif state is CircuitState.HALF_OPEN and probe_provider is not None:
                        # Without traffic the breaker would sit HALF_OPEN forever;
                        # one bounded background probe resolves it either way.
                        await probe_provider()
                    last_provider_state = state
                except Exception:  # noqa: BLE001 - a durable worker must survive one failed item
                    logger.exception("Identification provider health sweep failed")
        try:
            await wakeups.wait(
                "identification",
                after_revision=revision,
                timeout_seconds=wait_seconds,
            )
        except asyncio.CancelledError:
            break


async def run_target_operation_worker(
    supervisor_getter: Callable[[], LibraryOperationSupervisor],
    recovery_getter: Callable[[], LibraryManagementRecoveryService] | None = None,
    *,
    worker_id: str | None = None,
    work_wakeups: DurableWorkWakeups | None = None,
    enabled_getter: Callable[[], bool] | None = None,
) -> None:
    owner = worker_id or _worker_id("operation")
    wakeups = work_wakeups or DurableWorkWakeups()
    while True:
        revision = wakeups.revision("operation")
        processed = False
        wait_seconds = OPERATION_RECOVERY_INTERVAL_SECONDS
        try:
            if enabled_getter is None or enabled_getter():
                supervisor = supervisor_getter()
                await supervisor.recover()
                if recovery_getter is not None:
                    await recovery_getter().recover_once()
                processed = await supervisor.run_once(owner) is not None
        except Exception:  # noqa: BLE001 - a durable worker must survive one failed item
            logger.exception("Target operation worker iteration failed")
            wait_seconds = ERROR_RETRY_INTERVAL_SECONDS
        if processed:
            continue
        try:
            await wakeups.wait(
                "operation",
                after_revision=revision,
                timeout_seconds=wait_seconds,
            )
        except asyncio.CancelledError:
            break


async def run_library_contribution_verification_worker(
    worker_getter: Callable[[], LibraryContributionVerificationWorker],
    *,
    worker_id: str | None = None,
    work_wakeups: DurableWorkWakeups | None = None,
) -> None:
    owner = worker_id or _worker_id("library-contribution-verification")
    wakeups = work_wakeups or DurableWorkWakeups()
    while True:
        revision = wakeups.revision("contribution")
        processed = False
        wait_seconds = CONTRIBUTION_RECOVERY_INTERVAL_SECONDS
        try:
            worker = worker_getter()
            await worker.recover()
            processed = await worker.run_once(owner) is not None
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 - a durable worker must survive one failed item
            logger.exception(
                "Library contribution verification worker iteration failed"
            )
            wait_seconds = ERROR_RETRY_INTERVAL_SECONDS
        if processed:
            continue
        try:
            await wakeups.wait(
                "contribution",
                after_revision=revision,
                timeout_seconds=wait_seconds,
            )
        except asyncio.CancelledError:
            break


async def run_target_worker_watchdog(
    starters: dict[str, Callable[[], asyncio.Task[None]]],
    *,
    interval_seconds: float = WATCHDOG_INTERVAL_SECONDS,
) -> None:
    """Restart target workers whose tasks died (crashes auto-unregister them)."""
    registry = TaskRegistry.get_instance()
    while True:
        try:
            for name, starter in starters.items():
                if not registry.is_running(name):
                    logger.warning("Restarting stopped target worker %s", name)
                    starter()
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 - the supervisor must survive a failed restart
            logger.exception("Target worker watchdog iteration failed")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


def start_target_identification_worker(
    queue_getter: Callable[[], IdentificationQueueService],
    service_getter: Callable[[], AlbumIdentificationService],
    work_wakeups: DurableWorkWakeups,
    workload_gate: BackgroundWorkloadGate | None = None,
    provider_state_getter: Callable[[], CircuitState] | None = None,
    probe_provider: Callable[[], Awaitable[None]] | None = None,
    enabled_getter: Callable[[], bool] | None = None,
) -> asyncio.Task[None]:
    name = IDENTIFICATION_WORKER_TASK_NAME
    task = asyncio.create_task(
        run_target_identification_worker(
            queue_getter,
            service_getter,
            workload_gate=workload_gate,
            work_wakeups=work_wakeups,
            provider_state_getter=provider_state_getter,
            probe_provider=probe_provider,
            enabled_getter=enabled_getter,
        )
    )
    TaskRegistry.get_instance().register(name, task)
    task.add_done_callback(lambda item: _log_worker_error(item, name=name))
    return task


def start_target_operation_worker(
    supervisor_getter: Callable[[], LibraryOperationSupervisor],
    work_wakeups: DurableWorkWakeups,
    recovery_getter: Callable[[], LibraryManagementRecoveryService] | None = None,
    enabled_getter: Callable[[], bool] | None = None,
) -> asyncio.Task[None]:
    name = OPERATION_WORKER_TASK_NAME
    task = asyncio.create_task(
        run_target_operation_worker(
            supervisor_getter,
            recovery_getter,
            work_wakeups=work_wakeups,
            enabled_getter=enabled_getter,
        )
    )
    TaskRegistry.get_instance().register(name, task)
    task.add_done_callback(lambda item: _log_worker_error(item, name=name))
    return task


def start_library_contribution_verification_worker(
    worker_getter: Callable[[], LibraryContributionVerificationWorker],
    work_wakeups: DurableWorkWakeups,
) -> asyncio.Task[None]:
    name = CONTRIBUTION_VERIFICATION_WORKER_TASK_NAME
    task = asyncio.create_task(
        run_library_contribution_verification_worker(
            worker_getter, work_wakeups=work_wakeups
        )
    )
    TaskRegistry.get_instance().register(name, task)
    task.add_done_callback(lambda item: _log_worker_error(item, name=name))
    return task


def start_target_worker_watchdog(
    starters: dict[str, Callable[[], asyncio.Task[None]]],
) -> asyncio.Task[None]:
    name = TARGET_WORKER_WATCHDOG_TASK_NAME
    task = asyncio.create_task(run_target_worker_watchdog(starters))
    TaskRegistry.get_instance().register(name, task)
    task.add_done_callback(lambda item: _log_worker_error(item, name=name))
    return task
