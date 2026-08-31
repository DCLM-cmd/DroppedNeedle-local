"""A file moved to the recycle bin stops being part of the library.

Recycled files were pruned from disk on a timer, but the catalog rows naming them
stayed behind - still pointing into the bin, still counted as part of the album they
had been removed from. One such row was enough to make an album read as holding a
track it no longer has.
"""

import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore

_BIN = "/music/.recycle"


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


def _seed(connection, track_id: str, file_path: str) -> None:
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
        "'an album','mp3','scan',1.0,'automatic','missing')",
        (track_id, file_path, file_path, track_id),
    )


@pytest.mark.asyncio
async def test_a_recycled_row_is_removed_from_the_catalog(store, db_path):
    with sqlite3.connect(db_path) as connection:
        _seed(connection, "t-recycled", f"{_BIN}/20260101T000000-x/01 - Song.mp3")

    result = await store.purge_recycled_track_rows(_BIN)

    assert result == {"removed": 1, "detached": 0}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_tracks").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_library_file_is_left_alone(store, db_path):
    with sqlite3.connect(db_path) as connection:
        _seed(connection, "t-live", "/music/Artist/Album/01 - Song.mp3")

    assert await store.purge_recycled_track_rows(_BIN) == {"removed": 0, "detached": 0}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_tracks").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_the_tracks_own_satellite_data_goes_with_it(store, db_path):
    """Genres, credits and identities have no meaning without the track."""
    with sqlite3.connect(db_path) as connection:
        _seed(connection, "t-recycled", f"{_BIN}/20260101T000000-x/01 - Song.mp3")
        connection.execute(
            "INSERT INTO local_track_genres"
            "(local_track_id, position, name, folded_name, source) "
            "VALUES ('t-recycled',0,'Rock','rock','local')"
        )

    await store.purge_recycled_track_rows(_BIN)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM local_track_genres"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_track_someone_still_has_in_a_playlist_is_kept(store, db_path):
    """Silently dropping a track a user has in a playlist would be worse than an
    untidy row. It stays - it just stops claiming to live in the bin."""
    with sqlite3.connect(db_path) as connection:
        _seed(connection, "t-recycled", f"{_BIN}/20260101T000000-x/01 - Song.mp3")
        connection.execute(
            "INSERT INTO library_play_history"
            "(id,user_id,local_track_id,track_name,artist_name,played_at) "
            "VALUES ('h-1','admin','t-recycled','A Song','An Artist',1.0)"
        )

    result = await store.purge_recycled_track_rows(_BIN)

    assert result == {"removed": 0, "detached": 1}
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT file_path, availability FROM local_tracks WHERE id='t-recycled'"
        ).fetchone()
    assert row[0] == "" and row[1] == "missing"


@pytest.mark.asyncio
async def test_an_empty_bin_path_does_nothing(store):
    assert await store.purge_recycled_track_rows("") == {"removed": 0, "detached": 0}


# ---- rows for files a better copy replaced -----------------------------------------

