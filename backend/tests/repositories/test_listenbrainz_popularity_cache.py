"""B4 Change 2: per-MBID cache-aside around get_release_group_popularity_batch.

Covers: hit-no-POST, miss partitioning (POST only misses, body sorted),
degraded-gate caches nothing (and returns {} for the whole call), failure and
malformed-response cache nothing, False-sentinel negative cache at short TTL,
and _metadata_deduplicator coalescing of concurrent identical batches.
"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

import repositories.listenbrainz_repository as lb_module
from infrastructure.cache.memory_cache import InMemoryCache
from infrastructure.degradation import (
    clear_degradation_context,
    init_degradation_context,
)
from repositories.listenbrainz_repository import ListenBrainzRepository


def _make_repo() -> tuple[ListenBrainzRepository, InMemoryCache]:
    client = AsyncMock(spec=httpx.AsyncClient)
    cache = InMemoryCache(max_entries=100)
    return ListenBrainzRepository(client, cache), cache


def _key(mbid: str) -> str:
    return f"lb_rg_popularity:{mbid}"


def _post_result(pairs: list[tuple[str, int]]):
    return [
        {"release_group_mbid": mbid, "total_listen_count": count}
        for mbid, count in pairs
    ]


@pytest.fixture
def fresh_deduplicator():
    lb_module._metadata_deduplicator.clear()
    yield
    lb_module._metadata_deduplicator.clear()


class TestHitNoPost:
    @pytest.mark.asyncio
    async def test_fully_cached_batch_never_posts(self):
        repo, cache = _make_repo()
        repo._post = AsyncMock()
        await cache.set(_key("rg-a"), 111)
        await cache.set(_key("rg-b"), 222)

        counts = await repo.get_release_group_popularity_batch(["rg-a", "rg-b"])

        assert counts == {"rg-a": 111, "rg-b": 222}
        repo._post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_false_sentinel_counts_as_known_absent(self):
        repo, cache = _make_repo()
        repo._post = AsyncMock()
        await cache.set(_key("rg-a"), False)

        counts = await repo.get_release_group_popularity_batch(["rg-a"])

        assert counts == {}
        repo._post.assert_not_awaited()


class TestMissPartitioning:
    @pytest.mark.asyncio
    async def test_post_runs_only_for_misses_and_body_is_sorted(self):
        repo, cache = _make_repo()
        await cache.set(_key("rg-a"), 999)  # warm hit overrides wire value
        captured: dict = {}

        async def spy_post(endpoint, payload):
            captured["endpoint"] = endpoint
            captured["payload"] = payload
            return _post_result([("rg-c", 7)])

        repo._post = spy_post

        counts = await repo.get_release_group_popularity_batch(["rg-c", "rg-a", "rg-b"])

        # Only the misses (b, c) go to the wire, sorted; the hit (a) never does.
        assert captured["payload"]["release_group_mbids"] == ["rg-b", "rg-c"]
        assert counts["rg-a"] == 999  # cache wins over POST for hits
        assert counts["rg-c"] == 7

    @pytest.mark.asyncio
    async def test_found_mbids_cached_positive_absent_cached_negative(self):
        repo, cache = _make_repo()
        post_calls: list[dict] = []

        async def spy_post(endpoint, payload):
            post_calls.append(payload)
            return _post_result([("rg-a", 12)])

        repo._post = spy_post

        counts = await repo.get_release_group_popularity_batch(["rg-a", "rg-missing"])

        assert counts == {"rg-a": 12}
        assert await cache.get(_key("rg-a")) == 12  # positive TTL 3600 s
        assert await cache.get(_key("rg-missing")) is False  # negative sentinel

    @pytest.mark.asyncio
    async def test_duplicate_request_mbids_collapse_to_one_wire_call(self):
        repo, cache = _make_repo()
        post_calls: list[dict] = []

        async def spy_post(endpoint, payload):
            post_calls.append(payload)
            return _post_result([(m, 3) for m in payload["release_group_mbids"]])

        repo._post = spy_post

        counts = await repo.get_release_group_popularity_batch(["rg-x", "rg-x", "rg-x"])

        assert counts == {"rg-x": 3}
        assert len(post_calls) == 1  # duplicates collapsed before partitioning
        assert await cache.get(_key("rg-x")) == 3


class TestPoisoningGuards:
    @pytest.mark.asyncio
    async def test_degraded_gate_returns_empty_and_writes_nothing(self, monkeypatch):
        monkeypatch.setattr(lb_module, "lb_popularity_degraded", lambda: True)
        recorded: list = []
        ctx = init_degradation_context()
        original_record = ctx.record

        class _Spy:
            def record(self, result):
                recorded.append(result)
                original_record(result)

            def __getattr__(self, name):
                return getattr(ctx, name)

        monkeypatch.setattr(lb_module, "try_get_degradation_context", lambda: _Spy())

        repo, cache = _make_repo()
        repo._post = AsyncMock(side_effect=AssertionError("must not post"))
        await cache.set(_key("rg-hit"), 42)  # pre-existing hit survives untouched

        counts = await repo.get_release_group_popularity_batch(
            ["rg-hit", "rg-miss1", "rg-miss2"]
        )

        # Gate tripped with misses outstanding: whole call returns {} (current
        # early-return contract), no POST, and no cache writes for misses.
        assert counts == {}
        repo._post.assert_not_awaited()
        assert await cache.get(_key("rg-hit")) == 42
        assert await cache.get(_key("rg-miss1")) is None
        assert await cache.get(_key("rg-miss2")) is None
        assert any("popularity" in r.error_message.lower() for r in recorded)

        clear_degradation_context()

    @pytest.mark.asyncio
    async def test_post_exception_propagates_and_writes_nothing(self):
        repo, cache = _make_repo()

        async def boom(endpoint, payload):
            raise RuntimeError("lb down")

        repo._post = boom

        with pytest.raises(RuntimeError):
            await repo.get_release_group_popularity_batch(["rg-a", "rg-b"])

        assert await cache.get(_key("rg-a")) is None
        assert await cache.get(_key("rg-b")) is None

    @pytest.mark.asyncio
    async def test_non_list_response_writes_nothing(self):
        repo, cache = _make_repo()
        repo._post = AsyncMock(return_value={"unexpected": "shape"})

        counts = await repo.get_release_group_popularity_batch(["rg-a"])

        assert counts == {}
        assert await cache.get(_key("rg-a")) is None

    @pytest.mark.asyncio
    async def test_empty_response_negative_caches_all_misses(self):
        repo, cache = _make_repo()
        repo._post = AsyncMock(return_value=[])

        counts = await repo.get_release_group_popularity_batch(["rg-a"])

        # [] is a well-formed (empty) list -> sentinels written, not an outage.
        assert counts == {}
        assert await cache.get(_key("rg-a")) is False


class TestDedupeCoalesce:
    @pytest.mark.asyncio
    async def test_concurrent_identical_batches_share_one_leader(self):
        repo, cache = _make_repo()
        calls = {"n": 0}

        async def slow_post(endpoint, payload):
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return _post_result([(m, 9) for m in payload["release_group_mbids"]])

        repo._post = slow_post

        results = await asyncio.gather(
            *(
                repo.get_release_group_popularity_batch(["rg-z", "rg-y"])
                for _ in range(3)
            )
        )

        assert all(r == {"rg-y": 9, "rg-z": 9} for r in results)
        assert calls["n"] == 1  # followers awaited the leader's POST


class TestEmptyInput:
    @pytest.mark.asyncio
    async def test_empty_mbids_short_circuit(self):
        repo, _cache = _make_repo()
        repo._post = AsyncMock(side_effect=AssertionError("must not post"))
        assert await repo.get_release_group_popularity_batch([]) == {}
