"""Recovery items that cannot be finished must be dismissable.

``needs_attention`` is terminal for the recovery worker: the scan deliberately
skips those rows because there is nothing left to roll forward or back (typically
the destination and the backup are both gone). Nothing could clear the flag,
though, so the "File recovery needs attention" alert and its library-activity work
item stayed on screen for good, and every activity poll re-counted rows whose
answer would never change - an alert with no action behind it.
"""

import sqlite3
import threading

import pytest

from infrastructure.persistence._database import close_pooled_connections
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.library_administrative_work_service import (
    LibraryAdministrativeWorkService,
)


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users VALUES ('admin')")
    made = NativeLibraryStore(path, threading.Lock())
    made.test_db_path = path
    yield made
    close_pooled_connections()


_ORDINALS = iter(range(1000))


def _add_journal(store: NativeLibraryStore, journal_id: str, state: str) -> None:
    """Seed one journal row.

    Written straight to the file (FKs off by default on a bare connection) so the
    test does not have to build the whole snapshot -> plan-item parent chain just
    to exercise the state column.
    """
    with sqlite3.connect(store.test_db_path) as connection:
        connection.execute(
            "INSERT INTO library_file_mutation_journal "
            "(id, job_id, plan_item_ordinal, subject_kind, subject_key, state, "
            " failure_code, created_at, updated_at) "
            "VALUES (?, 'job-1', ?, 'sidecar', ?, ?, "
            "'RECOVERY_COMMITTED_DESTINATION_MISSING', 1.0, 2.0)",
            (journal_id, next(_ORDINALS), journal_id, state),
        )


@pytest.mark.asyncio
async def test_acknowledging_clears_the_attention_count(store) -> None:
    _add_journal(store, "stuck-1", "needs_attention")
    _add_journal(store, "stuck-2", "needs_attention")

    before = await store.library_management_recovery_diagnostics()
    assert before["needs_attention_count"] == 2

    acknowledged = await store.acknowledge_library_management_recovery_attention(
        now=500.0
    )

    assert acknowledged == 2
    after = await store.library_management_recovery_diagnostics()
    assert after["needs_attention_count"] == 0


@pytest.mark.asyncio
async def test_acknowledging_is_idempotent(store) -> None:
    """A second click must not report work it did not do."""
    _add_journal(store, "stuck-1", "needs_attention")

    assert await store.acknowledge_library_management_recovery_attention(now=500.0) == 1
    assert await store.acknowledge_library_management_recovery_attention(now=600.0) == 0


@pytest.mark.asyncio
async def test_acknowledging_keeps_the_row_as_a_record(store) -> None:
    """The journal is the audit trail of what recovery could not finish."""
    _add_journal(store, "stuck-1", "needs_attention")
    await store.acknowledge_library_management_recovery_attention(now=500.0)

    def read(connection: sqlite3.Connection):
        return connection.execute(
            "SELECT state, failure_code, acknowledged_at "
            "FROM library_file_mutation_journal WHERE id = 'stuck-1'"
        ).fetchone()

    row = await store._read(read)

    assert row["state"] == "needs_attention"
    assert row["failure_code"] == "RECOVERY_COMMITTED_DESTINATION_MISSING"
    assert row["acknowledged_at"] == 500.0


@pytest.mark.asyncio
async def test_still_recoverable_work_is_never_dismissed(store) -> None:
    """Only the give-up state is acknowledged; live work stays outstanding."""
    _add_journal(store, "stuck-1", "needs_attention")
    _add_journal(store, "in-flight", "cleanup_pending")

    await store.acknowledge_library_management_recovery_attention(now=500.0)

    diagnostics = await store.library_management_recovery_diagnostics()
    assert diagnostics["needs_attention_count"] == 0
    assert diagnostics["cleanup_pending_count"] == 1


@pytest.mark.asyncio
async def test_the_work_item_disappears_once_acknowledged(store) -> None:
    """This is what the operator actually sees: no more permanent alert."""
    _add_journal(store, "stuck-1", "needs_attention")
    work = LibraryAdministrativeWorkService(store, clock=lambda: 1000.0)

    before = await work.active()
    assert any(item.kind == "recovery" for item in before)

    await store.acknowledge_library_management_recovery_attention(now=500.0)

    after = await work.active()
    assert not any(item.kind == "recovery" for item in after)
