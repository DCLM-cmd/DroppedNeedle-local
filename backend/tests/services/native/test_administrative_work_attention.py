""""Needs attention" must mean something a person can act on.

Two things raised it that nobody could act on:

* An organizer run that ended with ``STALE_INPUT`` before attempting ANY work - the
  catalog moved between planning and execution, so the run was overtaken and simply
  re-planned. On the live library that was 57 of 71 "failed" runs, every one with
  zero succeeded, zero failed and zero skipped items.
* An import bundle in ``cleanup_pending`` - which has already PUBLISHED, meaning its
  files are organized, with only the staged-source tidy-up outstanding.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.native.library_administrative_work_service import (
    LibraryAdministrativeWorkService,
)


def _row(**overrides):
    row = {
        "id": "job-1", "kind": "library_management", "state": "failed",
        "terminal_code": None, "created_at": 100.0, "started_at": 100.0,
        "updated_at": 200.0, "terminal_at": 200.0, "completed_count": 0,
        "expected_work_count": 0, "succeeded_count": 0, "failed_count": 0,
        "skipped_count": 0, "management_summary_json": "{}",
        "management_mode": "apply", "management_phase": "applying",
        "journal_states_json": "[]", "management_origin": "manual",
        "management_profile_name": None,
    }
    row.update(overrides)
    return row


# ---- superseded runs -------------------------------------------------------------

def test_a_stale_input_run_that_did_nothing_is_not_a_failure() -> None:
    """The regression: overtaken, not broken."""
    item = LibraryAdministrativeWorkService._operation_item(
        _row(terminal_code="STALE_INPUT")
    )

    assert item.failure_event_id is None
    assert item.failure_at is None
    assert item.effect != "attention"


def test_a_stale_input_run_that_got_work_done_stays_a_failure() -> None:
    """Something half-happened; that is worth looking at."""
    item = LibraryAdministrativeWorkService._operation_item(
        _row(terminal_code="STALE_INPUT", succeeded_count=3)
    )

    assert item.failure_event_id == "job-1"
    assert item.effect == "attention"


@pytest.mark.parametrize("field", ["succeeded_count", "failed_count", "skipped_count"])
def test_any_attempted_work_keeps_the_failure(field) -> None:
    item = LibraryAdministrativeWorkService._operation_item(
        _row(terminal_code="STALE_INPUT", **{field: 1})
    )

    assert item.failure_event_id == "job-1"


def test_a_real_failure_is_still_a_failure() -> None:
    item = LibraryAdministrativeWorkService._operation_item(
        _row(terminal_code="WORKER_ERROR")
    )

    assert item.failure_event_id == "job-1"
    assert item.effect == "attention"


def test_a_run_with_no_terminal_code_is_still_a_failure() -> None:
    assert (
        LibraryAdministrativeWorkService._operation_item(_row()).failure_event_id
        == "job-1"
    )


def test_a_succeeded_run_is_never_a_failure() -> None:
    item = LibraryAdministrativeWorkService._operation_item(
        _row(state="succeeded", succeeded_count=4)
    )

    assert item.failure_event_id is None


# ---- cleanup pending -------------------------------------------------------------

def _service(*, needs_attention, cleanup_pending):
    store = MagicMock()
    store.list_active_administrative_library_work = AsyncMock(return_value=[])
    store.library_management_recovery_diagnostics = AsyncMock(
        return_value={
            "needs_attention_count": needs_attention,
            "cleanup_pending_count": cleanup_pending,
            "oldest_updated_at": 500.0,
        }
    )
    return LibraryAdministrativeWorkService(store)


@pytest.mark.asyncio
async def test_pending_cleanup_alone_raises_nothing() -> None:
    """The files are already organized; the tidy-up runs itself."""
    assert await _service(needs_attention=0, cleanup_pending=7).active() == []


@pytest.mark.asyncio
async def test_real_attention_still_surfaces() -> None:
    items = await _service(needs_attention=2, cleanup_pending=0).active()

    assert len(items) == 1
    assert items[0].kind == "recovery"
    assert items[0].failed_count == 2


@pytest.mark.asyncio
async def test_pending_cleanup_is_reported_but_does_not_inflate_the_count() -> None:
    items = await _service(needs_attention=2, cleanup_pending=5).active()

    assert items[0].total == 2  # not 7
    assert items[0].warning_count == 5  # still visible to anyone who wants it
