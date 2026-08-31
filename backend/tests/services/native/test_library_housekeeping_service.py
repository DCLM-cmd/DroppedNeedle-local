"""What the organizer tidies up once a rescan has finished.

Three ways a library gets untidy, all of them seen in the live catalog: the same song
present twice because the album arrived again in another format, one album split
across several catalog rows so half of it looks missing, and folders left holding
nothing that plays.
"""

import sqlite3
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.library_housekeeping_service import (
    LibraryHousekeepingService,
    _quality_rank,
)


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


def _album(connection, album_id="album-1", title="An Album") -> None:
    connection.execute(
        "INSERT INTO local_albums(id,root_id,grouping_key,title,title_folded,"
        "album_artist_id,grouping_source,created_at,updated_at) "
        "VALUES (?,'root-1',?,?,?,'artist-1','automatic',1.0,1.0)",
        (album_id, album_id, title, title.casefold()),
    )


def _track(
    connection,
    track_id: str,
    *,
    path: str,
    fmt: str,
    bit_rate: int | None,
    track_number: int = 1,
    album_id: str = "album-1",
    size: int = 1000,
) -> None:
    connection.execute(
        "INSERT INTO local_tracks(id,local_album_id,root_id,file_path,relative_path,"
        "path_hash,file_size_bytes,file_mtime_ns,stat_revision,title,title_folded,"
        "album_title,album_title_folded,file_format,bit_rate,track_number,"
        "disc_number,ingest_source,imported_at,membership_source,availability) "
        "VALUES (?,?,'root-1',?,?,?,?,1,'r','A Song','a song','An Album','an album',"
        "?,?,?,1,'scan',1.0,'automatic','indexed')",
        (track_id, album_id, path, path, track_id, size, fmt, bit_rate, track_number),
    )


def _service(store, tmp_path, **kwargs) -> LibraryHousekeepingService:
    return LibraryHousekeepingService(
        store, recycle_bin=tmp_path / "recycle", clock=lambda: 1_000.0, **kwargs
    )


# ---- keeping the better copy -------------------------------------------------------

def test_lossless_outranks_a_lossy_copy_whatever_its_bitrate() -> None:
    """The question the user asked in as many words: keep the FLAC, drop the MP3.
    A 320 kbps mp3 carries a bigger number than a flac reports, so ranking on bitrate
    alone would have kept exactly the wrong one."""
    flac = {"file_format": "flac", "bit_rate": None, "file_size_bytes": 30_000_000}
    mp3 = {"file_format": "mp3", "bit_rate": 320, "file_size_bytes": 9_000_000}

    assert _quality_rank(flac) > _quality_rank(mp3)


def test_a_higher_bitrate_wins_inside_the_same_tier() -> None:
    assert _quality_rank({"file_format": "mp3", "bit_rate": 320}) > _quality_rank(
        {"file_format": "mp3", "bit_rate": 192}
    )


def test_size_settles_a_tie_between_two_lossless_copies() -> None:
    """Two flacs of different depth must resolve the same way every run, not by the
    order rows happen to come back in."""
    deeper = {"file_format": "flac", "bit_rate": None, "file_size_bytes": 60_000_000}
    shallower = {"file_format": "flac", "bit_rate": None, "file_size_bytes": 30_000_000}

    assert _quality_rank(deeper) > _quality_rank(shallower)


@pytest.mark.asyncio
async def test_the_mp3_is_retired_and_the_flac_kept(store, db_path, tmp_path):
    library = tmp_path / "music"
    library.mkdir()
    good, bad = library / "01 - Song.flac", library / "01 - Song.mp3"
    good.write_bytes(b"lossless")
    bad.write_bytes(b"lossy")
    with sqlite3.connect(db_path) as connection:
        _album(connection)
        _track(connection, "t-flac", path=str(good), fmt="flac", bit_rate=None)
        _track(connection, "t-mp3", path=str(bad), fmt="mp3", bit_rate=320)

    assert await _service(store, tmp_path).deduplicate() == 1

    assert good.exists()
    assert not bad.exists()
    with sqlite3.connect(db_path) as connection:
        rows = dict(
            connection.execute(
                "SELECT id, availability FROM local_tracks"
            ).fetchall()
        )
    assert rows == {"t-flac": "indexed", "t-mp3": "missing"}


