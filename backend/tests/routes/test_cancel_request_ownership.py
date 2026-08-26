import asyncio
import threading

from fastapi import FastAPI
from unittest.mock import AsyncMock

from api.v1.routes.requests_page import router
from core.dependencies import get_requests_page_service
from infrastructure.persistence.request_history import RequestHistoryStore
from middleware import _get_current_user
from services.requests_page_service import RequestsPageService
from tests.helpers import build_test_client, mock_user

VALID_MBID = "11111111-1111-1111-1111-111111111111"
OWNER_ID = "owner-user-id"


def _make_service(
    tmp_path,
    status: str,
    *,
    request_kind: str = "album",
    include_album: bool = False,
    listeners: tuple[str, ...] = (),
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
        for listener_id in listeners:
            await store.async_add_requester(
                VALID_MBID, listener_id, request_kind=request_kind
            )

    asyncio.run(_seed())

    async def _mbids() -> set[str]:
        return set()

    fake_repo = AsyncMock()
    fake_repo.get_library_mbids = AsyncMock(return_value=set())
    fake_repo.get_library_album_mbids = AsyncMock(return_value=set())
    fake_repo.get_library_artist_mbids = AsyncMock(return_value=set())
    return RequestsPageService(
        library_repo=fake_repo,
        request_history=store,
        library_mbids_fn=_mbids,
    )


def _client(
    tmp_path,
    *,
    role: str,
    user_id: str,
    status: str = "awaiting_approval",
    request_kind: str = "album",
    include_album: bool = False,
    listeners: tuple[str, ...] = (),
):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_requests_page_service] = lambda: _make_service(
        tmp_path,
        status,
        request_kind=request_kind,
        include_album=include_album,
        listeners=listeners,
    )
    app.dependency_overrides[_get_current_user] = lambda: mock_user(role=role, user_id=user_id)
    return build_test_client(app)


def test_owner_can_cancel(tmp_path):
    client = _client(tmp_path, role="user", user_id=OWNER_ID)
    resp = client.delete(f"/requests/active/{VALID_MBID}")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_non_owner_gets_403(tmp_path):
    client = _client(tmp_path, role="user", user_id="someone-else")
    resp = client.delete(f"/requests/active/{VALID_MBID}")
    assert resp.status_code == 403


def test_admin_can_cancel_any(tmp_path):
    client = _client(tmp_path, role="admin", user_id="admin-id")
    resp = client.delete(f"/requests/active/{VALID_MBID}")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_invalid_mbid_returns_400(tmp_path):
    client = _client(tmp_path, role="user", user_id=OWNER_ID)
    resp = client.delete("/requests/active/not-a-valid-mbid")
    assert resp.status_code == 400


def test_track_owner_can_cancel_using_explicit_kind_without_touching_album(tmp_path):
    client = _client(
        tmp_path,
        role="user",
        user_id=OWNER_ID,
        request_kind="track",
        include_album=True,
    )

    response = client.delete(
        f"/requests/active/{VALID_MBID}?request_kind=track"
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    store = RequestHistoryStore(tmp_path / "library.db")
    track = asyncio.run(store.async_get_record(VALID_MBID, request_kind="track"))
    album = asyncio.run(store.async_get_record(VALID_MBID))
    assert track is not None
    assert track.status == "cancelled"
    assert album is not None
    assert album.status == "awaiting_approval"


def test_shared_track_listener_detaches_without_cancelling_generation(tmp_path):
    client = _client(
        tmp_path,
        role="user",
        user_id="second-listener",
        request_kind="track",
        listeners=("second-listener",),
    )

    response = client.delete(
        f"/requests/active/{VALID_MBID}?request_kind=track"
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert "another listener" in response.json()["message"]
    store = RequestHistoryStore(tmp_path / "library.db")
    track = asyncio.run(store.async_get_record(VALID_MBID, request_kind="track"))
    assert track is not None
    assert track.status == "awaiting_approval"
    assert asyncio.run(
        store.async_is_requester("second-listener", VALID_MBID, request_kind="track")
    ) is False
    assert asyncio.run(
        store.async_is_requester(OWNER_ID, VALID_MBID, request_kind="track")
    ) is True


def test_cancel_request_rejects_unknown_request_kind(tmp_path):
    client = _client(tmp_path, role="user", user_id=OWNER_ID)

    response = client.delete(
        f"/requests/active/{VALID_MBID}?request_kind=recording"
    )

    assert response.status_code == 422
