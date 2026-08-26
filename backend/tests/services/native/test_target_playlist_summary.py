"""F-TARGETCATALOG-07: ID-filtered target playlist summary.

The summary path must aggregate exactly one playlist row; the full-list
aggregation stays reserved for list callers."""

import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.target_reference_adapters import TargetPlaylistRepository
from services.playlist_service import PlaylistService


def _store(tmp_path: Path) -> NativeLibraryStore:
    db = tmp_path / "target.db"
    lock = threading.Lock()
    with sqlite3.connect(tmp_path / "auth.db") as c:
        c.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
    return NativeLibraryStore(db, lock)


def _seed_playlist(store: NativeLibraryStore, pid: str, tracks: int) -> None:
    with sqlite3.connect(store.db_path) as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute(
            "INSERT INTO library_playlists (id, user_id, name, is_public, "
            "created_at, updated_at) VALUES (?, 'user-1', ?, 0, 1, 1)",
            (pid, f"Playlist {pid}"),
        )
        for i in range(tracks):
            c.execute(
                "INSERT INTO library_playlist_tracks (id, playlist_id, "
                "position, track_name, artist_name, album_name, source_type, "
                "created_at, duration, cover_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{pid}-t{i}",
                    pid,
                    i,
                    f"Track {i}",
                    "Artist",
                    "Album",
                    "droppedneedle-local",
                    "1",
                    100 + i,
                    "cover.png" if i == 0 else "",
                ),
            )


@pytest.fixture
def seeded_store(tmp_path: Path) -> NativeLibraryStore:
    store = _store(tmp_path)
    _seed_playlist(store, "pl-target", 2)
    for i in range(20):
        _seed_playlist(store, f"pl-noise-{i}", 5)
    return store


@pytest.mark.asyncio
async def test_store_summary_aggregates_only_requested_playlist(
    seeded_store: NativeLibraryStore,
) -> None:
    """One populated playlist returns the same aggregate values the full list
    produces; unrelated playlists cannot affect the result."""
    store = seeded_store
    summary = await store.get_target_playlist_summary("pl-target")
    assert summary is not None
    assert summary["id"] == "pl-target"
    assert summary["track_count"] == 2
    assert summary["total_duration"] == 201
    assert summary["cover_urls"] == "cover.png"

    listed = next(
        row
        for row in await store.list_target_playlists()
        if row["id"] == "pl-target"
    )
    for key in ("track_count", "total_duration", "cover_urls"):
        assert summary[key] == listed[key]

    # Unrelated playlists are irrelevant: adding another does not change ours.
    _seed_playlist(store, "pl-added-later", 9)
    after = await store.get_target_playlist_summary("pl-target")
    assert after is not None and after["track_count"] == 2


@pytest.mark.asyncio
async def test_store_summary_handles_empty_and_missing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed_playlist(store, "pl-empty", 0)
    empty = await store.get_target_playlist_summary("pl-empty")
    assert empty is not None
    assert empty["track_count"] == 0
    assert empty["total_duration"] == 0
    assert not empty["cover_urls"]
    assert await store.get_target_playlist_summary("missing-id") is None


@pytest.mark.asyncio
async def test_adapter_summary_is_id_filtered_and_record_identical(
    seeded_store: NativeLibraryStore,
) -> None:
    repository = TargetPlaylistRepository(seeded_store)

    get_all_calls: list[tuple] = []
    original_get_all = type(repository).get_all_playlists

    async def spy_get_all(self, user_id=None):
        get_all_calls.append((user_id,))
        return await original_get_all(self, user_id)

    type(repository).get_all_playlists = spy_get_all
    try:
        summary = await repository.get_summary("pl-target")
    finally:
        type(repository).get_all_playlists = original_get_all

    # The adapter never loads the full list on the summary path.
    assert get_all_calls == []
    assert summary is not None
    assert summary.id == "pl-target"
    assert summary.track_count == 2
    assert summary.total_duration == 201
    assert summary.cover_urls == ["cover.png"]
    assert await repository.get_summary("missing-id") is None

    # Record conversion parity with the list path.
    listed = {
        record.id: record
        for record in await repository.get_all_playlists()
    }
    # Record conversion parity with the list path: every field the shared
    # helper maps must match between the ID-filtered and list aggregates.
    from_list = listed["pl-target"]
    assert (
        from_list.id == summary.id
        and from_list.name == summary.name
        and from_list.cover_image_path == summary.cover_image_path
        and from_list.created_at == summary.created_at
        and from_list.updated_at == summary.updated_at
        and from_list.track_count == summary.track_count
        and from_list.total_duration == summary.total_duration
        and from_list.cover_urls == summary.cover_urls
        and from_list.source_ref == summary.source_ref
        and from_list.user_id == summary.user_id
        and from_list.is_public == summary.is_public
    )


