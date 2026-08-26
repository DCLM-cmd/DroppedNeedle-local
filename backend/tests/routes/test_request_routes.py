"""Request-flow route tests for album and exact-track asks."""

from unittest.mock import AsyncMock
from types import SimpleNamespace

from fastapi import FastAPI

from api.v1.routes import requests, requests_page, tracks
from api.v1.schemas.requests_page import (
    CancelRequestResponse,
    RetryRequestResponse,
)
from core.dependencies import (
    get_request_service,
    get_requests_page_service,
)
from core.exceptions import PermissionDeniedError
from middleware import _get_current_user
from services.native.download_service import ALREADY_IN_LIBRARY
from services.request_service import RequestService
from tests.helpers import build_test_client, make_builtin_dispatcher, mock_user


ALBUM_MBID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TRACK_MBID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_NEW_BODY = {
    "musicbrainz_id": ALBUM_MBID,
    "artist": "Radiohead",
    "album": "OK Computer",
    "year": 1997,
}
_TRACK_BODY = {"artist_name": "Radiohead", "track_title": "Airbag"}


def _request_service(download_service: AsyncMock) -> tuple[RequestService, AsyncMock]:
    history = AsyncMock()
    history.async_get_record.return_value = None
    history.async_record_request = AsyncMock(
        side_effect=lambda **kwargs: SimpleNamespace(
            musicbrainz_id=str(kwargs["musicbrainz_id"]),
            request_kind=str(kwargs.get("request_kind", "album")),
            generation=1,
        )
    )
    history.async_bulk_record_requests = AsyncMock(return_value=[])
    history.async_update_download_task_id = AsyncMock(return_value=True)
    history.async_update_status = AsyncMock(return_value=True)
    get_ds = lambda: download_service  # noqa: E731
    service = RequestService(
        request_history=history,
        get_download_service=get_ds,
        acquisition=make_builtin_dispatcher(get_ds),
    )
    return service, history


def _requests_app(service: RequestService, role: str) -> FastAPI:
    app = FastAPI()
    app.include_router(requests.router)
    app.dependency_overrides[get_request_service] = lambda: service
    app.dependency_overrides[_get_current_user] = lambda: mock_user(
        role=role, user_id="u1"
    )
    return app


def _tracks_app(service: RequestService, role: str) -> FastAPI:
    app = FastAPI()
    app.include_router(tracks.router)
    app.dependency_overrides[get_request_service] = lambda: service
    app.dependency_overrides[_get_current_user] = lambda: mock_user(
        role=role, user_id="u1"
    )
    return app


def _requests_page_app(service, *, role: str = "user", user_id: str = "u1") -> FastAPI:
    app = FastAPI()
    app.include_router(requests_page.router)
    app.dependency_overrides[get_requests_page_service] = lambda: service
    app.dependency_overrides[_get_current_user] = lambda: mock_user(
        role=role, user_id=user_id
    )
    return app


def test_request_new_user_role_awaits_approval():
    ds = AsyncMock()
    service, _history = _request_service(ds)

    response = build_test_client(_requests_app(service, role="user")).post(
        "/requests/new", json=_NEW_BODY
    )

    assert response.status_code == 202
    assert response.json()["status"] == "awaiting_approval"
    ds.request_album.assert_not_awaited()


def test_request_new_trusted_auto_approves_and_links_task():
    ds = AsyncMock()
    ds.request_album.return_value = "task-123"
    service, history = _request_service(ds)

    response = build_test_client(_requests_app(service, role="trusted")).post(
        "/requests/new", json=_NEW_BODY
    )

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    ds.request_album.assert_awaited_once()
    history.async_update_download_task_id.assert_awaited_once_with(
        ALBUM_MBID, "task-123", request_kind="album", expected_generation=1
    )


def test_request_new_already_in_library_does_not_link_task():
    ds = AsyncMock()
    ds.request_album.return_value = ALREADY_IN_LIBRARY
    service, history = _request_service(ds)

    response = build_test_client(_requests_app(service, role="admin")).post(
        "/requests/new", json=_NEW_BODY
    )

    assert response.status_code == 202
    assert "already in the library" in response.json()["message"].lower()
    history.async_update_download_task_id.assert_not_awaited()


def test_request_new_unauthenticated_401():
    ds = AsyncMock()
    service, _history = _request_service(ds)
    app = FastAPI()
    app.include_router(requests.router)
    app.dependency_overrides[get_request_service] = lambda: service

    response = build_test_client(app).post("/requests/new", json=_NEW_BODY)

    assert response.status_code == 401
    ds.request_album.assert_not_awaited()


