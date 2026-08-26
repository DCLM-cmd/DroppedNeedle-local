"""While the MusicBrainz circuit breaker is open, repository methods degrade
quietly: the DegradationContext record is the error signal (per AGENTS.md), not
a per-call error log - a backed-off queue must not spam thousands of lines."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import repositories.musicbrainz_album as album_module
from infrastructure.queue.priority_queue import RequestPriority
from infrastructure.resilience.retry import CircuitOpenError
from repositories.musicbrainz_album import MusicBrainzAlbumMixin


class _Repo(MusicBrainzAlbumMixin):
    def __init__(self) -> None:
        self._cache = AsyncMock()
        self._cache.get = AsyncMock(return_value=None)
        self._cache.set = AsyncMock()
        self._preferences_service = SimpleNamespace(
            get_advanced_settings=lambda: SimpleNamespace(cache_ttl_search=3600)
        )


@pytest.fixture
def open_breaker(monkeypatch):
    monkeypatch.setattr(
        album_module,
        "mb_api_get",
        AsyncMock(side_effect=CircuitOpenError("open", breaker_name="musicbrainz")),
    )
    degradation = Mock()
    monkeypatch.setattr(album_module, "_record_mb_degradation", degradation)
    return degradation


@pytest.mark.asyncio
async def test_search_albums_degrades_quietly_when_breaker_open(
    open_breaker, caplog
) -> None:
    with caplog.at_level(logging.ERROR, logger="repositories.musicbrainz_album"):
        assert await _Repo().search_albums("query") == []
    assert caplog.records == []
    open_breaker.assert_called_once()


@pytest.mark.asyncio
async def test_search_recordings_degrades_quietly_when_breaker_open(
    open_breaker, caplog
) -> None:
    with caplog.at_level(logging.ERROR, logger="repositories.musicbrainz_album"):
        assert await _Repo().search_recordings("query", "title") == []
    assert caplog.records == []
    open_breaker.assert_called_once()


@pytest.mark.asyncio
async def test_get_release_group_by_id_degrades_quietly_when_breaker_open(
    open_breaker, caplog
) -> None:
    with caplog.at_level(logging.ERROR, logger="repositories.musicbrainz_album"):
        assert await _Repo().get_release_group_by_id("rg-1") is None
    assert caplog.records == []
    open_breaker.assert_called_once()


@pytest.mark.asyncio
async def test_get_release_by_id_degrades_quietly_when_breaker_open(
    open_breaker, caplog
) -> None:
    with caplog.at_level(logging.ERROR, logger="repositories.musicbrainz_album"):
        assert (
            await _Repo().get_release_by_id("release-1", priority=RequestPriority.USER_INITIATED)
            is None
        )
    assert caplog.records == []
    open_breaker.assert_called_once()


@pytest.mark.asyncio
async def test_get_recording_by_id_degrades_quietly_when_breaker_open(
    open_breaker, caplog
) -> None:
    with caplog.at_level(logging.ERROR, logger="repositories.musicbrainz_album"):
        assert await _Repo().get_recording_by_id("recording-1") is None
    assert caplog.records == []
    open_breaker.assert_called_once()


@pytest.mark.asyncio
async def test_search_release_groups_by_tag_degrades_quietly_when_breaker_open(
    open_breaker, caplog
) -> None:
    with caplog.at_level(logging.ERROR, logger="repositories.musicbrainz_album"):
        assert await _Repo().search_release_groups_by_tag("tag") == []
    assert caplog.records == []
    open_breaker.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_recording_to_release_group_degrades_quietly_when_breaker_open(
    open_breaker, caplog
) -> None:
    with caplog.at_level(logging.ERROR, logger="repositories.musicbrainz_album"):
        assert (
            await _Repo().resolve_recording_to_release_group("recording-1") is None
        )
    assert caplog.records == []
    open_breaker.assert_called_once()


@pytest.mark.asyncio
async def test_real_errors_still_log(open_breaker, caplog, monkeypatch) -> None:
    monkeypatch.setattr(
        album_module,
        "mb_api_get",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    with caplog.at_level(logging.ERROR, logger="repositories.musicbrainz_album"):
        assert await _Repo().get_recording_by_id("recording-1") is None
    assert len(caplog.records) == 1
    open_breaker.assert_called_once()
