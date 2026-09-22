"""File-tag release types through library_files: storage, aggregate, summary."""

import threading
from pathlib import Path

import pytest

from infrastructure.persistence.library_db import LibraryDB
from models.audio import AudioInfo, AudioTag
from services.native.library_manager import LibraryManager


@pytest.fixture
def db(tmp_path: Path) -> LibraryDB:
    return LibraryDB(db_path=tmp_path / "test.db", write_lock=threading.Lock())


_ABSENT = object()


def _file_row(
    rg_mbid: str,
    *,
    file_path: str,
    track_number: int = 1,
    disc_number: int = 1,
    album_artist_mbid: str | None = None,
    release_type: object = _ABSENT,
) -> dict:
    """Minimal library_files row; omit release_type to mimic a legacy NULL row."""
    row = {
        "release_group_mbid": rg_mbid,
        "release_mbid": None,
        "recording_mbid": None,
        "disc_number": disc_number,
        "track_number": track_number,
        "track_title": f"Track {track_number}",
        "artist_name": "Artist",
        "artist_mbid": None,
        "album_artist_name": "Artist",
        "album_artist_mbid": album_artist_mbid,
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
    if release_type is not _ABSENT:
        row["release_type"] = release_type
    return row


def _pragma_columns(db_path: Path, table: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db_path) as connection:
        return {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }


@pytest.mark.asyncio
async def test_release_type_column_is_idempotent_and_nullable(
    tmp_path: Path, db: LibraryDB
) -> None:
    """Constructing the store twice keeps one nullable column; legacy rows read NULL."""
    LibraryDB(db_path=tmp_path / "test.db", write_lock=threading.Lock())
    assert "release_type" in _pragma_columns(tmp_path / "test.db", "library_files")

    file_id = await db.upsert_library_file(
        _file_row("rg-legacy", file_path="/lib/legacy/1.flac")
    )
    row = await db.get_library_file_by_id(file_id)
    assert row is not None
    assert row["release_type"] is None


@pytest.mark.asyncio
async def test_album_aggregate_reports_dominant_release_type(db: LibraryDB) -> None:
    """Most frequent non-empty value wins; NULL rows do not vote."""
    await db.upsert_library_file(
        _file_row("rg-ep", file_path="/lib/ep/1.flac", track_number=1, release_type="EP")
    )
    await db.upsert_library_file(
        _file_row("rg-ep", file_path="/lib/ep/2.flac", track_number=2, release_type="EP")
    )
    await db.upsert_library_file(
        _file_row(
            "rg-ep", file_path="/lib/ep/3.flac", track_number=3, release_type="Single"
        )
    )
    await db.upsert_library_file(
        _file_row("rg-ep", file_path="/lib/ep/4.flac", track_number=4, release_type=None)
    )

    rows, _total = await db.get_albums_aggregated(limit=10, offset=0)
    assert rows[0]["release_type"] == "EP"
    assert "release_type_values" not in rows[0]

    summary = LibraryManager._to_summary(rows[0])
    assert summary.release_type == "EP"


@pytest.mark.asyncio
async def test_album_aggregate_tie_breaks_by_earliest_track(db: LibraryDB) -> None:
    """Equal counts keep the disc/track-earliest value."""
    await db.upsert_library_file(
        _file_row(
            "rg-tie", file_path="/lib/tie/1.flac", track_number=1, release_type="Single"
        )
    )
    await db.upsert_library_file(
        _file_row(
            "rg-tie", file_path="/lib/tie/2.flac", track_number=2, release_type="EP"
        )
    )

    rows, _total = await db.get_albums_aggregated(limit=10, offset=0)
    assert rows[0]["release_type"] == "Single"


@pytest.mark.asyncio
async def test_album_aggregate_without_types_stays_none(db: LibraryDB) -> None:
    """Blanks and NULLs never surface: legacy output (None) is preserved."""
    await db.upsert_library_file(
        _file_row("rg-none", file_path="/lib/none/1.flac", track_number=1)
    )
    await db.upsert_library_file(
        _file_row(
            "rg-none", file_path="/lib/none/2.flac", track_number=2, release_type="  "
        )
    )

    rows, _total = await db.get_albums_aggregated(limit=10, offset=0)
    assert rows[0]["release_type"] is None
    assert LibraryManager._to_summary(rows[0]).release_type is None


@pytest.mark.asyncio
async def test_albums_for_artist_carries_dominant_release_type(db: LibraryDB) -> None:
    """The getArtist aggregate exposes the same dominant value."""
    mbid = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
    await db.upsert_library_file(
        _file_row(
            "rg-artist",
            file_path="/lib/artist/1.flac",
            track_number=1,
            album_artist_mbid=mbid,
            release_type="Soundtrack",
        )
    )
    await db.upsert_library_file(
        _file_row(
            "rg-artist",
            file_path="/lib/artist/2.flac",
            track_number=2,
            album_artist_mbid=mbid,
            release_type="Soundtrack",
        )
    )

    rows = await db.get_albums_for_artist(mbid)
    assert rows[0]["release_type"] == "Soundtrack"


@pytest.mark.asyncio
async def test_album_track_rows_carry_release_type(db: LibraryDB) -> None:
    """Per-track reads surface the raw stored value (or NULL)."""
    await db.upsert_library_file(
        _file_row("rg-rows", file_path="/lib/rows/1.flac", track_number=1, release_type="EP")
    )
    await db.upsert_library_file(
        _file_row("rg-rows", file_path="/lib/rows/2.flac", track_number=2)
    )

    rows = await db.get_library_files_for_album("rg-rows")
    assert [row["release_type"] for row in rows] == ["EP", None]


def _tag(**overrides: object) -> AudioTag:
    fields: dict = {
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
        "album_artist": "Artist",
        "track_number": 1,
    }
    fields.update(overrides)
    return AudioTag(**fields)  # type: ignore[arg-type]


def _info() -> AudioInfo:
    return AudioInfo(
        duration_seconds=120,
        bitrate=900,
        sample_rate=44_100,
        channels=2,
        file_format="flac",
        file_size_bytes=100,
        bit_depth=16,
    )


@pytest.mark.asyncio
async def test_manager_upsert_writes_tag_release_type(
    tmp_path: Path, db: LibraryDB
) -> None:
    """The scan write path persists tag.release_type onto the row and summary."""
    manager = LibraryManager(db)
    file_id = await manager.upsert_file(
        tmp_path / "song.flac",
        _tag(release_type="EP"),
        _info(),
        release_group_mbid="rg-managed",
        recording_mbid="rec-1",
        file_mtime=1.0,
    )

    row = await db.get_library_file_by_id(file_id)
    assert row is not None
    assert row["release_type"] == "EP"

    rows, _total = await db.get_albums_aggregated(limit=10, offset=0)
    assert LibraryManager._to_summary(rows[0]).release_type == "EP"