@pytest.mark.asyncio
async def test_the_retired_copy_goes_to_the_recycle_bin(store, db_path, tmp_path):
    """An upgrade that turns out to have been wrong has to stay recoverable."""
    library = tmp_path / "music"
    library.mkdir()
    (library / "01 - Song.flac").write_bytes(b"lossless")
    (library / "01 - Song.mp3").write_bytes(b"the older bytes")
    with sqlite3.connect(db_path) as connection:
        _album(connection)
        _track(connection, "t-flac", path=str(library / "01 - Song.flac"),
               fmt="flac", bit_rate=None)
        _track(connection, "t-mp3", path=str(library / "01 - Song.mp3"),
               fmt="mp3", bit_rate=320)

    await _service(store, tmp_path).deduplicate()

    recycled = list((tmp_path / "recycle").rglob("*.mp3"))
    assert len(recycled) == 1
    assert recycled[0].read_bytes() == b"the older bytes"


@pytest.mark.asyncio
async def test_without_a_recycle_bin_nothing_is_removed(store, db_path, tmp_path):
    """Deleting the only copy of something in order to tidy up would be worse than
    the untidiness."""
    library = tmp_path / "music"
    library.mkdir()
    (library / "01 - Song.flac").write_bytes(b"lossless")
    (library / "01 - Song.mp3").write_bytes(b"lossy")
    with sqlite3.connect(db_path) as connection:
        _album(connection)
        _track(connection, "t-flac", path=str(library / "01 - Song.flac"),
               fmt="flac", bit_rate=None)
        _track(connection, "t-mp3", path=str(library / "01 - Song.mp3"),
               fmt="mp3", bit_rate=320)
    service = LibraryHousekeepingService(store, recycle_bin=None)

    assert await service.deduplicate() == 0
    assert (library / "01 - Song.mp3").exists()


@pytest.mark.asyncio
async def test_different_tracks_are_not_duplicates(store, db_path, tmp_path):
    library = tmp_path / "music"
    library.mkdir()
    for number in (1, 2):
        (library / f"0{number}.flac").write_bytes(b"x")
    with sqlite3.connect(db_path) as connection:
        _album(connection)
        _track(connection, "t-1", path=str(library / "01.flac"), fmt="flac",
               bit_rate=None, track_number=1)
        _track(connection, "t-2", path=str(library / "02.flac"), fmt="flac",
               bit_rate=None, track_number=2)

    assert await _service(store, tmp_path).deduplicate() == 0


@pytest.mark.asyncio
async def test_the_same_position_in_different_albums_is_not_a_duplicate(
    store, db_path, tmp_path
):
    library = tmp_path / "music"
    library.mkdir()
    (library / "a.flac").write_bytes(b"x")
    (library / "b.flac").write_bytes(b"x")
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-1", "First")
        _album(connection, "album-2", "Second")
        _track(connection, "t-1", path=str(library / "a.flac"), fmt="flac",
               bit_rate=None, album_id="album-1")
        _track(connection, "t-2", path=str(library / "b.flac"), fmt="flac",
               bit_rate=None, album_id="album-2")

    assert await _service(store, tmp_path).deduplicate() == 0


# ---- folders with nothing that plays ------------------------------------------------

@pytest.mark.asyncio
async def test_a_folder_holding_only_artwork_is_removed(store, tmp_path):
    library = tmp_path / "music"
    (library / "Artist" / "Album (2024)").mkdir(parents=True)
    (library / "Artist" / "Album (2024)" / "cover.jpg").write_bytes(b"art")
    service = _service(store, tmp_path, library_roots=lambda: [library])

    assert await service.remove_empty_folders() >= 1
    assert not (library / "Artist").exists()


@pytest.mark.asyncio
async def test_a_folder_with_music_is_left_alone(store, tmp_path):
    library = tmp_path / "music"
    album = library / "Artist" / "Album (2024)"
    album.mkdir(parents=True)
    (album / "01 - Song.flac").write_bytes(b"audio")
    (album / "cover.jpg").write_bytes(b"art")
    service = _service(store, tmp_path, library_roots=lambda: [library])

    assert await service.remove_empty_folders() == 0
    assert (album / "01 - Song.flac").exists()


@pytest.mark.asyncio
async def test_an_artist_folder_survives_while_one_album_still_has_music(
    store, tmp_path
):
    library = tmp_path / "music"
    keep = library / "Artist" / "Kept (2024)"
    drop = library / "Artist" / "Empty (2023)"
    keep.mkdir(parents=True)
    drop.mkdir(parents=True)
    (keep / "01.flac").write_bytes(b"audio")
    service = _service(store, tmp_path, library_roots=lambda: [library])

    await service.remove_empty_folders()

    assert keep.exists() and not drop.exists()


@pytest.mark.asyncio
async def test_the_recycle_bin_is_never_swept(store, tmp_path):
    """It exists to hold what earlier passes deliberately preserved."""
    library = tmp_path / "music"
    (library / ".recycle" / "20260101T000000-duplicates").mkdir(parents=True)
    service = _service(store, tmp_path, library_roots=lambda: [library])

    await service.remove_empty_folders()

    assert (library / ".recycle" / "20260101T000000-duplicates").exists()


