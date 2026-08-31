"""How many requests a given amount of work costs ListenBrainz.

The repository was correct but chatty: empty answers were never cached so they
were re-fetched on every rebuild, the artist-popularity keys included a ``count``
the endpoint ignores so one artist was fetched once per distinct count, the
popularity batch had no cache at all, and nothing coalesced the identical
concurrent calls that the discover fan-out produces. These tests count requests.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from infrastructure.cache.memory_cache import InMemoryCache
from repositories.listenbrainz_repository import ListenBrainzRepository

ARTIST = "b10bbff0-5354-4e2c-bd11-cd0a1a5b4e1a"
OTHER_ARTIST = "f59c5520-5f46-4d2c-b2c4-822eb7c3ad1b"


def _repo() -> tuple[ListenBrainzRepository, list[str]]:
    """A repository over a real cache, with every outbound URL recorded."""
    requested: list[str] = []
    http_client = AsyncMock(spec=httpx.AsyncClient)

    async def request(method, url, **kwargs):
        requested.append(url)
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.content = None
        if "top-release-groups-for-artist" in url:
            response.json.return_value = [
                {
                    "release_group_mbid": f"rg-{index}",
                    "total_listen_count": 100 - index,
                    "release_group": {"name": f"Album {index}"},
                    "artist": {"name": "Artist"},
                }
                for index in range(10)
            ]
        elif "popularity/release-group" in url:
            payload = kwargs.get("json") or {}
            response.json.return_value = [
                {"release_group_mbid": mbid, "total_listen_count": 7}
                for mbid in payload.get("release_group_mbids", [])
            ]
        else:
            response.json.return_value = []
        return response

    http_client.request = AsyncMock(side_effect=request)
    repo = ListenBrainzRepository(
        http_client=http_client,
        cache=InMemoryCache(),
        username="user",
        user_token="tok",
    )
    return repo, requested


@pytest.mark.asyncio
async def test_differing_counts_share_one_request():
    """The endpoint takes no count, so asking for 1, 2 and 10 is one fetch."""
    repo, requested = _repo()

    first = await repo.get_artist_top_release_groups(ARTIST, count=1)
    second = await repo.get_artist_top_release_groups(ARTIST, count=2)
    third = await repo.get_artist_top_release_groups(ARTIST, count=10)

    assert len(requested) == 1
    assert (len(first), len(second), len(third)) == (1, 2, 10)


@pytest.mark.asyncio
async def test_an_empty_answer_is_remembered():
    """The dud MBIDs used to be re-requested on every single rebuild."""
    repo, requested = _repo()

    for _ in range(5):
        assert await repo.get_artist_top_recordings(ARTIST) == []

    assert len(requested) == 1


@pytest.mark.asyncio
async def test_concurrent_identical_reads_are_coalesced():
    """The discover builders gather over the same artists from several tasks."""
    repo, requested = _repo()

    results = await asyncio.gather(
        *(repo.get_artist_top_release_groups(ARTIST, count=5) for _ in range(20))
    )

    assert len(requested) == 1
    assert all(len(value) == 5 for value in results)


@pytest.mark.asyncio
async def test_distinct_artists_are_still_fetched_separately():
    """Coalescing must not collapse different artists into one answer."""
    repo, requested = _repo()

    await asyncio.gather(
        repo.get_artist_top_release_groups(ARTIST),
        repo.get_artist_top_release_groups(OTHER_ARTIST),
    )

    assert len(requested) == 2


@pytest.mark.asyncio
async def test_popularity_batch_only_asks_for_what_it_lacks():
    repo, requested = _repo()

    first = await repo.get_release_group_popularity_batch(["rg-a", "rg-b"])
    assert first == {"rg-a": 7, "rg-b": 7}
    assert len(requested) == 1

    # Wholly cached: no request at all.
    assert await repo.get_release_group_popularity_batch(["rg-a"]) == {"rg-a": 7}
    assert len(requested) == 1

    # Overlapping: only the new MBID goes upstream.
    overlapping = await repo.get_release_group_popularity_batch(["rg-b", "rg-c"])
    assert overlapping == {"rg-b": 7, "rg-c": 7}
    assert len(requested) == 2


@pytest.mark.asyncio
async def test_popularity_batch_remembers_mbids_lb_has_no_count_for():
    """An album LB has never seen must not be re-asked on every listing render."""
    repo, requested = _repo()

    async def request(method, url, **kwargs):
        requested.append(url)
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.content = None
        response.json.return_value = []  # LB knows none of them
        return response

    repo._client.request = AsyncMock(side_effect=request)

    assert await repo.get_release_group_popularity_batch(["rg-unknown"]) == {}
    assert await repo.get_release_group_popularity_batch(["rg-unknown"]) == {}

    assert len(requested) == 1


@pytest.mark.asyncio
async def test_a_transient_outage_is_not_cached_as_an_empty_artist(monkeypatch):
    """Degradation is capability-wide and short-lived. Storing it under the artist's
    key would blank that artist for the whole negative TTL after LB recovers."""
    repo, requested = _repo()
    degraded = {"value": True}
    monkeypatch.setattr(
        "repositories.listenbrainz_repository.lb_popularity_degraded",
        lambda: degraded["value"],
    )

    assert await repo.get_artist_top_release_groups(ARTIST) == []
    assert requested == []

    degraded["value"] = False
    recovered = await repo.get_artist_top_release_groups(ARTIST)

    assert len(recovered) == 10
    assert len(requested) == 1


# ---- a rate limit must not be answered with more requests ---------------------------

def _rate_limited_repo() -> tuple[ListenBrainzRepository, list[str]]:
    """A repository whose every call is refused with 429."""
    requested: list[str] = []
    http_client = AsyncMock(spec=httpx.AsyncClient)

    async def request(method, url, **kwargs):
        requested.append(url)
        response = MagicMock()
        response.status_code = 429
        response.text = "<html>429 Too Many Requests</html>"
        response.headers = {}
        return response

    http_client.request = AsyncMock(side_effect=request)
    repo = ListenBrainzRepository(
        http_client=http_client,
        cache=InMemoryCache(),
        username="user",
        user_token="tok",
    )
    return repo, requested


@pytest.mark.asyncio
async def test_a_refusal_without_a_retry_hint_is_not_retried_into():
    """The exact escalation that got this installation blocked.

    RateLimitedError is an ExternalServiceError, so the retry wrapper treated a 429 as
    transient and sent the request twice more within seconds - tripling precisely the
    traffic the server had just asked us to stop. The discover homepage issues three
    stats calls per load, so one page view became nine requests into a rate limit, and
    MetaBrainz eventually stopped answering this address at all: dropped TLS handshakes
    for ListenBrainz, MusicBrainz and the Cover Art Archive, which share one IP.
    """
    from core.exceptions import RateLimitBlockedError

    repo, requested = _rate_limited_repo()

    with pytest.raises(RateLimitBlockedError):
        await repo.get_recommendation_playlists("user")

    assert len(requested) == 1, f"asked {len(requested)} times after being told to stop"


@pytest.mark.asyncio
async def test_a_throttle_that_names_an_interval_is_still_waited_out():
    """The co-operative half: when the server says when to come back, we come back."""
    from core.exceptions import RateLimitedError

    requested: list[str] = []
    http_client = AsyncMock(spec=httpx.AsyncClient)

    async def request(method, url, **kwargs):
        requested.append(url)
        response = MagicMock()
        response.status_code = 429
        response.text = ""
        response.headers = {"Retry-After": "1"}
        return response

    http_client.request = AsyncMock(side_effect=request)
    repo = ListenBrainzRepository(
        http_client=http_client,
        cache=InMemoryCache(),
        username="user",
        user_token="tok",
    )

    with pytest.raises(RateLimitedError):
        await repo.get_recommendation_playlists("user")

    assert len(requested) > 1
