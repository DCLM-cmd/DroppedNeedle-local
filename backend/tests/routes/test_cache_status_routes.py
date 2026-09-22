"""F-17: cache-sync cancel is curator-gated; status reads stay open."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException

from api.v1.routes import cache_status as cache_status_routes
from core.dependencies import get_cache_status_service
from middleware import _get_current_curator, _get_current_user
from tests.helpers import build_test_client, mock_admin_user, mock_user


@pytest.fixture
def mock_status():
    status = AsyncMock()
    status.cancel_current_sync = AsyncMock()
    status.wait_for_completion = AsyncMock()
    return status


def _client(mock_status, role):
    app = FastAPI()
    app.include_router(cache_status_routes.router)
    app.dependency_overrides[get_cache_status_service] = lambda: mock_status
    if role == "user":
        app.dependency_overrides[_get_current_user] = lambda: mock_user()
        app.dependency_overrides[_get_current_curator] = _deny
    elif role == "curator":
        app.dependency_overrides[_get_current_user] = lambda: mock_user(role="trusted")
        app.dependency_overrides[_get_current_curator] = lambda: mock_user(
            role="trusted"
        )
    elif role == "admin":
        app.dependency_overrides[_get_current_user] = mock_admin_user
        app.dependency_overrides[_get_current_curator] = mock_admin_user
    # role "none": no overrides -> request.state.user unset -> 401
    return build_test_client(app)


def _deny():
    raise HTTPException(status_code=403, detail="Curator access required")


def test_cancel_rejects_unauthenticated(mock_status):
    assert _client(mock_status, "none").post("/cache/sync/cancel").status_code == 401


def test_cancel_forbids_regular_users(mock_status):
    assert _client(mock_status, "user").post("/cache/sync/cancel").status_code == 403


def test_cancel_admits_curator(mock_status):
    resp = _client(mock_status, "curator").post("/cache/sync/cancel")
    assert resp.status_code == 200
    mock_status.cancel_current_sync.assert_awaited_once()


def test_cancel_admits_admin(mock_status):
    assert _client(mock_status, "admin").post("/cache/sync/cancel").status_code == 200


def test_status_read_open_to_regular_user(mock_status):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    mock_status.get_progress = MagicMock(
        return_value=SimpleNamespace(
            is_syncing=False,
            phase="idle",
            total_items=0,
            processed_items=0,
            progress_percent=0.0,
            current_item=None,
            started_at=None,
            error_message=None,
            total_artists=0,
            processed_artists=0,
            total_albums=0,
            processed_albums=0,
        )
    )
    resp = _client(mock_status, "user").get("/cache/sync/status")
    assert resp.status_code == 200


def test_cancel_awaits_registry_cancel(mock_status, monkeypatch):
    """F-21: the registry cancel must be awaited, not dropped as a coroutine."""
    from core.task_registry import TaskRegistry

    cancel = AsyncMock()
    monkeypatch.setattr(TaskRegistry.get_instance(), "cancel", cancel)
    resp = _client(mock_status, "admin").post("/cache/sync/cancel")
    assert resp.status_code == 200
    cancel.assert_awaited_once_with("precache-library")
