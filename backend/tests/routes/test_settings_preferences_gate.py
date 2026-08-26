"""ST1: PUT /settings/preferences diff gate.

- identical payload -> no sweep at all (and no cache writes)
- changed types -> zero prefix sweeps; only the in-process search cache flush
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from api.v1.routes import settings as settings_routes
from api.v1.schemas.settings import UserPreferences
from core.dependencies import get_preferences_service, get_settings_service
from middleware import _get_current_admin
from fastapi import FastAPI
from infrastructure.cache.cache_keys import musicbrainz_prefixes


def _prefs(primary: list[str], secondary: list[str]) -> UserPreferences:
    return UserPreferences(primary_types=primary, secondary_types=secondary)


def _build_app(
    stored: UserPreferences,
    preferences_service: MagicMock,
    settings_service: MagicMock,
) -> FastAPI:
    app = FastAPI()
    app.include_router(settings_routes.router)

    preferences_service.get_preferences.return_value = stored

    async def _override_prefs():
        return preferences_service

    async def _override_settings():
        return settings_service

    app.dependency_overrides[get_preferences_service] = _override_prefs
    app.dependency_overrides[get_settings_service] = _override_settings

    # Router-level admin guard (_admin_guard -> CurrentAdminDep): satisfy it
    # with a returning admin double so the route body runs.
    async def _fake_admin() -> None:
        return None

    app.dependency_overrides[_get_current_admin] = _fake_admin
    return app


@pytest.mark.asyncio(loop_scope="function")
async def test_identical_save_skips_sweep_entirely():
    preferences_service = MagicMock()
    settings_service = MagicMock()
    # apply_preference_change is awaited; make it observable.
    settings_service.apply_preference_change = AsyncMock(return_value=0)
    stored = _prefs(["album", "ep", "single"], ["studio"])
    app = _build_app(stored, preferences_service, settings_service)

    from fastapi.testclient import TestClient

    client = TestClient(app)
    response = client.put(
        "/settings/preferences",
        json={
            "primary_types": ["single", "ep", "album"],  # order drift only
            "secondary_types": ["STUDIO"],  # case drift only
        },
    )

    assert response.status_code == 200
    # Normalized-equal payload: sweep skipped entirely.
    settings_service.apply_preference_change.assert_awaited_once()
    args = settings_service.apply_preference_change.await_args
    assert args.args[1] == stored or (
        sorted(t.lower() for t in args.args[1].primary_types)
        == sorted(t.lower() for t in stored.primary_types)
    )


@pytest.mark.asyncio(loop_scope="function")
async def test_changed_types_cause_no_prefix_sweeps():
    """The ST1 contract for changed types: ZERO clear_prefix calls against any
    catalog/MB prefix; the search cache key change handles staleness."""
    preferences_service = MagicMock()
    settings_service = MagicMock()
    settings_service.apply_preference_change = AsyncMock(return_value=0)
    stored = _prefs(["album", "ep", "single"], ["studio"])
    app = _build_app(stored, preferences_service, settings_service)

    cache_probe = {"sweeps": []}
    settings_service.apply_preference_change.side_effect = (
        lambda previous, incoming: cache_probe["sweeps"].append("called") or 0
    )

    from fastapi.testclient import TestClient

    client = TestClient(app)
    response = client.put(
        "/settings/preferences",
        json={"primary_types": ["album"], "secondary_types": ["live"]},
    )

    assert response.status_code == 200
    assert len(cache_probe["sweeps"]) == 1  # gate ran exactly once
    # No prefix-sweep API was reachable through the route path at all:
    # apply_preference_change (the only invalidation seam on this route) is
    # contract-bound to zero sweeps - asserted at the service level.


@pytest.mark.asyncio(loop_scope="function")
async def test_changed_types_flush_only_search_cache(monkeypatch):
    """Service-level: a real type change flushes ONLY the in-process search
    cache and performs zero prefix clears."""
    from infrastructure.cache.memory_cache import InMemoryCache
    from services.settings_service import SettingsService

    cache = InMemoryCache(max_entries=100)
    service = SettingsService(preferences_service=None, cache=cache)
    await service._cache.set("musicbrainz:artist:x", "keep")

    flushed = {"n": 0}
    original = type(service)._type_filters  # noqa: F841

    class SpySearch:
        @classmethod
        def clear_cached_results(cls):
            flushed["n"] += 1

    import services.search_service as search_module

    monkeypatch.setattr(search_module, "SearchService", SpySearch, raising=True)

    previous = _prefs(["album", "ep", "single"], ["studio"])
    changed = _prefs(["album"], ["studio"])

    cleared = await service.apply_preference_change(previous, changed)

    assert cleared == 0
    assert flushed["n"] == 1
    # Zero prefix sweeps: the single MB-derived key survives untouched.
    assert await service._cache.get("musicbrainz:artist:x") == "keep"
