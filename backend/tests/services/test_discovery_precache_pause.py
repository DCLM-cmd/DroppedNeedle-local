"""Tests for the discovery precache upstream-outage pause/backoff."""

import asyncio
import logging
from contextlib import ExitStack, contextmanager
from time import monotonic
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.artist_discovery_service import ArtistDiscoveryService
import services.artist_discovery_service as _ads_module


@pytest.fixture(autouse=True)
def _reset_precache_flag():
    _ads_module._discovery_precache_running = False
    _ads_module._precache_consecutive_failures = 0
    _ads_module._precache_paused_until = 0.0
    yield
    _ads_module._discovery_precache_running = False
    _ads_module._precache_consecutive_failures = 0
    _ads_module._precache_paused_until = 0.0


def _make_service(
    *, lb_configured: bool = True, lastfm_enabled: bool = False,
    client_factory=None, auth_store=None, workload_gate=None,
):
    lb_repo = MagicMock()
    lb_repo.is_configured.return_value = lb_configured

    lastfm_repo = MagicMock() if lastfm_enabled else None
    prefs = MagicMock()
    prefs.is_lastfm_enabled.return_value = lastfm_enabled
    advanced = MagicMock()
    advanced.artist_discovery_precache_concurrency = 2
    prefs.get_advanced_settings.return_value = advanced

    cache = AsyncMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()

    library_db = AsyncMock()
    library_db.get_all_artist_mbids = AsyncMock(return_value=set())

    svc = ArtistDiscoveryService(
        listenbrainz_repo=lb_repo,
        musicbrainz_repo=MagicMock(),
        library_db=library_db,
        library_repo=MagicMock(),
        memory_cache=cache,
        lastfm_repo=lastfm_repo,
        preferences_service=prefs,
        client_factory=client_factory,
        auth_store=auth_store,
        workload_gate=workload_gate,
    )
    return svc


@contextmanager
def _patch_sources_hanging(svc):
    async def hang(*args, **kwargs):
        await asyncio.sleep(1)
        return MagicMock()  # pragma: no cover

    with ExitStack() as stack:
        sim = stack.enter_context(
            patch.object(svc, "get_similar_artists", new_callable=AsyncMock, side_effect=hang)
        )
        stack.enter_context(
            patch.object(svc, "get_top_songs", new_callable=AsyncMock, side_effect=hang)
        )
        stack.enter_context(
            patch.object(svc, "get_top_albums", new_callable=AsyncMock, side_effect=hang)
        )
        yield sim


@contextmanager
def _patch_sources_working(svc):
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(svc, "get_similar_artists", new_callable=AsyncMock, return_value=MagicMock())
        )
        stack.enter_context(
            patch.object(svc, "get_top_songs", new_callable=AsyncMock, return_value=MagicMock())
        )
        stack.enter_context(
            patch.object(svc, "get_top_albums", new_callable=AsyncMock, return_value=MagicMock())
        )
        yield


@pytest.mark.asyncio
async def test_consecutive_unit_failures_pause_precache(caplog, monkeypatch):
    """5 consecutive unit timeouts trip the pause; the next call does no work."""
    svc = _make_service()
    monkeypatch.setattr(_ads_module, "_DISCOVERY_WORKER_TIMEOUT", 0.05)

    with caplog.at_level(logging.INFO), _patch_sources_hanging(svc):
        for i in range(5):
            result = await svc.precache_artist_discovery([f"mbid-{i}"], delay=0)
            assert result == 0

    assert _ads_module._precache_paused_until > monotonic()
    assert "Discovery precache paused for 1800s after 5 consecutive unit failures" in caplog.text

    # The 6th call returns 0 in milliseconds without invoking any source.
    with _patch_sources_hanging(svc) as sim:
        result = await svc.precache_artist_discovery(["mbid-5"], delay=0)
        assert result == 0
        assert sim.await_count == 0


@pytest.mark.asyncio
async def test_pause_expiry_probe_success_resets(monkeypatch):
    """After the pause expires, a successful probe resets the failure streak."""
    svc = _make_service()
    monkeypatch.setattr(_ads_module, "_DISCOVERY_WORKER_TIMEOUT", 0.05)

    with _patch_sources_hanging(svc):
        for i in range(5):
            await svc.precache_artist_discovery([f"mbid-{i}"], delay=0)
    assert _ads_module._precache_paused_until > monotonic()

    # Pause expires (or is cleared); sources recover.
    _ads_module._precache_paused_until = 0.0
    with _patch_sources_working(svc):
        result = await svc.precache_artist_discovery(["mbid-ok"], delay=0)

    assert result == 1


@pytest.mark.asyncio
async def test_success_resets_failure_streak(monkeypatch):
    """A single success between failures keeps the streak below the threshold."""
    svc = _make_service()
    monkeypatch.setattr(_ads_module, "_DISCOVERY_WORKER_TIMEOUT", 0.05)

    # Two genuine unit failures (hanging sources time out).
    with _patch_sources_hanging(svc):
        await svc.precache_artist_discovery(["mbid-a"], delay=0)
        await svc.precache_artist_discovery(["mbid-b"], delay=0)
    assert _ads_module._precache_consecutive_failures == 2

    # One success resets the streak.
    with _patch_sources_working(svc):
        result = await svc.precache_artist_discovery(["mbid-c"], delay=0)
    assert result == 1
    assert _ads_module._precache_consecutive_failures == 0

    # Two more failures: streak is 2, not 4 - the pause must not trip.
    with _patch_sources_hanging(svc):
        await svc.precache_artist_discovery(["mbid-d"], delay=0)
        await svc.precache_artist_discovery(["mbid-e"], delay=0)
    assert _ads_module._precache_consecutive_failures == 2
    assert _ads_module._precache_paused_until == 0.0


@pytest.mark.asyncio
async def test_chunk_loop_aborts_mid_list_when_pause_trips(monkeypatch):
    """Queued chunk units fast-complete once the pause trips instead of fetching."""
    svc = _make_service()
    monkeypatch.setattr(_ads_module, "_DISCOVERY_WORKER_TIMEOUT", 0.05)

    with _patch_sources_hanging(svc) as sim:
        result = await svc.precache_artist_discovery(
            [f"mbid-{i}" for i in range(30)], delay=0
        )

    assert result == 0
    assert _ads_module._precache_paused_until > monotonic()
    # 5 recorded failures + at most 2 in-flight + slack: units queued behind the
    # pause must not invoke sources.
    assert sim.await_count <= 8
