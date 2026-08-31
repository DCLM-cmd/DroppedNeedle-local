"""Collisions either clear themselves or leave the downloads page.

A held unit whose reason code was not one of the three environmental ones got
``next_retry_at = None`` - never retried, never resolvable, sitting on the
downloads page for good with no action behind it. Collisions in particular are
often self-clearing (the thing occupying the destination is itself a held item),
so they are worth retrying; but they must also give up, because a collision that
survived every retry needs a person, not a permanent row.
"""

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.library_management import (
    BUNDLE_BLOCKED,
    METADATA_UNAVAILABLE,
    PATH_COLLISION_DIFFERENT,
    ROOT_UNAVAILABLE,
)
from services.native.download_service import (
    MANAGEMENT_ABANDON_AFTER_ATTEMPTS,
    DownloadService,
)


def _held(held_id: int, reason: str, retry_count: int, path: Path):
    return MagicMock(
        id=held_id,
        reason=f"management:{reason}",
        management_retry_count=retry_count,
        held_path=str(path),
    )


def _service(tmp_path, *, recycles: bool = True):
    service = DownloadService.__new__(DownloadService)
    service._store = AsyncMock()
    service._orchestrator = AsyncMock()
    file_processor = AsyncMock()
    file_processor.recycle_abandoned_hold = AsyncMock(return_value=recycles)
    service._file_processor = file_processor
    service._delete_discarded_held_files = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_a_collision_is_retried_rather_than_stranded(tmp_path) -> None:
    """It used to get next_retry_at=None, i.e. never looked at again."""
    service = _service(tmp_path)
    service._store.list_held_imports.return_value = [
        _held(1, PATH_COLLISION_DIFFERENT, 0, tmp_path / "a.flac")
    ]

    await service._schedule_management_hold_after_failure("task-1", "user-1")

    scheduled = service._store.schedule_management_hold_retry.await_args.kwargs
    assert scheduled["retry_count"] == 1
    assert scheduled["next_retry_at"] is not None
    assert scheduled["next_retry_at"] > time.time()
    service._store.resolve_held_imports.assert_not_called()


@pytest.mark.asyncio
async def test_an_unresolvable_collision_leaves_the_downloads_page(tmp_path) -> None:
    service = _service(tmp_path)
    held = [
        _held(1, PATH_COLLISION_DIFFERENT, MANAGEMENT_ABANDON_AFTER_ATTEMPTS, tmp_path / "a.flac")
    ]
    service._store.list_held_imports.return_value = held

    await service._schedule_management_hold_after_failure("task-1", "user-1")

    # removed from the page ...
    service._store.resolve_held_imports.assert_awaited_once_with([1], "discarded")
    # ... and NOT rescheduled for a retry that would never succeed
    service._store.schedule_management_hold_retry.assert_not_called()


@pytest.mark.asyncio
async def test_giving_up_recycles_rather_than_deletes(tmp_path) -> None:
    """The Organizer failing to place a file is not the user rejecting it."""
    service = _service(tmp_path, recycles=True)
    source = tmp_path / "a.flac"
    service._store.list_held_imports.return_value = [
        _held(1, PATH_COLLISION_DIFFERENT, MANAGEMENT_ABANDON_AFTER_ATTEMPTS, source)
    ]

    await service._schedule_management_hold_after_failure("task-1", "user-1")

    service._file_processor.recycle_abandoned_hold.assert_awaited_once_with(source)


@pytest.mark.asyncio
async def test_without_a_recycle_bin_it_still_leaves_the_page(tmp_path) -> None:
    """No bin configured degrades to the same cleanup an explicit discard does."""
    service = _service(tmp_path, recycles=False)
    service._store.list_held_imports.return_value = [
        _held(1, PATH_COLLISION_DIFFERENT, MANAGEMENT_ABANDON_AFTER_ATTEMPTS, tmp_path / "a.flac")
    ]

    await service._schedule_management_hold_after_failure("task-1", "user-1")

    service._store.resolve_held_imports.assert_awaited_once_with([1], "discarded")
    service._delete_discarded_held_files.assert_awaited_once()


@pytest.mark.asyncio
async def test_environmental_codes_are_never_abandoned(tmp_path) -> None:
    """A dead mount comes back; those must retry forever, not be thrown away."""
    service = _service(tmp_path)
    service._store.list_held_imports.return_value = [
        _held(1, ROOT_UNAVAILABLE, MANAGEMENT_ABANDON_AFTER_ATTEMPTS + 5, tmp_path / "a.flac")
    ]

    await service._schedule_management_hold_after_failure("task-1", "user-1")

    service._store.resolve_held_imports.assert_not_called()
    assert (
        service._store.schedule_management_hold_retry.await_args.kwargs["next_retry_at"]
        is not None
    )


@pytest.mark.asyncio
async def test_a_mixed_unit_is_left_for_a_person(tmp_path) -> None:
    """Two different reasons in one unit is not something to auto-resolve."""
    service = _service(tmp_path)
    service._store.list_held_imports.return_value = [
        _held(1, PATH_COLLISION_DIFFERENT, 9, tmp_path / "a.flac"),
        _held(2, METADATA_UNAVAILABLE, 9, tmp_path / "b.flac"),
    ]

    await service._schedule_management_hold_after_failure("task-1", "user-1")

    service._store.resolve_held_imports.assert_not_called()
    assert (
        service._store.schedule_management_hold_retry.await_args.kwargs["next_retry_at"]
        is None
    )


@pytest.mark.asyncio
async def test_a_blocked_bundle_is_also_retried_then_abandoned(tmp_path) -> None:
    """BUNDLE_BLOCKED means the durable evidence moved under the planner - the next
    attempt re-derives it, so it is worth retrying, and worth giving up on."""
    service = _service(tmp_path)
    service._store.list_held_imports.return_value = [
        _held(1, BUNDLE_BLOCKED, 0, tmp_path / "a.flac")
    ]

    await service._schedule_management_hold_after_failure("task-1", "user-1")
    assert (
        service._store.schedule_management_hold_retry.await_args.kwargs["next_retry_at"]
        is not None
    )

    service._store.reset_mock()
    service._store.list_held_imports.return_value = [
        _held(1, BUNDLE_BLOCKED, MANAGEMENT_ABANDON_AFTER_ATTEMPTS, tmp_path / "a.flac")
    ]
    await service._schedule_management_hold_after_failure("task-1", "user-1")
    service._store.resolve_held_imports.assert_awaited_once_with([1], "discarded")
