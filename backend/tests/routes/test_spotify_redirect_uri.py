"""GH-298: the Spotify OAuth redirect_uri must come from one derivation so the
authorize URL, the token-exchange body, and the admin display endpoint stay
byte-identical. An admin-configured ``spotify_redirect_origin`` wins; unset
keeps the historical request.base_url fallback."""

import sqlite3
import threading
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI

from api.v1.routes.me_connections import router as me_router
from api.v1.routes.settings import router as settings_router
from api.v1.schemas.settings import SpotifySettings
from core.config import Settings
from core.dependencies import (
    get_auth_store,
    get_preferences_service,
    get_user_connections_store,
)
from infrastructure.crypto import init_crypto
from infrastructure.persistence.user_connections_store import UserConnectionsStore
from services.preferences_service import PreferencesService
from tests.helpers import build_test_client, override_admin_auth, override_user_auth

CALLBACK_URI_PATH = "/api/v1/me/connections/spotify/auth/callback"
CALLBACK_REQUEST_PATH = "/me/connections/spotify/auth/callback"
BASE_ORIGIN = "http://testserver"


@pytest.fixture(autouse=True)
def _crypto(tmp_path):
    init_crypto(tmp_path / "config")


@pytest.fixture
def prefs(tmp_path) -> PreferencesService:
    settings = Settings(
        config_file_path=tmp_path / "config.json", root_app_dir=tmp_path
    )
    return PreferencesService(settings)


@pytest.fixture
def me_client(prefs, tmp_path):
    db = tmp_path / "library.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS auth_users (id TEXT PRIMARY KEY, username TEXT, role TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO auth_users (id, username, role) VALUES ('user-a', 'alice', 'user')"
        )
        conn.commit()
    finally:
        conn.close()

    auth_store = AsyncMock()
    auth_store.consume_spotify_state.return_value = "user-a"
    conn_store = UserConnectionsStore(db_path=db, write_lock=threading.Lock())

    app = FastAPI()
    app.include_router(me_router)
    app.dependency_overrides[get_preferences_service] = lambda: prefs
    app.dependency_overrides[get_auth_store] = lambda: auth_store
    app.dependency_overrides[get_user_connections_store] = lambda: conn_store
    override_user_auth(app, user_id="user-a")
    return build_test_client(app)


@pytest.fixture
def admin_client(prefs):
    app = FastAPI()
    app.include_router(settings_router)
    app.dependency_overrides[get_preferences_service] = lambda: prefs
    override_admin_auth(app)
    return build_test_client(app)


def _configure_spotify(prefs: PreferencesService, origin: str = "") -> None:
    prefs.save_spotify_settings(
        SpotifySettings(
            client_id="cid",
            client_secret="csecret",
            enabled=True,
            spotify_redirect_origin=origin,
        )
    )


def _authorize_redirect_uri(me_client) -> str:
    resp = me_client.get("/me/connections/spotify/auth/url")
    assert resp.status_code == 200
    query = parse_qs(urlsplit(resp.json()["auth_url"]).query)
    return query["redirect_uri"][0]


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def token_exchange(monkeypatch):
    """Replaces the inline httpx client and records the token-exchange POST."""
    recorded = []

    class _RecordingClient:
        def __init__(self, **kwargs):
            assert kwargs.get("timeout") == 15

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def post(self, url, data=None, headers=None):
            recorded.append({"method": "POST", "url": url, "data": dict(data or {})})
            return _FakeResponse(
                {"access_token": "sp-at", "refresh_token": "sp-rt", "expires_in": 3600}
            )

        async def get(self, url, headers=None):
            recorded.append({"method": "GET", "url": url})
            return _FakeResponse({"id": "sp-user", "display_name": "Spotify User"})

    import api.v1.routes.me_connections as me_connections

    monkeypatch.setattr(me_connections.httpx, "AsyncClient", _RecordingClient)
    return recorded


def test_unset_origin_falls_back_to_request_base_url(me_client, prefs):
    _configure_spotify(prefs)
    resp = me_client.get("/me/connections/spotify/auth/url")
    assert resp.status_code == 200
    query = parse_qs(urlsplit(resp.json()["auth_url"]).query)
    # pins the pre-GH-298 behavior exactly
    assert query["redirect_uri"] == [BASE_ORIGIN + CALLBACK_URI_PATH]


def test_configured_origin_appears_in_authorize_url(me_client, prefs):
    _configure_spotify(prefs, "https://music.example.com")
    query = parse_qs(
        urlsplit(
            me_client.get("/me/connections/spotify/auth/url").json()["auth_url"]
        ).query
    )
    assert query["redirect_uri"] == ["https://music.example.com" + CALLBACK_URI_PATH]