def _seed_at(connection, track_id: str, *, track: int, availability: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO local_albums(id,root_id,grouping_key,title,title_folded,"
        "album_artist_id,grouping_source,created_at,updated_at) "
        "VALUES ('album-1','root-1','album-1','An Album','an album','artist-1',"
        "'automatic',1.0,1.0)"
    )
    connection.execute(
        "INSERT INTO local_tracks(id,local_album_id,root_id,file_path,relative_path,"
        "path_hash,file_size_bytes,file_mtime_ns,stat_revision,title,title_folded,"
        "album_title,album_title_folded,file_format,track_number,disc_number,"
        "ingest_source,imported_at,membership_source,availability) "
        "VALUES (?,'album-1','root-1',?,?,?,1,1,'r','A Song','a song','An Album',"
        "'an album','mp3',?,1,'scan',1.0,'automatic',?)",
        (track_id, f"/music/{track_id}", f"{track_id}", track_id, track, availability),
    )


@pytest.mark.asyncio
async def test_a_replaced_copy_is_removed(store, db_path):
    """A scan marks a vanished file missing and stops there, so every replaced copy
    left its row behind - and a row sitting at a destination is what a later import
    trips over."""
    with sqlite3.connect(db_path) as connection:
        _seed_at(connection, "t-live", track=1, availability="indexed")
        _seed_at(connection, "t-replaced", track=1, availability="missing")

    assert await store.purge_superseded_track_rows() == {"removed": 1, "kept": 0}
    with sqlite3.connect(db_path) as connection:
        remaining = [
            r[0] for r in connection.execute("SELECT id FROM local_tracks")
        ]
    assert remaining == ["t-live"]


@pytest.mark.asyncio
async def test_a_track_the_album_simply_lost_is_kept(store, db_path):
    """That row is the record that an album is missing a song - information the
    library is meant to keep, and to show."""
    with sqlite3.connect(db_path) as connection:
        _seed_at(connection, "t-gone", track=7, availability="missing")

    assert await store.purge_superseded_track_rows() == {"removed": 0, "kept": 0}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_tracks").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_a_replaced_copy_someone_played_is_kept(store, db_path):
    """Play history belongs to the user, not to the file."""
    with sqlite3.connect(db_path) as connection:
        _seed_at(connection, "t-live", track=1, availability="indexed")
        _seed_at(connection, "t-replaced", track=1, availability="missing")
        connection.execute(
            "INSERT INTO library_play_history"
            "(id,user_id,local_track_id,track_name,artist_name,played_at) "
            "VALUES ('h-1','admin','t-replaced','A Song','An Artist',1.0)"
        )

    assert await store.purge_superseded_track_rows() == {"removed": 0, "kept": 1}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM local_tracks WHERE id='t-replaced'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_a_live_track_is_never_touched(store, db_path):
    with sqlite3.connect(db_path) as connection:
        _seed_at(connection, "t-live", track=1, availability="indexed")

    assert await store.purge_superseded_track_rows() == {"removed": 0, "kept": 0}


# ---- tracks left on a merged-away album row ----------------------------------------

def _album_row(connection, album_id: str, *, retired_into: str | None = None) -> None:
    connection.execute(
        "INSERT INTO local_albums(id,root_id,grouping_key,title,title_folded,"
        "album_artist_id,grouping_source,created_at,updated_at,retired_into_album_id) "
        "VALUES (?,'root-1',?,'An Album','an album','artist-1','automatic',1.0,1.0,?)",
        (album_id, album_id, retired_into),
    )


@pytest.mark.asyncio
async def test_tracks_follow_their_album_through_a_merge(store, db_path):
    """Retiring an album moves the ROW; anything still hanging off it goes invisible,
    because listings follow the survivor. One album kept ten of its eleven tracks on
    the retired half and showed a single song."""
    with sqlite3.connect(db_path) as connection:
        _album_row(connection, "album-survivor")
        _album_row(connection, "album-retired", retired_into="album-survivor")
        _seed_at(connection, "t-1", track=1, availability="indexed")
        connection.execute(
            "UPDATE local_tracks SET local_album_id='album-retired' WHERE id='t-1'"
        )

    assert await store.rehome_tracks_from_retired_albums() == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT local_album_id FROM local_tracks WHERE id='t-1'"
        ).fetchone()[0] == "album-survivor"


@pytest.mark.asyncio
async def test_a_chain_of_retirements_lands_on_the_album_that_is_shown(store, db_path):
    with sqlite3.connect(db_path) as connection:
        _album_row(connection, "album-final")
        _album_row(connection, "album-middle", retired_into="album-final")
        _album_row(connection, "album-first", retired_into="album-middle")
        _seed_at(connection, "t-1", track=1, availability="indexed")
        connection.execute(
            "UPDATE local_tracks SET local_album_id='album-first' WHERE id='t-1'"
        )

    await store.rehome_tracks_from_retired_albums()

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT local_album_id FROM local_tracks WHERE id='t-1'"
        ).fetchone()[0] == "album-final"


@pytest.mark.asyncio
async def test_a_retirement_cycle_does_not_spin(store, db_path):
    """A cycle would otherwise loop forever while holding the write lock."""
    with sqlite3.connect(db_path) as connection:
        _album_row(connection, "album-a")
        _album_row(connection, "album-b", retired_into="album-a")
        connection.execute(
            "UPDATE local_albums SET retired_into_album_id='album-b' WHERE id='album-a'"
        )
        _seed_at(connection, "t-1", track=1, availability="indexed")
        connection.execute(
            "UPDATE local_tracks SET local_album_id='album-a' WHERE id='t-1'"
        )

    await store.rehome_tracks_from_retired_albums()  # must simply return


@pytest.mark.asyncio
async def test_a_live_album_keeps_its_tracks(store, db_path):
    with sqlite3.connect(db_path) as connection:
        _album_row(connection, "album-live")
        _seed_at(connection, "t-1", track=1, availability="indexed")
        connection.execute(
            "UPDATE local_tracks SET local_album_id='album-live' WHERE id='t-1'"
        )

    assert await store.rehome_tracks_from_retired_albums() == 0
