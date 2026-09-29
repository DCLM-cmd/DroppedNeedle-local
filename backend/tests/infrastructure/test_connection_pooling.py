"""Pooled SQLite connections: reuse, transaction hygiene, and recovery.

Every store call used to open and close its own connection, which cost ~6.6 ms
against the production database before a single row was read. These tests pin the
reuse down and, more importantly, pin down the invariants that make reuse safe:
a connection that outlives the call must never be handed back mid-transaction.
"""

import asyncio
import sqlite3
import threading

import pytest

from infrastructure.persistence._database import (
    PersistenceBase,
    close_pooled_connections,
)


class _Store(PersistenceBase):
    def _ensure_tables(self) -> None:
        conn = self._connect()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
            conn.commit()
        finally:
            conn.close()

    async def insert(self, value: str) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO t (v) VALUES (?)", (value,))

        await self._write(operation)

    async def all_values(self) -> list[str]:
        def operation(conn: sqlite3.Connection) -> list[str]:
            return [row["v"] for row in conn.execute("SELECT v FROM t ORDER BY id")]

        return await self._read(operation)


@pytest.fixture
def store(tmp_path):
    made = _Store(tmp_path / "pool.db", threading.Lock())
    yield made
    close_pooled_connections()


@pytest.mark.asyncio
async def test_queries_reuse_connections_instead_of_opening_one_each(store):
    """The whole point: N queries must not mean N connections.

    Store work runs on the database thread pool, so the ceiling is one connection
    per pool thread, not one overall - asserting exactly one would be asserting
    which thread the executor happened to pick.
    """
    from infrastructure.persistence._database import _DB_EXECUTOR_WORKERS

    seen: set[int] = set()

    def operation(conn: sqlite3.Connection) -> None:
        seen.add(id(conn))

    queries = 200
    for _ in range(queries):
        await store._read(operation)

    assert len(seen) <= _DB_EXECUTOR_WORKERS
    assert len(seen) < queries / 10


@pytest.mark.asyncio
async def test_pooled_connection_still_reads_other_writers(store, tmp_path):
    """A long-lived reader must see commits made after it was opened, not a stale
    snapshot - the failure mode that makes connection reuse dangerous."""
    assert await store.all_values() == []

    outside = sqlite3.connect(tmp_path / "pool.db")
    try:
        outside.execute("INSERT INTO t (v) VALUES ('written-elsewhere')")
        outside.commit()
    finally:
        outside.close()

    assert await store.all_values() == ["written-elsewhere"]


@pytest.mark.asyncio
async def test_failed_write_rolls_back_and_connection_survives(store):
    """A raising write must not leave its partial statement visible, and must not
    poison the connection for every later call on that thread."""
    await store.insert("first")

    def failing(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO t (v) VALUES ('doomed')")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await store._write(failing)

    assert await store.all_values() == ["first"]

    await store.insert("second")
    assert await store.all_values() == ["first", "second"]


@pytest.mark.asyncio
async def test_read_that_writes_is_rolled_back_not_left_open(store):
    """A read path that unexpectedly writes must not hand back a connection with an
    open transaction - that would pin a WAL snapshot for the life of the thread."""
    captured: list[sqlite3.Connection] = []

    def sneaky(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO t (v) VALUES ('uncommitted')")
        captured.append(conn)

    await store._read(sneaky)

    assert captured[0].in_transaction is False
    assert await store.all_values() == []


@pytest.mark.asyncio
async def test_each_thread_gets_its_own_connection(store):
    """Connections are not thread-safe to share concurrently, so the pool is
    per-thread; two threads must never be handed the same object."""
    seen: list[int] = []
    lock = threading.Lock()

    def operation(conn: sqlite3.Connection) -> None:
        with lock:
            seen.append(id(conn))

    def in_thread() -> None:
        try:
            store._execute(operation, False)
        finally:
            close_pooled_connections()

    threads = [threading.Thread(target=in_thread) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(seen)) == len(threads)


@pytest.mark.asyncio
async def test_concurrent_writes_all_land(store):
    """Serialised writes over a shared pooled connection must not lose rows."""
    await asyncio.gather(*(store.insert(f"v{index}") for index in range(20)))
    assert sorted(await store.all_values()) == sorted(f"v{index}" for index in range(20))
