"""No task may touch library files while a settings dry run is planning.

A dry run for the automation settings predicts what the new settings would do to
the files that are on disk right now. If an import, an Organizer apply or a
recovery pass moves something while it plans, the preview describes a library
that no longer exists - and the apply that follows then fails with "a destination
was created after planning". Every library mutation goes through the filesystem
coordinator's write lease, so that is where the library is held still.
"""

import asyncio

import pytest

from services.native.library_filesystem_coordinator import (
    LibraryFilesystemCoordinator,
)


@pytest.mark.asyncio
async def test_writes_wait_while_quiesced() -> None:
    coordinator = LibraryFilesystemCoordinator()
    started = asyncio.Event()

    async def writer() -> None:
        async with coordinator.write("root-1"):
            started.set()

    async with coordinator.quiesce():
        task = asyncio.create_task(writer())
        await asyncio.sleep(0.05)
        assert not started.is_set(), "a write started during a dry run"

    await asyncio.wait_for(task, timeout=2)
    assert started.is_set(), "the write must resume once the dry run ends"


@pytest.mark.asyncio
async def test_reads_are_never_blocked() -> None:
    """The dry run itself has to inspect the files it is planning against."""
    coordinator = LibraryFilesystemCoordinator()

    async with coordinator.quiesce():
        async with coordinator.read("root-1"):
            pass  # must not deadlock


@pytest.mark.asyncio
async def test_quiesce_waits_for_an_in_flight_write_to_finish() -> None:
    """Blocking new writes is not enough - planning must not start mid-move."""
    coordinator = LibraryFilesystemCoordinator()
    release = asyncio.Event()
    holding = asyncio.Event()

    async def slow_writer() -> None:
        async with coordinator.write("root-1"):
            holding.set()
            await release.wait()

    task = asyncio.create_task(slow_writer())
    await asyncio.wait_for(holding.wait(), timeout=2)

    planning = asyncio.Event()

    async def dry_run() -> None:
        async with coordinator.quiesce():
            planning.set()

    quiesce_task = asyncio.create_task(dry_run())
    await asyncio.sleep(0.05)
    assert not planning.is_set(), "planning began while a write was still running"

    release.set()
    await asyncio.wait_for(task, timeout=2)
    await asyncio.wait_for(quiesce_task, timeout=2)
    assert planning.is_set()


@pytest.mark.asyncio
async def test_nested_dry_runs_only_release_on_the_last() -> None:
    coordinator = LibraryFilesystemCoordinator()

    async with coordinator.quiesce():
        async with coordinator.quiesce():
            assert coordinator.quiesced
        assert coordinator.quiesced, "an inner dry run must not free the library"
    assert not coordinator.quiesced


@pytest.mark.asyncio
async def test_a_failed_dry_run_still_releases_the_library() -> None:
    coordinator = LibraryFilesystemCoordinator()

    with pytest.raises(RuntimeError):
        async with coordinator.quiesce():
            raise RuntimeError("planning blew up")

    assert not coordinator.quiesced
    async with coordinator.write("root-1"):
        pass  # must not hang


@pytest.mark.asyncio
async def test_writes_to_unrelated_roots_are_held_too() -> None:
    """"No filesystem operations" is global: a second root is still the library."""
    coordinator = LibraryFilesystemCoordinator()
    started = asyncio.Event()

    async def writer() -> None:
        async with coordinator.write_many(["root-2", "root-3"]):
            started.set()

    async with coordinator.quiesce():
        task = asyncio.create_task(writer())
        await asyncio.sleep(0.05)
        assert not started.is_set()

    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_worker_holds_the_library_still_for_a_settings_dry_run() -> None:
    """The wiring: a preview that proposes new settings plans under a quiesce."""
    from unittest.mock import AsyncMock, MagicMock

    from services.native.library_management_worker import LibraryManagementWorker

    coordinator = LibraryFilesystemCoordinator()
    snapshot = MagicMock(mode="preview", phase="planning")
    snapshot.proposed_settings_revision = "settings-2"

    store = AsyncMock()
    store.get_library_management_job_snapshot.return_value = snapshot
    store.get_operation_job.return_value = {"id": "job-1", "row_revision": 1}

    quiesced_during_planning: list[bool] = []
    planner = AsyncMock()

    async def plan(_job, _worker):
        quiesced_during_planning.append(coordinator.quiesced)
        return MagicMock(origin="user", phase="ready")

    planner.run_claimed_preview = AsyncMock(side_effect=plan)

    worker = LibraryManagementWorker(
        store, planner, AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(),
        filesystem=coordinator,
    )
    await worker.run_claimed({"id": "job-1"}, "worker-1")

    assert quiesced_during_planning == [True]
    assert not coordinator.quiesced, "the library must be released afterwards"


@pytest.mark.asyncio
async def test_an_ordinary_preview_does_not_hold_the_library() -> None:
    """Only the dry run whose purpose is predicting new settings pays this cost."""
    from unittest.mock import AsyncMock, MagicMock

    from services.native.library_management_worker import LibraryManagementWorker

    coordinator = LibraryFilesystemCoordinator()
    snapshot = MagicMock(mode="preview", phase="planning")
    snapshot.proposed_settings_revision = None

    store = AsyncMock()
    store.get_library_management_job_snapshot.return_value = snapshot
    store.get_operation_job.return_value = {"id": "job-1", "row_revision": 1}

    seen: list[bool] = []
    planner = AsyncMock()

    async def plan(_job, _worker):
        seen.append(coordinator.quiesced)
        return MagicMock(origin="user", phase="ready")

    planner.run_claimed_preview = AsyncMock(side_effect=plan)

    worker = LibraryManagementWorker(
        store, planner, AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(),
        filesystem=coordinator,
    )
    await worker.run_claimed({"id": "job-1"}, "worker-1")

    assert seen == [False]
