"""Shared SQLite infrastructure for all persistence stores."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import contextvars
import functools
import json
import os
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Any, TypeVar

from infrastructure.persistence.connection_settings import (
    report_connection_settings,
)

T = TypeVar("T")


# Database work gets its own threads, separate from ``asyncio.to_thread``.
#
# ``asyncio.to_thread`` runs on the event loop's DEFAULT executor, which is only
# ``min(32, cpu_count + 4)`` threads - 8 on the 4-core server. That pool is shared
# with every heavy filesystem job in the app: hashing whole audio files, copying
# FLACs, walking the library tree, waiting on fpcalc. Those hold a thread for
# seconds at a time, and there are only eight, so a scan or an Organizer run could
# occupy all of them. Every API request needs the database - the auth check alone
# is a query - so requests then sat waiting for a thread rather than for data, and
# the UI showed a spinner that never resolved even though nothing was actually
# slow. Isolating database work means a busy library can no longer starve it.
#
# Sized independently of the heavy pool: these queries are short and sqlite3
# releases the GIL around them, so threads here are mostly idle. The pool also
# bounds the connection cache, which is per (thread, store).
_DB_EXECUTOR_WORKERS = min(12, (os.cpu_count() or 2) * 3)
_db_executor = ThreadPoolExecutor(
    max_workers=_DB_EXECUTOR_WORKERS, thread_name_prefix="droppedneedle-db"
)


async def _run_in_db_thread(function: Any, /, *args: Any, **kwargs: Any) -> Any:
    """``asyncio.to_thread``, but on the database pool.

    Copies the current context exactly as ``to_thread`` does, so anything reading a
    ContextVar (the degradation recorder, request-scoped flags) behaves the same.
    """
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    call = functools.partial(context.run, function, *args, **kwargs)
    return await loop.run_in_executor(_db_executor, call)


class PriorityWriteLock:
    """A foreground-first process lock with bounded background starvation."""

    def __init__(self, *, foreground_burst: int = 8) -> None:
        if foreground_burst < 1:
            raise ValueError("foreground_burst must be positive")
        self._condition = threading.Condition()
        self._foreground_burst = foreground_burst
        self._active = False
        self._foreground_waiters = 0
        self._background_waiters = 0
        self._foreground_grants = 0

    def __enter__(self) -> "PriorityWriteLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()

    def acquire(self) -> None:
        with self._condition:
            self._foreground_waiters += 1
            try:
                while self._active or (
                    self._background_waiters
                    and self._foreground_grants >= self._foreground_burst
                ):
                    self._condition.wait()
                self._active = True
                self._foreground_grants += 1
            finally:
                self._foreground_waiters -= 1

    def acquire_background(self) -> None:
        with self._condition:
            if self._background_waiters == 0:
                self._foreground_grants = 0
            self._background_waiters += 1
            try:
                while self._active or (
                    self._foreground_waiters
                    and self._foreground_grants < self._foreground_burst
                ):
                    self._condition.wait()
                self._active = True
                self._foreground_grants = 0
            finally:
                self._background_waiters -= 1

    def release(self) -> None:
        with self._condition:
            if not self._active:
                raise RuntimeError("Cannot release an unlocked persistence lock")
            self._active = False
            self._condition.notify_all()

    @contextmanager
    def background(self):
        self.acquire_background()
        try:
            yield self
        finally:
            self.release()


def _fold_text(value: Any) -> Any:
    """Casefold, strip diacritics, and normalize whitespace.

    Registered as the SQLite ``fold()`` function and applied to both column and
    pattern in LIKE searches, so library search is accent- and case-insensitive
    for keyboards that can't type the accent. NFKD also folds compatibility forms
    (ligatures, full-width chars) into their plain equivalents, which is desirable
    for forgiving search and matches the codebase's other search normalizers
    (search_service, plex/navidrome). Non-strings (incl. NULL) pass through
    unchanged so the surrounding LIKE keeps its normal semantics."""
    if not isinstance(value, str):
        return value
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    ).casefold()
    return " ".join(without_marks.split())


def _encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _decode_json(text: str) -> Any:
    return json.loads(text)


def _normalize(value: str | None) -> str:
    return value.lower() if isinstance(value, str) else ""


def _decode_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    decoded: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = _decode_json(row["raw_json"])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(payload, dict):
            decoded.append(payload)
    return decoded


def _safe_alter(conn: sqlite3.Connection, sql: str) -> bool:
    """Run an ``ALTER TABLE ... ADD COLUMN`` that may already have been applied.

    Returns True if the column was added, False if it already existed."""
    try:
        conn.execute(sql)
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise
        return False


class _ConnectionPool(threading.local):
    """Per-thread SQLite connections, reused across queries.

    Opening a connection is not free: it opens the database file, maps the WAL
    index, replays PRAGMAs and re-registers ``fold()``. Measured against the
    production library.db (55 MB data / 21 MB WAL) that is ~6.6 ms per query,
    against ~0.002 ms for a query on an already-open connection. Every store
    call used to pay it, so a page issuing a few dozen queries burned hundreds
    of milliseconds before touching a single row.

    Connections are per-thread (store work runs on the dedicated database pool of
    long-lived worker threads) and per (store class, db_path), so the
    subclass PRAGMAs applied in ``_connect`` - notably ``foreign_keys=ON``, which
    is a per-connection setting - stay attached to the connection they were set on.
    """

    def __init__(self) -> None:
        self.connections: dict[tuple[int, str], sqlite3.Connection] = {}
        self.generation = 0


_pool = _ConnectionPool()

# Bumped to invalidate every thread's pooled connections. A thread-local cannot be
# cleared from outside the thread that owns it, so instead each thread notices the
# generation moved and reopens on its next use.
_pool_generation = 0


def close_pooled_connections() -> None:
    """Close every pooled connection owned by the calling thread."""
    for conn in _pool.connections.values():
        try:
            conn.close()
        except sqlite3.Error:  # noqa: PERF203 - closing must never raise
            pass
    _pool.connections.clear()


def reset_connection_pool() -> None:
    """Make every thread reopen its connections the next time it needs one.

    For tests that swap out ``_connect`` (to trace statements, say) and need the
    replacement to actually be used rather than a connection opened earlier.
    """
    global _pool_generation
    _pool_generation += 1


class PooledSqliteStore:
    """Connection reuse and transaction hygiene for a SQLite-backed store.

    Subclasses supply ``db_path``, ``_write_lock`` and a ``_connect`` that builds a
    fresh connection with whatever PRAGMAs they need; everything else here is
    shared. ``PersistenceBase`` builds on this, and the older hand-rolled stores
    (auth, favorites, play history, ...) inherit it directly so they stop paying
    connection setup on every query too.
    """

    db_path: Path
    _write_lock: "threading.Lock | PriorityWriteLock"

    def _connect(self) -> sqlite3.Connection:
        raise NotImplementedError

    def _pooled_connection(self) -> sqlite3.Connection:
        """Borrow this thread's connection for this store, opening it on first use.

        ``_connect`` stays the "make me a fresh connection" hook so subclasses can
        keep layering PRAGMAs onto it and so ``_ensure_tables`` can own a private
        connection it is free to close.
        """
        if _pool.generation != _pool_generation:
            close_pooled_connections()
            _pool.generation = _pool_generation
        key = (id(type(self)), str(self.db_path))
        conn = _pool.connections.get(key)
        if conn is None:
            conn = self._connect()
            _pool.connections[key] = conn
        return conn

    def _discard_pooled_connection(self) -> None:
        key = (id(type(self)), str(self.db_path))
        conn = _pool.connections.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _run(self, operation: Any, *, commit: bool) -> Any:
        """Run one operation on the pooled connection, leaving it reusable.

        A pooled connection outlives the call, so it must never be handed back
        mid-transaction: a failed write is rolled back, and a read that turned out
        to write is rolled back too rather than holding a WAL snapshot open for
        the life of the thread. Anything that leaves the connection itself
        unusable retires it so the next call opens a healthy one.
        """
        conn = self._pooled_connection()
        try:
            result = operation(conn)
            if commit:
                conn.commit()
            elif conn.in_transaction:
                conn.rollback()
            return result
        except Exception:
            try:
                if conn.in_transaction:
                    conn.rollback()
            except sqlite3.Error:
                self._discard_pooled_connection()
            raise

    def _execute(self, operation: Any, write: bool) -> Any:
        if write:
            with self._write_lock:
                return self._run(operation, commit=True)
        return self._run(operation, commit=False)

    async def _read(self, operation: Any) -> Any:
        return await _run_in_db_thread(self._execute, operation, False)

    async def _write(self, operation: Any) -> Any:
        return await _run_in_db_thread(self._execute, operation, True)


class PersistenceBase(PooledSqliteStore):
    """Shared base for all domain-specific SQLite stores.

    All stores receive the *same* ``db_path`` and ``write_lock`` so they
    operate on a single database file with serialised writes.
    """

    # (GH-293) Telemetry role label for connection-settings reporting. Subclasses
    # that predate the shared base may pin their historical label (AuthStore).
    connection_label: str = "persistence_base"
    # (AUD-7) Explicit busy-handler timeout in ms applied at connect. None skips
    # the pragma, leaving Python's sqlite3.connect(timeout=5.0) driver default:
    # stores that historically never issued one override this so convergence
    # does not silently pin them to a future change of the base's value.
    busy_timeout_ms: int | None = 5000

    def __init__(
        self, db_path: Path, write_lock: threading.Lock | PriorityWriteLock
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = write_lock
        with self._write_lock:
            self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # accent/case-insensitive LIKE searches (see _fold_text)
        conn.create_function("fold", 1, _fold_text, deterministic=True)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # (AUD-7) Uniform backstop: a writer blocked by another writer waits up to
        # 5s for the lock instead of failing immediately with "database is locked".
        # Stores that historically never set one pin busy_timeout_ms = None above.
        if self.busy_timeout_ms is not None:
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        # (GH-293) Labeled connection-local settings telemetry (bounded, once per
        # role per process). Never inferred from a fresh probe connection.
        report_connection_settings(self.connection_label, conn)
        return conn

    def _execute_background(self, operation: Any) -> Any:
        background = getattr(self._write_lock, "background", None)
        lock_context = background() if background is not None else self._write_lock
        with lock_context:
            return self._run(operation, commit=True)

    async def _background_write(self, operation: Any) -> Any:
        return await _run_in_db_thread(self._execute_background, operation)

    def _ensure_tables(self) -> None:
        raise NotImplementedError