def test_token_exchange_body_matches_authorize_url_with_origin(
    me_client, prefs, token_exchange
):
    _configure_spotify(prefs, "https://music.example.com")
    auth_resp = me_client.get("/me/connections/spotify/auth/url")
    state = parse_qs(urlsplit(auth_resp.json()["auth_url"]).query)["state"][0]
    expected_redirect = "https://music.example.com" + CALLBACK_URI_PATH

    callback = me_client.get(
        CALLBACK_REQUEST_PATH,
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )

    assert callback.status_code == 307
    assert callback.headers["location"] == "/profile?spotify=connected"
    posts = [r for r in token_exchange if r["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["url"] == "https://accounts.spotify.com/api/token"
    # OAuth byte-match requirement: exchange value == authorize-time value
    assert posts[0]["data"]["redirect_uri"] == expected_redirect


def test_token_exchange_unset_origin_pins_base_url_behavior(
    me_client, prefs, token_exchange
):
    _configure_spotify(prefs)
    auth_resp = me_client.get("/me/connections/spotify/auth/url")
    state = parse_qs(urlsplit(auth_resp.json()["auth_url"]).query)["state"][0]

    callback = me_client.get(
        CALLBACK_REQUEST_PATH,
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )

    assert callback.headers["location"] == "/profile?spotify=connected"
    posts = [r for r in token_exchange if r["method"] == "POST"]
    assert posts[0]["data"]["redirect_uri"] == BASE_ORIGIN + CALLBACK_URI_PATH


@pytest.mark.parametrize("origin", ["", "https://music.example.com"])
def test_admin_display_matches_embedded_authorize_uri(
    me_client, admin_client, prefs, origin
):
    _configure_spotify(prefs, origin)

    display = admin_client.get("/settings/spotify/redirect-uri")
    assert display.status_code == 200
    embedded = _authorize_redirect_uri(me_client)

    if origin:
        assert display.json()["redirect_uri"] == origin + CALLBACK_URI_PATH
    else:
        assert display.json()["redirect_uri"] == BASE_ORIGIN + CALLBACK_URI_PATH
    # the dashboard guidance must never drift from the generated value
    assert display.json()["redirect_uri"] == embedded


def test_trailing_slash_normalized_once(admin_client, prefs):
    resp = admin_client.put(
        "/settings/spotify",
        json={
            "client_id": "cid",
            "client_secret": "csecret",
            "enabled": True,
            "spotify_redirect_origin": "https://music.example.com/",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["spotify_redirect_origin"] == "https://music.example.com"
    assert prefs.get_spotify_settings_raw().spotify_redirect_origin == (
        "https://music.example.com"
    )
    display = admin_client.get("/settings/spotify/redirect-uri").json()["redirect_uri"]
    assert display == "https://music.example.com" + CALLBACK_URI_PATH
    assert "//api" not in display


def test_settings_roundtrip_returns_origin_unmasked_and_secret_masked(
    admin_client, prefs
):
    _configure_spotify(prefs, "https://music.example.com")
    body = admin_client.get("/settings/spotify").json()
    assert body["spotify_redirect_origin"] == "https://music.example.com"
    assert body["client_secret"] == "spotify****"


@pytest.mark.parametrize(
    "bad_origin",
    [
        "ftp://example.com",
        "example.com",
        "//example.com",
        "https://example.com/callback/path",
        "https://example.com?x=1",
        "https://example.com#frag",
        "not a url",
    ],
)
def test_invalid_origins_rejected_with_typed_error(admin_client, prefs, bad_origin):
    resp = admin_client.put(
        "/settings/spotify",
        json={
            "client_id": "cid",
            "client_secret": "csecret",
            "enabled": True,
            "spotify_redirect_origin": bad_origin,
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    # nothing persisted on rejection
    assert prefs.get_spotify_settings_raw().spotify_redirect_origin == ""


def test_empty_origin_reverts_to_dynamic_derivation(admin_client, prefs):
    _configure_spotify(prefs, "https://music.example.com")
    resp = admin_client.put(
        "/settings/spotify",
        json={
            "client_id": "cid",
            "client_secret": "csecret",
            "enabled": True,
            "spotify_redirect_origin": "",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["spotify_redirect_origin"] == ""
    display = admin_client.get("/settings/spotify/redirect-uri").json()["redirect_uri"]
    assert display == BASE_ORIGIN + CALLBACK_URI_PATH
