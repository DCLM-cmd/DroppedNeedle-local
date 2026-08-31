"""Replacing an occupied import destination destroys what was there.

Unlike the recycle path - which sets a file aside and keeps its row usable - this is
a deliberate destruction the user has just confirmed against a named file. A row left
behind would keep the album claiming a track whose file is gone, and a reference left
behind would point at nothing; ON DELETE RESTRICT means it would also abort the
delete outright and leave the user stuck exactly where they asked to be unstuck.
"""

import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore

_PATH = "/music/Artist/Album (2016)/01 - Song.mp3"


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


def _seed(connection, track_id: str = "t-old", file_path: str = _PATH) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO local_albums(id,root_id,grouping_key,title,title_folded,"
        "album_artist_id,grouping_source,created_at,updated_at) "
        "VALUES ('album-1','root-1','album-1','An Album','an album','artist-1',"
        "'automatic',1.0,1.0)"
    )
    connection.execute(
        "INSERT INTO local_tracks(id,local_album_id,root_id,file_path,relative_path,"
        "path_hash,file_size_bytes,file_mtime_ns,stat_revision,title,title_folded,"
        "album_title,album_title_folded,file_format,ingest_source,imported_at,"
        "membership_source,availability) "
        "VALUES (?,'album-1','root-1',?,?,?,1,1,'r','A Song','a song','An Album',"
        "'an album','mp3','scan',1.0,'automatic','indexed')",
        (track_id, file_path, file_path, track_id),
    )


@pytest.mark.asyncio
async def test_the_occupant_is_found_by_its_path(store, db_path):
    with sqlite3.connect(db_path) as connection:
        _seed(connection)

    found = await store.find_track_by_file_path(_PATH)

    assert found is not None and found["title"] == "A Song"
    assert found["file_format"] == "mp3"


@pytest.mark.asyncio
async def test_an_unoccupied_path_finds_nothing(store):
    assert await store.find_track_by_file_path("/music/nothing/here.flac") is None


@pytest.mark.asyncio
async def test_the_row_is_deleted_not_parked(store, db_path):
    with sqlite3.connect(db_path) as connection:
        _seed(connection)

    removed = await store.delete_track_by_file_path(_PATH)

    assert removed is not None and removed["track_id"] == "t-old"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_tracks").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_restricting_references_go_too_rather_than_blocking_the_delete(
    store, db_path
):
    """A dozen tables carry ON DELETE RESTRICT. Leaving any of them would not
    degrade - it would refuse the delete."""
    with sqlite3.connect(db_path) as connection:
        _seed(connection)
        # RESTRICT, so leaving it would refuse the delete outright
        connection.execute(
            "INSERT INTO local_track_external_identities"
            "(local_track_id,provider,recording_mbid,decision_source,selected_at) "
            "VALUES ('t-old','musicbrainz','rec-1','automatic',1.0)"
        )

    removed = await store.delete_track_by_file_path(_PATH)

    assert removed["removed_references"].get("local_track_external_identities") == 1
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM local_track_external_identities"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM local_tracks").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_nothing_there_reports_nothing_removed(store):
    assert await store.delete_track_by_file_path("/music/absent.flac") is None
