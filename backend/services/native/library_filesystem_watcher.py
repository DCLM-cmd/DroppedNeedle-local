"""S-01 Hook C: zero-dependency recursive filesystem poller (D6).

The poller takes a recursive stat-only snapshot of every library root once
per poll interval and, when a snapshot moves, enqueues a single
``kind="incremental"`` / ``trigger="automatic"`` scan after a batching
window so rapid bursts (saves, multi-file copies, rename storms) collapse
into one ``request_run``.

Recursion is load-bearing, not incidental: on Linux only the immediate
parent directory's mtime moves when a nested file changes, so a shallow
single-level poll misses nested mutations silently and permanently. The
walk is stat-only (no tag reads, no hashing) and runs in
``asyncio.to_thread`` so the event loop never blocks on directory I/O.

Known limitation (documented, accepted): changes that preserve both mtime
and size (``touch -r``, timestamp-preserving copies, same-size rewrites
inside the filesystem timestamp granularity) are invisible to a stat-only
snapshot and are picked up only by the rolling schedule or a manual scan.

Symlinks are never followed (``follow_symlinks=False`` throughout), so a
symlinked directory is recorded as a single entry and never descended.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from core.task_registry import TaskRegistry
from infrastructure.queue.durable_work_wakeup import DurableWorkWakeups
from models.library_work import ScanRequest
from services.native.library_policy_resolver import LibraryPolicyResolver
from services.native.library_scan_coordinator import LibraryScanCoordinator
from services.native.library_scan_scheduler import LibraryAutomaticScanScheduler

logger = logging.getLogger(__name__)

WATCHER_TASK_NAME = "target-library-filesystem-watcher"

DEFAULT_POLL_INTERVAL_SECONDS = 300.0
DEFAULT_BATCH_WINDOW_SECONDS = 60.0
_MIN_POLL_INTERVAL_SECONDS = 1.0


class WatcherSettings(NamedTuple):
    """Duck-compatible with the prefs ``LibraryScanFilesystemWatcherSettings``
    section (``enabled`` / ``poll_interval_seconds`` / ``batch_window_seconds``)
    so the loop can run unconfigured with Hook C defaults."""

    enabled: bool = True
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    batch_window_seconds: float = DEFAULT_BATCH_WINDOW_SECONDS


_DEFAULT_SETTINGS = WatcherSettings()


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int, bool]]:
    """Recursive stat-only snapshot of ``root`` keyed by root-relative path.

    Values are ``(st_mtime_ns, st_size, is_dir)``. An unreadable subdirectory
    is skipped (the stale comparison simply retries next poll); a missing
    top-level root raises ``OSError`` to the caller, which logs and keeps the
    previous baseline instead of scan-storming on a transient unmount.
    """
    snapshot: dict[str, tuple[int, int, bool]] = {}
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                entries = list(iterator)
        except OSError:
            if current == root:
                raise
            logger.debug("Filesystem watcher skipping unreadable directory: %s", current)
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            try:
                stat_result = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            try:
                relative = str(Path(entry.path).relative_to(root))
            except ValueError:  # pragma: no cover - scandir child outside root
                continue
            snapshot[relative] = (stat_result.st_mtime_ns, stat_result.st_size, is_dir)
            if is_dir:
                stack.append(Path(entry.path))
    return snapshot


def _log_watcher_error(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error(
            "Target filesystem watcher stopped unexpectedly",
            exc_info=(type(error), error, error.__traceback__),
        )


def start_library_filesystem_watcher(
    coordinator_getter: Callable[[], LibraryScanCoordinator],
    root_paths_getter: Callable[[], dict[str, Path]],
    work_wakeups: DurableWorkWakeups,
    *,
    scheduler_getter: Callable[[], LibraryAutomaticScanScheduler] | None = None,
    resolver_getter: Callable[[], LibraryPolicyResolver] | None = None,
    watcher_settings_getter: Callable[[], WatcherSettings] | None = None,
    clock_getter: Callable[[], float] = time.monotonic,
) -> asyncio.Task[None]:
    registry = TaskRegistry.get_instance()
    if registry.is_running(WATCHER_TASK_NAME):
        raise RuntimeError(f"Task '{WATCHER_TASK_NAME}' is already running")
    task = asyncio.create_task(
        watch_library_filesystem(
            coordinator_getter,
            root_paths_getter,
            work_wakeups,
            scheduler_getter=scheduler_getter,
            resolver_getter=resolver_getter,
            watcher_settings_getter=watcher_settings_getter,
            clock_getter=clock_getter,
        )
    )
    try:
        registry.register(WATCHER_TASK_NAME, task)
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
        task.add_done_callback(_log_watcher_error)
        raise
    task.add_done_callback(_log_watcher_error)
    return task


async def watch_library_filesystem(
    coordinator_getter: Callable[[], LibraryScanCoordinator],
    root_paths_getter: Callable[[], dict[str, Path]],
    work_wakeups: DurableWorkWakeups | None = None,
    *,
    scheduler_getter: Callable[[], LibraryAutomaticScanScheduler] | None = None,
    resolver_getter: Callable[[], LibraryPolicyResolver] | None = None,
    watcher_settings_getter: Callable[[], WatcherSettings] | None = None,
    clock_getter: Callable[[], float] = time.monotonic,
) -> None:
    """Poll library roots for filesystem mutations and batch them into scans.

    Every getter is re-read each iteration (never captured): a settings save
    rebuilds singletons, and the next poll must see the new instances.
    """
    wakeups = work_wakeups or DurableWorkWakeups()
    settings_getter = watcher_settings_getter or (lambda: _DEFAULT_SETTINGS)
    baselines: dict[str, tuple[str, dict[str, tuple[int, int, bool]]]] = {}
    pending_since: float | None = None
    while True:
        sleep_seconds = DEFAULT_POLL_INTERVAL_SECONDS
        try:
            settings = settings_getter()
            poll_interval = max(float(settings.poll_interval_seconds), _MIN_POLL_INTERVAL_SECONDS)
            window = max(float(settings.batch_window_seconds), 0.0)
            sleep_seconds = poll_interval
            coordinator = coordinator_getter()
            resolver = resolver_getter() if resolver_getter is not None else None
            enabled = bool(settings.enabled) and (
                resolver is None or resolver.settings.enabled
            )
            if (
                enabled
                and resolver is not None
                and scheduler_getter is not None
            ):
                now = clock_getter()
                for root_id, root_path in root_paths_getter().items():
                    snapshot = await asyncio.to_thread(_snapshot_tree, root_path)
                    previous = baselines.get(root_id)
                    if previous is None or previous[0] != str(root_path):
                        # First sighting (or a re-pointed root id): seed the
                        # baseline silently so a (re)start never scans a
                        # steady tree. Settings-driven root changes are
                        # already covered by Hook B dirty marks.
                        baselines[root_id] = (str(root_path), snapshot)
                        continue
                    if snapshot != previous[1]:
                        baselines[root_id] = (str(root_path), snapshot)
                        if pending_since is None:
                            pending_since = now
                if pending_since is not None:
                    elapsed = clock_getter() - pending_since
                    if elapsed >= window:
                        scopes = scheduler_getter().scheduled_scopes(resolver)
                        if not scopes:
                            logger.debug(
                                "Filesystem watcher dropping pending scan: no scheduled scopes"
                            )
                            pending_since = None
                        else:
                            result = await coordinator.request_run(
                                ScanRequest(
                                    kind="incremental",
                                    trigger="automatic",
                                    policy_revision=resolver.policy_revision,
                                    scopes=scopes,
                                )
                            )
                            logger.info(
                                "Filesystem watcher requested incremental scan "
                                "(disposition=%s)",
                                result.disposition,
                            )
                            pending_since = None
                            wakeups.notify("scan")
                    else:
                        sleep_seconds = min(poll_interval, window - elapsed)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 - the lifetime watcher records and survives iteration failures
            logger.exception("Target filesystem watcher iteration failed")
        try:
            await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            break
