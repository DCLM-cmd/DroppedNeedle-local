"""A compat id must lead back to the item it was made from.

Four albums in the live catalog did not: their stored Jellyfin id pointed at a
different, empty catalog row, so Finamp listed them with no tracks while the same
albums played perfectly in DroppedNeedle.

Two things went wrong together. Resolving an id consulted the alias table and the
album table as an unordered pair, so the same id could resolve to itself in one query
and to an alias target in the next - and the write that stored the mapping happened to
take the alias. These tests pin both the precedence and the invariant that catches a
mapping which has drifted from it.
"""

import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore


def _derived(kind: str, internal_id: str) -> str:
    return hashlib.sha256(f"{kind}:{internal_id}".encode()).hexdigest()[:32]


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


def _album(connection, album_id: str, title: str) -> None:
    connection.execute(
        "INSERT INTO local_albums(id,root_id,grouping_key,title,title_folded,"
        "album_artist_id,grouping_source,created_at,updated_at) "
        "VALUES (?,'root-1',?,?,?,'artist-1','automatic',1.0,1.0)",
        (album_id, album_id, title, title.casefold()),
    )


# ---- an id that names a real album resolves to that album --------------------------

@pytest.mark.asyncio
async def test_a_live_album_wins_over_an_alias_that_points_elsewhere(store, db_path):
    """The exact live shape: the album holding the music was registered as an alias of
    an empty twin. Resolving it must still land on the album, not the twin."""
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-with-music", "Sexy")
        _album(connection, "album-empty", "Sexy")
        connection.execute(
            "INSERT INTO local_album_aliases(alias, local_album_id, kind, created_at) "
            "VALUES (?,?,'merged_album',1.0)",
            ("album-with-music", "album-empty"),
        )

    assert await store.resolve_target_id("album", "album-with-music") == (
        "album-with-music"
    )


@pytest.mark.asyncio
async def test_an_alias_still_resolves_when_it_names_nothing_real(store, db_path):
    """Aliases must keep working - they are how a retired id stays reachable."""
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-1", "Sexy")
        connection.execute(
            "INSERT INTO local_album_aliases(alias, local_album_id, kind, created_at) "
            "VALUES (?,?,'merged_album',1.0)",
            ("an-old-identifier", "album-1"),
        )

    assert await store.resolve_target_id("album", "an-old-identifier") == "album-1"


@pytest.mark.asyncio
async def test_resolution_is_stable_across_repeated_calls(store, db_path):
    """It used to depend on the order a compound SELECT happened to produce."""
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-a", "Sexy")
        _album(connection, "album-b", "Sexy")
        connection.execute(
            "INSERT INTO local_album_aliases(alias, local_album_id, kind, created_at) "
            "VALUES (?,?,'merged_album',1.0)",
            ("album-a", "album-b"),
        )

    answers = {await store.resolve_target_id("album", "album-a") for _ in range(8)}

    assert answers == {"album-a"}


# ---- a mapping that has drifted from its derivation is dropped ---------------------

@pytest.mark.asyncio
async def test_a_mapping_pointing_at_the_wrong_row_is_dropped(store, db_path):
    """Its id is the hash of what it maps to; anything else sends a client to the
    wrong album. Dropping it is safe - the id is re-derived, to the same value, the
    next time the item is served."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO library_compat_id_map(jf_id, kind, internal_id) VALUES (?,?,?)",
            (_derived("album", "album-with-music"), "album", "album-empty"),
        )

    assert await store.repair_target_compat_mappings() == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM library_compat_id_map"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_consistent_mapping_is_left_alone(store, db_path):
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO library_compat_id_map(jf_id, kind, internal_id) VALUES (?,?,?)",
            (_derived("album", "album-1"), "album", "album-1"),
        )

    assert await store.repair_target_compat_mappings() == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM library_compat_id_map"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_repairing_an_empty_map_is_not_an_error(store):
    assert await store.repair_target_compat_mappings() == 0
