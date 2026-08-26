from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.search_service as search_service_module
from api.v1.schemas.search import SearchResult
from infrastructure.degradation import (
    clear_degradation_context,
    init_degradation_context,
)
from infrastructure.integration_result import IntegrationResult
from services.search_service import SearchService


@pytest.fixture(autouse=True)
def clear_search_caches():
    SearchService.clear_cached_results()
    clear_degradation_context()
    yield
    SearchService.clear_cached_results()
    clear_degradation_context()


def _album_result(title: str = "Absolution") -> SearchResult:
    return SearchResult(
        type="album",
        title=title,
        musicbrainz_id=f"mbid-{title.casefold()}",
        artist="Muse",
        score=100,
    )


def _result(title: str = "Muse") -> SearchResult:
    return SearchResult(
        type="artist",
        title=title,
        musicbrainz_id=f"mbid-{title.casefold()}",
        score=100,
    )


def _service(results: list[SearchResult]) -> tuple[SearchService, MagicMock, MagicMock]:
    mb_repo = MagicMock()
    mb_repo.search_artists = AsyncMock(return_value=results)
    mb_repo.search_albums = AsyncMock(return_value=[])
    library_repo = MagicMock()
    library_repo.get_library_mbids = AsyncMock(return_value=set())
    preferences = MagicMock()
    preferences.get_preferences.return_value = SimpleNamespace(
        primary_types=["album", "single"],
        secondary_types=[],
    )
    service = SearchService(
        mb_repo=mb_repo,
        library_repo=library_repo,
        coverart_repo=MagicMock(),
        preferences_service=preferences,
    )
    return service, mb_repo, preferences


@pytest.mark.asyncio
async def test_provider_error_returns_copy_of_last_successful_bucket() -> None:
    service, mb_repo, _ = _service([_result()])

    live_results, live_top, live_status = await service.search_bucket(
        "artists", "Muse", limit=24
    )
    mb_repo.search_artists.side_effect = RuntimeError("offline")
    stale_results, stale_top, stale_status = await service.search_bucket(
        "artists", "Muse", limit=24
    )

    assert live_status == "ok"
    assert stale_status == "stale"
    assert stale_results[0].title == "Muse"
    assert stale_top is not None
    assert stale_top.title == "Muse"
    assert stale_results is not live_results
    assert stale_results[0] is not live_results[0]
    assert stale_top is not live_top


@pytest.mark.asyncio
async def test_timeout_uses_stale_bucket_but_first_failure_does_not() -> None:
    service, mb_repo, _ = _service([_result()])

    mb_repo.search_artists.side_effect = TimeoutError()
    first_results, _, first_status = await service.search_bucket("artists", "New")
    assert first_results == []
    assert first_status == "timeout"

    mb_repo.search_artists.side_effect = None
    mb_repo.search_artists.return_value = [_result()]
    await service.search_bucket("artists", "Muse")
    mb_repo.search_artists.side_effect = TimeoutError()
    stale_results, _, stale_status = await service.search_bucket("artists", "Muse")

    assert [item.title for item in stale_results] == ["Muse"]
    assert stale_status == "stale"


@pytest.mark.asyncio
async def test_stale_cache_isolated_by_query_offset_and_filters() -> None:
    service, mb_repo, preferences = _service([_result()])
    await service.search_bucket("artists", "Muse", limit=24, offset=0)
    mb_repo.search_artists.side_effect = RuntimeError("offline")

    assert (await service.search_bucket("artists", "Radiohead", limit=24, offset=0))[
        2
    ] == "error"
    assert (await service.search_bucket("artists", "Muse", limit=24, offset=24))[
        2
    ] == "error"

    preferences.get_preferences.return_value = SimpleNamespace(
        primary_types=["ep"],
        secondary_types=[],
    )
    assert (await service.search_bucket("artists", "Muse", limit=24, offset=0))[
        2
    ] == "error"


@pytest.mark.asyncio
async def test_expired_bucket_is_not_served(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(search_service_module.time, "monotonic", lambda: now)
    service, mb_repo, _ = _service([_result()])
    await service.search_bucket("artists", "Muse")

    now += search_service_module.SEARCH_STALE_CACHE_TTL
    mb_repo.search_artists.side_effect = RuntimeError("offline")
    results, _, status = await service.search_bucket("artists", "Muse")

    assert results == []
    assert status == "error"
    assert SearchService._stale_bucket_cache == {}


@pytest.mark.asyncio
async def test_stale_album_reprojects_ownership_for_current_service() -> None:
    live_service, live_repo, _ = _service([])
    live_repo.search_albums.return_value = [_album_result()]
    live_ownership = AsyncMock()
    live_ownership.project_albums.return_value = [SimpleNamespace(owned=True)]
    live_service._ownership = live_ownership

    live_results, _, live_status = await live_service.search_bucket(
        "albums", "Absolution"
    )

    stale_service, stale_repo, _ = _service([])
    stale_repo.search_albums.side_effect = RuntimeError("offline")
    stale_ownership = AsyncMock()
    stale_ownership.project_albums.return_value = [SimpleNamespace(owned=False)]
    stale_service._ownership = stale_ownership
    stale_results, _, stale_status = await stale_service.search_bucket(
        "albums", "Absolution"
    )

    assert live_status == "ok"
    assert live_results[0].in_library is True
    assert stale_status == "stale"
    assert stale_results[0].in_library is False


@pytest.mark.asyncio
async def test_empty_successful_bucket_is_not_cached() -> None:
    service, _, _ = _service([])

    _, _, status = await service.search_bucket("artists", "Missing")

    assert status == "ok"
    assert SearchService._stale_bucket_cache == {}


@pytest.mark.asyncio
async def test_partial_bucket_is_not_cached() -> None:
    service, _, _ = _service([_result()])
    context = init_degradation_context()
    context.record(IntegrationResult.error(source="musicbrainz", msg="partial"))

    _, _, status = await service.search_bucket("artists", "Muse")

    assert status == "partial"
    assert SearchService._stale_bucket_cache == {}


def test_stale_bucket_cache_evicts_oldest_and_clears(monkeypatch) -> None:
    monkeypatch.setattr(search_service_module, "SEARCH_STALE_CACHE_MAX_SIZE", 2)
    result = _result()

    SearchService._store_stale_bucket("one", [result], 1.0)
    SearchService._store_stale_bucket("two", [result], 2.0)
    SearchService._store_stale_bucket("three", [result], 3.0)

    assert set(SearchService._stale_bucket_cache) == {"two", "three"}
    SearchService.clear_cached_results()
    assert SearchService._stale_bucket_cache == {}
