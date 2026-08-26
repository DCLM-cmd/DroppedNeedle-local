import json
from unittest.mock import AsyncMock

import pytest

from services.native.library_administrative_work_service import (
    LibraryAdministrativeWorkService,
)


def _row(**overrides):
    return {
        "id": "operation-1",
        "kind": "library_management",
        "state": "running",
        "created_at": 90.0,
        "started_at": 100.0,
        "updated_at": 110.0,
        "terminal_at": None,
        "completed_count": 2,
        "expected_work_count": 5,
        "succeeded_count": 2,
        "failed_count": 0,
        "skipped_count": 0,
        "management_mode": "apply",
        "management_origin": "manual",
        "management_phase": "applying",
        "management_summary_json": json.dumps(
            {
                "item_count": 47,
                "selected_item_count": 120,
                "warning_count": 3,
                "blocked_count": 7,
            }
        ),
        "management_profile_name": "Picard-style Organizer + Lyrics",
        "repair_purpose": None,
        "bulk_action": None,
        "local_album_id": None,
        "album_title": None,
        "journal_states_json": '["snapshot_saved"]',
        **overrides,
    }


def _diagnostics(**overrides):
    return {
        "recoverable_bundle_count": 0,
        "nonterminal_journal_count": 0,
        "needs_attention_count": 0,
        "cleanup_pending_count": 0,
        "oldest_updated_at": None,
        "state_counts": {},
        **overrides,
    }


@pytest.mark.asyncio
async def test_projects_file_writing_progress_and_durable_phase() -> None:
    store = AsyncMock()
    store.list_active_administrative_library_work.return_value = [_row()]
    store.library_management_recovery_diagnostics.return_value = _diagnostics()

    items = await LibraryAdministrativeWorkService(store, clock=lambda: 1_000).active()

    assert len(items) == 1
    item = items[0]
    assert item.kind == "library_management"
    assert item.effect == "file_writing"
    assert item.phase == "writing_staged_files"
    assert (item.processed, item.total, item.unit) == (2, 5, "releases")
    assert item.subject_count == 47
    assert item.profile_name == "Picard-style Organizer + Lyrics"
    assert (item.warning_count, item.blocked_count) == (3, 7)


@pytest.mark.asyncio
async def test_file_writing_phase_uses_the_earliest_unfinished_journal_state() -> None:
    store = AsyncMock()
    store.list_active_administrative_library_work.return_value = [
        _row(journal_states_json='["published","snapshot_saved","completed"]')
    ]
    store.library_management_recovery_diagnostics.return_value = _diagnostics()

    item = (await LibraryAdministrativeWorkService(store).active())[0]

    assert item.phase == "writing_staged_files"


@pytest.mark.asyncio
async def test_preview_uses_the_exact_selected_file_count() -> None:
    store = AsyncMock()
    store.list_active_administrative_library_work.return_value = [
        _row(
            management_mode="preview",
            management_phase="planning",
            completed_count=0,
            expected_work_count=0,
            management_summary_json=json.dumps(
                {"item_count": 35, "selected_item_count": 200}
            ),
            journal_states_json="[]",
        )
    ]
    store.library_management_recovery_diagnostics.return_value = _diagnostics()

    item = (await LibraryAdministrativeWorkService(store).active())[0]

    assert item.effect == "catalog_only"
    assert item.indeterminate is False
    assert (item.processed, item.total, item.unit) == (35, 200, "files")


@pytest.mark.asyncio
async def test_recovery_attention_outranks_ordinary_work_and_cannot_look_idle() -> None:
    store = AsyncMock()
    store.list_active_administrative_library_work.return_value = [
        _row(
            id="identity-1",
            kind="repair",
            management_mode=None,
            management_origin=None,
            management_phase=None,
            management_summary_json=None,
            management_profile_name=None,
            repair_purpose="management_readiness",
            journal_states_json="[]",
        )
    ]
    store.library_management_recovery_diagnostics.return_value = _diagnostics(
        needs_attention_count=2,
        cleanup_pending_count=1,
        oldest_updated_at=500.0,
    )

    items = await LibraryAdministrativeWorkService(store, clock=lambda: 1_000).active()

    assert [item.kind for item in items] == ["recovery", "identity_preparation"]
    assert items[0].effect == "attention"
    assert items[0].remaining_count == 3
    assert items[0].failure_event_id == "recovery:3"
