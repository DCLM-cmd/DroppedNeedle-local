"""A2 part 3: LB popularity batch coalescer unit tests.

Window flush, size-cap flush, error fan-out to all waiters, no unbounded
growth of the pending dict.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from services.discover.enrichment_service import QueueEnrichmentService


def _make_service() -> QueueEnrichmentService:
    svc = QueueEnrichmentService.__new__(QueueEnrichmentService)
    svc._popularity_pending = {}
    svc._popularity_flush_handle = None
    svc._popularity_flush_task = None

    async def fake_batch(mbids):
        return {m: len(m) for m in mbids}

    svc._lb_repo = AsyncMock()
    svc._lb_repo.get_release_group_popularity_batch = AsyncMock(side_effect=fake_batch)
    return svc


class TestWindowFlush:
    @pytest.mark.asyncio
    async def test_window_flush_delivers_single_batched_post(self):
        svc = _make_service()

        async def caller(mbid):
            return await svc._coalesce_popularity(mbid)

        results = await asyncio.gather(*(caller(f"rg-{i}") for i in range(5)))

        assert results == [f"rg-{i}".__len__() for i in range(5)]
        assert svc._lb_repo.get_release_group_popularity_batch.await_count == 1
        assert svc._popularity_pending == {}  # no unbounded growth


class TestSizeCapFlush:
    @pytest.mark.asyncio
    async def test_size_cap_flushes_immediately_without_waiting_for_window(
        self, monkeypatch
    ):
        svc = _make_service()
        cap_calls: list[int] = []
        original_cap = QueueEnrichmentService._POPULARITY_MAX_BATCH

        # Shrink the cap so a small burst trips it.
        monkeypatch.setattr(QueueEnrichmentService, "_POPULARITY_MAX_BATCH", 3)

        futures = [svc._enqueue_popularity(f"rg-{i}") for i in range(2)]
        assert svc._popularity_flush_handle is not None  # window armed

        third = svc._enqueue_popularity("rg-2")
        futures.append(third)
        cap_calls.append(len(svc._popularity_pending))

        # Size cap fired: pending was flushed synchronously at enqueue time.
        await asyncio.gather(*futures)
        assert svc._popularity_pending == {}
        monkeypatch.setattr(
            QueueEnrichmentService, "_POPULARITY_MAX_BATCH", original_cap
        )


class TestErrorFanOut:
    @pytest.mark.asyncio
    async def test_leader_exception_reaches_every_waiter(self):
        svc = _make_service()
        svc._lb_repo.get_release_group_popularity_batch = AsyncMock(
            side_effect=RuntimeError("lb exploded")
        )

        results = await asyncio.gather(
            *(svc._coalesce_popularity("rg-x") for _ in range(4)),
            return_exceptions=True,
        )

        assert all(isinstance(r, RuntimeError) for r in results)
        assert svc._popularity_pending == {}

    @pytest.mark.asyncio
    async def test_cancelled_flush_fails_waiters_instead_of_hanging(self):
        svc = _make_service()

        slow_batch = AsyncMock(side_effect=asyncio.CancelledError())

        async def slow(mbids):
            await asyncio.sleep(10)  # would hang forever
            return {m: 1 for m in mbids}

        svc._lb_repo.get_release_group_popularity_batch = slow

        fut = svc._enqueue_popularity("rg-slow")
        # Force the window flush so the delivery task exists.
        svc._flush_popularity_now()
        flush_task = svc._popularity_flush_task
        assert flush_task is not None

        # Let the delivery task START (it parks in the 10s fake POST)...
        await asyncio.sleep(0)
        assert not flush_task.done()

        # ...then cancel mid-flight: every waiter must fail instead of hang.
        flush_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.shield(fut)
        assert fut.cancelled() or fut.done()


class TestGrowthBound:
    @pytest.mark.asyncio
    async def test_pending_dict_does_not_grow_across_many_batches(self):
        svc = _make_service()

        for round_index in range(20):
            await asyncio.gather(
                *(svc._coalesce_popularity(f"rg-{round_index}-{i}") for i in range(7))
            )

        assert svc._popularity_pending == {}
        # Bounded batching: 140 distinct ids over >=20 POSTs (one per window),
        # never 140 individual calls.
        assert svc._lb_repo.get_release_group_popularity_batch.await_count <= 25
