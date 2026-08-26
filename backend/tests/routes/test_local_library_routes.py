"""Route coverage for native/local library reads."""

from unittest.mock import AsyncMock

from fastapi import FastAPI

from api.v1.routes.local_library import router
from core.dependencies import get_native_lyrics_service
from services.compat.native_lyrics_service import NativeLyrics, NativeLyricsLine
from tests.helpers import build_test_client, override_user_auth


def _app(service: AsyncMock, *, authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_native_lyrics_service] = lambda: service
    if authenticated:
        override_user_auth(app)
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
