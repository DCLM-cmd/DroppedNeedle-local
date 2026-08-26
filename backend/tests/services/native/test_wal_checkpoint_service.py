"""GH-293 safe PASSIVE checkpoint policy tests against real SQLite files.

Covers bounded PASSIVE calls, active-versus-allocated WAL accounting,
high/low-water hysteresis, reader-blocked suspension, foreground-safe run loop,
and the no-live-TRUNCATE invariant.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native import wal_checkpoint_service as wal_module
from services.native.wal_checkpoint_service import WalCheckpointService


class _RecordingConnection(sqlite3.Connection):
    """Connection subclass that records every PRAGMA/statement executed."""

    def __init__(self, *args, recorded: list[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._recorded = recorded

    def execute(self, sql, *params):
        if self._recorded is not None:
            self._recorded.append(str(sql))
        return super().execute(sql, *params)


def _seed_rows(path: Path, count: int = 2000) -> None:
    with sqlite3.connect(path, factory=_RecordingConnection) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        # Keep uncheckpointed frames around so PASSIVE has measurable work.
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS local_artists (id TEXT PRIMARY KEY, "
            "display_name TEXT NOT NULL, folded_name TEXT NOT NULL, kind TEXT NOT NULL, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        connection.executemany(
            "INSERT OR IGNORE INTO local_artists VALUES (?,?,?,'group',1,1)",
            [
                (f"artist-{i:05d}", f"Artist {i:05d}", f"artist {i:05d}")
                for i in range(count)
            ],
        )


def _outcome_keys(outcome: dict[str, object]) -> set[str]:
    return set(outcome)


def test_passive_checkpoint_reports_active_and_allocated_wal(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE local_artists (id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
            "folded_name TEXT NOT NULL, kind TEXT NOT NULL, created_at REAL NOT NULL, "
            "updated_at REAL NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO local_artists VALUES (?,?,?,'group',1,1)",
            [(f"a{i}", f"A{i}", f"a{i}") for i in range(5000)],
        )
    service = WalCheckpointService(db)
    outcome = service.run_once()

    keys = _outcome_keys(outcome)
    assert {
        "at",
        "busy",
        "log_frames",
        "checkpointed_frames",
        "active_bytes",
        "wal_file_bytes",
        "duration_seconds",
        "suspended",
        "reader_blocked_seconds",
    } <= keys
    assert outcome["active_bytes"] >= 0
    assert outcome["active_bytes"] <= outcome["wal_file_bytes"]
    # A completed PASSIVE pass has checkpointed every available frame.
    assert outcome["checkpointed_frames"] == outcome["log_frames"]
    assert outcome["suspended"] is False


def test_passive_uses_only_passive_never_truncate(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "library.db"
    _seed_rows(db)
    recorded: list[str] = []
    monkeypatch.setattr(
        wal_module.sqlite3,
        "connect",
        lambda *args, **kwargs: _RecordingConnection(
            *args, recorded=recorded, **kwargs
        ),
    )
    service = WalCheckpointService(db)
    service.run_once()

    checkpoint_calls = [
        sql for sql in recorded if str(sql).upper().strip().startswith("PRAGMA WAL_CHECKPOINT")
    ]
    assert checkpoint_calls, "expected at least one wal_checkpoint"
    assert all("PASSIVE" in sql.upper() for sql in checkpoint_calls)
    assert not any("TRUNCATE" in sql.upper() or "RESTART" in sql.upper() for sql in recorded)


def test_high_water_backpressure_wiring_on_real_file(tmp_path: Path) -> None:
    """End-to-end wiring on a real shared SQLite file.

    A healthy PASSIVE pass reports truthful frame/WAL telemetry and surfaces the
    state-machine outcome keys. Modern SQLite drains every committed frame
    (active_bytes == 0 after a pass), so the deterministic high/low-water and
    stall-guard behavior is pinned at the pure state-machine boundary in
    test_reader_blocked_state_machine_suspends_and_recovers below; the outcome
    observed here is fed by the same ``_update_state`` the production loop uses.
    """
    db = tmp_path / "library.db"
    _seed_rows(db)
    service = WalCheckpointService(
        db,
        high_water_bytes=1024,
        low_water_bytes=1024,
        reader_blocked_max_seconds=10.0,
    )
    outcome = service.run_once()
    assert {"suspended", "reader_blocked_seconds", "progress"} <= set(outcome)
    assert outcome["suspended"] is False
    assert outcome["active_bytes"] == 0
    assert outcome["checkpointed_frames"] == outcome["log_frames"]
    assert outcome["wal_file_bytes"] > 0
    assert service.background_suspended is False
    # The recorded real pass becomes the baseline for the next decision.
    next_outcome = service.run_once()
    assert next_outcome["progress"] is False or next_outcome["active_bytes"] == 0


def test_high_low_water_hysteresis_state_machine() -> None:
    """Deterministic water-branch coverage of the owner-calibrated policy.

    Drive the pure state machine with real-data-shaped frame counts: crossing
    the 64 MiB high water suspends; a drain back to the 16 MiB low water (or
    measurable checkpoint progress) resumes; a partially drained WAL that stays
    above low water keeps the suspension.
    """
    high = 64 * 1024 * 1024
    low = 16 * 1024 * 1024
    service = WalCheckpointService(
        "unused.db",
        high_water_bytes=high,
        low_water_bytes=low,
        reader_blocked_max_seconds=3600.0,
    )
    first = service._update_state(
        busy=0, log_frames=10_000, checkpointed_frames=0,
        active_bytes=high + 1, now=0.0,
    )
    assert first["suspended"] is True
    # No checkpoint progress and active above low water: suspension holds.
    still = service._update_state(
        busy=0, log_frames=10_000, checkpointed_frames=0,
        active_bytes=(high + low) // 2, now=1.0,
    )
    assert still["progress"] is False
    assert still["suspended"] is True
    # Measurable progress (checkpointed advanced): resume per the calibration
    # contract even while active is above low water.
    resumed = service._update_state(
        busy=0, log_frames=10_000, checkpointed_frames=5_000,
        active_bytes=(high + low) // 2, now=2.0,
    )
    assert resumed["progress"] is True
    assert resumed["suspended"] is False
    # Fully drained to the low water: no progress needed to stay resumed.
    drained = service._update_state(
        busy=0, log_frames=10_000, checkpointed_frames=10_000,
        active_bytes=low, now=3.0,
    )
    assert drained["suspended"] is False
    assert service.background_suspended is False


def test_reader_blocked_state_machine_suspends_and_recovers() -> None:
    """The stall guard suspends after the bounded reader-blocked interval and
    resumes on measurable checkpoint progress (pure state machine; modern
    SQLite reports PASSIVE ``busy`` rarely, so the water branch is the primary
    real-file mechanism and this branch is the deterministic stall guard)."""
    service = WalCheckpointService(
        "unused.db",
        high_water_bytes=1024,
        low_water_bytes=1024,
        reader_blocked_max_seconds=10.0,
    )
    # A busy (= reader/writer-blocked) PASSIVE pass starts the blocked clock.
    first = service._update_state(busy=1, log_frames=100, checkpointed_frames=50, active_bytes=9_000_000, now=0.0)
    assert first["suspended"] is True  # high water triggers immediately
    # After high water clears but PASSIVE is still busy past the bounded
    # interval, the stall guard keeps the suspension.
    second = service._update_state(busy=1, log_frames=100, checkpointed_frames=50, active_bytes=100, now=12.0)
    assert second["suspended"] is True
    assert second["reader_blocked_seconds"] == 12.0
    # Measurable progress (checkpointed advanced) while under the bound resumes.
    resumed = service._update_state(busy=0, log_frames=100, checkpointed_frames=100, active_bytes=0, now=13.0)
    assert resumed["progress"] is True
    assert resumed["suspended"] is False
    assert service.background_suspended is False
    # No progress and active above the low water keeps a clean idle state off.
    idle = service._update_state(busy=0, log_frames=100, checkpointed_frames=100, active_bytes=50, now=14.0)
    assert idle["suspended"] is False


def test_run_once_handles_lock_contention_as_busy_pass(tmp_path: Path, monkeypatch) -> None:
    """An in-flight writer can make PASSIVE fail fast with 'database is locked';
    run_once records it as a busy pass instead of crashing (foreground never
    waits; background producers stay suspended when the bound is crossed)."""
    db = tmp_path / "library.db"
    _seed_rows(db)
    service = WalCheckpointService(
        db,
        high_water_bytes=1024,
        low_water_bytes=1024,
        reader_blocked_max_seconds=0.0,
    )
    class _LockedConnection(sqlite3.Connection):
        def execute(self, sql, *params):
            if str(sql).strip().upper().startswith("PRAGMA WAL_CHECKPOINT"):
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *params)

    monkeypatch.setattr(
        wal_module.sqlite3,
        "connect",
        lambda *args, **kwargs: _LockedConnection(*args, **kwargs),
    )
    outcome = service.run_once()  # must not raise
    assert outcome["busy"] == 1
    assert outcome["suspended"] is True  # busy + reader-blocked bound (0 s)


def test_run_once_keeps_non_lock_operational_errors_distinct(
    tmp_path: Path, monkeypatch,
) -> None:
    """Only lock/busy OperationalErrors convert to a busy pass; any other
    operational error is recorded distinctly and leaves the backpressure state
    untouched while the loop survives."""
    db = tmp_path / "library.db"
    _seed_rows(db)
    service = WalCheckpointService(db)

    class _BrokenConnection(sqlite3.Connection):
        def execute(self, sql, *params):
            if str(sql).strip().upper().startswith("PRAGMA WAL_CHECKPOINT"):
                raise sqlite3.OperationalError("malformed database schema")
            return super().execute(sql, *params)

    monkeypatch.setattr(
        wal_module.sqlite3,
        "connect",
        lambda *args, **kwargs: _BrokenConnection(*args, **kwargs),
    )
    outcome = service.run_once()  # must not raise
    assert outcome["busy"] == -1
    assert "error" in outcome and "malformed" in str(outcome["error"])
    assert service.background_suspended is False  # state untouched


@pytest.mark.asyncio
async def test_run_forever_offloads_checkpoint_to_thread(
    tmp_path: Path, monkeypatch,
) -> None:
    """The synchronous PASSIVE pass runs via asyncio.to_thread so a slow
    checkpoint cannot stall the event loop (heartbeat/route responsiveness)."""
    import threading as _threading

    db = tmp_path / "library.db"
    _seed_rows(db)
    service = WalCheckpointService(db, cadence_seconds=1.0)
    threads: list[str] = []

    def slow_run_once() -> dict[str, object]:
        threads.append(_threading.current_thread().name)
        time.sleep(0.2)
        return {"ok": True}

    monkeypatch.setattr(service, "run_once", slow_run_once)
    task = asyncio.create_task(service.run_forever())
    started = time.monotonic()
    await asyncio.sleep(0.05)  # would be delayed >=0.2s if the loop were blocked
    loop_latency = time.monotonic() - started
    assert loop_latency < 0.15  # event loop stayed responsive during the pass
    await asyncio.sleep(1.05)  # one 1 s cadence turn
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert threads, "checkpoint never ran"
    # Python 3.13 names the default-executor threads ``asyncio_N``; what matters
    # is the sync pass never ran on the event-loop (MainThread) thread.
    assert all(name != "MainThread" for name in threads)


@pytest.mark.asyncio
async def test_run_forever_survives_isolated_checkpoint_errors(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "library.db"
    _seed_rows(db)
    service = WalCheckpointService(db, cadence_seconds=1.0)
    real_run_once = service.run_once
    calls = [0]

    def wrapped_run_once():
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("injected checkpoint failure")
        return real_run_once()

    monkeypatch.setattr(service, "run_once", wrapped_run_once)
    task = asyncio.create_task(service.run_forever())
    # The cadence clamp is min 1 s; two full loop turns prove the task kept
    # running after the isolated failure.
    await asyncio.sleep(2.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls[0] >= 2  # the task kept looping after the isolated failure


def test_run_once_lock_error_keeps_measured_baseline(tmp_path: Path, monkeypatch) -> None:
    """F-181: a lock-error busy pass reports active_bytes=-1 and never feeds the
    fabricated zeros into the progress comparison baseline."""
    db = tmp_path / "library.db"
    _seed_rows(db)
    service = WalCheckpointService(
        db, high_water_bytes=1024, low_water_bytes=1024
    )
    measured = service.run_once()

    class _LockedConnection(sqlite3.Connection):
        def execute(self, sql, *params):
            if str(sql).strip().upper().startswith("PRAGMA WAL_CHECKPOINT"):
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *params)

    monkeypatch.setattr(
        wal_module.sqlite3,
        "connect",
        lambda *args, **kwargs: _LockedConnection(*args, **kwargs),
    )
    busy = service.run_once()
    assert busy["busy"] == 1
    assert busy["active_bytes"] == -1
    assert busy["progress"] is False
    # fabricated zeros did not become the comparison baseline
    assert service._last_log_frames == measured["log_frames"]
    assert service._last_checkpointed_frames == measured["checkpointed_frames"]

    monkeypatch.undo()
    following = service.run_once()
    expected = (
        following["log_frames"] < measured["log_frames"]
        or following["checkpointed_frames"] > measured["checkpointed_frames"]
    )
    assert following["progress"] is expected


def test_run_once_lock_error_does_not_clear_suspension(
    tmp_path: Path, monkeypatch
) -> None:
    """F-181: an unmeasured pass cannot resume background producers via the low
    water mark because it carries no real active-byte measurement."""
    db = tmp_path / "library.db"
    reader = sqlite3.connect(db)
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE t (x INTEGER)")
        writer.commit()
        # open a read transaction: its snapshot mark pins what PASSIVE may
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        writer.executemany(
            "INSERT INTO t VALUES (?)", [(value,) for value in range(4000)]
        )
        # commit AFTER the reader's snapshot: the new frames sit beyond the
        # reader mark and PASSIVE cannot copy them while the reader is open.
        writer.commit()
        service = WalCheckpointService(
            db, high_water_bytes=1024, low_water_bytes=0
        )
        suspended = service.run_once()
        assert suspended["active_bytes"] > 1024
        assert suspended["suspended"] is True

        class _LockedConnection(sqlite3.Connection):
            def execute(self, sql, *params):
                if str(sql).strip().upper().startswith("PRAGMA WAL_CHECKPOINT"):
                    raise sqlite3.OperationalError("database is locked")
                return super().execute(sql, *params)

        monkeypatch.setattr(
            wal_module.sqlite3,
            "connect",
            lambda *args, **kwargs: _LockedConnection(*args, **kwargs),
        )
        busy = service.run_once()
        assert busy["active_bytes"] == -1
        assert service.background_suspended is True
    finally:
        reader.close()
        writer.close()
