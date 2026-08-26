"""QW1: repo-level cache + dedup on get_release_groups_by_artist.

Covers: cache-hit-no-refetch, concurrent-coalesce-to-one wire call,
failure-not-cached (propagation + degradation record), empty negative cache,
and priority passthrough to mb_api_get.
"""

import asyncio
import inspect
from types import SimpleNamespace

import pytest

import repositories.musicbrainz_artist as artist_module
from core.exceptions import ExternalServiceError
from infrastructure.cache.memory_cache import InMemoryCache
from infrastructure.degradation import (
    clear_degradation_context,
    init_degradation_context,
)
from infrastructure.queue.priority_queue import RequestPriority
from repositories.musicbrainz_artist import MusicBrainzArtistMixin

_ARTIST = "f4a31f0a-51dd-4fa7-986d-3095c40c5ed9"
_RGS = [
    {"id": "bbbbbbbb-0000-0000-0000-000000000001", "title": "RG One"},
    {"id": "bbbbbbbb-0000-0000-0000-000000000002", "title": "RG Two"},
]


class _Repo(MusicBrainzArtistMixin):
    def __init__(self) -> None:
        self._cache = InMemoryCache(max_entries=100)
        self._preferences_service = SimpleNamespace(
            get_advanced_settings=lambda: SimpleNamespace(cache_ttl_search=3600)
        )


def _payload(release_groups):
    return SimpleNamespace(
        release_groups=release_groups, release_group_count=len(release_groups)
    )


@pytest.fixture
def fresh_deduplicator(monkeypatch):
    """Isolate the module-singleton deduplicator per test."""
    artist_module.mb_deduplicator.clear()
    yield artist_module.mb_deduplicator
    artist_module.mb_deduplicator.clear()


class TestCacheHitNoRefetch:
    @pytest.mark.asyncio
    async def test_second_call_served_from_cache(self, monkeypatch, fresh_deduplicator):
        repo = _Repo()
        calls = {"n": 0}

        async def fake_get(*args, **kwargs):
            calls["n"] += 1
            return _payload(_RGS)

        monkeypatch.setattr(artist_module, "mb_api_get", fake_get)

        first = await repo.get_release_groups_by_artist(_ARTIST, limit=10)
        second = await repo.get_release_groups_by_artist(_ARTIST, limit=10)

        assert first == _RGS and second == _RGS
        assert calls["n"] == 1  # one wire call total

    @pytest.mark.asyncio
    async def test_different_limits_use_distinct_keys(
        self, monkeypatch, fresh_deduplicator
    ):
        repo = _Repo()
        seen_params: list[dict] = []

        async def fake_get(path, params=None, **kwargs):
            seen_params.append(params or {})
            return _payload(_RGS)

        monkeypatch.setattr(artist_module, "mb_api_get", fake_get)

        await repo.get_release_groups_by_artist(_ARTIST, limit=15)
        await repo.get_release_groups_by_artist(_ARTIST, limit=100)

        assert len(seen_params) == 2  # distinct limit -> distinct key -> refetch


class TestConcurrentCoalesce:
    @pytest.mark.asyncio
    async def test_concurrent_cold_callers_share_one_wire_call(
        self, monkeypatch, fresh_deduplicator
    ):
        repo = _Repo()
        calls = {"n": 0}

        async def slow_get(*args, **kwargs):
            # Long enough that both tasks enter dedupe() before the leader lands.
            await asyncio.sleep(0.05)
            calls["n"] += 1
            return _payload(_RGS)

        monkeypatch.setattr(artist_module, "mb_api_get", slow_get)

        results = await asyncio.gather(
            repo.get_release_groups_by_artist(_ARTIST, limit=10),
            repo.get_release_groups_by_artist(_ARTIST, limit=10),
        )

        assert results[0] == _RGS and results[1] == _RGS
        assert calls["n"] == 1  # exactly one wire call for N cold viewers


class TestFailureNotCached:
    @pytest.mark.asyncio
    async def test_failure_propagates_records_and_never_caches(
        self, monkeypatch, fresh_deduplicator
    ):
        recorded: list = []
        ctx = init_degradation_context()
        original_record = ctx.record

        class _Spy:
            def record(self, result):
                recorded.append(result)
                original_record(result)

            def __getattr__(self, name):
                return getattr(ctx, name)

        monkeypatch.setattr(
            artist_module, "try_get_degradation_context", lambda: _Spy()
        )

        repo = _Repo()

        async def failing_get(*args, **kwargs):
            raise ExternalServiceError("MusicBrainz API error (503)")

        monkeypatch.setattr(artist_module, "mb_api_get", failing_get)

        with pytest.raises(ExternalServiceError):
            await repo.get_release_groups_by_artist(_ARTIST, limit=10)

        assert any("release groups failed" in r.error_message for r in recorded)
        # Nothing cached under the browse key: a retry goes back to the wire.
        key = artist_module.mb_artist_rgs_browse_key(_ARTIST, 10)
        assert await repo._cache.get(key) is None

        clear_degradation_context()


class TestEmptyNegativeCache:
    @pytest.mark.asyncio
    async def test_genuinely_empty_result_negative_cached(
        self, monkeypatch, fresh_deduplicator
    ):
        repo = _Repo()
        calls = {"n": 0}

        async def empty_get(*args, **kwargs):
            calls["n"] += 1
            return _payload([])

        monkeypatch.setattr(artist_module, "mb_api_get", empty_get)

        first = await repo.get_release_groups_by_artist(_ARTIST, limit=10)
        second = await repo.get_release_groups_by_artist(_ARTIST, limit=10)

        assert first == [] and second == []
        assert calls["n"] == 1  # negative entry prevents refetch within TTL


class TestPriorityPassthrough:
    @pytest.mark.asyncio
    async def test_priority_reaches_mb_api_get(self, monkeypatch, fresh_deduplicator):
        repo = _Repo()
        captured: dict = {}

        async def capture_get(path, params=None, priority=None, **kwargs):
            captured["priority"] = priority
            return _payload(_RGS)

        monkeypatch.setattr(artist_module, "mb_api_get", capture_get)

        await repo.get_release_groups_by_artist(
            _ARTIST, limit=10, priority=RequestPriority.USER_INITIATED
        )
        assert captured["priority"] == RequestPriority.USER_INITIATED

    def test_or_raise_default_lane_unchanged_for_poller(self):
        # The follow poller relies on the defaulted kwarg keeping its lane.
        sig = inspect.signature(
            MusicBrainzArtistMixin.get_artist_release_groups_or_raise
        )
        assert sig.parameters["priority"].default == RequestPriority.BACKGROUND_SYNC
