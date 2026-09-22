"""Issue #242 - file-tag release types surface as OpenSubsonic releaseTypes."""

from types import SimpleNamespace

import pytest

from api.compat.subsonic.models import release_types_for_album, to_album_id3
from services.compat.library_view_service import LibraryViewService
from services.compat.view_models import ViewAlbum


def _album(**overrides):
    base = {"rg_mbid": "rg-1", "title": "Test Album"}
    base.update(overrides)
    return ViewAlbum(**base)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Album", ["Album"]),
        ("album", ["Album"]),
        ("Single", ["Single"]),
        ("EP", ["EP"]),
        ("ep", ["EP"]),
        ("Compilation", ["Compilation"]),
        ("Soundtrack", ["Soundtrack"]),
        ("Live", ["Live"]),
        ("Remix", ["Remix"]),
        ("Mixtape", ["Mixtape"]),
        ("mixtape/street", ["Mixtape"]),
        ("Demo", ["Demo"]),
        ("Audiobook", ["Audiobook"]),
        ("Spokenword", ["Spokenword"]),
        ("Interview", ["Interview"]),
        ("DJ-mix", ["DJ-mix"]),
        ("dj-mix", ["DJ-mix"]),
        ("Audio drama", ["Audio drama"]),
        ("Field recording", ["Field recording"]),
        ("Broadcast", ["Broadcast"]),
        ("Other", ["Other"]),
        ("  ep  ", ["EP"]),
    ],
)
def test_known_types_map_to_display_tokens(raw, expected):
    assert release_types_for_album(raw, False) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "Boxset", "Deluxe Edition"])
def test_unknown_or_empty_falls_back_to_legacy(raw):
    assert release_types_for_album(raw, False) is None
    assert release_types_for_album(raw, True) == ["Compilation"]


def test_compilation_flag_appends_without_duplicating():
    assert release_types_for_album("album", True) == ["Album", "Compilation"]
    assert release_types_for_album("Compilation", True) == ["Compilation"]
    assert release_types_for_album("compilation", True) == ["Compilation"]


def test_to_album_id3_emits_mapped_release_types():
    assert to_album_id3(_album(release_types=["EP"])).releaseTypes == ["EP"]
    assert to_album_id3(
        _album(release_types=["Album", "Compilation"])
    ).releaseTypes == ["Album", "Compilation"]


def test_to_album_id3_legacy_none_for_untagged_album():
    assert to_album_id3(_album()).releaseTypes is None


def test_to_album_id3_legacy_compilation_for_untagged_compilation():
    assert to_album_id3(_album(is_compilation=True)).releaseTypes == ["Compilation"]


def _summary_ns(**overrides):
    base = {
        "release_group_mbid": "rg-1",
        "album_title": "Test Album",
        "album_artist_name": "Artist",
        "year": 1997,
        "track_count": 2,
        "cover_url": None,
        "last_imported_at": 1_700_000_000.0,
        "is_compilation": False,
        "album_artist_mbid": None,
        "album_sort_name": None,
        "original_release_date": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_album_from_summary_carries_mapped_release_type():
    album = LibraryViewService._album_from_summary(_summary_ns(release_type="ep"))
    assert album.release_types == ["EP"]
    assert to_album_id3(album).releaseTypes == ["EP"]


def test_album_from_summary_without_aggregate_keeps_legacy_output():
    album = LibraryViewService._album_from_summary(_summary_ns())
    assert album.release_types is None
    assert to_album_id3(album).releaseTypes is None


def _track_row(release_type, *, disc=1, track=1):
    return {
        "album_title": "Test Album",
        "album_artist_name": "Artist",
        "album_artist_mbid": None,
        "year": 1997,
        "genre": "Rock",
        "duration_seconds": 200.0,
        "imported_at": 1_700_000_000.0,
        "is_compilation": False,
        "album_sort_name": None,
        "original_release_date": None,
        "disc_number": disc,
        "track_number": track,
        "disc_subtitle": None,
        "release_type": release_type,
    }


@pytest.mark.asyncio
async def test_album_from_rows_uses_dominant_release_type(library_view_service):
    rows = [
        _track_row("EP", track=1),
        _track_row("EP", track=2),
        _track_row("Single", track=3),
        _track_row(None, track=4),
        _track_row("   ", track=5),
    ]
    album = await library_view_service._album_from_rows("rg-1", rows)
    assert album.release_types == ["EP"]


@pytest.mark.asyncio
async def test_album_from_rows_tie_breaks_by_track_order(library_view_service):
    rows = [_track_row("Single", track=1), _track_row("EP", track=2)]
    album = await library_view_service._album_from_rows("rg-1", rows)
    assert album.release_types == ["Single"]
