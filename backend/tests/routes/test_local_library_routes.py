"""Route coverage for native/local library reads."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from api.v1.routes.local_library import router
from api.v1.schemas.local_files import (
    LocalAlbumMatch,
    LocalAlbumSummary,
    LocalPaginatedResponse,
    LocalSearchResponse,
    LocalTrackInfo,
)
from core.dependencies import (
    get_local_files_service,
    get_native_lyrics_service,
    get_preferences_service,
)
from services.compat.native_lyrics_service import NativeLyrics, NativeLyricsLine
from tests.helpers import build_test_client, override_user_auth


def _app(service: AsyncMock, *, authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_native_lyrics_service] = lambda: service
    if authenticated:
        override_user_auth(app)
    return app


def _match_app(
    match: LocalAlbumMatch, *, role: str, allowed: bool, seen: list[str]
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    service = AsyncMock()
    service.match_album_by_mbid.return_value = match
    app.dependency_overrides[get_local_files_service] = lambda: service

    def _check(caller_role: str) -> bool:
        seen.append(caller_role)
        return allowed

    prefs = SimpleNamespace(is_library_download_allowed=_check)
    app.dependency_overrides[get_preferences_service] = lambda: prefs
    override_user_auth(app, role=role)
    return app


def test_local_track_lyrics_returns_normalized_plain_lyrics() -> None:
    service = AsyncMock()
    service.get.return_value = NativeLyrics(
        language="und",
        synced=False,
        lines=(NativeLyricsLine("First line"), NativeLyricsLine("Second line")),
        source="embedded",
    )

    response = build_test_client(_app(service)).get("/local/tracks/file-1/lyrics")

    assert response.status_code == 200
    assert response.json() == {
        "text": "First line\nSecond line",
        "is_synced": False,
        "lines": [
            {"text": "First line", "start_seconds": None},
            {"text": "Second line", "start_seconds": None},
        ],
    }
    service.get.assert_awaited_once_with("file-1")


def test_local_track_lyrics_converts_milliseconds_to_seconds() -> None:
    service = AsyncMock()
    service.get.return_value = NativeLyrics(
        language="und",
        synced=True,
        lines=(NativeLyricsLine("Timed line", 12_345),),
        source="sidecar",
    )

    response = build_test_client(_app(service)).get("/local/tracks/file-2/lyrics")

    assert response.status_code == 200
    assert response.json()["lines"] == [{"text": "Timed line", "start_seconds": 12.345}]


def test_local_track_lyrics_returns_404_when_absent() -> None:
    service = AsyncMock()
    service.get.return_value = None

    response = build_test_client(_app(service)).get("/local/tracks/file-3/lyrics")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Lyrics not available"


def test_local_track_lyrics_requires_authentication() -> None:
    service = AsyncMock()

    response = build_test_client(_app(service, authenticated=False)).get(
        "/local/tracks/file-4/lyrics"
    )

    assert response.status_code == 401
    service.get.assert_not_awaited()


def _match(*, found: bool = True) -> LocalAlbumMatch:
    return LocalAlbumMatch(
        found=found,
        musicbrainz_id="mbid-1",
        tracks=[
            LocalTrackInfo(
                track_file_id="file-1",
                title="Song",
                track_number=1,
                size_bytes=1000,
                format="flac",
            )
        ]
        if found
        else [],
        total_size_bytes=1000 if found else 0,
    )


@pytest.mark.parametrize(
    ("role", "allowed", "expected"),
    [
        ("user", True, True),
        ("user", False, False),
        ("trusted", False, False),
    ],
)
def test_match_album_piggybacks_download_allowed(
    role: str, allowed: bool, expected: bool
) -> None:
    seen: list[str] = []
    app = _match_app(_match(), role=role, allowed=allowed, seen=seen)

    response = build_test_client(app).get("/local/albums/match/mbid-1")

    assert response.status_code == 200
    assert response.json()["download_allowed"] is expected
    assert response.json()["total_size_bytes"] == 1000
    assert seen == [role]


def test_match_album_sets_download_allowed_when_not_found() -> None:
    seen: list[str] = []
    app = _match_app(_match(found=False), role="user", allowed=False, seen=seen)

    response = build_test_client(app).get("/local/albums/match/mbid-9")

    assert response.status_code == 200
    assert response.json()["found"] is False
    assert response.json()["download_allowed"] is False
    assert seen == ["user"]


def _summary(*, mbid: str = "mbid-1") -> LocalAlbumSummary:
    return LocalAlbumSummary(
        musicbrainz_id=mbid, name="Avalon", artist_name="Roxy Music"
    )


def _list_app(
    service: AsyncMock, *, role: str, allowed: bool, seen: list[str]
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_local_files_service] = lambda: service

    def _check(caller_role: str) -> bool:
        seen.append(caller_role)
        return allowed

    prefs = SimpleNamespace(is_library_download_allowed=_check)
    app.dependency_overrides[get_preferences_service] = lambda: prefs
    override_user_auth(app, role=role)
    return app


@pytest.mark.parametrize(
    ("role", "allowed", "expected"),
    [
        ("user", True, True),
        ("user", False, False),
        ("trusted", False, False),
        ("admin", True, True),
    ],
)
def test_list_search_and_recent_stamp_download_allowed(
    role: str, allowed: bool, expected: bool
) -> None:
    seen: list[str] = []
    service = AsyncMock()
    service.get_albums.return_value = LocalPaginatedResponse(
        items=[_summary()], total=1
    )
    service.search.return_value = LocalSearchResponse(albums=[_summary()], tracks=[])
    service.get_recently_added.return_value = [_summary()]
    client = build_test_client(
        _list_app(service, role=role, allowed=allowed, seen=seen)
    )

    albums = client.get("/local/albums")
    search = client.get("/local/search", params={"q": "avalon"})
    recent = client.get("/local/recent")

    assert albums.status_code == 200
    assert albums.json()["items"][0]["download_allowed"] is expected
    assert search.status_code == 200
    assert search.json()["albums"][0]["download_allowed"] is expected
    assert recent.status_code == 200
    assert recent.json()[0]["download_allowed"] is expected
    assert seen == [role] * 3


def test_recent_requires_authentication() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_local_files_service] = lambda: AsyncMock()

    response = build_test_client(app).get("/local/recent")

    assert response.status_code == 401
