"""Tests for cover-art fail-fast behavior during a sustained CAA outage."""

import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import repositories.coverart_repository as coverart_module
from infrastructure.resilience.retry import CircuitState
from infrastructure.service_health import service_health
from repositories.coverart_artist import TransientImageFetchError
from repositories.coverart_repository import CoverArtRepository

RG_MBID = "11111111-1111-1111-1111-111111111111"
REL_MBID = "22222222-2222-2222-2222-222222222222"
ARTIST_MBID = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def _breaker_and_health_cleanup():
    yield
    coverart_module._coverart_circuit_breaker.reset()
    service_health.clear()


def _repo(tmp_path, http_client):
    repo = CoverArtRepository(http_client=http_client, cache=MagicMock(), cache_dir=tmp_path)
    repo._disk_cache.read = AsyncMock(return_value=None)
    repo._disk_cache.is_negative = AsyncMock(return_value=False)
    repo._disk_cache.write_negative = AsyncMock()
    return repo


def _open_breaker():
    breaker = coverart_module._coverart_circuit_breaker
    breaker.state = CircuitState.OPEN
    breaker.last_failure_time = time.time()
    return breaker


@pytest.mark.asyncio
async def test_release_group_cover_fails_fast_when_breaker_open(tmp_path):
    """Breaker open: no inline fetch, no deferred resolve, transient negative banked."""
    _open_breaker()
    async with httpx.AsyncClient() as http_client:
        repo = _repo(tmp_path, http_client)
        repo._album_fetcher.fetch_release_group_cover = AsyncMock()
        repo._album_fetcher.fetch_cached_audiodb_cover = AsyncMock(return_value=None)

        result = await repo.get_release_group_cover(
            RG_MBID, size="250", defer_best_release=True
        )

        assert result is None
        repo._album_fetcher.fetch_release_group_cover.assert_not_awaited()
        repo._disk_cache.write_negative.assert_awaited_once()
        assert (
            repo._disk_cache.write_negative.await_args.kwargs["ttl_seconds"]
            == coverart_module.COVER_TRANSIENT_NEGATIVE_TTL_SECONDS
        )
        assert repo._deferred_rg_inflight == set()


@pytest.mark.asyncio
async def test_release_cover_fails_fast_when_breaker_open(tmp_path):
    """Breaker open: release covers serve the placeholder instead of the inline fetch."""
    _open_breaker()
    async with httpx.AsyncClient() as http_client:
        repo = _repo(tmp_path, http_client)
        repo._album_fetcher.fetch_release_cover = AsyncMock()
        repo._album_fetcher.fetch_release_audiodb_cover = AsyncMock(return_value=None)

        result = await repo.get_release_cover(REL_MBID, size="500")

        assert result is None
        repo._album_fetcher.fetch_release_cover.assert_not_awaited()
        repo._disk_cache.write_negative.assert_awaited_once()
        assert (
            repo._disk_cache.write_negative.await_args.kwargs["ttl_seconds"]
            == coverart_module.COVER_TRANSIENT_NEGATIVE_TTL_SECONDS
        )


@pytest.mark.asyncio
async def test_breaker_open_marks_service_health_and_heals():
    """The CAA breaker mirrors into the service-health registry and heals on close."""
    breaker = coverart_module._coverart_circuit_breaker
    breaker.reset()
    service_health.clear()

    assert not service_health.is_degraded("coverartarchive")

    for _ in range(5):
        breaker.record_failure()

    assert service_health.is_degraded("coverartarchive")

    breaker.state = CircuitState.HALF_OPEN
    breaker.record_success()
    breaker.record_success()

    assert not service_health.is_degraded("coverartarchive")


@pytest.mark.asyncio
async def test_artist_transient_negative_short_circuits_next_request(tmp_path):
    """A transient artist failure banks a short negative; the next request skips the fetch."""
    async with httpx.AsyncClient() as http_client:
        repo = _repo(tmp_path, http_client)
        repo._disk_cache.is_negative = AsyncMock(side_effect=[False, True])
        repo._artist_fetcher.fetch_artist_image = AsyncMock(
            side_effect=TransientImageFetchError("transient fetch failure")
        )

        first = await repo.get_artist_image(ARTIST_MBID, size=500)

        assert first is None
        repo._disk_cache.write_negative.assert_awaited_once()
        assert (
            repo._disk_cache.write_negative.await_args.kwargs["ttl_seconds"]
            == coverart_module.COVER_TRANSIENT_NEGATIVE_TTL_SECONDS
        )

        second = await repo.get_artist_image(ARTIST_MBID, size=500)

        assert second is None
        # The second request short-circuited at is_negative: no new fetch.
        assert repo._artist_fetcher.fetch_artist_image.await_count == 1


@pytest.mark.asyncio
async def test_local_cover_served_while_breaker_open(tmp_path):
    """Local folder art never depended on CAA, so an open breaker must not hide it."""
    _open_breaker()
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")
    cover = b"\xff\xd8\xff\xe0" + b"\x00" * 16
    (tmp_path / "cover.jpg").write_bytes(cover)
    library_db = MagicMock()
    library_db.get_library_files_for_album = AsyncMock(
        return_value=[{"file_path": str(track)}]
    )

    async with httpx.AsyncClient() as http_client:
        repo = CoverArtRepository(
            http_client=http_client,
            cache=MagicMock(),
            cache_dir=tmp_path,
            library_db=library_db,
            local_cover_priority=lambda: True,
        )
        repo._disk_cache.read = AsyncMock(return_value=None)
        repo._disk_cache.is_negative = AsyncMock(return_value=False)
        repo._disk_cache.write_negative = AsyncMock()
        repo._disk_cache.write = AsyncMock()
        repo._album_fetcher.fetch_release_group_cover = AsyncMock()
        repo._album_fetcher.fetch_cached_audiodb_cover = AsyncMock(return_value=None)

        result = await repo.get_release_group_cover(
            RG_MBID, size="250", defer_best_release=True
        )

        assert result == (cover, "image/jpeg", "folder")
        repo._album_fetcher.fetch_release_group_cover.assert_not_awaited()
        repo._disk_cache.write_negative.assert_not_awaited()
