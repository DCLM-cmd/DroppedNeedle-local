"""Two fixes: the recycle bin is not library content, and an accepted mapping's
missing content gets requested.

The recycle bin lives at ``<library>/.recycle`` precisely so the scanner skips it,
but nothing did - a file the Organizer had recycled was indexed straight back into
the catalog, so a removed track reappeared in the library it had just left.

Choosing a track mapping states what the album should contain. Anything the mapping
names and the library does not hold is missing content the user implicitly asked for,
so acceptance enrols it with the wanted watcher - which already paces the fetching.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.native.library_inventory_scanner import _is_hidden_directory
from services.native.wanted_watcher_service import WantedWatcherService


@pytest.mark.parametrize(
    "name", [".recycle", ".Trash", ".stfolder", ".DS_Store", ".droppedneedle"]
)
def test_hidden_directories_are_not_library_content(name) -> None:
    assert _is_hidden_directory(name)


@pytest.mark.parametrize("name", ["Radiohead", "CD 1", "2023 - Album", "recycle"])
def test_ordinary_directories_are_still_scanned(name) -> None:
    """Only the dot prefix is hidden - a folder merely NAMED recycle is content."""
    assert not _is_hidden_directory(name)


def _watcher(*, tracks, covered_rows):
    service = WantedWatcherService.__new__(WantedWatcherService)
    service._store = AsyncMock()
    service._store.create_watch = AsyncMock(return_value=True)
    service._tracklist = AsyncMock(return_value=tracks)
    service._file_rows = AsyncMock(return_value=covered_rows)
    service._interval_seconds = MagicMock(return_value=3600.0)
    return service


def _track(position, title):
    return MagicMock(
        position=position, disc_number=1, title=title, length=200000, recording_id=None
    )


def _row(position, title):
    return {
        "track_number": position,
        "disc_number": 1,
        "track_title": title,
        "duration_seconds": 200.0,
        "recording_mbid": None,
    }


@pytest.mark.asyncio
async def test_an_incomplete_mapping_is_enrolled() -> None:
    service = _watcher(
        tracks=[_track(1, "One"), _track(2, "Two"), _track(3, "Three")],
        covered_rows=[_row(1, "One")],
    )

    enrolled = await service.enrol_incomplete_mapping(
        "rg-1", user_id="user-1", artist_name="A", album_title="B", now=100.0
    )

    assert enrolled
    kwargs = service._store.create_watch.await_args.kwargs
    assert kwargs["release_group_mbid"] == "rg-1"
    assert kwargs["kind"] == "partial"
    assert kwargs["user_id"] == "user-1"


@pytest.mark.asyncio
async def test_a_fully_covered_mapping_is_not_enrolled() -> None:
    """Nothing is missing, so there is nothing to ask for."""
    service = _watcher(
        tracks=[_track(1, "One"), _track(2, "Two")],
        covered_rows=[_row(1, "One"), _row(2, "Two")],
    )

    assert not await service.enrol_incomplete_mapping(
        "rg-1", user_id="user-1", artist_name="A", album_title="B", now=100.0
    )
    service._store.create_watch.assert_not_called()


@pytest.mark.asyncio
async def test_an_unmeasurable_mapping_never_enrols() -> None:
    """No tracklist means no way to tell what is missing; never search on missing
    data, which is the same rule the sweep already follows."""
    service = _watcher(tracks=None, covered_rows=[])

    assert not await service.enrol_incomplete_mapping(
        "rg-1", user_id="user-1", artist_name="A", album_title="B", now=100.0
    )
    service._store.create_watch.assert_not_called()


@pytest.mark.asyncio
async def test_without_a_requester_nothing_is_enrolled() -> None:
    """A want needs someone to act for."""
    service = _watcher(tracks=[_track(1, "One")], covered_rows=[])

    assert not await service.enrol_incomplete_mapping(
        "rg-1", user_id=None, artist_name="A", album_title="B", now=100.0
    )
    service._store.create_watch.assert_not_called()


@pytest.mark.asyncio
async def test_acceptance_enrols_but_never_downloads_directly() -> None:
    """The watcher owns dispatch: it re-checks coverage, paces by release age and
    can be stopped. Acceptance firing downloads itself would bypass all of that."""
    service = _watcher(tracks=[_track(1, "One"), _track(2, "Two")], covered_rows=[])

    await service.enrol_incomplete_mapping(
        "rg-1", user_id="user-1", artist_name="A", album_title="B", now=100.0
    )

    assert service._store.create_watch.await_count == 1
    # Only the watch was registered: acceptance touched nothing that starts a
    # download, so the watcher's pacing and stop control stay in charge.
    called = {name for name, *_ in service._store.mock_calls}
    assert called == {"create_watch"}
