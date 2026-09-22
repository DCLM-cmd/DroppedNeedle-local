import asyncio

import pytest

from core.tasks import cleanup_disk_cache_periodically
from infrastructure.cache.disk_cache import DiskMetadataCache


@pytest.mark.asyncio
async def test_cleanup_retries_after_cover_failure_without_skipping_cadence(tmp_path, monkeypatch):
    clock = [2_000_000_000.0]
    cache = DiskMetadataCache(base_path=tmp_path, clock=lambda: clock[0])
    await cache.set_album("first", {"title": "First"}, ttl_seconds=60)
    intervals = []

    async def advance(interval):
        intervals.append(interval)
        if len(intervals) == 3:
            raise asyncio.CancelledError
        clock[0] += 120

    class CoverCache:
        failed = False

        async def enforce_size_limit(self, *, force):
            if not self.failed:
                self.failed = True
                await cache.set_album("after-error", {"title": "After error"}, ttl_seconds=60)
                raise OSError("fixture cover cleanup failure")

        def cleanup_expired(self):
            return 0

    monkeypatch.setattr(asyncio, "sleep", advance)
    await cleanup_disk_cache_periodically(cache, interval=120, cover_disk_cache=CoverCache())

    assert intervals == [120, 120, 120]
    assert list((tmp_path / "recent" / "albums").glob("*.json")) == []
