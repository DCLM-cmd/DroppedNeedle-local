from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import repositories.musicbrainz_artist as artist_module
from infrastructure.queue.priority_queue import RequestPriority
from repositories.musicbrainz_artist import MusicBrainzArtistMixin


@pytest.mark.asyncio
async def test_artist_lookup_threads_priority_to_both_musicbrainz_calls(
    monkeypatch,
) -> None:
    artist_payload = {"id": "artist-id", "name": "Test Artist"}
    browse_payload = SimpleNamespace(release_groups=[], release_group_count=0)
    mb_get = AsyncMock(side_effect=[artist_payload, browse_payload])
    monkeypatch.setattr(artist_module, "mb_api_get", mb_get)

    repository = MusicBrainzArtistMixin.__new__(MusicBrainzArtistMixin)
    repository._cache = SimpleNamespace(
        get=AsyncMock(return_value=None),
        set=AsyncMock(),
    )

    result = await repository.get_artist_by_id(
        "artist-id",
        priority=RequestPriority.BACKGROUND_SYNC,
    )

    assert result == artist_payload
    assert mb_get.await_count == 2
    assert all(
        call.kwargs["priority"] == RequestPriority.BACKGROUND_SYNC
        for call in mb_get.await_args_list
    )
