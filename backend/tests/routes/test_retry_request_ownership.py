import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from api.v1.routes.requests_page import router
from core.dependencies import get_requests_page_service
from infrastructure.persistence.request_history import RequestHistoryStore
from middleware import _get_current_admin, _get_current_user
from services.requests_page_service import RequestsPageService
from tests.helpers import build_test_client, make_builtin_dispatcher, mock_user

VALID_MBID = "22222222-2222-2222-2222-222222222222"
OWNER_ID = "owner-user-id"


def _make_service(
    tmp_path,
    status: str,
    *,
    request_kind: str = "album",
    include_album: bool = False,
) -> RequestsPageService:
    store = RequestHistoryStore(db_path=tmp_path / "library.db", write_lock=threading.Lock())

    async def _seed() -> None:
        if include_album and request_kind == "track":
            await store.async_record_request(
                VALID_MBID,
                "Artist",
                "Album",
                user_id=OWNER_ID,
                initial_status=status,
                request_kind="album",
            )
        await store.async_record_request(
            VALID_MBID,
            "Artist",
            "Album",
            user_id=OWNER_ID,
            initial_status=status,
            request_kind=request_kind,
            track_title="Track" if request_kind == "track" else None,
            duration_seconds=200 if request_kind == "track" else None,
            track_release_group_mbid=(
                "33333333-3333-3333-3333-333333333333"
                if request_kind == "track"
                else None
            ),
        )

    asyncio.run(_seed())

    async def _mbids() -> set[str]:
        return set()

    download_service = MagicMock()
    download_service.request_album = AsyncMock(return_value="task-xyz")
    download_service.request_track = AsyncMock(return_value="track-task-xyz")

    fake_repo = AsyncMock()
    fake_repo.get_library_mbids = AsyncMock(return_value=set())
    fake_repo.get_library_album_mbids = AsyncMock(return_value=set())
    fake_repo.get_library_artist_mbids = AsyncMock(return_value=set())
    get_download_service = lambda: download_service  # noqa: E731
    return RequestsPageService(
        library_repo=fake_repo,
        request_history=store,
        library_mbids_fn=_mbids,
        get_download_service=get_download_service,
        acquisition=make_builtin_dispatcher(get_download_service),
    )


def _client(
    tmp_path,
    *,
    role: str,
    user_id: str,
    status: str = "failed",
    request_kind: str = "album",
    include_album: bool = False,
):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_requests_page_service] = lambda: _make_service(
        tmp_path,
        status,
        request_kind=request_kind,
        include_album=include_album,
    )
    app.dependency_overrides[_get_current_user] = lambda: mock_user(role=role, user_id=user_id)
    app.dependency_overrides[_get_current_admin] = lambda: mock_user(
        role=role, user_id=user_id
    )
    return build_test_client(app)


def test_owner_can_retry(tmp_path):
    client = _client(tmp_path, role="user", user_id=OWNER_ID)
    resp = client.post(f"/requests/retry/{VALID_MBID}")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_non_owner_gets_403(tmp_path):
    client = _client(tmp_path, role="user", user_id="someone-else")
    resp = client.post(f"/requests/retry/{VALID_MBID}")
    assert resp.status_code == 403


def test_admin_can_retry_any(tmp_path):
    client = _client(tmp_path, role="admin", user_id="admin-id")
    resp = client.post(f"/requests/retry/{VALID_MBID}")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_track_owner_can_retry_with_explicit_kind_without_widening_to_album(tmp_path):
    client = _client(
        tmp_path,
        role="user",
        user_id=OWNER_ID,
        request_kind="track",
        include_album=True,
    )

    response = client.post(f"/requests/retry/{VALID_MBID}?request_kind=track")

    assert response.status_code == 200
    assert response.json()["success"] is True
    store = RequestHistoryStore(tmp_path / "library.db")
    track = asyncio.run(store.async_get_record(VALID_MBID, request_kind="track"))
    album = asyncio.run(store.async_get_record(VALID_MBID))
    assert track is not None
    assert track.request_kind == "track"
    assert track.status == "pending"
    assert track.download_task_id == "track-task-xyz"
    assert album is not None
    assert album.status == "failed"


def test_retry_request_rejects_unknown_request_kind(tmp_path):
    client = _client(tmp_path, role="user", user_id=OWNER_ID)

    response = client.post(
        f"/requests/retry/{VALID_MBID}?request_kind=recording"
    )

    assert response.status_code == 422


def test_track_retry_rejects_non_owner_before_dispatch(tmp_path):
    client = _client(
        tmp_path,
        role="user",
        user_id="someone-else",
        request_kind="track",
    )

    response = client.post(f"/requests/retry/{VALID_MBID}?request_kind=track")

    assert response.status_code == 403


def test_admin_can_approve_track_without_touching_album(tmp_path):
    client = _client(
        tmp_path,
        role="admin",
        user_id="admin-id",
        status="awaiting_approval",
        request_kind="track",
        include_album=True,
    )

    response = client.post(f"/requests/approve/{VALID_MBID}?request_kind=track")

    assert response.status_code == 200
    assert response.json()["success"] is True
    store = RequestHistoryStore(tmp_path / "library.db")
    track = asyncio.run(store.async_get_record(VALID_MBID, request_kind="track"))
    album = asyncio.run(store.async_get_record(VALID_MBID))
    assert track is not None
    assert track.request_kind == "track"
    assert track.status == "pending"
    assert track.download_task_id == "track-task-xyz"
    assert album is not None
    assert album.status == "awaiting_approval"


def test_admin_can_reject_track_without_touching_album(tmp_path):
    client = _client(
        tmp_path,
        role="admin",
        user_id="admin-id",
        status="awaiting_approval",
        request_kind="track",
        include_album=True,
    )

    response = client.post(f"/requests/reject/{VALID_MBID}?request_kind=track")

    assert response.status_code == 200
    assert response.json()["success"] is True
    store = RequestHistoryStore(tmp_path / "library.db")
    track = asyncio.run(store.async_get_record(VALID_MBID, request_kind="track"))
    album = asyncio.run(store.async_get_record(VALID_MBID))
    assert track is not None
    assert track.request_kind == "track"
    assert track.status == "rejected"
    assert album is not None
    assert album.status == "awaiting_approval"


@pytest.mark.parametrize("action", ("approve", "reject"))
def test_approval_routes_reject_unknown_request_kind(tmp_path, action):
    client = _client(
        tmp_path,
        role="admin",
        user_id="admin-id",
        status="awaiting_approval",
    )

    response = client.post(
        f"/requests/{action}/{VALID_MBID}?request_kind=recording"
    )

    assert response.status_code == 422
