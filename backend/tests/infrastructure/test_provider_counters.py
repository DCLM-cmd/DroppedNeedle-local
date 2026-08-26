"""QW9 Part 3: provider-call counter classification, lane labels, and
snapshot row math."""

import pytest

from infrastructure.cache.cache_metrics import WindowedCounterMap
from infrastructure.observability import provider_counters
from infrastructure.queue.priority_queue import RequestPriority


class TestClassifyOutcome:
    @pytest.mark.asyncio
    async def test_success_statuses(self):
        assert provider_counters.classify_outcome(200) == "ok"
        assert provider_counters.classify_outcome(204) == "ok"

    @pytest.mark.asyncio
    async def test_404_is_empty(self):
        assert provider_counters.classify_outcome(404) == "empty_404"

    @pytest.mark.asyncio
    async def test_other_statuses_and_transport_failures_are_http_error(self):
        assert provider_counters.classify_outcome(429) == "http_error"
        assert provider_counters.classify_outcome(500) == "http_error"
        assert provider_counters.classify_outcome(503) == "http_error"
        assert provider_counters.classify_outcome(None) == "http_error"


class TestLaneLabel:
    @pytest.mark.asyncio
    async def test_request_priority_names_lowercase(self):
        assert (
            provider_counters.lane_label(RequestPriority.USER_INITIATED)
            == "user_initiated"
        )
        assert (
            provider_counters.lane_label(RequestPriority.IMAGE_FETCH) == "image_fetch"
        )

    @pytest.mark.asyncio
    async def test_none_means_unlaned(self):
        assert provider_counters.lane_label(None) == "unlaned"

    @pytest.mark.asyncio
    async def test_plain_string_passes_through(self):
        assert provider_counters.lane_label("background_sync") == "background_sync"


class TestRecordAndSnapshot:
    @pytest.fixture
    def fresh_counters(self, monkeypatch):
        counters = WindowedCounterMap()
        monkeypatch.setattr(provider_counters, "_counters", counters)
        return counters

    @pytest.mark.asyncio
    async def test_rows_carry_required_fields_and_math(
        self, fresh_counters: WindowedCounterMap
    ):
        for _ in range(5):
            provider_counters.record_provider_call(
                "musicbrainz", RequestPriority.USER_INITIATED, 200
            )
        provider_counters.record_provider_call(
            "musicbrainz", RequestPriority.USER_INITIATED, 404
        )
        provider_counters.record_provider_call("audiodb", None, None)

        rows = provider_counters.snapshot_provider_rows()

        by_key = {(r["provider"], r["priority"], r["outcome"]): r for r in rows}
        mb_ok = by_key[("musicbrainz", "user_initiated", "ok")]
        assert mb_ok["count_total"] == 5
        # default window is 3600 s -> per-minute rate = window_count / 60
        assert mb_ok["rate_per_min_window"] == round(5 / 60, 2)
        assert (
            by_key[("musicbrainz", "user_initiated", "empty_404")]["count_total"] == 1
        )
        unlaned_err = by_key[("audiodb", "unlaned", "http_error")]
        assert unlaned_err["count_total"] == 1

    @pytest.mark.asyncio
    async def test_rows_sorted_deterministically(self, fresh_counters):
        provider_counters.record_provider_call("lastfm", None, 200)
        provider_counters.record_provider_call("coverart", "image_fetch", 200)
        provider_counters.record_provider_call("coverart", "user_initiated", 200)

        rows = provider_counters.snapshot_provider_rows()
        keys = [(r["provider"], r["priority"], r["outcome"]) for r in rows]
        assert keys == sorted(keys)

    @pytest.mark.asyncio
    async def test_empty_snapshot_yields_no_rows(self, fresh_counters):
        assert provider_counters.snapshot_provider_rows() == []

    @pytest.mark.asyncio
    async def test_custom_window_scales_rate(self, fresh_counters):
        provider_counters.record_provider_call("discogs", "user_initiated", 200)
        rows = provider_counters.snapshot_provider_rows(window_seconds=600)
        assert rows[0]["rate_per_min_window"] == round(1 / 10, 2)

    @pytest.mark.asyncio
    async def test_counters_since_and_providers_constant(self):
        assert isinstance(provider_counters.counters_since(), int)
        assert set(provider_counters.PROVIDER_NAMES) >= {
            "musicbrainz",
            "listenbrainz",
            "lastfm",
            "coverart",
            "audiodb",
            "discogs",
        }
