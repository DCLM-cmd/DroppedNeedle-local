"""Album rows left behind by failed imports are removed; real ones are not.

Every failed attempt created a row, so a single album carried up to twenty of them.
They hold no tracks, so the compat API never showed them, but they are what made the
catalog read as chaos everywhere that does not filter on track count.
"""

import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users(id) VALUES ('admin')")
    return path


@pytest.fixture
def store(db_path: Path) -> NativeLibraryStore:
    return NativeLibraryStore(db_path, threading.Lock())


def _album(connection, album_id: str) -> None:
    connection.execute(
        "INSERT INTO local_albums(id,root_id,grouping_key,title,title_folded,"
        "album_artist_id,grouping_source,created_at,updated_at) "
        "VALUES (?,'root-1',?,'An Album','an album','artist-1','automatic',1.0,1.0)",
        (album_id, album_id),
    )


def _track(connection, track_id: str, album_id: str, availability: str) -> None:
    connection.execute(
        "INSERT INTO local_tracks(id,local_album_id,root_id,file_path,relative_path,"
        "path_hash,file_size_bytes,file_mtime_ns,stat_revision,title,title_folded,"
        "album_title,album_title_folded,file_format,ingest_source,imported_at,"
        "membership_source,availability) "
        "VALUES (?,?,'root-1',?,?,?,1,1,'r','A Song','a song','An Album','an album',"
        "'mp3','scan',1.0,'automatic',?)",
        (track_id, album_id, track_id, track_id, track_id, availability),
    )


@pytest.mark.asyncio
async def test_an_album_with_no_tracks_at_all_is_removed(store, db_path):
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-empty")

    result = await store.purge_empty_album_rows()

    assert result["removed"] == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_albums").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_an_album_with_indexed_tracks_is_kept(store, db_path):
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-live")
        _track(connection, "t-1", "album-live", "indexed")

    assert (await store.purge_empty_album_rows())["removed"] == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_albums").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_an_album_owning_only_a_missing_track_is_kept(store, db_path):
    """A missing track is the library recording something it expects to find again,
    not debris - dropping its album would erase that expectation."""
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-missing")
        _track(connection, "t-2", "album-missing", "missing")

    assert (await store.purge_empty_album_rows())["removed"] == 0


@pytest.mark.asyncio
async def test_a_dry_run_changes_nothing(store, db_path):
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-empty")

    result = await store.purge_empty_album_rows(dry_run=True)

    assert result["removed"] == 0 and result["candidates"] == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_albums").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_one_undeletable_row_does_not_abort_the_pass(store, db_path):
    """Each row is its own savepoint, so an album some table still pins cannot take
    the rest of the cleanup down with it."""
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-a")
        _album(connection, "album-b")

    result = await store.purge_empty_album_rows()

    assert result["removed"] == 2
