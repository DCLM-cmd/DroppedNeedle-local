"""File-tag release types through local_tracks: scan writes, reads, aggregate."""

import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.audio import AudioInfo, AudioTag
from models.library_work import ScanRun
from services.native.library_indexer import LibraryIndexer


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
    return path


def _tag(release_type: str | None, *, title: str = "Track", track_number: int = 1) -> AudioTag:
    return AudioTag(
        title=title,
        artist="Artist",
        album="Album",
        album_artist="Artist",
        track_number=track_number,
        release_type=release_type,
    )


def _info() -> AudioInfo:
    return AudioInfo(
        duration_seconds=180,
        bitrate=900,
        sample_rate=44_100,
        channels=2,
        file_format="flac",
        file_size_bytes=100,
        bit_depth=16,
    )


def _item(name: str, *, track_id: str | None = None) -> dict:
    return {
        "root_id": "root-1",
        "relative_path": f"Album/{name}",
        "absolute_path": f"/music/Album/{name}",
        "local_track_id": track_id,
        "file_size_bytes": 100,
        "file_mtime_ns": 1_000_000_000,
        "stat_revision": f"stat-{name}",
        "policy_revision": "policy-1",
        "effective_policy": "automatic",
        "comparison_result": "new",
    }


_INCREMENTS = {
    "inspected_count": 1,
    "new_count": 1,
    "changed_count": 0,
    "indexed_count": 1,
    "unchanged_count": 0,
    "excluded_count": 0,
    "errored_count": 0,
}


@pytest.mark.asyncio
async def test_local_tracks_release_type_is_idempotent_and_nullable(
    tmp_path: Path,
) -> None:
    """Constructing the store twice keeps one nullable column."""
    path = _database(tmp_path)
    NativeLibraryStore(path, threading.Lock())
    NativeLibraryStore(path, threading.Lock())

    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(local_tracks)").fetchall()
        }
    assert "release_type" in columns


def test_prepare_tagged_carries_release_type() -> None:
    """The scan track build writes tag.release_type onto the LocalTrack."""
    indexer = LibraryIndexer(object(), object())  # type: ignore[arg-type]
    write = indexer._prepare_tagged(
        "scan-1", _item("track.flac"), _tag("EP"), _info(), now=100.0
    )

    assert write.track.release_type == "EP"


@pytest.mark.asyncio
async def test_scan_batch_round_trip_and_album_dominant(tmp_path: Path) -> None:
    """Insert + rescan UPDATE persist values; the album aggregate reports dominant."""
    store = NativeLibraryStore(_database(tmp_path), threading.Lock())
    await store.create_scan_run(
        ScanRun(
            id="scan-1",
            kind="incremental",
            trigger="manual",
            state="indexing",
            phase="indexing",
            queued_at=1,
            updated_at=1,
        )
    )
    indexer = LibraryIndexer(store, object())  # type: ignore[arg-type]
    specs = [
        ("01.flac", "EP", "One", 1),
        ("02.flac", "EP", "Two", 2),
        ("03.flac", "Single", "Three", 3),
        ("04.flac", None, "Four", 4),
    ]
    writes = [
        indexer._prepare_tagged(
            "scan-1",
            _item(name),
            _tag(release_type, title=title, track_number=track_number),
            _info(),
            now=100.0,
        )
        for name, release_type, title, track_number in specs
    ]
    album_id = writes[0].album.id
    assert {write.album.id for write in writes} == {album_id}

    for updated_at in (2, 3):
        await store.commit_scan_index_batch(
            "scan-1",
            writes=writes,
            states={},
            failures=[],
            increments=dict(_INCREMENTS),
            updated_at=updated_at,
        )

    tracks = await store.get_target_album_tracks(album_id)
    assert {track["track_title"]: track["release_type"] for track in tracks} == {
        "One": "EP",
        "Two": "EP",
        "Three": "Single",
        "Four": None,
    }

    rows, _total = await store.list_target_albums(limit=10, offset=0, sort="name")
    row = next(item for item in rows if item["release_group_mbid"] == album_id)
    assert row["release_type"] == "EP"
    assert "release_type_values" not in row


@pytest.mark.asyncio
async def test_scan_batch_tie_breaks_by_earliest_track(tmp_path: Path) -> None:
    """Equal counts keep the disc/track-earliest value on the native aggregate."""
    store = NativeLibraryStore(_database(tmp_path), threading.Lock())
    await store.create_scan_run(
        ScanRun(
            id="scan-1",
            kind="incremental",
            trigger="manual",
            state="indexing",
            phase="indexing",
            queued_at=1,
            updated_at=1,
        )
    )
    indexer = LibraryIndexer(store, object())  # type: ignore[arg-type]
    writes = [
        indexer._prepare_tagged(
            "scan-1",
            _item("01.flac"),
            _tag("Single", title="One", track_number=1),
            _info(),
            now=100.0,
        ),
        indexer._prepare_tagged(
            "scan-1",
            _item("02.flac"),
            _tag("EP", title="Two", track_number=2),
            _info(),
            now=100.0,
        ),
    ]
    await store.commit_scan_index_batch(
        "scan-1",
        writes=writes,
        states={},
        failures=[],
        increments=dict(_INCREMENTS),
        updated_at=2,
    )

    rows, _total = await store.list_target_albums(limit=10, offset=0, sort="name")
    assert rows[0]["release_type"] == "Single"


@pytest.mark.asyncio
async def test_rescan_refreshes_release_type(tmp_path: Path) -> None:
    """A rescan that re-tags the type overwrites the stored value (no stale win)."""
    store = NativeLibraryStore(_database(tmp_path), threading.Lock())
    await store.create_scan_run(
        ScanRun(
            id="scan-1",
            kind="incremental",
            trigger="manual",
            state="indexing",
            phase="indexing",
            queued_at=1,
            updated_at=1,
        )
    )
    indexer = LibraryIndexer(store, object())  # type: ignore[arg-type]
    await store.commit_scan_index_batch(
        "scan-1",
        writes=[
            indexer._prepare_tagged(
                "scan-1", _item("track.flac"), _tag("EP"), _info(), now=100.0
            )
        ],
        states={},
        failures=[],
        increments=dict(_INCREMENTS),
        updated_at=2,
    )
    await store.commit_scan_index_batch(
        "scan-1",
        writes=[
            indexer._prepare_tagged(
                "scan-1", _item("track.flac"), _tag("Single"), _info(), now=101.0
            )
        ],
        states={},
        failures=[],
        increments=dict(_INCREMENTS),
        updated_at=3,
    )

    rows, _total = await store.list_target_albums(limit=10, offset=0, sort="name")
    assert rows[0]["release_type"] == "Single"
