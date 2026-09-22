import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from infrastructure.cache.memory_cache import InMemoryCache
from infrastructure.http.deduplication import RequestDeduplicator
from infrastructure.observability.optional_work import (
    OptionalWorkBudget,
    OptionalWorkDeferred,
    optional_work_budget,
)
from infrastructure.resilience.retry import CircuitState
from repositories import audiodb_repository as adb
from repositories import lastfm_repository as lfm
from repositories import listenbrainz_repository as lb
from repositories import wikidata_repository as wd
from repositories.wikidata_repository import WikidataRepository
from services.audiodb_image_service import AudioDBImageService

@pytest.fixture(autouse=True)
def clean_breakers(monkeypatch):
    for module, prefix in ((lfm, "lastfm"), (lb, "listenbrainz"), (adb, "audiodb"), (wd, "wikidata")):
        breaker = getattr(module, f"_{prefix}_circuit_breaker")
        monkeypatch.setattr(breaker, "state", CircuitState.CLOSED)
        monkeypatch.setattr(breaker, "failure_count", 0)
        monkeypatch.setattr(breaker, "success_count", 0)
        monkeypatch.setattr(breaker, "last_failure_time", 0)
        monkeypatch.setattr(breaker, "_on_state_change", None)



@pytest.fixture
def providers(monkeypatch):
    cache = InMemoryCache(max_entries=100)
    client = AsyncMock(spec=httpx.AsyncClient)
    limiter = AsyncMock()
    monkeypatch.setattr(lfm, "_lastfm_rate_limiter", limiter)
    monkeypatch.setattr(lb, "_listenbrainz_rate_limiter", limiter)
    lb._reset_listenbrainz_rate_limit_state()
    preferences = MagicMock()
    preferences.get_advanced_settings.return_value.audiodb_api_key = "123"
    lastfm = lfm.LastFmRepository(client, cache, api_key="key")
    listenbrainz = lb.ListenBrainzRepository(client, cache)
    audiodb = adb.AudioDBRepository(client, preferences)
    audiodb._rate_limiter = limiter
    yield client, limiter, lastfm, listenbrainz, audiodb
    lb._reset_listenbrainz_rate_limit_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["lastfm", "listenbrainz", "audiodb"])
async def test_guard_change_while_waiting_refunds_unsent_owner(providers, provider):
    client, limiter, lastfm, listenbrainz, audiodb = providers
    entered = asyncio.Event()
    release = asyncio.Event()
    eligible = True

    async def acquire():
        entered.set()
        await release.wait()

    limiter.acquire.side_effect = acquire
    calls = {
        "lastfm": lambda: lastfm._request("artist.getInfo"),
        "listenbrainz": lambda: listenbrainz._get("/test"),
        "audiodb": lambda: audiodb._request("artist-mb.php"),
    }
    budget = OptionalWorkBudget(remaining=1, guard=lambda: eligible)
    with optional_work_budget(budget):
        owner = asyncio.create_task(calls[provider]())
        await entered.wait()
        eligible = False
        release.set()
        with pytest.raises(OptionalWorkDeferred):
            await owner
    assert budget.remaining == 1
    client.get.assert_not_awaited()
    client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_and_cached_result_share_one_operation(providers, monkeypatch):
    client, _, lastfm, _, _ = providers
    client.get.side_effect = [
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, json={"topalbums": {"album": []}}),
    ]
    monkeypatch.setattr("infrastructure.resilience.retry.asyncio.sleep", AsyncMock())
    with optional_work_budget(OptionalWorkBudget(remaining=1)) as budget:
        assert await lastfm.get_artist_top_albums("Artist") == []
        assert await lastfm.get_artist_top_albums("Artist") == []
        with pytest.raises(OptionalWorkDeferred):
            await lastfm.get_artist_top_albums("Other")
    assert client.get.await_count == 2
    assert budget.remaining == 0


@pytest.mark.asyncio
async def test_biography_second_hop_defers_without_negative_cache():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = httpx.Response(200, json={
        "entities": {"Q123": {"sitelinks": {"enwiki": {"title": "Artist"}}}}
    })
    cache = InMemoryCache(max_entries=100)
    repo = WikidataRepository(client, cache)
    url = "https://www.wikidata.org/wiki/Q123"
    with optional_work_budget(OptionalWorkBudget(remaining=1)):
        with pytest.raises(OptionalWorkDeferred):
            await repo.get_wikipedia_extract(url)
    from infrastructure.cache.cache_keys import wikipedia_extract_key
    assert await cache.get(wikipedia_extract_key(url)) is None
    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_image_fallback_defer_preserves_only_completed_mbid_miss():
    repository = AsyncMock()
    repository.get_artist_by_mbid.return_value = None
    repository.search_artist_by_name.side_effect = OptionalWorkDeferred()
    disk = AsyncMock()
    disk.get_audiodb_artist.return_value = None
    preferences = MagicMock()
    service = AudioDBImageService(repository, disk, preferences)
    with pytest.raises(OptionalWorkDeferred):
        await service.fetch_and_cache_artist_images("artist-id", name="Artist")
    assert disk.set_audiodb_artist.await_count == 1
    assert disk.set_audiodb_artist.call_args.args[1].lookup_source == "mbid"


@pytest.mark.asyncio
async def test_wiki_join_costs_zero_with_last_unit():
    client = AsyncMock(spec=httpx.AsyncClient)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def get(url):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"query": {"pages": {"1": {
            "pageid": 1, "extract": "Artist biography",
        }}}})

    client.get.side_effect = get
    repo = WikidataRepository(client, InMemoryCache(max_entries=100))
    url = "https://en.wikipedia.org/wiki/Joined_Artist"
    with optional_work_budget(OptionalWorkBudget(remaining=1)) as budget:
        owner = asyncio.create_task(repo.get_wikipedia_extract(url))
        await entered.wait()
        waiter = asyncio.create_task(repo.get_wikipedia_extract(url))
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(owner, waiter) == ["Artist biography"] * 2
    assert budget.remaining == 0
    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_foreground_waiter_retries_completed_optional_defer():
    dedup = RequestDeduplicator()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def deferred():
        entered.set()
        await release.wait()
        raise OptionalWorkDeferred()

    async def background():
        with optional_work_budget():
            return await dedup.dedupe("same", deferred)

    foreground_factory = AsyncMock(return_value="foreground result")
    owner = asyncio.create_task(background())
    await entered.wait()
    waiter = asyncio.create_task(dedup.dedupe("same", foreground_factory))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(OptionalWorkDeferred):
        await owner
    assert await waiter == "foreground result"
    foreground_factory.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_providers_share_last_operation(providers):
    client, _, lastfm, listenbrainz, audiodb = providers
    client.get.return_value = httpx.Response(200, json={})
    client.request.return_value = httpx.Response(200, json={})
    with optional_work_budget(OptionalWorkBudget(remaining=1)) as budget:
        results = await asyncio.gather(
            lastfm._request("artist.getInfo"),
            listenbrainz._get("/test"),
            audiodb._request("artist-mb.php"),
            return_exceptions=True,
        )
    assert sum(isinstance(result, OptionalWorkDeferred) for result in results) == 2
    assert client.get.await_count + client.request.await_count == 1
    assert budget.remaining == 0
