"""Safe PASSIVE WAL checkpoint policy and background-producer backpressure.

(GH-293) The owner-approved calibration (2026-08-20) governs everything here:

- PASSIVE checkpoint cadence: 30 s; reader-blocked bound: 60 s
- active-WAL (uncheckpointed frames) high water: 64 MiB; low water: 16 MiB
- when high water is crossed, or a PASSIVE checkpoint can make no measurable
  progress for the reader-blocked bound, only *background producers* suspend;
  foreground writes are never gated
- ``PRAGMA wal_checkpoint(PASSIVE)`` is the only checkpoint mode used; live
  ``TRUNCATE``/``RESTART`` are never issued from a request, startup, healthcheck,
  or worker path

Active WAL (uncheckpointed frames) is distinguished from physical file
allocation: ``PRAGMA wal_checkpoint(PASSIVE)`` returns (busy, log_frames,
checkpointed_frames); active bytes = (log_frames - checkpointed_frames) * page
size, while the ``-wal`` file size on disk is allocation and may include frames
already checkpointed but not yet reset.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from core.task_registry import TaskRegistry
from infrastructure.persistence.gh293_calibration import (
    ACTIVE_WAL_HIGH_WATER_BYTES,
    ACTIVE_WAL_LOW_WATER_BYTES,
    CHECKPOINT_CADENCE_SECONDS,
    CHECKPOINT_READER_BLOCKED_MAX_SECONDS,
)

logger = logging.getLogger(__name__)

WAL_CHECKPOINT_TASK_NAME = "target-sqlite-wal-checkpoint"


class WalCheckpointService:
    """Bounded PASSIVE checkpoint policy with cardinality-free telemetry."""

    def __init__(
        self,
        db_path: Path,
        *,
        high_water_bytes: int = ACTIVE_WAL_HIGH_WATER_BYTES,
        low_water_bytes: int = ACTIVE_WAL_LOW_WATER_BYTES,
        cadence_seconds: float = CHECKPOINT_CADENCE_SECONDS,
        reader_blocked_max_seconds: float = CHECKPOINT_READER_BLOCKED_MAX_SECONDS,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db_path = Path(db_path)
        self._high_water_bytes = high_water_bytes
        self._low_water_bytes = low_water_bytes
        self._cadence_seconds = max(1.0, cadence_seconds)
        self._reader_blocked_max_seconds = reader_blocked_max_seconds
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._suspended = False
        self._busy_since: float | None = None
        self._last_log_frames: int | None = None
        self._last_checkpointed_frames: int | None = None
        self._last_checkpoint_at: float | None = None
        self._last_outcome: dict[str, object] | None = None

    @property
    def background_suspended(self) -> bool:
        """True when background producers must yield (never gates foreground)."""
        with self._lock:
            return self._suspended

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._last_outcome or {})

    def run_once(self) -> dict[str, object]:
        """One bounded PASSIVE checkpoint pass; returns the outcome record."""
        started = self._monotonic()
        connection = sqlite3.connect(self._db_path)
        try:
            connection.execute("PRAGMA busy_timeout=0")
            # An in-flight foreground writer can make PASSIVE fail fast with
            # "database is locked" even though PASSIVE normally reports busy via
            # its return tuple. ONLY lock/busy errors convert to a busy pass;
            # other OperationalErrors are recorded distinctly and leave the
            # backpressure state untouched while the loop survives.
            try:
                busy, log_frames, checkpointed_frames = connection.execute(
                    "PRAGMA wal_checkpoint(PASSIVE)"
                ).fetchone()
                measured = True
            except sqlite3.OperationalError as error:
                message = str(error).lower()
                if "locked" in message or "busy" in message:
                    logger.warning(
                        "WAL checkpoint busy while a writer holds a lock: %s", error
                    )
                    busy, log_frames, checkpointed_frames = 1, 0, 0
                    # F-181: a lock-error pass carries no frame evidence; the
                    # fabricated zeros must not become the progress baseline.
                    measured = False
                else:
                    logger.exception("WAL checkpoint failed with a non-lock error")
                    return {
                        "at": self._clock(),
                        "busy": -1,
                        "log_frames": 0,
                        "checkpointed_frames": 0,
                        "active_bytes": -1,
                        "wal_file_bytes": 0,
                        "duration_seconds": self._monotonic() - started,
                        "error": str(error),
                    }
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            if measured:
                active_frames = max(
                    0, int(log_frames) - int(checkpointed_frames)
                )
                active_bytes = active_frames * int(page_size)
            else:
                # F-181: unmeasured pass; -1 mirrors the non-lock error branch
                # and never crosses the high/low water marks.
                active_bytes = -1
            duration = self._monotonic() - started
            wal_path = Path(str(self._db_path) + "-wal")
            wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
            outcome: dict[str, object] = {
                "at": self._clock(),
                "busy": int(busy),
                "log_frames": int(log_frames),
                "checkpointed_frames": int(checkpointed_frames),
                "active_bytes": active_bytes,
                "wal_file_bytes": wal_bytes,
                "duration_seconds": duration,
            }
            outcome.update(
                self._update_state(
                    busy=int(busy),
                    log_frames=int(log_frames),
                    checkpointed_frames=int(checkpointed_frames),
                    active_bytes=active_bytes,
                    now=self._monotonic(),
                    measured=measured,
                )
            )
            with self._lock:
                self._last_outcome = outcome
                self._last_checkpoint_at = self._clock()
            return outcome
        finally:
            connection.close()

    def _update_state(
        self,
        *,
        busy: int,
        log_frames: int,
        checkpointed_frames: int,
        active_bytes: int,
        now: float,
        measured: bool = True,
    ) -> dict[str, object]:
        """Backpressure state machine (pure, deterministic, unit-testable).

        - Suspension triggers: active WAL above the high water, or a PASSIVE
          pass that made no measurable progress for the reader-blocked bound
          (PASSIVE reports ``busy`` when a reader/writer lock contender blocks
          it; modern SQLite reports this rarely, so the water branch is the
          primary mechanism and this branch is the stall guard).
        - Measurable progress = the log shrank or checkpointed frames advanced
          since the previous pass.
        - Resume (while suspended): active at/below the low water, or any
          measurable progress.
        - Only the ``suspended`` flag is exposed to background producers;
          foreground writes are never gated.
        """
        with self._lock:
            if measured:
                progress = (
                    self._last_log_frames is not None
                    and log_frames < self._last_log_frames
                ) or (
                    self._last_checkpointed_frames is not None
                    and checkpointed_frames > self._last_checkpointed_frames
                )
                self._last_log_frames = log_frames
                self._last_checkpointed_frames = checkpointed_frames
            else:
                # F-181: an unmeasured (lock-error) pass never feeds the
                # fabricated zeros into the comparison baseline, never counts
                # as progress, and cannot clear suspension via a water mark.
                progress = False
            if busy:
                if self._busy_since is None:
                    self._busy_since = now
                reader_blocked_for = now - self._busy_since
            else:
                self._busy_since = None
                reader_blocked_for = 0.0
            if (
                active_bytes > self._high_water_bytes
                or (
                    self._busy_since is not None
                    and reader_blocked_for >= self._reader_blocked_max_seconds
                )
            ):
                if not self._suspended:
                    logger.warning(
                        "WAL backpressure: suspending background producers "
                        "(active_bytes=%d busy=%d reader_blocked_seconds=%.1f)",
                        active_bytes,
                        busy,
                        reader_blocked_for,
                    )
                self._suspended = True
            elif (
                self._suspended
                and (
                    (0 <= active_bytes <= self._low_water_bytes)
                    or progress
                )
            ):
                logger.info(
                    "WAL backpressure cleared: resuming background producers "
                    "(active_bytes=%d)",
                    active_bytes,
                )
                self._suspended = False
            return {
                "suspended": self._suspended,
                "reader_blocked_seconds": reader_blocked_for,
                "progress": progress,
            }

    async def run_forever(self) -> None:
        """Registered checkpoint loop: one sleep per iteration, no live TRUNCATE.

        The synchronous PASSIVE pass is offloaded with ``asyncio.to_thread`` so
        a large checkpoint can never stall the event loop (heartbeat and route
        responsiveness are preserved while the WAL drains).
        """
        while True:
            try:
                await asyncio.to_thread(self.run_once)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 - a checkpoint failure must not kill the task
                logger.exception("WAL checkpoint iteration failed")
            try:
                await asyncio.sleep(self._cadence_seconds)
            except asyncio.CancelledError:
                break

    async def wait_until_ready(self) -> None:
        """Admission helper for background producers.

        Yields while backpressure suspends background work; the caller re-checks
        per unit. Returns immediately when not suspended.
        """
        while self.background_suspended:
            await asyncio.sleep(1.0)
            if not self.background_suspended:
                return


def start_target_wal_checkpoint_task(
    service: WalCheckpointService,
) -> asyncio.Task[None]:
    """Start the registered WAL checkpoint task (single sleep per iteration)."""
    name = WAL_CHECKPOINT_TASK_NAME

    def _log_error(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "%s stopped unexpectedly",
                name,
                exc_info=(type(error), error, error.__traceback__),
            )

    task = asyncio.create_task(service.run_forever())
    TaskRegistry.get_instance().register(name, task)
    task.add_done_callback(_log_error)
    return task
