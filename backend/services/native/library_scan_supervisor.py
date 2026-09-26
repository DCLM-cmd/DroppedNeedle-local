"""Stable target supervisor that resolves settings-dependent services each tick."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from core.task_registry import TaskRegistry
from infrastructure.queue.durable_work_wakeup import DurableWorkWakeups
from models.library_work import ScanRequest, ScanScope
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
    dirty_scopes_getter: Callable[[], list[str]] | None = None,
    dirty_scopes_clearer: Callable[[list[str]], None] | None = None,
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
            dirty_scopes_getter=dirty_scopes_getter,
            dirty_scopes_clearer=dirty_scopes_clearer,
        )
    )
    try:
        registry.register(SUPERVISOR_TASK_NAME, task)
    except RuntimeError:
        # R-05: single-loop double-start is impossible (no await sits between
        # the is_running check above and register), so a register conflict
        # here means threads/reentrancy. Cancel the orphan just created - the
        # already-registered task keeps running - and re-raise. This starter
        # is synchronous, so the orphan cannot be awaited here; cancel plus
        # the standard error-logging done-callback consumes its outcome (a
        # cancelled task carries no retrievable exception, and the callback
        # returns early for cancelled tasks).
        task.cancel()
        task.add_done_callback(_log_supervisor_error)
        raise
    task.add_done_callback(_log_supervisor_error)
    return task


def _scopes_for_dirty_ids(
    resolver: LibraryPolicyResolver, scope_ids: list[str]
) -> list[ScanScope]:
    """Resolve Hook B dirty scope ids against current settings.

    Ids that no longer resolve (e.g. a removed root) are dropped by the
    caller: dirty marks are hints only, and removals converge through the
    policy-reconciliation flow instead.
    """
    wanted = set(scope_ids)
    scopes: list[ScanScope] = []
    for root in resolver.settings.library_roots:
        if root.id in wanted:
            scopes.append(
                ScanScope(
                    root_id=root.id,
                    scope_id=root.id,
                    relative_path=".",
                    root_path=root.path,
                    effective_policy=root.policy,
                    policy_revision=resolver.policy_revision,
                )
            )
        for rule in getattr(root, "rules", []):
            if rule.id in wanted:
                scopes.append(
                    ScanScope(
                        root_id=root.id,
                        scope_id=rule.id,
                        relative_path=rule.relative_path,
                        root_path=root.path,
                        effective_policy=rule.policy,
                        policy_revision=resolver.policy_revision,
                    )
                )
    return scopes


async def supervise_target_scans(
    coordinator_getter: Callable[[], LibraryScanCoordinator],
    root_paths_getter: Callable[[], dict[str, Path]],
    work_wakeups: DurableWorkWakeups | None = None,
    scheduler_getter: Callable[[], LibraryAutomaticScanScheduler] | None = None,
    resolver_getter: Callable[[], LibraryPolicyResolver] | None = None,
    schedule_settings_getter: Callable[[], dict[str, str]] | None = None,
    now_getter: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    *,
    dirty_scopes_getter: Callable[[], list[str]] | None = None,
    dirty_scopes_clearer: Callable[[list[str]], None] | None = None,
) -> None:
    wakeups = work_wakeups or DurableWorkWakeups()
    # S-01 Hook A inputs, captured during recovery so the one-shot below
    # reuses them without extra getter reads.
    hook_coordinator: LibraryScanCoordinator | None = None
    hook_resolver: LibraryPolicyResolver | None = None
    hook_enabled = False
    hook_recovered: list[object] | None = None
    try:
        coordinator = coordinator_getter()
        resolver = resolver_getter() if resolver_getter is not None else None
        enabled = resolver is None or resolver.settings.enabled
        hook_coordinator, hook_resolver, hook_enabled = coordinator, resolver, enabled
        if enabled:
            hook_recovered = await coordinator.recover()
        else:
            await coordinator.recover_stopping()
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001 - startup recovery failure must not kill the supervisor
        logger.exception("Target scan startup recovery failed")
    try:
        # S-01 Hook A (D5: every boot): one-shot startup reconciliation scan.
        # Guards mirror the loop gates below: enabled -> not-manual -> no
        # resumable/current run -> non-empty scheduled scopes. Every
        # request_run disposition is acceptable (conflict leaves queued work
        # alone); raced boots coalesce inside request_run. Best-effort: a
        # failure logs and the loop below still runs.
        if (
            hook_enabled
            and hook_coordinator is not None
            and hook_resolver is not None
            and scheduler_getter is not None
            and schedule_settings_getter is not None
            and schedule_settings_getter()["frequency"] != "manual"
            and hook_recovered is not None
            and not hook_recovered
            and not await hook_coordinator.current()
        ):
            hook_scopes = scheduler_getter().scheduled_scopes(hook_resolver)
            if hook_scopes:
                await hook_coordinator.request_run(
                    ScanRequest(
                        kind="incremental",
                        trigger="startup_resume",
                        policy_revision=hook_resolver.policy_revision,
                        scopes=hook_scopes,
                    )
                )
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001 - the startup scan is best-effort; the loop still runs
        logger.exception("Target scan startup reconciliation request failed")
    while True:
        revision = wakeups.revision("scan")
        processed = False
        wait_seconds = EMPTY_RECOVERY_INTERVAL_SECONDS
        try:
            coordinator = coordinator_getter()
            resolver = resolver_getter() if resolver_getter is not None else None
            enabled = resolver is None or resolver.settings.enabled
            # S-01 Hook B consumer: dirty scope marks left by settings saves.
            # Runs in manual mode too (after the enabled check only) - dirty
            # marks fire regardless of frequency. Marks clear only on a
            # non-conflict request (conflict keeps them for the next tick);
            # a crash loses them safely (hints only - the next rolling scan
            # converges).
            if (
                enabled
                and resolver is not None
                and dirty_scopes_getter is not None
                and dirty_scopes_clearer is not None
            ):
                dirty_ids = dirty_scopes_getter()
                if dirty_ids:
                    dirty_scopes = _scopes_for_dirty_ids(resolver, dirty_ids)
                    if not dirty_scopes:
                        dirty_scopes_clearer(dirty_ids)
                    else:
                        dirty_result = await coordinator.request_run(
                            ScanRequest(
                                kind="incremental",
                                trigger="policy_apply",
                                policy_revision=resolver.policy_revision,
                                scopes=dirty_scopes,
                            )
                        )
                        if dirty_result.disposition != "conflict":
                            dirty_scopes_clearer(dirty_ids)
            if (
                enabled
                and scheduler_getter is not None
                and resolver is not None
                and schedule_settings_getter is not None
            ):
                schedule = schedule_settings_getter()
                tick_scheduled = await scheduler_getter().tick(
                    coordinator,
                    resolver,
                    frequency=schedule["frequency"],
                    daily_time=schedule["daily_time"],
                    timezone_name=schedule["timezone_name"],
                    now=now_getter(),
                )
                if not tick_scheduled:
                    # S-05: not due, nothing to schedule, or an incompatible
                    # queued follow-up conflicted (rare and self-healing -
                    # retried next iteration, no backoff change, no wakeup).
                    logger.debug("Target scan scheduler tick did not start a run")
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
        except Exception:  # noqa: BLE001 - a failed wait must not kill the supervisor
            logger.exception("Target scan supervisor wait failed")
            await asyncio.sleep(ERROR_RETRY_INTERVAL_SECONDS)
