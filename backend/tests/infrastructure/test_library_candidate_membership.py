"""E3 candidate-scoped membership for the materialised library tables.

``existing_library_albums``/``existing_library_artists`` must return exactly
the full-set result restricted to the supplied candidates: same ghost-row
filter, same owned-artist (not contributor-appearance) membership, without
scanning the tables.
"""

import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.library_db import LibraryDB


@pytest.fixture
def db(tmp_path: Path) -> LibraryDB:
    return LibraryDB(db_path=tmp_path / "test.db", write_lock=threading.Lock())


def _seed_album_row(db_path: Path, mbid: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO library_albums (mbid_lower, mbid, title, raw_json)"
            " VALUES (?, ?, ?, ?)",
            (mbid.casefold(), mbid, f"Album {mbid}", "{}"),
        )


def _seed_artist_row(db_path: Path, mbid: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO library_artists (mbid_lower, mbid, name, raw_json)"
            " VALUES (?, ?, ?, ?)",
            (mbid.casefold(), mbid, f"Artist {mbid}", "{}"),
        )


def _file_row(rg_mbid: str, *, file_path: str, artist_mbid: str | None = None) -> dict:
    return {
        "release_group_mbid": rg_mbid,
        "release_mbid": None,
        "recording_mbid": None,
        "disc_number": 1,
        "track_number": 1,
        "track_title": "Track 1",
        "artist_name": "Artist",
        "artist_mbid": artist_mbid,
        "album_artist_name": "Artist",
        "album_artist_mbid": None,
        "album_title": "Album",
        "year": None,
        "file_path": file_path,
        "source_path": None,
        "file_size_bytes": 1,
        "file_mtime": 0.0,
        "duration_seconds": None,
        "file_format": "flac",
        "bit_rate": None,
        "sample_rate": None,
        "bit_depth": None,
        "source": "scan",
        "confidence": 1.0,
        "is_compilation": 0,
        "tagged_at": None,
    }


@pytest.mark.asyncio
async def test_existing_library_albums_matches_full_set_restricted(
    db: LibraryDB, tmp_path: Path
) -> None:
    owned = "aaaaaaaa-0000-4000-8000-000000000001"
    ghost = "bbbbbbbb-0000-4000-8000-000000000002"
    other_owned = "cccccccc-0000-4000-8000-000000000003"
    _seed_album_row(tmp_path / "test.db", owned)
    _seed_album_row(tmp_path / "test.db", ghost)
    _seed_album_row(tmp_path / "test.db", other_owned)
    await db.upsert_library_file(_file_row(owned, file_path="/lib/owned/1.flac"))
    await db.upsert_library_file(_file_row(other_owned, file_path="/lib/other/1.flac"))

    full = await db.get_all_album_mbids()
    assert full == {owned, other_owned}

    found = await db.existing_library_albums([owned.upper(), ghost, "missing-id", ""])
    assert found == {owned}
    assert await db.existing_library_albums([]) == set()


@pytest.mark.asyncio
async def test_existing_library_albums_ignores_deleted_files(
    db: LibraryDB, tmp_path: Path
) -> None:
    removed = "dddddddd-0000-4000-8000-000000000004"
    _seed_album_row(tmp_path / "test.db", removed)
    await db.upsert_library_file(_file_row(removed, file_path="/lib/removed/1.flac"))
    await db.soft_delete_album_files(removed)

    assert await db.existing_library_albums([removed]) == set()
    assert await db.get_all_album_mbids() == set()


@pytest.mark.asyncio
async def test_existing_library_artists_matches_full_set_restricted(
    db: LibraryDB, tmp_path: Path
) -> None:
    owned = "aaaaaaaa-0000-4000-8000-000000000011"
    other_owned = "bbbbbbbb-0000-4000-8000-000000000012"
    _seed_artist_row(tmp_path / "test.db", owned)
    _seed_artist_row(tmp_path / "test.db", other_owned)

    full = await db.get_all_artist_mbids()
    assert full == {owned, other_owned}

    found = await db.existing_library_artists([owned.upper(), "missing-id", ""])
    assert found == {owned}
    assert await db.existing_library_artists([]) == set()


@pytest.mark.asyncio
async def test_existing_library_artists_ignores_contributor_file_credits(
    db: LibraryDB, tmp_path: Path
) -> None:
    """File-level contributor credits are appearances, not owned artists."""
    contributor = "cccccccc-0000-4000-8000-000000000013"
    await db.upsert_library_file(
        _file_row(
            "dddddddd-0000-4000-8000-000000000014",
            file_path="/lib/feat/1.flac",
            artist_mbid=contributor,
        )
    )

    assert await db.existing_library_artists([contributor]) == set()
    assert await db.get_all_artist_mbids() == set()
