"""Stable target supervisor that resolves settings-dependent services each tick."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from core.task_registry import TaskRegistry
from infrastructure.queue.durable_work_wakeup import DurableWorkWakeups
from services.native.library_policy_resolver import LibraryPolicyResolver
from services.native.library_scan_coordinator import LibraryScanCoordinator
from services.native.library_scan_scheduler import LibraryAutomaticScanScheduler

logger = logging.getLogger(__name__)
EMPTY_RECOVERY_INTERVAL_SECONDS = 47.0
ERROR_RETRY_INTERVAL_SECONDS = 1.0
SUPERVISOR_TASK_NAME = "target-library-scan-supervisor"


def _log_supervisor_error(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error(
            "Target scan supervisor stopped unexpectedly",
            exc_info=(type(error), error, error.__traceback__),
        )


def start_target_scan_supervisor(
    coordinator_getter: Callable[[], LibraryScanCoordinator],
    root_paths_getter: Callable[[], dict[str, Path]],
    work_wakeups: DurableWorkWakeups,
    *,
    scheduler_getter: Callable[[], LibraryAutomaticScanScheduler] | None = None,
    resolver_getter: Callable[[], LibraryPolicyResolver] | None = None,
    schedule_settings_getter: Callable[[], dict[str, str]] | None = None,
) -> asyncio.Task[None]:
    registry = TaskRegistry.get_instance()
    if registry.is_running(SUPERVISOR_TASK_NAME):
        raise RuntimeError(f"Task '{SUPERVISOR_TASK_NAME}' is already running")
    task = asyncio.create_task(
        supervise_target_scans(
            coordinator_getter,
            root_paths_getter,
            work_wakeups,
            scheduler_getter,
            resolver_getter,
            schedule_settings_getter,
        )
    )
    registry.register(SUPERVISOR_TASK_NAME, task)
    task.add_done_callback(_log_supervisor_error)
    return task


async def supervise_target_scans(
    coordinator_getter: Callable[[], LibraryScanCoordinator],
    root_paths_getter: Callable[[], dict[str, Path]],
    work_wakeups: DurableWorkWakeups | None = None,
    scheduler_getter: Callable[[], LibraryAutomaticScanScheduler] | None = None,
    resolver_getter: Callable[[], LibraryPolicyResolver] | None = None,
    schedule_settings_getter: Callable[[], dict[str, str]] | None = None,
    now_getter: Callable[[], datetime] = lambda: datetime.now().astimezone(),
) -> None:
    wakeups = work_wakeups or DurableWorkWakeups()
    try:
        # None resolver getter means the scheduler is not wired up; the library
        # is treated as enabled so plain supervisor deployments keep working.
        if resolver_getter is None or resolver_getter().settings.enabled:
            await coordinator_getter().recover()
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001 - startup recovery failure must not kill the supervisor
        logger.exception("Target scan startup recovery failed")
    while True:
        revision = wakeups.revision("scan")
        processed = False
        wait_seconds = EMPTY_RECOVERY_INTERVAL_SECONDS
        try:
            coordinator = coordinator_getter()
            resolver = resolver_getter() if resolver_getter is not None else None
            enabled = resolver is None or resolver.settings.enabled
            if (
                enabled
                and scheduler_getter is not None
                and resolver is not None
                and schedule_settings_getter is not None
            ):
                schedule = schedule_settings_getter()
                await scheduler_getter().tick(
                    coordinator,
                    resolver,
                    frequency=schedule["frequency"],
                    daily_time=schedule["daily_time"],
                    timezone_name=schedule["timezone_name"],
                    now=now_getter(),
                )
            if enabled:
                processed = await coordinator.run_once(root_paths_getter()) is not None
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 - the lifetime supervisor records and survives run failures
            logger.exception("Target scan supervisor iteration failed")
            wait_seconds = ERROR_RETRY_INTERVAL_SECONDS
        if processed:
            continue
        try:
            await wakeups.wait(
                "scan", after_revision=revision, timeout_seconds=wait_seconds
            )
        except asyncio.CancelledError:
            break
