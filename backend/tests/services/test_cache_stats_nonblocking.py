import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.cache_service import CacheService


def _make_service() -> CacheService:
    cache = MagicMock()
    cache.size.return_value = 10
    cache.estimate_memory_bytes.return_value = 1024
    library = AsyncMock()
    library.get_cache_stats.return_value = {
        "artist_count": 0,
        "album_count": 0,
        "db_size_bytes": 4096,
    }
    disk = MagicMock()
    disk.get_stats.return_value = {
        "total_count": 0,
        "album_count": 0,
        "artist_count": 0,
        "total_size_bytes": 128,
    }
    responses = MagicMock()
    responses.stats.return_value = {
        "response_entries": 3,
        "response_logical_bytes": 2048,
        "database_allocated_bytes": 4096,
        "database_wal_bytes": 512,
        "response_hits": 0,
        "response_evictions": 0,
        "response_speculative_used": 0,
    }
    return CacheService(cache, library, disk, responses)


@pytest.mark.asyncio
async def test_slow_metadata_inventory_keeps_loop_responsive_and_coalesces(
    monkeypatch, tmp_path
):
    service = _make_service()
    monkeypatch.setattr("services.cache_service.get_covers_cache_dir", lambda: tmp_path)
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    entered = threading.Event()
    release = threading.Event()
    inventory = service._disk_cache.get_stats.return_value
    scans = 0
    loop_thread = threading.get_ident()
    worker_threads = []

    def slow_stats():
        nonlocal scans
        scans += 1
        worker_threads.append(threading.get_ident())
        entered.set()
        if not release.wait(timeout=2):
            raise TimeoutError("event loop failed to release inventory")
        return inventory

    service._disk_cache.get_stats.side_effect = slow_stats
    callers = [asyncio.create_task(service.get_stats()) for _ in range(8)]
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        # Cancellation of one request must not cancel the shared inventory.
        callers[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await callers[0]
        await asyncio.sleep(0)
    finally:
        release.set()
    results = await asyncio.gather(*callers[1:])
    assert scans == 1
    assert worker_threads[0] != loop_thread
    assert all(result.disk_cover_count == 1 for result in results)
    assert all(result.disk_cover_size_bytes == 5 for result in results)
    stats = await service.get_stats()
    assert scans == 1
    assert stats.response_entries == 3
    assert stats.response_logical_bytes == 2048
    assert stats.database_allocated_bytes == 4096
    assert stats.database_wal_bytes == 512
    assert stats.total_size_bytes == 1024 + 128 + 5 + 4096 + 512
    assert stats.memory_accounting == "shallow"


@pytest.mark.asyncio
async def test_clear_during_inventory_discards_preclear_snapshot(monkeypatch, tmp_path):
    service = _make_service()
    service._cache.clear = AsyncMock()
    monkeypatch.setattr("services.cache_service.get_covers_cache_dir", lambda: tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original = service._disk_cache.get_stats.return_value
    scans = 0

    def slow_first_stats():
        nonlocal scans
        scans += 1
        if scans == 1:
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("inventory was not released")
        return original

    service._disk_cache.get_stats.side_effect = slow_first_stats
    first = asyncio.create_task(service.get_stats())
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        await service.clear_memory_cache()
        service._cache.size.return_value = 0
        second = asyncio.create_task(service.get_stats())
        await asyncio.sleep(0)
    finally:
        release.set()
    before, after = await asyncio.gather(first, second)
    assert before.memory_entries == after.memory_entries == 0
    assert scans == 2


def test_cover_scan_does_not_follow_symlinks_and_deletes_only_cache(tmp_path):
    covers = tmp_path / "covers"
    covers.mkdir()
    nested = covers / "nested"
    nested.mkdir()
    (nested / "cover.jpg").write_bytes(b"cover")
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.jpg"
    protected.write_bytes(b"keep")
    (covers / "external").symlink_to(outside, target_is_directory=True)

    count, _ = CacheService._scan_covers(covers, delete=True)

    assert count == 2
    assert protected.read_bytes() == b"keep"
    assert not (nested / "cover.jpg").exists()
    assert CacheService._scan_covers(covers) == (0, 0)
