"""QW9 Part 2: WindowedCounterMap windowing, prefix extraction, and
InstrumentedCache conformance/recording semantics."""

import pytest

from infrastructure.cache.cache_metrics import (
    InstrumentedCache,
    WindowedCounterMap,
    build_prefix_registry,
    prefix_label,
)
from infrastructure.cache.memory_cache import CacheInterface, InMemoryCache


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _map(clock: FakeClock, buckets: int = 6, bucket_seconds: float = 10.0):
    return WindowedCounterMap(
        buckets=buckets, bucket_seconds=bucket_seconds, clock=clock
    )


class TestWindowedCounterMap:
    @pytest.mark.asyncio
    async def test_increment_visible_in_current_bucket(self):
        clock = FakeClock()
        counters = _map(clock)

        counters.increment(("p", "hit"))
        counters.increment(("p", "hit"), 2)

        assert counters.snapshot() == {("p", "hit"): 3}
        assert counters.totals() == {("p", "hit"): 3}

    @pytest.mark.asyncio
    async def test_window_sums_across_buckets(self):
        clock = FakeClock()
        counters = _map(clock)

        counters.increment(("p", "hit"))
        clock.advance(25)
        counters.increment(("p", "hit"))

        assert counters.snapshot() == {("p", "hit"): 2}
        # 25 s ago is outside a 15 s window but inside the full ring.
        assert counters.snapshot(window_seconds=15) == {("p", "hit"): 1}

    @pytest.mark.asyncio
    async def test_expired_buckets_leave_window_but_totals_persist(self):
        clock = FakeClock()
        counters = _map(clock)  # ring covers 60 s

        counters.increment(("p", "set"), 4)
        clock.advance(120)

        assert counters.snapshot() == {("p", "set"): 0}
        assert counters.totals() == {("p", "set"): 4}

    @pytest.mark.asyncio
    async def test_ring_reuse_does_not_resurrect_stale_counts(self):
        clock = FakeClock()
        counters = _map(clock, buckets=3, bucket_seconds=10.0)

        counters.increment(("k", "hit"), 7)  # epoch 0 -> slot 0
        clock.advance(30)  # epoch 3 reuses slot 0
        counters.increment(("k", "hit"), 2)

        assert counters.snapshot() == {("k", "hit"): 2}
        assert counters.snapshot(window_seconds=31) == {("k", "hit"): 2}

    @pytest.mark.asyncio
    async def test_window_seconds_property(self):
        assert (
            _map(FakeClock(), buckets=360, bucket_seconds=10.0).window_seconds == 3600
        )


class TestPrefixExtraction:
    @pytest.mark.asyncio
    async def test_longest_match_wins_over_shorter_overlap(self):
        # MB_RECORDING_SEARCH_PREFIX ("mb:recording:search:") must beat
        # MB_RECORDING_PREFIX ("mb:recording:").
        assert prefix_label("mb:recording:search:query:10:0") == "mb:recording:search:"
        # MB_DUPLICATE_SEARCH_PREFIX must beat MB_RELEASE_DETAIL_PREFIX's family.
        assert (
            prefix_label("mb:release:duplicate-search:abc:5")
            == "mb:release:duplicate-search:"
        )

    @pytest.mark.asyncio
    async def test_registered_prefix_matches_verbatim_value(self):
        from infrastructure.cache import cache_keys

        assert (
            prefix_label(f"{cache_keys.MB_ARTIST_SEARCH_PREFIX}alice")
            == cache_keys.MB_ARTIST_SEARCH_PREFIX
        )
        assert (
            prefix_label(f"{cache_keys.LB_PREFIX}management:x") == cache_keys.LB_PREFIX
        )
        assert (
            prefix_label(f"{cache_keys.AUDIODB_PREFIX}artist:123")
            == cache_keys.AUDIODB_PREFIX
        )

    @pytest.mark.asyncio
    async def test_unmatched_key_falls_back_to_first_colon_segment(self):
        assert prefix_label("customthing:rest-of-key") == "customthing"

    @pytest.mark.asyncio
    async def test_unmatched_key_without_colon_is_other(self):
        assert prefix_label("no-colon-here-but-also-not-registered") == "other"

    @pytest.mark.asyncio
    async def test_registry_covers_every_cache_keys_prefix_constant(self):
        from infrastructure.cache import cache_keys

        registry_values = {prefix for prefix, _name in build_prefix_registry()}
        module_values = {
            value
            for name, value in vars(cache_keys).items()
            if name.endswith("_PREFIX") and isinstance(value, str) and value
        }
        assert registry_values == module_values

    @pytest.mark.asyncio
    async def test_registry_sorted_longest_first(self):
        prefixes = [prefix for prefix, _name in build_prefix_registry()]
        assert prefixes == sorted(prefixes, key=len, reverse=True)


