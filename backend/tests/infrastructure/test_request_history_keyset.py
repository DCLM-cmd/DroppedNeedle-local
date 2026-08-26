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
    request_kind: str = "album",
) -> None:
    typed_key = (
        mbid.lower()
        if request_kind == "album"
        else f"track:{mbid.lower()}"
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO request_history "
            "(musicbrainz_id_lower, musicbrainz_id, artist_name, album_title, "
            "requested_at, status, user_id, download_task_id, request_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                typed_key,
                mbid,
                f"Artist {mbid}",
                f"Album {mbid}",
                requested_at or "2026-01-01T00:00:00+00:00",
                status,
                user_id,
                task_id,
                request_kind,
            ),
        )
        if user_id is not None:
            conn.execute(
                "INSERT OR REPLACE INTO request_history_requesters "
                "(user_id, musicbrainz_id_lower, requested_at, requested_by_name) "
                "VALUES (?, ?, ?, ?)",
                (
                    user_id,
                    typed_key,
                    requested_at or "2026-01-01T00:00:00+00:00",
                    f"User {user_id}",
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
async def test_keyset_owner_scope_joins_requesters_and_keeps_typed_rows_distinct(
    store: TracedHistory,
) -> None:
    _seed_row(store.db_path, "shared-id", user_id="user-a")
    _seed_row(store.db_path, "mine-a", user_id="user-a")
    _seed_row(store.db_path, "mine-b", user_id="user-b")
    _seed_row(
        store.db_path,
        "shared-id",
        user_id="user-b",
        request_kind="track",
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO request_history_requesters "
            "(user_id, musicbrainz_id_lower, requested_at, requested_by_name) "
            "VALUES (?, ?, ?, ?)",
            (
                "user-b",
                "shared-id",
                "2026-01-01T00:00:00+00:00",
                "User user-b",
            ),
        )

    user_a, _ = await store.async_get_retrying_page(
        "failed",
        page_size=50,
        owner_id="user-a",
        request_kind="album",
    )
    user_b_albums, _ = await store.async_get_retrying_page(
        "failed",
        page_size=50,
        owner_id="user-b",
        request_kind="album",
    )
    user_b_tracks, _ = await store.async_get_retrying_page(
        "failed",
        page_size=50,
        owner_id="user-b",
        request_kind="track",
    )
    admin, _ = await store.async_get_retrying_page(
        "failed", page_size=50, owner_id=None, request_kind=None
    )

    assert {record.musicbrainz_id for record in user_a} == {"shared-id", "mine-a"}
    assert {record.musicbrainz_id for record in user_b_albums} == {
        "shared-id",
        "mine-b",
    }
    assert [record.musicbrainz_id for record in user_b_tracks] == ["shared-id"]
    assert all(record.request_kind == "track" for record in user_b_tracks)
    # All-users queries are over request rows, not requester rows.
    assert len(admin) == 4
    assert {
        (record.request_kind, record.musicbrainz_id)
        for record in admin
    } == {
        ("album", "shared-id"),
        ("album", "mine-a"),
        ("album", "mine-b"),
        ("track", "shared-id"),
    }


@pytest.mark.asyncio
async def test_keyset_cursor_carries_typed_internal_key_for_equal_timestamps(
    store: TracedHistory,
) -> None:
    _seed_row(
        store.db_path,
        "same-id",
        user_id="user-a",
        requested_at="2026-06-01T00:00:00+00:00",
        request_kind="track",
    )

    page, cursor = await store.async_get_retrying_page(
        "failed",
        page_size=1,
        owner_id="user-a",
        request_kind="track",
    )
    assert [record.musicbrainz_id for record in page] == ["same-id"]
    assert cursor == ("2026-06-01T00:00:00+00:00", "track:same-id")


@pytest.mark.asyncio
async def test_user_history_and_active_queries_are_listener_scoped(
    store: TracedHistory,
) -> None:
    _seed_row(store.db_path, "shared-id", status="pending", user_id="user-a")
    _seed_row(store.db_path, "only-a", status="awaiting_approval", user_id="user-a")
    _seed_row(store.db_path, "only-b", status="pending", user_id="user-b")
    await store.async_add_requester("shared-id", "user-b", "Second listener")

    active_a = await store.async_get_active_requests_for_user("user-a")
    active_b = await store.async_get_active_requests_for_user("user-b")
    history_a, total_a = await store.async_get_history_for_user("user-a")
    history_b, total_b = await store.async_get_history_for_user("user-b")
    global_active = await store.async_get_active_requests()

    assert {record.musicbrainz_id for record in active_a} == {"shared-id", "only-a"}
    assert {record.musicbrainz_id for record in active_b} == {"shared-id", "only-b"}
    assert {record.user_id for record in active_a} == {"user-a"}
    assert {record.user_id for record in active_b} == {"user-b"}
    assert {record.musicbrainz_id for record in history_a} == {"shared-id", "only-a"}
    assert {record.musicbrainz_id for record in history_b} == {"shared-id", "only-b"}
    assert total_a == 2 and total_b == 2
    assert {record.musicbrainz_id for record in global_active} == {
        "shared-id",
        "only-b",
    }


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


@pytest.mark.asyncio
async def test_bulk_begin_returns_exact_winners_for_partial_overlap(
    store: TracedHistory,
) -> None:
    first = await store.async_bulk_record_requests(
        [{"musicbrainz_id": "rg-a", "artist_name": "Artist A", "album_title": "Album A"}],
        user_id="user-a",
        initial_status="pending",
    )
    assert [(item.musicbrainz_id, item.request_kind, item.generation) for item in first] == [
        ("rg-a", "album", 1)
    ]

    second = await store.async_bulk_record_requests(
        [
            {"musicbrainz_id": "rg-a", "artist_name": "Replacement", "album_title": "A"},
            {"musicbrainz_id": "rg-b", "artist_name": "Artist B", "album_title": "Album B"},
        ],
        user_id="user-a",
        initial_status="pending",
    )
    assert [(item.musicbrainz_id, item.request_kind, item.generation) for item in second] == [
        ("rg-b", "album", 1)
    ]
    retained = await store.async_get_record("rg-a")
    created = await store.async_get_record("rg-b")
    assert retained is not None and retained.artist_name == "Artist A"
    assert created is not None and created.artist_name == "Artist B"
