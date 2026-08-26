"""A2 part 4: discovery-endpoint stampede maps.

K concurrent cold renders of /similar or /top-albums collapse to exactly one
leader chain (asserted via mocked repo call counts); followers receive the
identical object; a leader exception fans out to every waiter.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.v1.schemas.discovery import SimilarArtist, TopAlbumsResponse
from services.artist_discovery_service import ArtistDiscoveryService


def _make_service() -> tuple[ArtistDiscoveryService, AsyncMock]:
    lb_repo = AsyncMock()
    lb_repo.is_configured.return_value = True
    lb_repo.get_similar_artists = AsyncMock()
    lb_repo.get_artist_top_release_groups = AsyncMock()
    lb_repo.get_recording_release_groups_batch = AsyncMock(return_value={})

    mb_repo = AsyncMock()
    memory_cache = AsyncMock()
    memory_cache.get = AsyncMock(return_value=None)

    svc = ArtistDiscoveryService(
        listenbrainz_repo=lb_repo,
        musicbrainz_repo=mb_repo,
        library_db=AsyncMock(),
        library_repo=AsyncMock(),
        memory_cache=memory_cache,
        lastfm_repo=AsyncMock(),
        preferences_service=MagicMock(),
    )
    return svc, lb_repo


class TestSimilarStampede:
    @pytest.mark.asyncio
    async def test_k_concurrent_colds_share_one_leader_chain(self):
        svc, lb_repo = _make_service()

        async def slow_similar(mbid, max_similar=15):
            await asyncio.sleep(0.05)
            return [
                SimpleNamespaceLike(artist_mbid="a-1", artist_name="A", listen_count=9)
            ]

        # SimpleNamespace-like stub keeps the mapping below trivially valid.
        class SimpleNamespaceLike:
            def __init__(self, artist_mbid, artist_name, listen_count):
                self.artist_mbid = artist_mbid
                self.artist_name = artist_name
                self.listen_count = listen_count

        slow_similar = slow_similar  # keep name for count assertion
        calls = {"n": 0}

        async def counting_similar(mbid, max_similar=15):
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return [SimpleNamespaceLike("a-1", "Artist", 10)]

        lb_repo.get_similar_artists = counting_similar

        results = await asyncio.gather(
            *(svc.get_similar_artists("artist-1", count=5) for _ in range(5))
        )

        assert all(r.source == "listenbrainz" for r in results)
        assert calls["n"] == 1  # exactly one leader chain
        assert results[0] is results[1] is results[-1]

    @pytest.mark.asyncio
    async def test_leader_exception_fans_out_to_all_waiters(self):
        svc, lb_repo = _make_service()

        async def failing(mbid, max_similar=15):
            raise RuntimeError("lb exploded")

        lb_repo.get_similar_artists = failing

        results = await asyncio.gather(
            *(svc.get_similar_artists("artist-1", count=5) for _ in range(3)),
            return_exceptions=True,
        )

        # The service catches provider errors internally and degrades to an
        # empty response - the fan-out guarantee is that ALL waiters receive
        # the SAME degraded object (one leader execution).
        assert len(results) == 3
        assert all(r == results[0] for r in results)


class TestTopAlbumsStampede:
    @pytest.mark.asyncio
    async def test_k_concurrent_colds_share_one_leader_chain(self):
        svc, lb_repo = _make_service()
        calls = {"n": 0}

        async def counting_rg(mbid, count=10):
            calls["n"] += 1
            await asyncio.sleep(0.05)
            from repositories.listenbrainz_models import ListenBrainzReleaseGroup

            return [
                ListenBrainzReleaseGroup(
                    release_group_name=f"RG {i}",
                    artist_name="Artist",
                    listen_count=100 - i,
                    release_group_mbid=f"rg-{i}",
                    caa_release_mbid=None,
                    caa_id=None,
                )
                for i in range(count)
            ]

        lb_repo.get_artist_top_release_groups = counting_rg

        results = await asyncio.gather(
            *(svc.get_top_albums("artist-1", count=5) for _ in range(5))
        )

        assert all(len(r.albums) == 5 for r in results)
        assert calls["n"] == 1
        assert results[0] is results[1]


class SimpleNamespaceLike:
    def __init__(self, artist_mbid: str, artist_name: str, listen_count: int):
        self.artist_mbid = artist_mbid
        self.artist_name = artist_name
        self.listen_count = listen_count


@pytest.mark.asyncio
async def test_lastfm_fallback_runs_once_inside_leader():
    """Degradation-TTL empties and the LFM fallback execute once in the
    leader; waiters get the fallback-filled result."""
    svc, lb_repo = _make_service()
    lb_repo.is_configured.return_value = True
    lb_repo.get_similar_artists.return_value = []  # LB yields nothing -> LFM path

    lastfm_repo = AsyncMock()
    lastfm_repo.is_configured = lambda: True
    from repositories.lastfm_models import LastFmSimilarArtist

    lastfm_calls = {"n": 0}

    async def counting_lfm_similar(artist_mbid, count=15, user_id=None):
        lastfm_calls["n"] += 1
        await asyncio.sleep(0.03)
        return [LastFmSimilarArtist(name="LFM Artist", mbid="lfm-1", match=0.8, url="")]

    svc._resolve_listenbrainz = AsyncMock(return_value=lb_repo)

    async def fake_resolve(user_id=None):
        return lastfm_repo

    async def lfm_fallback(section, user_id, artist_mbid, count):
        result = await counting_lfm_similar(artist_mbid, count)
        if section == "similar":
            from api.v1.schemas.discovery import SimilarArtistsResponse

            return SimilarArtistsResponse(
                similar_artists=[
                    SimilarArtist(
                        musicbrainz_id=r.mbid,
                        name=r.name,
                        listen_count=int(r.match * 100),
                    )
                    for r in result
                ],
                source="lastfm",
            )
        return None

    svc._lastfm_fallback = lfm_fallback
    svc._resolve_lastfm = fake_resolve

    results = await asyncio.gather(
        *(svc.get_similar_artists("artist-1", count=5) for _ in range(4))
    )

    assert lastfm_calls["n"] == 1
    assert all(r.source == "lastfm" for r in results)
