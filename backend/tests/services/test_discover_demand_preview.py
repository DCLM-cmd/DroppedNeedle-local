import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.v1.schemas.settings import YouTubeConnectionSettings
from core.exceptions import ConfigurationError, ExternalServiceError, RateLimitedError, ValidationError
from repositories.musicbrainz_repository import MusicBrainzRepository
from services.discover.enrichment_service import QueueEnrichmentService

RG = "074aa5b0-712e-4d6c-8d14-8aedc43e84fd"
VIDEO = "https://www.youtube.com/watch?v=abcdefghijk"
EMBED = "https://www.youtube.com/embed/abcdefghijk"


def relations(url=VIDEO):
    return {"relations": [{"type": "streaming", "url": {"resource": url}}]}


def make_service():
    mb = AsyncMock()
    mb.extract_youtube_url_from_relations = MusicBrainzRepository.extract_youtube_url_from_relations
    mb.youtube_url_to_embed = MusicBrainzRepository.youtube_url_to_embed
    mb.get_release_group_by_id.return_value = {
        "id": RG, "title": "Album", "tags": [{"name": "jazz"}],
        "artist-credit": [{"artist": {"id": "artist", "name": "Artist"}}],
        "releases": [{"id": "release", "date": "2020"}],
    }
    mb.get_release_by_id.return_value = {}
    mb.get_recording_by_id.return_value = {}
    mb.get_artist_core.return_value = {"country": "GB"}
    prefs = MagicMock()
    prefs.get_youtube_connection.return_value = YouTubeConnectionSettings()
    integration = MagicMock()
    integration.is_lastfm_enabled.return_value = False
    service = QueueEnrichmentService(mb, AsyncMock(), prefs, integration)
    service._coalesce_popularity = AsyncMock(return_value=12)
    youtube = AsyncMock()
    youtube.search_video.side_effect = ConfigurationError("disabled")
    return service, mb, youtube


@pytest.mark.asyncio
async def test_core_card_is_useful_without_recordings_tracklist_or_artist_browse():
    service, mb, _ = make_service()
    core = await service.enrich_queue_item(RG)
    assert (core.tags, core.country, core.release_date, core.listen_count) == (["jazz"], "GB", "2020", 12)
    assert core.youtube_url is None
    assert "Artist+Album" in core.youtube_search_url
    mb.get_release_by_id.assert_not_awaited()
    mb.get_recording_by_id.assert_not_awaited()
    mb.get_artist_by_id.assert_not_awaited()
    mb.get_artist_release_groups.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_group_video_works_with_search_disabled_without_deeper_hunt():
    service, mb, youtube = make_service()
    mb.get_release_group_by_id.return_value.update(relations())
    result = await service.preview_queue_item(RG, youtube)
    assert (result.status, result.youtube_url) == ("available", EMBED)
    mb.get_release_by_id.assert_not_awaited()
    mb.get_recording_by_id.assert_not_awaited()
    youtube.search_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_release_relation_precedes_tracklist_and_non_video_group_url():
    service, mb, youtube = make_service()
    mb.get_release_group_by_id.return_value.update(relations("https://www.youtube.com/channel/channel-id"))

    async def release(_id, *, includes, priority):
        assert includes == ["url-rels"]
        return relations()

    mb.get_release_by_id.side_effect = release
    result = await service.preview_queue_item(RG, youtube)
    assert (result.status, result.youtube_url) == ("available", EMBED)
    assert mb.get_release_by_id.await_count == 1
    mb.get_recording_by_id.assert_not_awaited()
    youtube.search_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_recording_hunt_is_sequential_deduplicated_and_capped_at_three():
    service, mb, youtube = make_service()
    order = []

    async def release(_id, *, includes, priority):
        order.append(tuple(includes))
        if includes == ["url-rels"]:
            return {}
        return {"media": [{"tracks": [{"recording": {"id": value}} for value in ["r1", "r1", "r2", "r3", "r4"]]}]}

    async def recording(recording_id, *, includes):
        order.append(recording_id)
        return {}

    async def search(*_args):
        order.append("youtube")
        return None

    mb.get_release_by_id.side_effect = release
    mb.get_recording_by_id.side_effect = recording
    youtube.search_video.side_effect = search
    result = await service.preview_queue_item(RG, youtube)
    assert result.status == "not_found"
    assert result.youtube_url is None
    assert order == [("url-rels",), ("recordings",), "r1", "r2", "r3", "youtube"]
    assert "Artist+Album" in result.youtube_search_url


@pytest.mark.asyncio
async def test_recording_video_stops_before_later_recordings_and_search():
    service, mb, youtube = make_service()
    mb.get_release_by_id.side_effect = [{}, {"media": [{"tracks": [{"recording": {"id": "r1"}}, {"recording": {"id": "r2"}}]}]}]
    mb.get_recording_by_id.return_value = relations()
    result = await service.preview_queue_item(RG, youtube)
    assert (result.status, result.youtube_url) == ("available", EMBED)
    assert mb.get_recording_by_id.await_count == 1
    youtube.search_video.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ConfigurationError("disabled"), RateLimitedError("quota", details={"reason": "youtube_daily_quota"})])
async def test_local_search_unavailability_preserves_external_search(error):
    service, _, youtube = make_service()
    youtube.search_video.side_effect = error
    result = await service.preview_queue_item(RG, youtube)
    assert result.status == "unavailable"
    assert result.youtube_url is None
    assert "Artist+Album" in result.youtube_search_url


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ExternalServiceError("upstream failed"), RateLimitedError("upstream limited"), asyncio.CancelledError()])
async def test_search_failure_or_cancellation_is_not_authoritative_absence(error):
    service, _, youtube = make_service()
    youtube.search_video.side_effect = error
    with pytest.raises(type(error)):
        await service.preview_queue_item(RG, youtube)


@pytest.mark.asyncio
async def test_musicbrainz_failure_does_not_trigger_youtube_fallback():
    service, mb, youtube = make_service()
    mb.get_release_by_id.side_effect = ExternalServiceError("MusicBrainz failed")
    with pytest.raises(ExternalServiceError):
        await service.preview_queue_item(RG, youtube)
    youtube.search_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_id_is_rejected_before_provider_work():
    service, mb, youtube = make_service()
    with pytest.raises(ValidationError):
        await service.preview_queue_item("not-an-mbid", youtube)
    mb.get_release_group_by_id.assert_not_awaited()
    youtube.search_video.assert_not_awaited()
