"""Charts fetch efficiency.

The three chart overviews fetch the SAME upstream window (count=26,
offset=0) that the range-expansion endpoints consume, so the first expansion of
any range is a pure repo-cache hit. Concurrent identical overviews coalesce
through the house deduplicator - one leader per method."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.v1.schemas.settings import (
    LastFmConnectionSettings,
    ListenBrainzConnectionSettings,
    PrimaryMusicSourceSettings,
)
from services.home.charts_service import CHARTS_FETCH_WINDOW, HomeChartsService

RANGES = ["this_week", "this_month", "this_year", "all_time"]


def _make_service(
    primary_source: str = "listenbrainz",
) -> tuple[HomeChartsService, AsyncMock, AsyncMock]:
    lb_repo = AsyncMock()
    lb_repo.get_sitewide_top_artists = AsyncMock(return_value=[])
    lb_repo.get_sitewide_top_release_groups = AsyncMock(return_value=[])

    lfm_repo = AsyncMock()
    library_repo = AsyncMock()
    mb_repo = AsyncMock()

    prefs = MagicMock()
    prefs.get_listenbrainz_connection.return_value = ListenBrainzConnectionSettings(
        user_token="tok", username="lbuser", enabled=True
    )
    prefs.get_lastfm_connection.return_value = LastFmConnectionSettings(
        api_key="key",
        shared_secret="secret",
        session_key="sk",
        username="lfmuser",
        enabled=False,
    )
    prefs.is_lastfm_enabled.return_value = False
    prefs.get_primary_music_source.return_value = PrimaryMusicSourceSettings(
        source=primary_source
    )

    service = HomeChartsService(
        listenbrainz_repo=lb_repo,
        library_repo=library_repo,
        musicbrainz_repo=mb_repo,
        lastfm_repo=lfm_repo,
        preferences_service=prefs,
    )
    return service, lb_repo, lfm_repo


def _make_user_factory(per_user_clients: dict[str, AsyncMock]) -> MagicMock:
    factory = MagicMock()
    factory.resolve_listenbrainz = AsyncMock(
        side_effect=lambda user_id: per_user_clients[user_id]
    )
    factory.resolve_lastfm = AsyncMock(return_value=None)
    factory.resolve_listenbrainz_username = AsyncMock(return_value="lbuser")
    factory.resolve_lastfm_username = AsyncMock(return_value=None)
    return factory


@pytest.mark.asyncio
async def test_trending_overview_fetches_the_expansion_window():
    service, lb_repo, _ = _make_service()

    await service.get_trending_artists(limit=10)

    # one call per rendered range, at exactly the window the expand path reads
    assert lb_repo.get_sitewide_top_artists.await_count == 4
    for awaited in lb_repo.get_sitewide_top_artists.await_args_list:
        assert awaited.kwargs["count"] == CHARTS_FETCH_WINDOW
        assert awaited.kwargs["offset"] == 0
    covered = {
        awaited.kwargs["range_"]
        for awaited in lb_repo.get_sitewide_top_artists.await_args_list
    }
    assert covered == set(RANGES)


@pytest.mark.asyncio
async def test_trending_expansion_hits_the_same_window_keys():
    """Overview (limit=10) and first expansion (limit=25, offset=0) must issue
    byte-identical repo arguments - that identity is what makes the expansion a
    cache hit instead of a fresh upstream call."""
    service, lb_repo, _ = _make_service()

    await service.get_trending_artists(limit=10)
    await service.get_trending_artists_by_range(
        range_key="this_week", limit=25, offset=0
    )

    first = lb_repo.get_sitewide_top_artists.await_args_list[0].kwargs
    expansion = lb_repo.get_sitewide_top_artists.await_args_list[-1].kwargs
    assert (first["range_"], first["count"], first["offset"]) == (
        expansion["range_"],
        expansion["count"],
        expansion["offset"],
    )
    assert expansion["count"] == 26 and expansion["offset"] == 0


@pytest.mark.asyncio
async def test_popular_overview_fetches_the_expansion_window():
    service, lb_repo, _ = _make_service()

    await service.get_popular_albums(limit=10)
    await service.get_popular_albums_by_range(range_key="all_time", limit=25, offset=0)

    assert lb_repo.get_sitewide_top_release_groups.await_count == 5
    for awaited in lb_repo.get_sitewide_top_release_groups.await_args_list:
        assert awaited.kwargs["count"] == CHARTS_FETCH_WINDOW
        assert awaited.kwargs["offset"] == 0


@pytest.mark.asyncio
async def test_your_top_overview_fetches_the_expansion_window():
    lb_client = AsyncMock()
    lb_client.get_user_top_release_groups = AsyncMock(return_value=[])
    service, _, _ = _make_service()
    service._client_factory = _make_user_factory({"u1": lb_client})

    await service.get_your_top_albums(user_id="u1", limit=10)
    await service.get_your_top_albums_by_range(user_id="u1", limit=25, offset=0)

    assert lb_client.get_user_top_release_groups.await_count == 5
    for awaited in lb_client.get_user_top_release_groups.await_args_list:
        assert awaited.kwargs["count"] == CHARTS_FETCH_WINDOW
        assert awaited.kwargs["offset"] == 0


@pytest.mark.asyncio
async def test_concurrent_cold_overviews_coalesce_per_method():
    """N concurrent cold requests share one leader: still exactly 4 upstream
    calls (one per range), not 4 x N."""
    lb_repo = AsyncMock()

    async def _slow(*args, **kwargs):
        await asyncio.sleep(0.02)
        return []

    lb_repo.get_sitewide_top_artists = AsyncMock(side_effect=_slow)
    lb_repo.get_sitewide_top_release_groups = AsyncMock(return_value=[])
    lfm_repo = AsyncMock()
    library_repo = AsyncMock()
    mb_repo = AsyncMock()
    prefs = MagicMock()
    prefs.get_listenbrainz_connection.return_value = ListenBrainzConnectionSettings(
        user_token="tok", username="lbuser", enabled=True
    )
    prefs.get_lastfm_connection.return_value = LastFmConnectionSettings(
        api_key="key",
        shared_secret="secret",
        session_key="sk",
        username="lfmuser",
        enabled=False,
    )
    prefs.is_lastfm_enabled.return_value = False
    prefs.get_primary_music_source.return_value = PrimaryMusicSourceSettings(
        source="listenbrainz"
    )
    service = HomeChartsService(
        listenbrainz_repo=lb_repo,
        library_repo=library_repo,
        musicbrainz_repo=mb_repo,
        lastfm_repo=lfm_repo,
        preferences_service=prefs,
    )

    results = await asyncio.gather(
        *(service.get_trending_artists(limit=3) for _ in range(5))
    )

    assert len(results) == 5
    assert lb_repo.get_sitewide_top_artists.await_count == 4


@pytest.mark.asyncio
async def test_your_top_dedup_is_keyed_per_user():
    """Different users never share an overview result."""
    clients = {"u1": AsyncMock(), "u2": AsyncMock()}
    for client in clients.values():
        client.get_user_top_release_groups = AsyncMock(return_value=[])
    service, _, _ = _make_service()
    service._client_factory = _make_user_factory(clients)

    await asyncio.gather(
        service.get_your_top_albums(user_id="u1", limit=4),
        service.get_your_top_albums(user_id="u2", limit=4),
    )

    assert clients["u1"].get_user_top_release_groups.await_count == 4
    assert clients["u2"].get_user_top_release_groups.await_count == 4
