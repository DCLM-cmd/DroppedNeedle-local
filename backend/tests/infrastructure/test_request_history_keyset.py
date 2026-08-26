"""F-PERF-03 persistence coverage: keyset-paged retrying history and the
batched task lookup. Real SQLite rows, real query-plan and count assertions."""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.download_store import DownloadStore
from infrastructure.persistence.request_history import RequestHistoryStore


def _seed_row(
    db_path: Path,
    mbid: str,
    *,
    status: str = "failed",
    user_id: str | None = "user-a",
    requested_at: str | None = None,
    task_id: str | None = "task-1",
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO request_history "
            "(musicbrainz_id_lower, musicbrainz_id, artist_name, album_title, "
            "requested_at, status, user_id, download_task_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mbid.lower(),
                mbid,
                f"Artist {mbid}",
                f"Album {mbid}",
                requested_at or "2026-01-01T00:00:00+00:00",
                status,
                user_id,
                task_id,
            ),
        )


class TracedHistory(RequestHistoryStore):
    """Records every SQL statement after construction (schema setup excluded)."""

    def __init__(self, *args, **kwargs):
        self.statements: list[str] = []
        super().__init__(*args, **kwargs)

    def _connect(self):
        conn = super()._connect()
        conn.set_trace_callback(self.statements.append)
        return conn


@pytest.fixture
def store(tmp_path: Path) -> TracedHistory:
    return TracedHistory(db_path=tmp_path / "requests.db")


@pytest.mark.asyncio
async def test_keyset_pages_return_every_row_exactly_once_with_tie_breaks(
    store: TracedHistory,
) -> None:
    # 25 rows: ten share one timestamp (tie-break territory), rest vary.
    for i in range(10):
        _seed_row(store.db_path, f"rg-tie-{i}", requested_at="2026-01-02T00:00:00+00:00")
    for i in range(15):
        _seed_row(
            store.db_path,
            f"rg-var-{i:02d}",
            requested_at=f"2026-01-01T{i % 24:02d}:00:00+00:00",
        )
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        records, next_cursor = await store.async_get_retrying_page(
            status_filter="failed", page_size=7, cursor=cursor, owner_id=None
        )
        pages += 1
        seen.extend(record.musicbrainz_id for record in records)
        assert len(records) <= 7
        if next_cursor is None:
            break
        cursor = next_cursor
    assert pages == 4  # ceil(25 / 7)
    assert len(seen) == 25 and len(set(seen)) == 25  # no duplicates, no skips


@pytest.mark.asyncio
async def test_keyset_ordering_is_requested_at_desc_then_mbid_desc(
    store: TracedHistory,
) -> None:
    _seed_row(store.db_path, "rg-b", requested_at="2026-03-01T00:00:00+00:00")
    _seed_row(store.db_path, "rg-a", requested_at="2026-03-01T00:00:00+00:00")
    _seed_row(store.db_path, "rg-z", requested_at="2026-04-01T00:00:00+00:00")
    records, next_cursor = await store.async_get_retrying_page("failed", page_size=10)
    assert [record.musicbrainz_id for record in records] == ["rg-z", "rg-b", "rg-a"]
    assert next_cursor is None  # short final page terminates the walk


@pytest.mark.asyncio
async def test_owner_scope_is_applied_inside_the_query(store: TracedHistory) -> None:
    _seed_row(store.db_path, "rg-mine", user_id="user-a")
    _seed_row(store.db_path, "rg-theirs", user_id="user-b")

    mine, _ = await store.async_get_retrying_page("failed", page_size=50, owner_id="user-a")
    everyone, _ = await store.async_get_retrying_page("failed", page_size=50, owner_id=None)
    assert [record.musicbrainz_id for record in mine] == ["rg-mine"]
    assert {record.musicbrainz_id for record in everyone} == {"rg-mine", "rg-theirs"}

    # legacy NULL-owner rows stay invisible to a scoped caller, visible to admin
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO request_history (musicbrainz_id_lower, musicbrainz_id, "
            "artist_name, album_title, requested_at, status, user_id) VALUES "
            "('rg-null', 'rg-null', 'A', 'B', '2026-05-01T00:00:00+00:00', "
            "'failed', NULL)"
        )
    scoped, _ = await store.async_get_retrying_page("failed", page_size=50, owner_id="user-a")
    admin, _ = await store.async_get_retrying_page("failed", page_size=50, owner_id=None)
    assert all(record.musicbrainz_id != "rg-null" for record in scoped)
    assert any(record.musicbrainz_id == "rg-null" for record in admin)


@pytest.mark.asyncio
async def test_no_count_query_and_plan_uses_the_keyset_index(
    store: TracedHistory,
) -> None:
    for i in range(5):
        _seed_row(store.db_path, f"rg-{i}")

    store.statements.clear()
    records, cursor = await store.async_get_retrying_page("failed", page_size=2)
    assert cursor is not None and len(records) == 2

    selects = [s for s in store.statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1  # the page SELECT only - no COUNT companion
    assert "COUNT" not in selects[0].upper()

    with sqlite3.connect(store.db_path) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM request_history "
            "WHERE status = ? AND (requested_at < ? OR (requested_at = ? AND "
            "musicbrainz_id_lower < ?)) ORDER BY requested_at DESC, "
            "musicbrainz_id_lower DESC LIMIT 200",
            ("failed", "2026-01-01", "2026-01-01", "rg-x"),
        ).fetchall()
    plan_text = " ".join(str(row[-1]) for row in plan)
    assert "USING INDEX idx_request_history_retrying_keyset" in plan_text
    assert "TEMP B-TREE FOR ORDER BY" not in plan_text


# --- DownloadStore.get_tasks -------------------------------------------------


def _make_download_store(tmp_path: Path) -> DownloadStore:
    class TracedDownloads(DownloadStore):
        def __init__(self, *args, **kwargs):
            self.statements: list[str] = []
            super().__init__(*args, **kwargs)

        def _connect(self):
            conn = super()._connect()
            conn.set_trace_callback(self.statements.append)
            return conn

    db = tmp_path / "downloads.db"
    download_store = TracedDownloads(db_path=db, write_lock=threading.Lock())
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS auth_users "
            "(id TEXT PRIMARY KEY, username TEXT, role TEXT)"
        )
        conn.execute("INSERT OR IGNORE INTO auth_users VALUES ('user-a', 'a', 'user')")
    return download_store


@pytest.mark.asyncio
async def test_get_tasks_batches_in_one_query_and_skips_empty(tmp_path: Path) -> None:
    download_store = _make_download_store(tmp_path)
    first = await download_store.create_task(user_id="user-a", release_group_mbid="rg-1")
    second = await download_store.create_task(user_id="user-a", release_group_mbid="rg-2")

    download_store.statements.clear()
    batched = await download_store.get_tasks([first.id, second.id, "ghost"])
    empty = await download_store.get_tasks([])

    assert set(batched) == {first.id, second.id}
    assert batched[first.id].release_group_mbid == "rg-1"
    assert empty == {}
    selects = [
        s for s in download_store.statements if s.lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 1  # one IN query; the empty input opened none
    # trace expands parameters: three bound ids in one statement
    assert "IN (" in selects[0] and selects[0].count(",") >= 2
