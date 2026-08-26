"""B3.2: top-songs positions gathered without changing output.

Positions are in-process dict reads; the gather must keep positions mapped
by index (order identical to the old serial loop) with zero MB wire calls.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from repositories.listenbrainz_models import ListenBrainzRecording
from services.artist_discovery_service import ArtistDiscoveryService


def _recording(idx: int, *, release_mbid: str | None = None) -> ListenBrainzRecording:
    return ListenBrainzRecording(
        track_name=f"Track {idx}",
        artist_name="Artist",
        listen_count=100 - idx,
        recording_mbid=f"rec-{idx}",
        release_name=f"Release {idx}",
        release_mbid=release_mbid,
    )


def _make_service() -> tuple[ArtistDiscoveryService, AsyncMock]:
    lb_repo = MagicMock()
    lb_repo.is_configured.return_value = True
    lb_repo.get_artist_top_recordings = AsyncMock(return_value=[])
    lb_repo.get_recording_release_groups_batch = AsyncMock(return_value={})

    mb_repo = AsyncMock()

    library_db = AsyncMock()
    library_db.get_all_artist_mbids = AsyncMock(return_value=set())
    memory_cache = AsyncMock()
    memory_cache.get = AsyncMock(return_value=None)
    memory_cache.set = AsyncMock()

    svc = ArtistDiscoveryService(
        listenbrainz_repo=lb_repo,
        musicbrainz_repo=mb_repo,
        library_db=library_db,
        library_repo=AsyncMock(),
        memory_cache=memory_cache,
        lastfm_repo=AsyncMock(),
        preferences_service=MagicMock(),
    )
    return svc, mb_repo


class TestTopSongsPositionGather:
    @pytest.mark.asyncio
    async def test_positions_unchanged_and_order_preserved_under_gather(self):
        svc, mb_repo = _make_service()
        recordings = [
            _recording(0, release_mbid="rel-0"),
            _recording(1, release_mbid="rel-1"),
            _recording(2),  # no release/recording pair -> position stays None
            _recording(3, release_mbid="rel-3"),
            _recording(4),
        ]
        svc._lb_repo.get_artist_top_recordings = AsyncMock(return_value=recordings)

        async def fake_position(release_mbid, recording_mbid):
            # Distinct values prove index-mapped zip, not shared/last-write.
            idx = int(recording_mbid.split("-")[1])
            return (idx + 1, idx * 10)

        mb_repo.get_recording_position_on_release = AsyncMock(side_effect=fake_position)

        result = await svc.get_top_songs("artist-1", count=10)

        assert [s.title for s in result.songs] == [f"Track {i}" for i in range(5)]
        assert (result.songs[0].disc_number, result.songs[0].track_number) == (1, 0)
        assert (result.songs[1].disc_number, result.songs[1].track_number) == (2, 10)
        assert result.songs[2].disc_number is None
        assert result.songs[2].track_number is None
        assert (result.songs[3].disc_number, result.songs[3].track_number) == (4, 30)
        assert result.songs[4].disc_number is None
        # Zero MB wire calls: positions come from the in-memory dict read.
        mb_repo.browse_release_groups.assert_not_called()

    @pytest.mark.asyncio
    async def test_count_slices_recordings(self):
        svc, mb_repo = _make_service()
        recordings = [_recording(i, release_mbid=f"rel-{i}") for i in range(6)]
        svc._lb_repo.get_artist_top_recordings = AsyncMock(return_value=recordings)
        mb_repo.get_recording_position_on_release = AsyncMock(return_value=(1, 1))

        result = await svc.get_top_songs("artist-1", count=3)

        assert len(result.songs) == 3
        assert mb_repo.get_recording_position_on_release.await_count == 3
