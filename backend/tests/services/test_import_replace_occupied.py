"""Confirming a held file whose destination is taken.

Blocking with "a planned destination is occupied by different content" left the user
with no move: the collision cannot be resolved anywhere else in the app, and the
message named no file. The import now reports WHICH file is in the way so the choice
can be put to them, and replacing destroys that file - disk and catalog both - rather
than setting it aside, because that is what the user is answering.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.exceptions import AutomaticManagementHoldError, ImportDestinationOccupiedError
from services.native.download_service import PATH_COLLISION_DIFFERENT

from tests.services.test_download_service import _make_service


def _held(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        reason="fingerprint_mismatch",
        origin="user",
        held_path=str(tmp_path / "held.flac"),
        album_title="Long Term Effects of SUFFERING",
        track_title="Bleach",
        source_task_id="task-1",
        release_group_mbid="rg-1",
    )


def _occupied(destination: str) -> AutomaticManagementHoldError:
    return AutomaticManagementHoldError(
        PATH_COLLISION_DIFFERENT,
        "A planned destination is occupied by different content.",
        destination=destination,
    )


def _service(tmp_path, occupant_row=None):
    service, store, *_ = _make_service()
    store.get_held_import.return_value = _held(tmp_path)
    store.find_active_edition_conversion_for_held_path.return_value = None
    service._file_processor = SimpleNamespace(place_held_file=AsyncMock())
    service._native_library_store = AsyncMock()
    service._native_library_store.find_track_by_file_path.return_value = occupant_row
    service._native_library_store.delete_track_by_file_path.return_value = (
        {"track_id": "t-old", "removed_references": {"play_history": 3}}
        if occupant_row
        else None
    )
    service._library_reconciler = AsyncMock()
    service._orchestrator.settle_after_manual_import = AsyncMock()
    return service, store


@pytest.mark.asyncio
async def test_an_occupied_destination_asks_instead_of_failing(tmp_path):
    destination = str(tmp_path / "01 - Bleach.flac")
    service, _store = _service(
        tmp_path, occupant_row={"title": "Something Else", "file_format": "mp3",
                                "file_size_bytes": 4242}
    )
    service._file_processor.place_held_file.side_effect = _occupied(destination)

    with pytest.raises(ImportDestinationOccupiedError) as raised:
        await service.import_held(7, "u1", "admin")

    assert raised.value.destination == destination
    # the question cannot be put without naming the file
    assert raised.value.occupant["title"] == "Something Else"
    assert raised.value.occupant["file_format"] == "mp3"
    assert raised.value.occupant["in_catalog"] is True


@pytest.mark.asyncio
async def test_replacing_deletes_the_row_and_the_file_then_imports(tmp_path):
    destination = tmp_path / "01 - Bleach.flac"
    destination.write_bytes(b"the old file")
    service, _store = _service(tmp_path, occupant_row={"title": "Old"})
    placed = tmp_path / "placed.flac"
    service._file_processor.place_held_file.side_effect = [
        _occupied(str(destination)),
        placed,
    ]

    result = await service.import_held(7, "u1", "admin", replace_existing=True)

    assert result == str(placed)
    service._native_library_store.delete_track_by_file_path.assert_awaited_once_with(
        str(destination)
    )
    assert not destination.exists(), "replacing must delete, not merely move"


@pytest.mark.asyncio
async def test_a_second_collision_is_not_answered_by_deleting_again(tmp_path):
    """Something else is writing there. Deleting on a moving target destroys the
    wrong file, so the second collision is reported rather than cleared."""
    destination = tmp_path / "01 - Bleach.flac"
    destination.write_bytes(b"the old file")
    service, _store = _service(tmp_path, occupant_row={"title": "Old"})
    service._file_processor.place_held_file.side_effect = _occupied(str(destination))

    with pytest.raises(AutomaticManagementHoldError):
        await service.import_held(7, "u1", "admin", replace_existing=True)

    assert service._native_library_store.delete_track_by_file_path.await_count == 1


@pytest.mark.asyncio
async def test_a_collision_without_a_named_path_is_not_replaced(tmp_path):
    """Nothing to name and nothing to delete: replacing blind would be a guess at
    which file to destroy."""
    service, _store = _service(tmp_path)
    service._file_processor.place_held_file.side_effect = AutomaticManagementHoldError(
        PATH_COLLISION_DIFFERENT, "occupied"
    )

    with pytest.raises(AutomaticManagementHoldError):
        await service.import_held(7, "u1", "admin", replace_existing=True)

    service._native_library_store.delete_track_by_file_path.assert_not_awaited()
