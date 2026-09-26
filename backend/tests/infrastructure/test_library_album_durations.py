"""Album runtimes through library_files: aggregate sum, summary mapping.

Legacy aggregates GROUP BY release group; the duration sum lets compat album
DTOs carry a real RunTimeTicks instead of bucketing everything as Singles.
"""

import threading
from pathlib import Path

import pytest

from infrastructure.persistence.library_db import LibraryDB
from services.native.library_manager import LibraryManager


@pytest.fixture
def db(tmp_path: Path) -> LibraryDB:
    return LibraryDB(db_path=tmp_path / "test.db", write_lock=threading.Lock())


def _file_row(
    rg_mbid: str,
    *,
    file_path: str,
    track_number: int = 1,
    disc_number: int = 1,
    album_artist_mbid: str | None = None,
    duration_seconds: float | None = None,
) -> dict:
    """Minimal library_files row; default duration mimics a legacy NULL row."""
    return {
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
        "duration_seconds": duration_seconds,
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
async def test_album_aggregate_sums_track_durations(db: LibraryDB) -> None:
    """The GROUP BY row carries the summed runtime of its tracks."""
    await db.upsert_library_file(
        _file_row("rg-dur", file_path="/lib/dur/1.flac", track_number=1,
                  duration_seconds=201.0)
    )
    await db.upsert_library_file(
        _file_row("rg-dur", file_path="/lib/dur/2.flac", track_number=2,
                  duration_seconds=202.0)
    )

    rows, _total = await db.get_albums_aggregated(limit=10, offset=0)
    assert rows[0]["total_duration_seconds"] == pytest.approx(403.0)

    summary = LibraryManager._to_summary(rows[0])
    assert summary.total_duration_seconds == pytest.approx(403.0)


@pytest.mark.asyncio
async def test_album_aggregate_without_durations_stays_none(db: LibraryDB) -> None:
    """All-NULL durations never surface as a fake zero: legacy output (None)."""
    await db.upsert_library_file(
        _file_row("rg-nodur", file_path="/lib/nodur/1.flac", track_number=1)
    )
    await db.upsert_library_file(
        _file_row("rg-nodur", file_path="/lib/nodur/2.flac", track_number=2)
    )

    rows, _total = await db.get_albums_aggregated(limit=10, offset=0)
    assert rows[0]["total_duration_seconds"] in (None, 0)
    assert LibraryManager._to_summary(rows[0]).total_duration_seconds is None


@pytest.mark.asyncio
async def test_album_aggregate_partial_durations_sum_known_values(
    db: LibraryDB,
) -> None:
    """NULL tracks contribute nothing; known durations still sum."""
    await db.upsert_library_file(
        _file_row("rg-part", file_path="/lib/part/1.flac", track_number=1,
                  duration_seconds=180.0)
    )
    await db.upsert_library_file(
        _file_row("rg-part", file_path="/lib/part/2.flac", track_number=2)
    )

    rows, _total = await db.get_albums_aggregated(limit=10, offset=0)
    assert rows[0]["total_duration_seconds"] == pytest.approx(180.0)
    assert LibraryManager._to_summary(rows[0]).total_duration_seconds == pytest.approx(
        180.0
    )


@pytest.mark.asyncio
async def test_albums_for_artist_carries_summed_durations(db: LibraryDB) -> None:
    """The getArtist aggregate exposes the same summed runtime."""
    mbid = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
    await db.upsert_library_file(
        _file_row("rg-adur", file_path="/lib/adur/1.flac", track_number=1,
                  album_artist_mbid=mbid, duration_seconds=100.0)
    )
    await db.upsert_library_file(
        _file_row("rg-adur", file_path="/lib/adur/2.flac", track_number=2,
                  album_artist_mbid=mbid, duration_seconds=150.0)
    )

    rows = await db.get_albums_for_artist(mbid)
    assert rows[0]["total_duration_seconds"] == pytest.approx(250.0)
