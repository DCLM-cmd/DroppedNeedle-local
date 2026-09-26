from unittest.mock import AsyncMock

import pytest

from core.exceptions import ConfigurationError
from services.youtube_service import YouTubeService


@pytest.mark.asyncio
async def test_saved_album_and_track_links_remain_available_when_search_is_disabled():
    repository = AsyncMock()
    repository.search_video.side_effect = ConfigurationError("disabled")
    repository.search_track.side_effect = ConfigurationError("disabled")
    store = AsyncMock()
    store.get_youtube_link.return_value = {
        "album_id": "album", "album_name": "Album", "artist_name": "Artist",
        "created_at": "2026-09-06T00:00:00Z", "video_id": "abcdefghijk",
        "embed_url": "https://www.youtube.com/embed/abcdefghijk",
    }
    store.get_youtube_track_links.return_value = [{
        "album_id": "album", "album_name": "Album", "artist_name": "Artist",
        "created_at": "2026-09-06T00:00:00Z", "video_id": "track-video",
        "embed_url": "https://www.youtube.com/embed/track-video",
        "disc_number": 2, "track_number": 1, "track_name": "Track",
    }]
    service = YouTubeService(repository, store)
    album = await service.generate_link("Artist", "Album", "album")
    track = await service.generate_track_link("album", "Album", "Artist", "Track", 1, disc_number=2)
    generated, failed = await service.generate_track_links_batch(
        "album", "Album", "Artist", [{"track_number": 1, "disc_number": 2, "track_name": "Track"}],
    )
    assert album.video_id == "abcdefghijk"
    assert track.video_id == "track-video"
    assert generated == [track]
    assert failed == []
    repository.search_video.assert_not_awaited()
    repository.search_track.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_link_disabled_failure_is_not_reported_as_absence():
    repository = AsyncMock()
    repository.search_video.side_effect = ConfigurationError("disabled")
    store = AsyncMock()
    store.get_youtube_link.return_value = None
    service = YouTubeService(repository, store)
    with pytest.raises(ConfigurationError):
        await service.generate_link("Artist", "Album", "album")
    store.save_youtube_link.assert_not_awaited()
