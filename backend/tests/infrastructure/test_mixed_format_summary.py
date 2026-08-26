"""F-PERF-10: truthful mixed-format album summaries.

Signed display policy: a homogeneous album exposes its normalized format; an
album whose indexed tracks carry more than one normalized non-empty format
exposes the literal ``mixed``. Lexical ``MAX`` is never used as a policy -
string ordering is not a quality ranking. Applies identically to album browse
and artist-appearance projections."""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore
from tests.infrastructure.test_native_library_store import (
    _membership,
    _seed_auth,
)


def _add_track(db_path: Path, track_id: str, fmt: str, *, album_id: str = "album-1") -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO local_tracks (id, local_album_id, root_id, "
            "relative_path, file_path, path_hash, file_size_bytes, "
            "file_mtime_ns, stat_revision, tag_revision, title, title_folded, "
            "artist_name, artist_name_folded, album_title, album_title_folded, "
            "album_artist_name, album_artist_name_folded, ingest_source, "
            "stat_revision_kind, membership_source, disc_number, track_number, "
            "duration_seconds, file_format, availability, imported_at) VALUES "
            "(?, ?, 'root-1', ?, ?, ?, 90, 200, ?, 'tag-x', ?, ?, "
            "'Artist 1', 'artist 1', 'Album 1', 'album 1', 'Artist 1', "
            "'artist 1', 'scan', 'exact', 'automatic', 1, 2, 120.0, ?, "
            "'indexed', 5)",
            (
                track_id,
                album_id,
                f"{track_id}.x",
                f"/music/{track_id}.x",
                f"hash-{track_id}",
                f"stat-{track_id}",
                f"Track {track_id}",
                f"track {track_id}",
                fmt,
            ),
        )


def _add_contributor(db_path: Path, artist_id: str, track_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO local_artists (id, display_name, folded_name, kind, "
            "created_at, updated_at) VALUES (?, 'Guest', 'guest', 'person', 1, 1)",
            (artist_id,),
        )
        conn.execute(
            "INSERT INTO local_track_artists (local_track_id, position, "
            "local_artist_id, role, join_phrase) VALUES (?, 1, ?, 'featured', '')",
            (track_id, artist_id),
        )


@pytest.fixture
def store(tmp_path: Path) -> NativeLibraryStore:
    path = tmp_path / "library.db"
    _seed_auth(path)
    return NativeLibraryStore(path, threading.Lock())


def _summary_format(store: NativeLibraryStore, album_id: str) -> str:
    rows, total = asyncio.run(store.list_target_albums(limit=50))
    assert any(r["release_group_mbid"] == album_id for r in rows)
    return next(r["file_format"] for r in rows if r["release_group_mbid"] == album_id)


def test_homogeneous_albums_keep_normalized_formats(store: NativeLibraryStore):
    asyncio.run(store.create_catalog_membership(_membership("1")))  # flac
    _add_track(store.db_path, "t-1b", "FLAC")  # same format, different case

    # homogeneous mp3 album
    await_done = asyncio.run(store.create_catalog_membership(_membership("2")))
    assert await_done >= 1
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE local_tracks SET file_format='mp3' WHERE local_album_id='album-2'")

    assert _summary_format(store, "album-1") == "flac"
    assert _summary_format(store, "album-2") == "mp3"


def test_mixed_album_returns_explicit_mixed_in_browse_and_appearances(
    store: NativeLibraryStore,
):
    asyncio.run(store.create_catalog_membership(_membership("1")))
    _add_track(store.db_path, "t-1b", "mp3")  # album-1 already has a flac track
    _add_contributor(store.db_path, "artist-guest", "t-1b")

    assert _summary_format(store, "album-1") == "mixed"

    appearances, _total, _tracks = asyncio.run(
        store.list_target_artist_appearances("artist-guest", limit=10)
    )
    app_album = next(
        a["album"]
        for a in appearances
        if a["album"]["release_group_mbid"] == "album-1"
    )
    assert app_album["file_format"] == "mixed"


def test_format_filter_summary_reflects_the_included_subset(
    store: NativeLibraryStore,
):
    asyncio.run(store.create_catalog_membership(_membership("1")))
    _add_track(store.db_path, "t-1b", "mp3")

    # Track-level filter unchanged: only the flac track qualifies, so the
    # filtered page's grouped subset is homogeneous and reports 'flac'.
    rows, total = asyncio.run(
        store.list_target_albums(file_format="flac", limit=10)
    )
    assert total == 1
    assert rows[0]["file_format"] == "flac"

    mp3_rows, mp3_total = asyncio.run(
        store.list_target_albums(file_format="MP3", limit=10)  # case-insensitive
    )
    assert mp3_total == 1
    assert mp3_rows[0]["file_format"] == "mp3"


def test_unknown_formats_are_distinct_for_the_mixed_check(
    store: NativeLibraryStore,
):
    """D20: every admitted non-empty format counts as distinct for the mixed
    check; an unfamiliar value is not collapsed into a known fallback."""
    asyncio.run(store.create_catalog_membership(_membership("3")))
    _add_track(store.db_path, "t-3b", "wv", album_id="album-3")

    # flac + wv are two distinct normalized formats -> 'mixed', not 'flac'.
    assert _summary_format(store, "album-3") == "mixed"


def test_empty_formats_do_not_participate_in_the_mixed_check(
    store: NativeLibraryStore,
):
    asyncio.run(store.create_catalog_membership(_membership("3")))
    _add_track(store.db_path, "t-3b", "", album_id="album-3")

    # only one non-empty normalized format exists -> homogeneous 'flac'.
    assert _summary_format(store, "album-3") == "flac"