def test_track_request_user_role_awaits_approval_without_dispatch():
    ds = AsyncMock()
    service, history = _request_service(ds)

    response = build_test_client(_tracks_app(service, "user")).post(
        f"/tracks/{TRACK_MBID}/request", json=_TRACK_BODY
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert body["task_id"] is None
    ds.request_track.assert_not_awaited()
    assert history.async_record_request.await_args.kwargs["request_kind"] == "track"


def test_track_request_trusted_returns_task_id():
    ds = AsyncMock()
    ds.request_track.return_value = "task-track-1"
    service, history = _request_service(ds)

    response = build_test_client(_tracks_app(service, "trusted")).post(
        f"/tracks/{TRACK_MBID}/request", json=_TRACK_BODY
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["task_id"] == "task-track-1"
    ds.request_track.assert_awaited_once()
    history.async_update_download_task_id.assert_awaited_once_with(
        TRACK_MBID, "task-track-1", request_kind="track", expected_generation=1
    )

def test_track_request_already_in_library():
    ds = AsyncMock()
    ds.request_track.return_value = ALREADY_IN_LIBRARY
    service, history = _request_service(ds)

    response = build_test_client(_tracks_app(service, "admin")).post(
        f"/tracks/{TRACK_MBID}/request", json=_TRACK_BODY
    )

    assert response.status_code == 200
    assert response.json()["status"] == "already_in_library"
    history.async_update_status.assert_awaited_once()


def test_track_request_unauthenticated_401():
    ds = AsyncMock()
    service, _history = _request_service(ds)
    app = FastAPI()
    app.include_router(tracks.router)
    app.dependency_overrides[get_request_service] = lambda: service

    response = build_test_client(app).post(
        f"/tracks/{TRACK_MBID}/request", json=_TRACK_BODY
    )

    assert response.status_code == 401


def test_track_request_invalid_recording_mbid_returns_400():
    ds = AsyncMock()
    service, _history = _request_service(ds)

    response = build_test_client(_tracks_app(service, "user")).post(
        "/tracks/not-a-valid-mbid/request", json=_TRACK_BODY
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Invalid MBID format"
    ds.request_track.assert_not_awaited()


def test_request_mutations_propagate_album_and_track_kind():
    service = AsyncMock()
    service.cancel_request.return_value = CancelRequestResponse(
        success=True, message="cancelled"
    )
    service.retry_request.return_value = RetryRequestResponse(
        success=True, message="retried"
    )
    service.clear_history_item.return_value = True
    client = build_test_client(_requests_page_app(service))

    album_cancel = client.delete(f"/requests/active/{ALBUM_MBID}")
    track_cancel = client.delete(
        f"/requests/active/{TRACK_MBID}?request_kind=track"
    )
    track_retry = client.post(f"/requests/retry/{TRACK_MBID}?request_kind=track")
    album_clear = client.delete(f"/requests/history/{ALBUM_MBID}")

    assert album_cancel.status_code == 200
    assert track_cancel.status_code == 200
    assert track_retry.status_code == 200
    assert album_clear.status_code == 200
    service.cancel_request.assert_any_await(
        ALBUM_MBID, user_id="u1", user_role="user", request_kind="album"
    )
    service.cancel_request.assert_any_await(
        TRACK_MBID, user_id="u1", user_role="user", request_kind="track"
    )
    service.retry_request.assert_awaited_once_with(
        TRACK_MBID, user_id="u1", user_role="user", request_kind="track"
    )
    service.clear_history_item.assert_awaited_once_with(
        ALBUM_MBID, user_id="u1", user_role="user", request_kind="album"
    )


def test_track_mutation_preserves_owner_context_and_permission_errors():
    service = AsyncMock()
    service.cancel_request.side_effect = PermissionDeniedError(
        "Cannot cancel another user's request"
    )
    client = build_test_client(
        _requests_page_app(service, role="user", user_id="other-user")
    )

    response = client.delete(
        f"/requests/active/{TRACK_MBID}?request_kind=track"
    )

    assert response.status_code == 403
    service.cancel_request.assert_awaited_once_with(
        TRACK_MBID,
        user_id="other-user",
        user_role="user",
        request_kind="track",
    )


def test_track_mutation_allows_admin_owner_override():
    service = AsyncMock()
    service.cancel_request.return_value = CancelRequestResponse(
        success=True, message="cancelled"
    )
    client = build_test_client(
        _requests_page_app(service, role="admin", user_id="admin-1")
    )

    response = client.delete(
        f"/requests/active/{TRACK_MBID}?request_kind=track"
    )

    assert response.status_code == 200
    service.cancel_request.assert_awaited_once_with(
        TRACK_MBID,
        user_id="admin-1",
        user_role="admin",
        request_kind="track",
    )
