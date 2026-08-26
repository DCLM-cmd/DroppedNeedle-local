from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from api.v1.routes.library_policies_target import router
from api.v1.schemas.library_policies import LibrarySettingsResponse
from core.dependencies import get_legacy_pending_migration_service
from core.dependencies.service_providers import get_target_library_policy_service
from core.exceptions import StaleRevisionError
from tests.helpers import build_test_client, override_admin_auth


@pytest.fixture
def app() -> tuple[FastAPI, AsyncMock, AsyncMock]:
    application = FastAPI()
    application.include_router(router)
    target = AsyncMock()
    target.get_settings.return_value = LibrarySettingsResponse(
        policy_revision="policy-2",
        reconciliation_required=True,
        reconciliation_state="awaiting_reconciliation",
        pending_policy_revision="policy-2",
        affected_scope_ids=["root"],
    )
    target.save_settings.return_value = target.get_settings.return_value
    target.restore_roots.return_value = target.get_settings.return_value
    pending_migration = AsyncMock()
    pending_migration.schedule.return_value = False
    application.dependency_overrides[get_target_library_policy_service] = (
        lambda: target
    )
    application.dependency_overrides[get_legacy_pending_migration_service] = (
        lambda: pending_migration
    )
    override_admin_auth(application)
    return application, target, pending_migration


def test_update_library_settings_schedules_pending_migration(
    app: tuple[FastAPI, AsyncMock, AsyncMock],
) -> None:
    application, _, pending_migration = app
    client = build_test_client(application)
    response = client.put(
        "/settings/library",
        json={
            "settings": {"library_roots": []},
            "expected_policy_revision": "policy-2",
        },
    )
    assert response.status_code == 200
    pending_migration.schedule.assert_awaited_once()


def test_restore_roots_schedules_pending_migration(
    app: tuple[FastAPI, AsyncMock, AsyncMock],
) -> None:
    application, _, pending_migration = app
    client = build_test_client(application)
    response = client.post(
        "/settings/library/restore-roots",
        json={
            "expected_policy_revision": "policy-2",
            "paths": {"root": "/music"},
        },
    )
    assert response.status_code == 200
    pending_migration.schedule.assert_awaited_once()


def test_update_library_settings_does_not_schedule_when_save_is_stale(
    app: tuple[FastAPI, AsyncMock, AsyncMock],
) -> None:
    application, target, pending_migration = app
    target.save_settings.side_effect = StaleRevisionError("stale policy revision")
    client = build_test_client(application)
    response = client.put(
        "/settings/library",
        json={
            "settings": {"library_roots": []},
            "expected_policy_revision": "policy-1",
        },
    )
    assert response.status_code == 409
    pending_migration.schedule.assert_not_awaited()


def test_update_library_settings_skips_schedule_when_library_disabled(
    app: tuple[FastAPI, AsyncMock, AsyncMock],
) -> None:
    application, target, pending_migration = app
    target.save_settings.return_value = LibrarySettingsResponse(
        policy_revision="policy-2", enabled=False
    )
    client = build_test_client(application)
    response = client.put(
        "/settings/library",
        json={
            "settings": {"library_roots": [], "enabled": False},
            "expected_policy_revision": "policy-2",
        },
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    pending_migration.schedule.assert_not_awaited()


def test_restore_roots_skips_schedule_when_library_disabled(
    app: tuple[FastAPI, AsyncMock, AsyncMock],
) -> None:
    application, target, pending_migration = app
    target.restore_roots.return_value = LibrarySettingsResponse(
        policy_revision="policy-2", enabled=False
    )
    client = build_test_client(application)
    response = client.post(
        "/settings/library/restore-roots",
        json={
            "expected_policy_revision": "policy-2",
            "paths": {"root": "/music"},
        },
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    pending_migration.schedule.assert_not_awaited()
