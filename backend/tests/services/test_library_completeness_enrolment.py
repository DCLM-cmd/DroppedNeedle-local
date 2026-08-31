"""Completeness is decided by the FILES, not by a request's status field.

Enrolment only ever walked request records in ``failed``/``incomplete`` state, so an
album whose request read ``imported`` was never re-examined. On the live library that
hid three incomplete albums holding 38 missing tracks between them - "OK Computer"
(0 of 12) and "Birds in the Trap Sing McKnight" (3 of 14) were both marked imported,
and "Chromakopia" (0 of 15) was marked cancelled.

Enrolment creates a WATCH; it never downloads. The watcher's own backoff,
satisfaction re-check and per-cycle dispatch cap still decide what is fetched.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.native.wanted_watcher_service import (
    _SweepMembershipState,
)
from services.native.wanted_watcher_service import (
    _MAX_LIBRARY_ENROL_PER_SWEEP,
    WantedWatcherService,
)


def _track(position, title):
    return SimpleNamespace(
        position=position, disc_number=1, title=title, length=200000, recording_id=None
    )


def _row(position, title):
    return {
        "track_number": position, "disc_number": 1, "track_title": title,
        "duration_seconds": 200.0, "recording_mbid": None,
    }


def _record(user_id="user-1", status="imported"):
    return SimpleNamespace(
        musicbrainz_id="rg-1", user_id=user_id, artist_name="Radiohead",
        album_title="OK Computer", artist_mbid="a-1", year=1997, cover_url=None,
        status=status,
    )


def _watcher(*, mbids, tracks, rows, record=_record(), existing_watch=None):
    service = WantedWatcherService.__new__(WantedWatcherService)
    service._library = MagicMock()
    service._library.get_library_mbids = AsyncMock(return_value=set(mbids))
    service._store = AsyncMock()
    service._store.get_watch = AsyncMock(return_value=existing_watch)
    service._store.create_watch = AsyncMock(return_value=True)
    service._requests = MagicMock()
    service._requests.async_get_record = AsyncMock(return_value=record)
    service._tracklist = AsyncMock(return_value=tracks)
    service._file_rows = AsyncMock(return_value=rows)
    service._interval_seconds = MagicMock(return_value=3600.0)
    return service


@pytest.mark.asyncio
async def test_an_imported_request_with_missing_files_is_enrolled() -> None:
    """The regression, exactly: the status says done, the library says otherwise."""
    service = _watcher(
        mbids=["rg-1"],
        tracks=[_track(i, f"T{i}") for i in range(1, 13)],
        rows=[],
    )

    assert await service._enrol_from_library(_SweepMembershipState()) == 1
    kwargs = service._store.create_watch.await_args.kwargs
    assert kwargs["kind"] == "partial"
    assert kwargs["release_group_mbid"] == "rg-1"
    assert kwargs["user_id"] == "user-1"


@pytest.mark.asyncio
async def test_a_cancelled_request_with_missing_files_is_enrolled() -> None:
    service = _watcher(
        mbids=["rg-1"],
        tracks=[_track(1, "A"), _track(2, "B")],
        rows=[_row(1, "A")],
        record=_record(status="cancelled"),
    )

    assert await service._enrol_from_library(_SweepMembershipState()) == 1


@pytest.mark.asyncio
async def test_a_complete_album_is_not_enrolled() -> None:
    service = _watcher(
        mbids=["rg-1"],
        tracks=[_track(1, "A"), _track(2, "B")],
        rows=[_row(1, "A"), _row(2, "B")],
    )

    assert await service._enrol_from_library(_SweepMembershipState()) == 0
    service._store.create_watch.assert_not_called()


@pytest.mark.asyncio
async def test_an_album_that_already_has_a_watch_is_left_alone() -> None:
    """Watching, stopped or fulfilled - the existing state is the human's choice."""
    service = _watcher(
        mbids=["rg-1"], tracks=[_track(1, "A")], rows=[],
        existing_watch=SimpleNamespace(state="stopped"),
    )

    assert await service._enrol_from_library(_SweepMembershipState()) == 0
    service._requests.async_get_record.assert_not_called()


@pytest.mark.asyncio
async def test_an_album_nobody_requested_is_not_enrolled() -> None:
    """A want needs someone to act for; ownership is never invented."""
    service = _watcher(mbids=["rg-1"], tracks=[_track(1, "A")], rows=[], record=None)

    assert await service._enrol_from_library(_SweepMembershipState()) == 0


@pytest.mark.asyncio
async def test_a_request_without_a_user_is_not_enrolled() -> None:
    service = _watcher(
        mbids=["rg-1"], tracks=[_track(1, "A")], rows=[], record=_record(user_id=None)
    )

    assert await service._enrol_from_library(_SweepMembershipState()) == 0


@pytest.mark.asyncio
async def test_an_unmeasurable_album_is_never_enrolled() -> None:
    """No tracklist means no way to tell what is missing - never search on missing
    data, the same rule the dispatch follows."""
    service = _watcher(mbids=["rg-1"], tracks=None, rows=[])

    assert await service._enrol_from_library(_SweepMembershipState()) == 0


@pytest.mark.asyncio
async def test_one_sweep_cannot_enrol_the_whole_library_at_once() -> None:
    """Enrolment downloads nothing, but a library-sized burst of new watches is still
    a burst; the cap keeps a first run gradual."""
    many = [f"rg-{i}" for i in range(_MAX_LIBRARY_ENROL_PER_SWEEP * 3)]
    service = _watcher(mbids=many, tracks=[_track(1, "A")], rows=[])

    assert await service._enrol_from_library(_SweepMembershipState()) == _MAX_LIBRARY_ENROL_PER_SWEEP


@pytest.mark.asyncio
async def test_a_library_read_failure_skips_the_pass_quietly() -> None:
    service = _watcher(mbids=[], tracks=[], rows=[])
    service._library.get_library_mbids = AsyncMock(side_effect=RuntimeError("db gone"))

    assert await service._enrol_from_library(_SweepMembershipState()) == 0


@pytest.mark.asyncio
async def test_enrolment_never_dispatches_a_download() -> None:
    """The whole safety argument: this pass writes a watch and nothing else."""
    service = _watcher(mbids=["rg-1"], tracks=[_track(1, "A")], rows=[])

    await service._enrol_from_library(_SweepMembershipState())

    assert {name for name, *_ in service._store.mock_calls} == {
        "get_watch", "create_watch",
    }
