"""Per-track request route tests."""

from unittest.mock import AsyncMock

from fastapi import FastAPI

from api.v1.routes import tracks
from api.v1.schemas.download import TrackRequestResponse
from core.dependencies import get_request_service
from core.exceptions import ValidationError
from middleware import _get_current_user
from tests.helpers import build_test_client, mock_user


TRACK_MBID = "11111111-1111-1111-1111-111111111111"
TRACK_BODY = {"artist_name": "Radiohead", "track_title": "Airbag"}


def _app(service, role="user") -> FastAPI:
    app = FastAPI()
    app.include_router(tracks.router)
    app.dependency_overrides[get_request_service] = lambda: service
    app.dependency_overrides[_get_current_user] = lambda: mock_user(
        role=role, user_id="u1"
    )
    return app


def test_request_track_user_role_awaits_approval():
    service = AsyncMock()
    service.request_track.return_value = TrackRequestResponse(
        status="awaiting_approval"
    )

    response = build_test_client(_app(service)).post(
        f"/tracks/{TRACK_MBID}/request", json=TRACK_BODY
    )

    assert response.status_code == 200
    assert response.json() == {"status": "awaiting_approval", "task_id": None}
    service.request_track.assert_awaited_once()
    assert service.request_track.await_args.args == (TRACK_MBID,)
    kwargs = service.request_track.await_args.kwargs
    assert kwargs["user_id"] == "u1"
    assert kwargs["user_role"] == "user"
    assert kwargs["artist_name"] == "Radiohead"
    assert kwargs["track_title"] == "Airbag"


def test_request_track_trusted_returns_service_task():
    service = AsyncMock()
    service.request_track.return_value = TrackRequestResponse(
        status="queued", task_id="task-trusted"
    )

    response = build_test_client(_app(service, role="trusted")).post(
        f"/tracks/{TRACK_MBID}/request", json=TRACK_BODY
    )

    assert response.status_code == 200
    assert response.json() == {"status": "queued", "task_id": "task-trusted"}
    kwargs = service.request_track.await_args.kwargs
    assert kwargs["user_id"] == "u1"
    assert kwargs["user_role"] == "trusted"


def test_request_track_admin_returns_service_task():
    service = AsyncMock()
    service.request_track.return_value = TrackRequestResponse(
        status="queued", task_id="task-admin"
    )

    response = build_test_client(_app(service, role="admin")).post(
        f"/tracks/{TRACK_MBID}/request", json=TRACK_BODY
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["task_id"] == "task-admin"
    assert service.request_track.await_args.kwargs["user_role"] == "admin"


def test_request_track_already_in_library():
    service = AsyncMock()
    service.request_track.return_value = TrackRequestResponse(
        status="already_in_library"
    )

    response = build_test_client(_app(service, role="admin")).post(
        f"/tracks/{TRACK_MBID}/request", json=TRACK_BODY
    )

    assert response.status_code == 200
    assert response.json() == {"status": "already_in_library", "task_id": None}


def test_request_track_unauthenticated_401():
    service = AsyncMock()
    app = FastAPI()
    app.include_router(tracks.router)
    app.dependency_overrides[get_request_service] = lambda: service

    response = build_test_client(app).post(
        f"/tracks/{TRACK_MBID}/request", json=TRACK_BODY
    )

    assert response.status_code == 401
    service.request_track.assert_not_awaited()


def test_request_track_over_quota_rejected_at_submit():
    """Request-service quota failures remain a clear 400 at the route boundary."""
    service = AsyncMock()
    service.request_track.side_effect = ValidationError(
        "Request limit reached (5 per 7 days)"
    )

    response = build_test_client(_app(service)).post(
        f"/tracks/{TRACK_MBID}/request", json=TRACK_BODY
    )

    assert response.status_code == 400
    assert "Request limit reached" in response.json()["error"]["message"]
    service.request_track.assert_awaited_once()


def test_request_track_invalid_recording_mbid_returns_400():
    service = AsyncMock()

    response = build_test_client(_app(service)).post(
        "/tracks/not-a-valid-mbid/request", json=TRACK_BODY
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Invalid MBID format"
    service.request_track.assert_not_awaited()
