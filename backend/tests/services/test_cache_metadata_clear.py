"""QW9 Part 4: CacheService.clear_metadata_cache clears memory + disk metadata
only, and QW9 Part 2: get_stats surfaces ratio fields when the cache is
instrumented."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from infrastructure.cache.cache_metrics import InstrumentedCache
from infrastructure.cache.memory_cache import InMemoryCache
from services.cache_service import CacheService


def _make_service(cache) -> CacheService:
    lib_cache = AsyncMock()
    lib_cache.get_cache_stats = AsyncMock(
        return_value={
            "db_size_bytes": 0,
            "artist_count": 0,
            "album_count": 0,
            "last_sync": None,
        }
    )
    disk_cache = AsyncMock()
    disk_cache.get_stats = MagicMock(
        return_value={
            "total_count": 7,
            "album_count": 3,
            "artist_count": 4,
            "audiodb_artist_count": 0,
            "audiodb_album_count": 0,
        }
    )
    return CacheService(cache=cache, library_db=lib_cache, disk_cache=disk_cache)


class TestClearMetadataCache:
    @pytest.mark.asyncio
    async def test_clears_memory_and_disk_metadata_only(self):
        # size()/estimate_memory_bytes() are sync on CacheInterface; only the
        # mutating calls are async, so the cache double is a MagicMock with an
        # AsyncMock clear.
        cache = MagicMock()
        cache.size.return_value = 5
        cache.clear = AsyncMock()
        service = _make_service(cache)

        result = await service.clear_metadata_cache()

        assert result.success is True
        assert result.cleared_memory_entries == 5
        assert result.cleared_disk_files == 7
        # The non-destructive receipt: covers untouched by construction.
        assert result.cover_files_cleared == 0
        assert "covers preserved" in result.message
        cache.clear.assert_awaited_once()
        service._disk_cache.clear_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failure_reports_error_receipt(self):
        cache = MagicMock()
        cache.size.side_effect = RuntimeError("boom")
        cache.clear = AsyncMock()
        service = _make_service(cache)

        result = await service.clear_metadata_cache()

        cache.clear.assert_not_awaited()
        service._disk_cache.clear_all.assert_not_awaited()
        assert "boom" in result.message
        assert result.cover_files_cleared == 0


class TestGetStatsObservabilityFields:
    @pytest.mark.asyncio
    async def test_ratio_fields_populated_from_instrumented_cache(self, monkeypatch):
        instrumented = InstrumentedCache(InMemoryCache(max_entries=10))
        service = _make_service(instrumented)

        await instrumented.set("mb:rg:detail:x", {"a": 1})
        await instrumented.get("mb:rg:detail:x")
        await instrumented.get("mb:rg:detail:missing")

        monkeypatch.setattr(
            "services.cache_service.get_covers_cache_dir",
            lambda: MagicMock(exists=lambda: False),
        )
        stats = await service.get_stats()

        assert stats.memory_hits == 1
        assert stats.memory_misses == 1
        assert stats.memory_hit_rate_percent == 50.0
        prefixes = [row.prefix for row in stats.per_prefix]
        assert prefixes == ["mb:rg:detail:"]
        row = stats.per_prefix[0]
        assert (row.hits, row.misses, row.sets) == (1, 1, 1)
        assert isinstance(stats.counters_since, int)

    @pytest.mark.asyncio
    async def test_ratio_fields_defaulted_without_instrumentation(self, monkeypatch):
        cache = MagicMock()
        cache.size.return_value = 0
        cache.estimate_memory_bytes.return_value = 0
        service = _make_service(cache)

        monkeypatch.setattr(
            "services.cache_service.get_covers_cache_dir",
            lambda: MagicMock(exists=lambda: False),
        )
        stats = await service.get_stats()