class TestInstrumentedCache:
    def _make(self) -> tuple[InstrumentedCache, InMemoryCache]:
        inner = InMemoryCache(max_entries=100)
        return InstrumentedCache(inner), inner

    @pytest.mark.asyncio
    async def test_preserves_cache_interface_conformance(self):
        instrumented, _inner = self._make()
        assert isinstance(instrumented, CacheInterface)

    @pytest.mark.asyncio
    async def test_get_records_hit_and_miss_by_return_value(self):
        instrumented, _inner = self._make()

        await instrumented.get("mb:artist:detail:a")  # miss
        await instrumented.set("mb:artist:detail:a", {"x": 1})
        await instrumented.get("mb:artist:detail:a")  # hit

        totals = instrumented.op_totals()
        assert totals[("mb:artist:detail:", "miss")] == 1
        assert totals[("mb:artist:detail:", "hit")] == 1
        assert totals[("mb:artist:detail:", "set")] == 1

    @pytest.mark.asyncio
    async def test_expired_entry_counts_as_miss(self):
        instrumented, _inner = self._make()

        await instrumented.set("library:status", "v", ttl_seconds=0)
        value = await instrumented.get("library:status")

        assert value is None
        totals = instrumented.op_totals()
        assert totals[("library:", "miss")] == 1
        assert ("library:", "hit") not in totals

    @pytest.mark.asyncio
    async def test_delete_recorded_and_delegates(self):
        instrumented, inner = self._make()

        await instrumented.set("lb_x", 1)
        await instrumented.delete("lb_x")

        assert await inner.get("lb_x") is None
        assert instrumented.op_totals()[("lb_", "delete")] == 1

    @pytest.mark.asyncio
    async def test_size_memory_bytes_and_get_stats_pass_through(self):
        instrumented, inner = self._make()

        await instrumented.set("home_response:k", {"a": 1})

        assert instrumented.size() == inner.size()
        assert instrumented.estimate_memory_bytes() == inner.estimate_memory_bytes()
        assert instrumented.get_stats() == inner.get_stats()

    @pytest.mark.asyncio
    async def test_clear_and_clear_prefix_delegate_without_resetting_counters(self):
        instrumented, inner = self._make()

        await instrumented.set("caa:management:x", 1)
        await instrumented.get("caa:management:x")
        before = instrumented.op_totals()

        await instrumented.clear()
        assert inner.size() == 0
        # Deliberate: clears never reset counters - ratios measure the window,
        # not contents.
        assert instrumented.op_totals() == before

        removed = await instrumented.clear_prefix("caa:")
        await instrumented.set("caa:management:y", 2)
        removed_again = await instrumented.cleanup_expired()
        assert isinstance(removed, int) and isinstance(removed_again, int)

    @pytest.mark.asyncio
    async def test_per_prefix_rows_shape_and_math(self):
        instrumented, _inner = self._make()

        await instrumented.get("audiodb_a")  # miss
        await instrumented.get("audiodb_b")  # miss
        await instrumented.set("audiodb_c", 1)
        await instrumented.get("audiodb_c")  # hit

        rows = instrumented.per_prefix_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row["prefix"] == "audiodb_"
        assert row["hits"] == 1
        assert row["misses"] == 2
        assert row["sets"] == 1
        assert row["hit_rate_percent"] == round(1 / 3 * 100, 2)
        assert row["window_seconds"] == 3600

    @pytest.mark.asyncio
    async def test_per_prefix_rows_empty_without_traffic(self):
        instrumented, _inner = self._make()
        assert instrumented.per_prefix_rows() == []

    @pytest.mark.asyncio
    async def test_observability_reuses_inner_global_computation(self):
        instrumented, inner = self._make()

        await instrumented.set("genre_artist:g", 1)
        await instrumented.get("genre_artist:g")

        obs = instrumented.observability()
        stats = inner.get_stats()
        assert obs["memory_hits"] == stats["hits"] == 1
        assert obs["memory_misses"] == stats["misses"]
        assert obs["memory_hit_rate_percent"] == stats["hit_rate_percent"]
        assert obs["counters_since"] == instrumented.counters_since
        assert isinstance(obs["counters_since"], int)
        assert obs["per_prefix"][0]["prefix"] == "genre_artist:"

    @pytest.mark.asyncio
    async def test_peek_reads_expired_entry_without_evicting(self):
        inner = InMemoryCache(max_entries=10)
        instrumented = InstrumentedCache(inner)

        await inner.set("lb_sitewide_artists:week:25:0", ["stale"], ttl_seconds=0)

        # peek: expired-tolerant read, no eviction, repeatable
        assert await inner.peek("lb_sitewide_artists:week:25:0") == ["stale"]
        assert await instrumented.peek("lb_sitewide_artists:week:25:0") == ["stale"]

        # ordinary get on the same expired key: miss AND evicts the entry
        assert await instrumented.get("lb_sitewide_artists:week:25:0") is None
        assert await inner.peek("lb_sitewide_artists:week:25:0") is None

    @pytest.mark.asyncio
    async def test_peek_returns_none_for_absent_key(self):
        instrumented, _inner = self._make()
        assert await instrumented.peek("nope") is None

    @pytest.mark.asyncio
    async def test_peek_does_not_record_hit_or_miss(self):
        instrumented, _inner = self._make()

        await instrumented.set("mb:rg:detail:x", 1)
        await instrumented.peek("mb:rg:detail:x")
        await instrumented.peek("mb:rg:detail:missing")

        totals = instrumented.op_totals()
        assert ("mb:rg:detail:", "hit") not in totals
        assert ("mb:rg:detail:", "miss") not in totals
