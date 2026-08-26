"""Sweep-coherent home freshness bookkeeping (sidecar).

The freshness timestamp lives in a cache entry under HOME_RESPONSE_PREFIX so it
is swept together with the payload it describes. These tests pin three
behaviors: sweeps trigger immediate rebuilds, failed-build
backoff survives via the sidecar's ok=False marker, and a missing/swept sidecar
never masks a missing payload."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.v1.schemas.settings import (
    LastFmConnectionSettings,
    ListenBrainzConnectionSettings,
    PrimaryMusicSourceSettings,
)
from services.home_service import HomeService


def _make_prefs(
    lb_enabled: bool = True,
    lfm_enabled: bool = True,
    primary_source: str = "listenbrainz",
) -> MagicMock:
    prefs = MagicMock()
    prefs.get_listenbrainz_connection.return_value = ListenBrainzConnectionSettings(
        user_token="tok", username="lbuser", enabled=lb_enabled
    )
    prefs.get_lastfm_connection.return_value = LastFmConnectionSettings(
        api_key="key",
        shared_secret="secret",
        session_key="sk",
        username="lfmuser",
        enabled=lfm_enabled,
    )
    prefs.is_lastfm_enabled.return_value = lfm_enabled
    prefs.get_primary_music_source.return_value = PrimaryMusicSourceSettings(
        source=primary_source
    )
    jf = MagicMock()
    jf.enabled = False
    prefs.get_jellyfin_connection.return_value = jf
    dc = MagicMock()
    dc.enabled = False
    prefs.get_download_client_settings.return_value = dc
    yt = MagicMock()
    yt.enabled = False
    prefs.get_youtube_connection.return_value = yt
    lf = MagicMock()
    lf.enabled = False
    prefs.get_local_files_connection.return_value = lf
    return prefs


def _make_service() -> tuple[HomeService, AsyncMock, dict]:
    lb_repo = AsyncMock()
    lb_repo.get_sitewide_top_artists = AsyncMock(return_value=[])
    lb_repo.get_sitewide_top_release_groups = AsyncMock(return_value=[])
    lb_repo.get_user_listens = AsyncMock(return_value=[])
    lb_repo.get_user_loved_recordings = AsyncMock(return_value=[])
    lb_repo.get_user_genre_activity = AsyncMock(return_value=None)
    lb_repo.get_recommendation_playlists = AsyncMock(return_value=[])
    lb_repo.get_playlist_tracks = AsyncMock(return_value=None)
    lb_repo.get_recording_release_groups_batch = AsyncMock(return_value={})

    lfm_repo = AsyncMock()
    jf_repo = AsyncMock()
    library_repo = AsyncMock()
    library_repo.has_any_files = AsyncMock(return_value=False)
    mb_repo = AsyncMock()

    service = HomeService(
        listenbrainz_repo=lb_repo,
        jellyfin_repo=jf_repo,
        library_repo=library_repo,
        musicbrainz_repo=mb_repo,
        preferences_service=_make_prefs(),
    )

    store: dict = {}
    cache = MagicMock()
    cache.get = AsyncMock(side_effect=lambda k: store.get(k))
    cache.set = AsyncMock(side_effect=lambda k, v, ttl=None: store.__setitem__(k, v))
    service._memory_cache = cache
    return service, lb_repo, store


def _sweep_home_prefix(store: dict) -> None:
    """Emulate the production sweep: clear every home_response:* entry."""
    for key in [k for k in store if k.startswith("home_response:")]:
        del store[key]


@pytest.mark.asyncio
async def test_sweep_coherence_next_request_rebuilds_immediately():
    service, lb_repo, store = _make_service()
    triggered: list[str] = []
    service._trigger_warm = lambda user_id: triggered.append(user_id)  # type: ignore[method-assign]

    await service.warm_cache("u1")
    assert any(k.startswith("home_response:") and ":built:" not in k for k in store)

    _sweep_home_prefix(store)
    assert store == {}  # sidecar shares fate with the payload

    resp = await service.get_home_data("u1")

    # no dead-shell window: the very next request re-arms the full build
    assert triggered == ["u1"]
    assert resp.refreshing is True


@pytest.mark.asyncio
async def test_failed_build_backoff_preserved_via_sidecar():
    service, lb_repo, store = _make_service()
    triggered: list[str] = []
    service._trigger_warm = lambda user_id: triggered.append(user_id)  # type: ignore[method-assign]
    service._build_full = AsyncMock(side_effect=RuntimeError("boom"))

    await service.warm_cache("u1")

    music = await service._resolve_user_music("u1", None)
    key = service._get_home_cache_key("u1", music.lb_enabled, music.lfm_enabled)
    sidecar = store[service._home_built_sidecar_key(key)]
    assert sidecar["ok"] is False
    assert all(k != key for k in store)  # no payload was cached

    resp = await service.get_home_data("u1")

    # recent FAILED attempt suppresses the doomed-rebuild loop
    assert triggered == []
    assert resp.refreshing is False


@pytest.mark.asyncio
async def test_expired_failure_sidecar_releases_the_rebuild():
    service, lb_repo, store = _make_service()
    triggered: list[str] = []
    service._trigger_warm = lambda user_id: triggered.append(user_id)  # type: ignore[method-assign]
    music = await service._resolve_user_music("u1", None)
    key = service._get_home_cache_key("u1", music.lb_enabled, music.lfm_enabled)

    store[service._home_built_sidecar_key(key)] = {
        "at": time.time() - 301,  # outside the 300 s backoff window
        "ok": False,
    }

    await service.get_home_data("u1")
    assert triggered == ["u1"]


@pytest.mark.asyncio
async def test_success_sidecar_never_blocks_miss_path_rebuild():
    """A swept payload must not be masked even by a fresh ok=True sidecar."""
    service, lb_repo, store = _make_service()
    triggered: list[str] = []
    service._trigger_warm = lambda user_id: triggered.append(user_id)  # type: ignore[method-assign]
    music = await service._resolve_user_music("u1", None)
    key = service._get_home_cache_key("u1", music.lb_enabled, music.lfm_enabled)

    # impossible under shared-prefix sweeping, but defensive: bookkeeping
    # present, payload gone -> rebuild fires
    store[service._home_built_sidecar_key(key)] = {
        "at": time.time(),
        "ok": True,
    }

    await service.get_home_data("u1")
    assert triggered == ["u1"]


@pytest.mark.asyncio
async def test_hit_path_falls_back_when_sidecar_missing():
    """Payload without sidecar (defensive path): treated as infinitely stale."""
    service, lb_repo, store = _make_service()
    triggered: list[str] = []
    service._trigger_warm = lambda user_id: triggered.append(user_id)  # type: ignore[method-assign]

    from api.v1.schemas.home import HomeResponse

    music = await service._resolve_user_music("u1", None)
    key = service._get_home_cache_key("u1", music.lb_enabled, music.lfm_enabled)
    store[key] = HomeResponse()  # payload present, no sidecar

    resp = await service.get_home_data("u1")

    assert triggered == ["u1"]
    assert resp.refreshing is True


@pytest.mark.asyncio
async def test_warm_failure_then_success_updates_sidecar_ok_flag():
    service, lb_repo, store = _make_service()
    music = await service._resolve_user_music("u1", None)
    key = service._get_home_cache_key("u1", music.lb_enabled, music.lfm_enabled)
    built_key = service._home_built_sidecar_key(key)

    service._build_full = AsyncMock(side_effect=RuntimeError("cold failure"))
    await service.warm_cache("u1")
    assert store[built_key]["ok"] is False

    fast = await service._build_fast("u1", music, refreshing=False)
    service._build_full = AsyncMock(return_value=fast)  # type: ignore[method-assign]
    await service.warm_cache("u1")
    assert store[built_key]["ok"] is True


@pytest.mark.asyncio
async def test_concurrent_cold_polls_coalesce_into_one_leader():
    """N simultaneous cold polls share one dedupe leader -> one rebuild armed."""
    service, lb_repo, store = _make_service()
    triggered: list[str] = []
    service._trigger_warm = lambda user_id: triggered.append(user_id)  # type: ignore[method-assign]

    results = await asyncio.gather(*(service.get_home_data("u1") for _ in range(5)))

    # every follower received the leader's exact response object
    assert len({id(result) for result in results}) == 1
    assert triggered == ["u1"]
    lb_repo.get_sitewide_top_artists.assert_not_awaited()
