"""Deleting a run from the history keeps what it recorded about the library.

A run owns its plan, its work rows and its snapshots - those go with it. What OTHER
rows keep is a pointer to "the run that last touched me": the catalog audit trail and
each track's management state. Those are records about the LIBRARY, not about the
run, so the pointer is cleared and the row survives.

Written out explicitly rather than left to ON DELETE CASCADE: foreign keys are not
enforced on this connection, so a bare DELETE would silently leave dangling rows
behind - and six of the referencing tables are declared RESTRICT precisely because
they must not be swept away.
"""

import sqlite3

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore


@pytest.fixture()
def store(tmp_path):
    path = tmp_path / "library.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE library_operation_jobs (id TEXT PRIMARY KEY, state TEXT);
        CREATE TABLE library_operation_work (job_id TEXT, ordinal INT);
        CREATE TABLE library_operation_control_idempotency (job_id TEXT);
        CREATE TABLE library_management_external_refresh_deliveries (operation_job_id TEXT);
        CREATE TABLE library_bulk_review_snapshots (job_id TEXT);
        CREATE TABLE library_reidentification_snapshots (job_id TEXT);
        CREATE TABLE library_repair_snapshots (job_id TEXT);
        CREATE TABLE library_identity_repair_findings (job_id TEXT);
        CREATE TABLE library_management_job_snapshots (
            job_id TEXT PRIMARY KEY, linked_operation_job_id TEXT
        );
        CREATE TABLE library_artist_reconciliation_state (operation_job_id TEXT, note TEXT);
        CREATE TABLE library_track_management_state (
            local_track_id TEXT, last_operation_job_id TEXT
        );
        CREATE TABLE library_catalog_actions (id TEXT, operation_job_id TEXT, action TEXT);
        CREATE TABLE library_edition_conversion_jobs (id TEXT, final_preview_job_id TEXT);
        """
    )
    connection.commit()
    connection.close()

    instance = NativeLibraryStore.__new__(NativeLibraryStore)

    async def _write(operation):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                return operation(conn)
        finally:
            conn.close()

    instance._write = _write
    instance._path = path
    return instance


def _seed(path, state="succeeded"):
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO library_operation_jobs VALUES ('job-1', ?)", (state,))
    conn.execute("INSERT INTO library_operation_work VALUES ('job-1', 0)")
    conn.execute("INSERT INTO library_management_job_snapshots VALUES ('job-1', NULL)")
    conn.execute("INSERT INTO library_repair_snapshots VALUES ('job-1')")
    conn.execute(
        "INSERT INTO library_catalog_actions VALUES ('a-1', 'job-1', 'renamed')"
    )
    conn.execute(
        "INSERT INTO library_track_management_state VALUES ('t-1', 'job-1')"
    )
    conn.execute(
        "INSERT INTO library_artist_reconciliation_state VALUES ('job-1', 'note')"
    )
    conn.execute("INSERT INTO library_edition_conversion_jobs VALUES ('e-1', 'job-1')")
    # a second run that must be untouched
    conn.execute("INSERT INTO library_operation_jobs VALUES ('job-2', 'succeeded')")
    conn.execute("INSERT INTO library_operation_work VALUES ('job-2', 0)")
    conn.commit()
    conn.close()


def _rows(path, sql, *args):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_a_finished_run_is_deleted(store) -> None:
    _seed(store._path)

    assert await store.delete_library_operation("job-1") == "deleted"
    assert _rows(store._path, "SELECT id FROM library_operation_jobs") == [("job-2",)]


@pytest.mark.asyncio
async def test_the_runs_own_rows_go_with_it(store) -> None:
    _seed(store._path)

    await store.delete_library_operation("job-1")

    for table, column in (
        ("library_operation_work", "job_id"),
        ("library_management_job_snapshots", "job_id"),
        ("library_repair_snapshots", "job_id"),
    ):
        assert not _rows(
            store._path, f"SELECT 1 FROM {table} WHERE {column} = 'job-1'"
        ), table


@pytest.mark.asyncio
async def test_the_catalog_audit_trail_survives(store) -> None:
    """The regression this guards: what the run did to the LIBRARY is not the run."""
    _seed(store._path)

    await store.delete_library_operation("job-1")

    assert _rows(store._path, "SELECT action, operation_job_id FROM library_catalog_actions") == [
        ("renamed", None)
    ]
    assert _rows(
        store._path,
        "SELECT local_track_id, last_operation_job_id FROM library_track_management_state",
    ) == [("t-1", None)]
    assert _rows(
        store._path,
        "SELECT id, final_preview_job_id FROM library_edition_conversion_jobs",
    ) == [("e-1", None)]


@pytest.mark.asyncio
async def test_another_run_is_untouched(store) -> None:
    _seed(store._path)

    await store.delete_library_operation("job-1")

    assert _rows(store._path, "SELECT job_id FROM library_operation_work") == [("job-2",)]


@pytest.mark.parametrize("state", ["queued", "running", "paused", "ready"])
@pytest.mark.asyncio
async def test_a_run_still_in_flight_is_refused(store, state) -> None:
    """Never delete a job out from under its worker."""
    _seed(store._path, state=state)

    assert await store.delete_library_operation("job-1") == "running"
    assert _rows(store._path, "SELECT id FROM library_operation_jobs WHERE id='job-1'")


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "stopped"])
@pytest.mark.asyncio
async def test_every_terminal_state_can_be_deleted(store, state) -> None:
    _seed(store._path, state=state)

    assert await store.delete_library_operation("job-1") == "deleted"


@pytest.mark.asyncio
async def test_an_unknown_run_reports_not_found(store) -> None:
    _seed(store._path)

    assert await store.delete_library_operation("nope") == "not_found"
