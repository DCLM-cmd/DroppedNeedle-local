import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.v1.schemas.settings import YouTubeConnectionSettings
from services.discover.enrichment_service import QueueEnrichmentService

RG = "074aa5b0-712e-4d6c-8d14-8aedc43e84fd"


def make_service():
    mb = AsyncMock()
    mb.get_release_group_by_id.return_value = {
        "id": RG, "title": "Some Album", "tags": [{"name": "rock"}],
        "artist-credit": [{"artist": {"id": "artist-1", "name": "The Artist"}}],
        "releases": [{"id": "release-1", "date": "2020-01-01"}],
    }
    mb.get_artist_core.return_value = {"country": "GB", "relations": []}
    mb.extract_youtube_url_from_relations = MagicMock(return_value=None)
    prefs = MagicMock()
    prefs.get_youtube_connection.return_value = YouTubeConnectionSettings()
    integration = MagicMock()
    integration.is_lastfm_enabled.return_value = False
    lastfm = AsyncMock()
    service = QueueEnrichmentService(mb, AsyncMock(), prefs, integration, lastfm_repo=lastfm)
    service._coalesce_popularity = AsyncMock(return_value=15)
    return service, mb, integration, lastfm


@pytest.mark.asyncio
async def test_artist_core_and_popularity_resolve_independently():
    service, mb, _, _ = make_service()
    artist_entered, popularity_entered = asyncio.Event(), asyncio.Event()

    async def artist(*_args, **_kwargs):
        artist_entered.set()
        await popularity_entered.wait()
        return {"country": "GB"}

    async def popularity(*_args):
        popularity_entered.set()
        await artist_entered.wait()
        return 15

    mb.get_artist_core.side_effect = artist
    service._coalesce_popularity.side_effect = popularity
    result = await asyncio.wait_for(service.enrich_queue_item(RG), timeout=1)
    assert (result.country, result.listen_count, result.tags, result.release_date) == ("GB", 15, ["rock"], "2020-01-01")
    assert result.youtube_url is None
    mb.get_release_by_id.assert_not_awaited()
    mb.get_recording_by_id.assert_not_awaited()
    mb.get_artist_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_lastfm_bio_fallback_retains_existing_core_tags():
    service, _, integration, lastfm = make_service()
    integration.is_lastfm_enabled.return_value = True
    lastfm.get_album_info.return_value = SimpleNamespace(tags=[], summary="An album recorded in London.")
    result = await service.enrich_queue_item(RG)
    assert result.artist_description == "An album recorded in London."
    assert result.tags == ["rock"]
    assert result.country == "GB"
    lastfm.get_artist_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelling_core_hydration_stops_artist_work_and_allows_retry():
    service, mb, _, _ = make_service()
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def artist(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    mb.get_artist_core.side_effect = artist
    task = asyncio.create_task(service.enrich_queue_item(RG))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(stopped.wait(), timeout=1)
    mb.get_artist_core.side_effect = None
    mb.get_artist_core.return_value = {"country": "FR"}
    retry = await service.enrich_queue_item(RG)
    assert retry.country == "FR"
    assert retry.tags == ["rock"]