@pytest.mark.asyncio
async def test_the_library_root_itself_is_never_removed(store, tmp_path):
    library = tmp_path / "music"
    library.mkdir()
    service = _service(store, tmp_path, library_roots=lambda: [library])

    await service.remove_empty_folders()

    assert library.exists()


@pytest.mark.asyncio
async def test_no_roots_configured_means_nothing_to_sweep(store, tmp_path):
    assert await _service(store, tmp_path).remove_empty_folders() == 0


# ---- merging, and the whole run ------------------------------------------------------

@pytest.mark.asyncio
async def test_merging_asks_the_catalog_to_fold_split_albums(store, tmp_path):
    hygiene = AsyncMock()
    hygiene.enqueue_backfill = AsyncMock(return_value={"expected_work_count": 7})
    service = _service(store, tmp_path, hygiene=hygiene)

    assert await service.merge_split_albums() == 7
    hygiene.enqueue_backfill.assert_awaited_once()


@pytest.mark.asyncio
async def test_one_failing_pass_does_not_stop_the_others(store, tmp_path):
    """A scan that found the music is not undone by untidy shelves."""
    service = _service(store, tmp_path)
    service.deduplicate = AsyncMock(side_effect=OSError("the disk hiccuped"))
    service.merge_split_albums = AsyncMock(return_value=3)
    service.rehome_retired_tracks = AsyncMock(return_value=5)
    service.purge_superseded_rows = AsyncMock(return_value=4)
    service.empty_recycle_bin = AsyncMock(return_value=1)
    service.remove_empty_folders = AsyncMock(return_value=2)

    counts = await service.run_after_scan()

    assert counts == {
        "tracks_rehomed": 5,
        "deduplicated": 0,
        "merged": 3,
        "rows_purged": 4,
        "recycled_removed": 1,
        "folders_removed": 2,
    }


@pytest.mark.asyncio
async def test_the_recycle_bin_is_resolved_per_run(store, tmp_path):
    """A bin configured after the service was built must still be used - the provider
    passes a callable for exactly that reason."""
    library = tmp_path / "music"
    library.mkdir()
    (library / "01 - Song.flac").write_bytes(b"lossless")
    (library / "01 - Song.mp3").write_bytes(b"lossy")
    with sqlite3.connect(store.db_path) as connection:
        _album(connection)
        _track(connection, "t-flac", path=str(library / "01 - Song.flac"),
               fmt="flac", bit_rate=None)
        _track(connection, "t-mp3", path=str(library / "01 - Song.mp3"),
               fmt="mp3", bit_rate=320)
    configured: list[Path] = []
    service = LibraryHousekeepingService(
        store, recycle_bin=lambda: (configured[0] if configured else None)
    )

    assert await service.deduplicate() == 0  # no bin yet: nothing is touched
    configured.append(tmp_path / "bin")

    assert await service.deduplicate() == 1
    assert list((tmp_path / "bin").rglob("*.mp3"))


@pytest.mark.asyncio
async def test_a_retired_album_row_does_not_hide_its_duplicates(store, db_path, tmp_path):
    """Retiring moves the ALBUM row, not the files hanging off it.

    Straight from the live catalog: one album sat with eleven flacs and eleven mp3s of
    the same songs while the sweep reported none, because the row they hung on had
    been retired into its sibling and the query skipped retired rows outright.
    """
    library = tmp_path / "music"
    library.mkdir()
    (library / "01 - Song.flac").write_bytes(b"lossless")
    (library / "01 - Song.mp3").write_bytes(b"lossy")
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-surviving", "An Album")
        _album(connection, "album-retired", "An Album")
        connection.execute(
            "UPDATE local_albums SET retired_into_album_id='album-surviving' "
            "WHERE id='album-retired'"
        )
        _track(connection, "t-flac", path=str(library / "01 - Song.flac"),
               fmt="flac", bit_rate=None, album_id="album-retired")
        _track(connection, "t-mp3", path=str(library / "01 - Song.mp3"),
               fmt="mp3", bit_rate=320, album_id="album-retired")

    assert await _service(store, tmp_path).deduplicate() == 1
    assert (library / "01 - Song.flac").exists()
    assert not (library / "01 - Song.mp3").exists()


