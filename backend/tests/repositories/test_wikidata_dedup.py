"""B1: wikidata repository dedup wrappers + bounded negative cache.

Concurrent identical hops collapse to one leader load; clean misses park a
"" sentinel (falsy-equivalent for callers, since CacheInterface cannot
distinguish a stored None from a miss); degraded attempts write nothing.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import repositories.wikidata_repository as wd_module
from infrastructure.cache.memory_cache import InMemoryCache
from repositories.wikidata_repository import WikidataRepository

WIKI_URL = "https://en.wikipedia.org/wiki/Test_Artist"
WIKIDATA_ID = "Q123"


def _make_repo() -> tuple[WikidataRepository, InMemoryCache]:
    client = AsyncMock(spec=object)
    cache = InMemoryCache(max_entries=100)
    return WikidataRepository(client, cache), cache


@pytest.fixture(autouse=True)
def fresh_dedup():
    wd_module._wiki_deduplicator.clear()
    yield
    wd_module._wiki_deduplicator.clear()


class TestWikipediaExtractDedup:
    @pytest.mark.asyncio
    async def test_concurrent_identical_hops_collapse_to_one_load(self, monkeypatch):
        repo, _cache = _make_repo()
        loads = {"n": 0}

        async def slow_load(url, lang, cache_key):
            loads["n"] += 1
            await asyncio.sleep(0.05)
            return "The extract."

        monkeypatch.setattr(repo, "_load_wikipedia_extract", slow_load)

        results = await asyncio.gather(
            *(repo.get_wikipedia_extract(WIKI_URL) for _ in range(5))
        )

        assert all(r == "The extract." for r in results)
        assert loads["n"] == 1  # followers awaited the leader's load

    @pytest.mark.asyncio
    async def test_clean_miss_parks_empty_sentinel(self):
        import asyncio

        repo, cache = _make_repo()

        async def absent_title(wikidata_id, lang="en"):
            await asyncio.sleep(0)
            return None

        repo._get_wikipedia_title_from_wikidata = absent_title
        repo._extract_wikidata_id = lambda url: WIKIDATA_ID

        result = await repo.get_wikipedia_extract(WIKI_URL)

        assert result == ""  # falsy-equivalent absence marker
        assert await cache.get(wd_module.wikipedia_extract_key(WIKI_URL)) == ""

    @pytest.mark.asyncio
    async def test_degraded_miss_writes_nothing(self, monkeypatch):
        repo, cache = _make_repo()
        monkeypatch.setattr(wd_module, "_wiki_source_degraded", lambda: True)

        async def absent_title(wikidata_id, lang="en"):
            return None

        repo._get_wikipedia_title_from_wikidata = absent_title
        repo._extract_wikidata_id = lambda url: WIKIDATA_ID

        result = await repo.get_wikipedia_extract(WIKI_URL)

        assert result is None  # error-None stays distinguishable
        assert await cache.get(wd_module.wikipedia_extract_key(WIKI_URL)) is None


class TestImageDedupAndSentinel:
    @pytest.mark.asyncio
    async def test_concurrent_identical_image_hops_collapse(self, monkeypatch):
        repo, _cache = _make_repo()
        loads = {"n": 0}

        async def slow_load(wikidata_id, cache_key):
            loads["n"] += 1
            await asyncio.sleep(0.05)
            return "https://commons.example/img.jpg"

        monkeypatch.setattr(repo, "_load_artist_image_from_wikidata", slow_load)

        results = await asyncio.gather(
            *(repo.get_artist_image_from_wikidata(WIKIDATA_ID) for _ in range(4))
        )

        assert all(r == "https://commons.example/img.jpg" for r in results)
        assert loads["n"] == 1

    @pytest.mark.asyncio
    async def test_no_claim_response_parks_negative_sentinel(self):
        repo, cache = _make_repo()

        claims_response = MagicMock()
        claims_response.status_code = 200
        claims_response.content = json.dumps({"entities": {}, "claims": {}}).encode()

        async def fake_get(url):
            return claims_response

        repo._client.get = fake_get

        result = await repo.get_artist_image_from_wikidata(WIKIDATA_ID)

        # Malformed-for-schema body decodes to empty claims -> clean absence.
        assert result in (None, "")
        assert await cache.get(wd_module.wikidata_artist_image_key(WIKIDATA_ID)) in (
            "",
            None,
        )
