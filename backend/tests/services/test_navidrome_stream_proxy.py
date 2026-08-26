"""Tests for NavidromePlaybackService.proxy_head / proxy_stream."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.navidrome_playback_service import NavidromePlaybackService
from core.exceptions import ExternalServiceError


def _make_service():
    repo = MagicMock()
    service = NavidromePlaybackService(navidrome_repo=repo)
    return service, repo


@pytest.mark.asyncio
async def test_proxy_head_returns_response():
    service, repo = _make_service()

    from repositories.navidrome_repository import StreamProxyResult
    mock_result = StreamProxyResult(
        status_code=200,
        headers={"Content-Type": "audio/flac", "Content-Length": "12345"},
        media_type="audio/flac",
        body_chunks=None,
    )
    repo.proxy_head_stream = AsyncMock(return_value=mock_result)

    response = await service.proxy_head("song-1")
    assert response.status_code == 200
    assert response.headers.get("Content-Type") == "audio/flac"


@pytest.mark.asyncio
async def test_proxy_head_raises_on_error():
    service, repo = _make_service()
    repo.proxy_head_stream = AsyncMock(side_effect=ExternalServiceError("Failed to reach Navidrome"))

    with pytest.raises(ExternalServiceError):
        await service.proxy_head("song-1")


@pytest.mark.asyncio
async def test_proxy_stream_returns_streaming_response():
    service, repo = _make_service()

    async def fake_chunks():
        yield b"chunk1"
        yield b"chunk2"

    from repositories.navidrome_repository import StreamProxyResult
    mock_result = StreamProxyResult(
        status_code=200,
        headers={"Content-Type": "audio/mpeg"},
        media_type="audio/mpeg",
        body_chunks=fake_chunks(),
    )
    repo.proxy_get_stream = AsyncMock(return_value=mock_result)

    response = await service.proxy_stream("song-1", None)
    assert response.status_code == 200
    assert response.media_type == "audio/mpeg"


@pytest.mark.asyncio
async def test_proxy_stream_with_range_header():
    service, repo = _make_service()

    async def fake_chunks():
        yield b"partial"

    from repositories.navidrome_repository import StreamProxyResult
    mock_result = StreamProxyResult(
        status_code=206,
        headers={"Content-Type": "audio/mpeg", "Content-Range": "bytes 0-999/5000"},
        media_type="audio/mpeg",
        body_chunks=fake_chunks(),
    )
    repo.proxy_get_stream = AsyncMock(return_value=mock_result)

    response = await service.proxy_stream("song-1", "bytes=0-999")
    assert response.status_code == 206


@pytest.mark.asyncio
async def test_proxy_stream_raises_416():
    service, repo = _make_service()
    repo.proxy_get_stream = AsyncMock(
        side_effect=ExternalServiceError("416 Range not satisfiable")
    )

    with pytest.raises(ExternalServiceError, match="416"):
        await service.proxy_stream("song-1", "bytes=9999-")


class TestPerUserProxy:
    """Streaming proxies through the user's linked repo, app account as fallback."""

    @staticmethod
    def _stream_result():
        from repositories.navidrome_repository import StreamProxyResult

        async def fake_chunks():
            yield b"audio"

        return StreamProxyResult(
            status_code=200,
            headers={"Content-Type": "audio/flac"},
            media_type="audio/flac",
            body_chunks=fake_chunks(),
        )

    @staticmethod
    def _factory(per_user_repo):
        factory = MagicMock()
        factory.resolve_navidrome = AsyncMock(return_value=per_user_repo)
        return factory

    @pytest.mark.asyncio
    async def test_proxy_stream_uses_per_user_repo_when_linked(self):
        app_repo = MagicMock()
        per_user = MagicMock()
        per_user.proxy_get_stream = AsyncMock(return_value=self._stream_result())
        service = NavidromePlaybackService(
            navidrome_repo=app_repo, client_factory=self._factory(per_user)
        )

        await service.proxy_stream("song-1", "bytes=0-", user_id="u1")

        per_user.proxy_get_stream.assert_awaited_once_with(
            "song-1", range_header="bytes=0-"
        )
        app_repo.proxy_get_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_proxy_head_uses_per_user_repo_when_linked(self):
        app_repo = MagicMock()
        per_user = MagicMock()
        per_user.proxy_head_stream = AsyncMock(return_value=self._stream_result())
        service = NavidromePlaybackService(
            navidrome_repo=app_repo, client_factory=self._factory(per_user)
        )

        await service.proxy_head("song-1", user_id="u1")

        per_user.proxy_head_stream.assert_awaited_once_with("song-1")
        app_repo.proxy_head_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_proxy_stream_falls_back_to_app_repo_when_unlinked(self):
        app_repo = MagicMock()
        app_repo.proxy_get_stream = AsyncMock(return_value=self._stream_result())
        service = NavidromePlaybackService(
            navidrome_repo=app_repo, client_factory=self._factory(None)
        )

        await service.proxy_stream("song-1", None, user_id="u1")

        app_repo.proxy_get_stream.assert_awaited_once_with(
            "song-1", range_header=None
        )
