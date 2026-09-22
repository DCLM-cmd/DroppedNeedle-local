"""Prowlarr route tests: admin auth matrix, masked config get/put, and the
connection Test (submitted creds, body-carried valid/version/count)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException

from api.v1.routes import prowlarr
from api.v1.schemas.settings import PROWLARR_API_KEY_MASK, ProwlarrConnectionSettings
from core.dependencies import get_preferences_service
from core.exceptions import ProwlarrApiError, ProwlarrAuthError, RateLimitedError
from middleware import _get_current_admin
from tests.helpers import build_test_client, mock_admin_user


def _prefs() -> MagicMock:
    prefs = MagicMock()
    prefs.get_prowlarr_connection.return_value = ProwlarrConnectionSettings(
        enabled=True, url="http://prowlarr:9696", api_key=PROWLARR_API_KEY_MASK
    )
    prefs.get_prowlarr_connection_raw.return_value = ProwlarrConnectionSettings(
        enabled=True, url="http://prowlarr:9696", api_key="stored-secret"
    )
    prefs.save_prowlarr_connection.return_value = None
    return prefs


def _app(prefs=None) -> FastAPI:
    app = FastAPI()
    app.include_router(prowlarr.router)
    app.dependency_overrides[get_preferences_service] = lambda: prefs or _prefs()
    return app


def _deny_admin():
    raise HTTPException(status_code=403, detail="admin only")


def test_get_config_admin_returns_masked_key():
    app = _app()
    app.dependency_overrides[_get_current_admin] = mock_admin_user
    response = build_test_client(app).get("/prowlarr/config")
    assert response.status_code == 200
    body = response.json()
    assert body["api_key"] == PROWLARR_API_KEY_MASK
    assert body["url"] == "http://prowlarr:9696"


def test_get_config_non_admin_forbidden():
    app = _app()
    app.dependency_overrides[_get_current_admin] = _deny_admin
    assert build_test_client(app).get("/prowlarr/config").status_code == 403


def test_get_config_unauthenticated():
    assert build_test_client(_app()).get("/prowlarr/config").status_code == 401


def test_put_config_non_admin_forbidden():
    app = _app()
    app.dependency_overrides[_get_current_admin] = _deny_admin
    response = build_test_client(app).put(
        "/prowlarr/config", json={"enabled": True, "url": "", "api_key": ""}
    )
    assert response.status_code == 403


def test_put_config_saves_and_clears_cache(monkeypatch):
    from core.dependencies import get_prowlarr_indexer

    prefs = _prefs()
    app = _app(prefs)
    app.dependency_overrides[_get_current_admin] = mock_admin_user
    clear = MagicMock()
    monkeypatch.setattr(get_prowlarr_indexer, "cache_clear", clear)
    response = build_test_client(app).put(
        "/prowlarr/config",
        json={"enabled": True, "url": "prowlarr:9696", "api_key": "k"},
    )
    assert response.status_code == 200
    assert response.json() == {"success": True}
    prefs.save_prowlarr_connection.assert_called_once()
    clear.assert_called_once()


def _test_post(body: dict, client: MagicMock):
    app = _app()
    app.dependency_overrides[_get_current_admin] = mock_admin_user
    with patch.object(prowlarr, "build_prowlarr_client", return_value=client):
        return build_test_client(app).post("/prowlarr/test", json=body)


def test_test_uses_submitted_creds_before_first_save():
    client = MagicMock()
    client.system_status = AsyncMock(return_value=MagicMock(version="1.32.0"))
    client.list_indexers = AsyncMock(return_value=[MagicMock(), MagicMock()])
    with patch.object(prowlarr, "build_prowlarr_client", return_value=client) as build:
        response = build_test_client(_app_with_admin()).post(
            "/prowlarr/test",
            json={"enabled": True, "url": "http://fresh:9696", "api_key": "fresh-key"},
        )
    assert response.status_code == 200
    build.assert_called_once_with("http://fresh:9696", "fresh-key")
    body = response.json()
    assert body["valid"] is True
    assert body["version"] == "1.32.0"
    assert body["indexer_count"] == 2


def test_test_normalizes_suffixed_url_via_post_init():
    # MsgSpecBody conversion runs __post_init__: a pasted /api/v1 URL must test
    # the bare origin, not .../api/v1/api/v1/....
    client = MagicMock()
    client.system_status = AsyncMock(return_value=None)
    client.list_indexers = AsyncMock(return_value=[])
    with patch.object(prowlarr, "build_prowlarr_client", return_value=client) as build:
        response = build_test_client(_app_with_admin()).post(
            "/prowlarr/test",
            json={"enabled": True, "url": "fresh:9696/api/v1", "api_key": "k"},
        )
    assert response.status_code == 200
    build.assert_called_once_with("http://fresh:9696", "k")
    assert response.json()["valid"] is True


def _app_with_admin() -> FastAPI:
    app = _app()
    app.dependency_overrides[_get_current_admin] = mock_admin_user
    return app


def test_test_masked_key_resolves_to_stored():
    client = MagicMock()
    client.system_status = AsyncMock(return_value=MagicMock(version="1.32.0"))
    client.list_indexers = AsyncMock(return_value=[])
    with patch.object(prowlarr, "build_prowlarr_client", return_value=client) as build:
        response = build_test_client(_app_with_admin()).post(
            "/prowlarr/test",
            json={
                "enabled": True,
                "url": "http://prowlarr:9696",
                "api_key": PROWLARR_API_KEY_MASK,
            },
        )
    assert response.status_code == 200
    build.assert_called_once_with("http://prowlarr:9696", "stored-secret")
    assert response.json()["valid"] is True


def test_test_auth_failure_carried_in_body():
    client = MagicMock()
    client.system_status = AsyncMock(side_effect=ProwlarrAuthError("nope"))
    response = _test_post(
        {"enabled": True, "url": "http://prowlarr:9696", "api_key": "bad"}, client
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "API key" in body["message"]


def test_test_unreachable_carried_in_body_without_url_echo():
    client = MagicMock()
    client.system_status = AsyncMock(side_effect=ProwlarrApiError("down"))
    response = _test_post(
        {"enabled": True, "url": "http://secret-host:9696", "api_key": "k"}, client
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "secret-host" not in body["message"]


def test_test_rate_limited_carried_in_body():
    client = MagicMock()
    client.system_status = AsyncMock(side_effect=RateLimitedError("slow"))
    response = _test_post(
        {"enabled": True, "url": "http://prowlarr:9696", "api_key": "k"}, client
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False


def test_test_non_admin_forbidden():
    app = _app()
    app.dependency_overrides[_get_current_admin] = _deny_admin
    response = build_test_client(app).post(
        "/prowlarr/test", json={"enabled": True, "url": "", "api_key": ""}
    )
    assert response.status_code == 403