@pytest.mark.asyncio
async def test_set_public_uses_filtered_summary_and_keeps_owner_gate(
    seeded_store: NativeLibraryStore, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    store = seeded_store
    repository = TargetPlaylistRepository(store)
    service = PlaylistService(
        None, tmp_path, library_db=None, async_repo=repository
    )

    owner = SimpleNamespace(id="user-1", role="user")
    other = SimpleNamespace(id="user-2", role="user")
    admin = SimpleNamespace(id="admin-1", role="admin")

    list_calls: list[tuple] = []
    original_list = type(store).list_target_playlists

    async def spy_list(self, user_id=None):
        list_calls.append((user_id,))
        return await original_list(self, user_id)

    type(store).list_target_playlists = spy_list
    try:
        published = await service.set_public("pl-target", owner, True)
    finally:
        type(store).list_target_playlists = original_list

    assert published.record.is_public is True
    assert published.record.track_count == 2
    # The whole-list aggregation is never touched by publish.
    assert list_calls == []

    # Owner-only gate unchanged: another user and admin cannot publish (D4).
    from core.exceptions import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        await service.set_public("pl-target", other, False)
    with pytest.raises(PermissionDeniedError):
        await service.set_public("pl-target", admin, False)
    unpublished = await service.set_public("pl-target", owner, False)
    assert unpublished.record.is_public is False


@pytest.mark.asyncio
async def test_sql_trace_proves_single_id_filtered_statement(
    seeded_store: NativeLibraryStore,
) -> None:
    """SQLite trace proof: the summary path issues one aggregate statement
    carrying the playlist-ID predicate; no unfiltered catalog aggregation."""

    class TracedStore(NativeLibraryStore):
        def __init__(self, *args, **kwargs):
            # _connect runs inside super().__init__, so collect first.
            self.statements: list[str] = []
            super().__init__(*args, **kwargs)

        def _connect(self):
            connection = super()._connect()
            connection.set_trace_callback(self.statements.append)
            return connection

    traced = TracedStore(seeded_store.db_path, threading.Lock())
    repository = TargetPlaylistRepository(traced)
    # Drop schema/migration statements emitted during construction.
    traced.statements.clear()

    summary = await repository.get_summary("pl-target")
    assert summary is not None

    selects = [s for s in traced.statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1
    statement = selects[0]
    assert "FROM library_playlists p" in statement
    # sqlite trace expands bound parameters; the predicate is still p.id.
    assert "WHERE p.id = " in statement
    assert "GROUP_CONCAT(NULLIF(pt.cover_url, ''))" in statement
    # No unfiltered list aggregate on this path.
    assert not any("ORDER BY p.updated_at" in s for s in selects)


@pytest.mark.asyncio
async def test_detail_path_never_touches_summary_aggregation(
    seeded_store: NativeLibraryStore, tmp_path: Path
) -> None:
    """Owner/public/admin/private detail access keeps using get_playlist and
    get_tracks; the summary aggregate is not acquired (ticket non-goal)."""
    from types import SimpleNamespace

    store = seeded_store
    repository = TargetPlaylistRepository(store)
    service = PlaylistService(
        None, tmp_path, library_db=None, async_repo=repository
    )
    owner = SimpleNamespace(id="user-1", role="user")
    other = SimpleNamespace(id="user-2", role="user")
    admin = SimpleNamespace(id="admin-1", role="admin")

    await service.set_public("pl-target", owner, True)

    summary_calls: list[str] = []
    original_summary = type(store).get_target_playlist_summary

    async def spy_summary(self, playlist_id):
        summary_calls.append(playlist_id)
        return await original_summary(self, playlist_id)

    type(store).get_target_playlist_summary = spy_summary
    try:
        owner_view = await service.get_playlist_with_tracks("pl-target", owner)
        public_view = await service.get_playlist_with_tracks("pl-target", other)
        admin_view = await service.get_playlist_with_tracks("pl-target", admin)
    finally:
        type(store).get_target_playlist_summary = original_summary

    assert owner_view.is_owner and len(owner_view.tracks) == 2
    assert not public_view.is_owner and len(public_view.tracks) == 2
    assert not admin_view.is_owner and len(admin_view.tracks) == 2
    assert summary_calls == []


@pytest.mark.asyncio
async def test_runtime_smoke_publish_unpublish_with_unrelated_playlists(
    seeded_store: NativeLibraryStore, tmp_path: Path
) -> None:
    """Ticket smoke: publish and unpublish one playlist while 20 unrelated
    playlists exist; list and detail views keep public/private visibility."""
    from types import SimpleNamespace

    store = seeded_store
    repository = TargetPlaylistRepository(store)
    service = PlaylistService(
        None, tmp_path, library_db=None, async_repo=repository
    )
    owner = SimpleNamespace(id="user-1", role="user")
    other = SimpleNamespace(id="user-2", role="user")

    published = await service.set_public("pl-target", owner, True)
    assert published.record.is_public is True
    assert published.record.track_count == 2

    listed = await repository.get_all_playlists(other.id)
    visible = [record for record in listed if record.id == "pl-target"]
    assert len(visible) == 1 and visible[0].is_public is True

    detail = await service.get_playlist_with_tracks("pl-target", other)
    assert not detail.is_owner and len(detail.tracks) == 2

    await service.set_public("pl-target", owner, False)
    listed_private = [
        record
        for record in await repository.get_all_playlists(other.id)
        if record.id == "pl-target"
    ]
    assert listed_private == []