@pytest.mark.asyncio
async def test_copies_split_across_a_retirement_are_still_one_position(
    store, db_path, tmp_path
):
    """The two copies can end up on either side of the merge - they are still the
    same song at the same position of the same album."""
    library = tmp_path / "music"
    library.mkdir()
    (library / "01 - Song.flac").write_bytes(b"lossless")
    (library / "01 - Song.mp3").write_bytes(b"lossy")
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-surviving", "An Album")
        _album(connection, "album-retired", "An Album")
        connection.execute(
            "UPDATE local_albums SET retired_into_album_id='album-surviving' "
            "WHERE id='album-retired'"
        )
        _track(connection, "t-flac", path=str(library / "01 - Song.flac"),
               fmt="flac", bit_rate=None, album_id="album-surviving")
        _track(connection, "t-mp3", path=str(library / "01 - Song.mp3"),
               fmt="mp3", bit_rate=320, album_id="album-retired")

    assert await _service(store, tmp_path).deduplicate() == 1
    assert not (library / "01 - Song.mp3").exists()


# ---- the bin is emptied, and un-filed from the catalog ------------------------------

@pytest.mark.asyncio
async def test_recycled_files_are_taken_out_of_the_catalog(store, tmp_path):
    """Files were pruned on a timer, but the rows naming them stayed - still pointing
    into the bin, still counted as part of the album they had been removed from. One
    such row was enough to make an album read as holding a track it no longer has."""
    service = _service(store, tmp_path)
    service._store.purge_recycled_track_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"removed": 1, "detached": 0}
    )
    (tmp_path / "recycle").mkdir()

    await service.empty_recycle_bin()

    service._store.purge_recycled_track_rows.assert_awaited_once_with(
        str(tmp_path / "recycle")
    )


@pytest.mark.asyncio
async def test_entries_inside_the_retention_window_are_kept(store, tmp_path):
    """What is inside the window is what makes a wrong upgrade recoverable."""
    import time as _time

    bin_path = tmp_path / "recycle"
    entry = bin_path / f"{_time.strftime('%Y%m%dT%H%M%S')}-duplicates"
    entry.mkdir(parents=True)
    (entry / "old.mp3").write_bytes(b"the previous copy")
    service = _service(store, tmp_path, recycle_retention_days=lambda: 30)
    service._store.purge_recycled_track_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"removed": 0, "detached": 0}
    )

    assert await service.empty_recycle_bin() == 0
    assert (entry / "old.mp3").exists()


@pytest.mark.asyncio
async def test_entries_past_the_window_are_deleted(store, tmp_path):
    bin_path = tmp_path / "recycle"
    entry = bin_path / "20200101T000000-duplicates"
    entry.mkdir(parents=True)
    (entry / "old.mp3").write_bytes(b"long expired")
    service = _service(store, tmp_path, recycle_retention_days=lambda: 30)
    service._store.purge_recycled_track_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"removed": 0, "detached": 0}
    )

    assert await service.empty_recycle_bin() == 1
    assert not entry.exists()


@pytest.mark.asyncio
async def test_no_bin_configured_is_not_an_error(store, tmp_path):
    service = LibraryHousekeepingService(store, recycle_bin=None)

    assert await service.empty_recycle_bin() == 0


@pytest.mark.asyncio
async def test_a_retired_row_follows_its_file_into_the_bin(store, db_path, tmp_path):
    """Left pointing at the library path it no longer occupies, the row reads as a
    second copy of the song that is merely missing - which is what made an album look
    like it still held both, and what blocked a later import from taking the slot."""
    library = tmp_path / "music"
    library.mkdir()
    (library / "01 - Song.flac").write_bytes(b"lossless")
    (library / "01 - Song.mp3").write_bytes(b"lossy")
    with sqlite3.connect(db_path) as connection:
        _album(connection)
        _track(connection, "t-flac", path=str(library / "01 - Song.flac"),
               fmt="flac", bit_rate=None)
        _track(connection, "t-mp3", path=str(library / "01 - Song.mp3"),
               fmt="mp3", bit_rate=320)

    await _service(store, tmp_path).deduplicate()

    with sqlite3.connect(db_path) as connection:
        path = connection.execute(
            "SELECT file_path FROM local_tracks WHERE id='t-mp3'"
        ).fetchone()[0]
    assert str(tmp_path / "recycle") in path


@pytest.mark.asyncio
async def test_the_kept_copy_keeps_its_own_path(store, db_path, tmp_path):
    library = tmp_path / "music"
    library.mkdir()
    (library / "01 - Song.flac").write_bytes(b"lossless")
    (library / "01 - Song.mp3").write_bytes(b"lossy")
    with sqlite3.connect(db_path) as connection:
        _album(connection)
        _track(connection, "t-flac", path=str(library / "01 - Song.flac"),
               fmt="flac", bit_rate=None)
        _track(connection, "t-mp3", path=str(library / "01 - Song.mp3"),
               fmt="mp3", bit_rate=320)

    await _service(store, tmp_path).deduplicate()

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT file_path, availability FROM local_tracks WHERE id='t-flac'"
        ).fetchone()
    assert row == (str(library / "01 - Song.flac"), "indexed")
