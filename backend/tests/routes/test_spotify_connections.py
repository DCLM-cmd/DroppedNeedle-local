"""Route-level base-path coverage for the Spotify connection flow (PR #178).

The redirect_uri must stay byte-identical across the authorize URL, the token
exchange body and the admin display endpoint while gaining ``Settings.base_path``
exactly once. A configured ``spotify_redirect_origin`` is canonical - even a
hostile Host plus forged X-Forwarded-* headers cannot move it - while the
empty-origin fallback follows the real request host and ignores raw forwarded
headers. Every profile success/error browser redirect comes back relative with
the base path prefixed once.
"""

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
SPOOF_HEADERS = {
    "Host": "evil.example",
    "X-Forwarded-Host": "evil.example",
    "X-Forwarded-Proto": "https",
}

# Fallback phase keeps the real Host: raw forwarded headers never reach the
# derivation untrusted, so only they get forged here.
FORWARDED_ONLY_HEADERS = {
    "X-Forwarded-Host": "evil.example",
    "X-Forwarded-Proto": "https",
}


@pytest.fixture(autouse=True)
def _crypto(tmp_path):
    init_crypto(tmp_path / "config")


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _RecordingClient:
    """Inline httpx double recording authorize-driven exchange calls."""

    status_code = 200
    fail = False

    def __init__(self, **kwargs):
        assert kwargs.get("timeout") == 15

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, data=None, headers=None):
        self.posts.append({"url": url, "data": dict(data or {})})
        if self.fail:
            raise ConnectionError("network down")
        return _FakeResponse(
            self.status_code,
            {"access_token": "sp-at", "refresh_token": "sp-rt", "expires_in": 3600},
        )

    async def get(self, url, headers=None):
        return _FakeResponse(200, {"id": "sp-user", "display_name": "Spotify User"})


@pytest.fixture
def token_exchange(monkeypatch):
    recorded = _RecordingClient(timeout=15)
    recorded.posts = []
    import api.v1.routes.me_connections as me_connections

    monkeypatch.setattr(me_connections.httpx, "AsyncClient", lambda **kw: recorded)
    return recorded


def _clients(tmp_path, *, base_path="", origin="", consume_returns="user-a"):
    prefs = PreferencesService(
        Settings(
            config_file_path=tmp_path / "config.json",
            root_app_dir=tmp_path,
            base_path=base_path,
        )
    )
    prefs.save_spotify_settings(
        SpotifySettings(
            client_id="cid",
            client_secret="csecret",
            enabled=True,
            spotify_redirect_origin=origin,
        )
    )

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
    auth_store.consume_spotify_state.return_value = consume_returns
    conn_store = UserConnectionsStore(db_path=db, write_lock=threading.Lock())

    app = FastAPI()
    app.include_router(me_router)
    app.include_router(settings_router)
    app.dependency_overrides[get_preferences_service] = lambda: prefs
    app.dependency_overrides[get_auth_store] = lambda: auth_store
    app.dependency_overrides[get_user_connections_store] = lambda: conn_store
    override_user_auth(app, user_id="user-a")
    override_admin_auth(app)
    return build_test_client(app)


def test_base_appended_once_and_byte_identical_across_phases(tmp_path, token_exchange):
    client = _clients(tmp_path, base_path="/musicapp", origin="https://music.example.com")
    expected = "https://music.example.com/musicapp" + CALLBACK_URI_PATH

    resp = client.get("/me/connections/spotify/auth/url")
    assert resp.status_code == 200
    query = parse_qs(urlsplit(resp.json()["auth_url"]).query)

    callback = client.get(CALLBACK_REQUEST_PATH + "?code=c&state=s", follow_redirects=False)
    display = client.get("/settings/spotify/redirect-uri")

    # authorize-time value, exchange body value, admin-display value
    assert query["redirect_uri"] == [expected]
    assert token_exchange.posts[0]["data"]["redirect_uri"] == expected
    assert display.json()["redirect_uri"] == expected


def test_empty_base_keeps_legacy_bytes_across_phases(tmp_path, token_exchange):
    client = _clients(tmp_path)
    expected = BASE_ORIGIN + CALLBACK_URI_PATH

    query = parse_qs(urlsplit(client.get("/me/connections/spotify/auth/url").json()["auth_url"]).query)
    client.get(CALLBACK_REQUEST_PATH + "?code=c&state=s", follow_redirects=False)
    display = client.get("/settings/spotify/redirect-uri")

    assert query["redirect_uri"] == [expected]
    assert token_exchange.posts[0]["data"]["redirect_uri"] == expected
    assert display.json()["redirect_uri"] == expected


def test_forwarded_headers_cannot_change_derivation(tmp_path, token_exchange):
    client = _clients(
        tmp_path, base_path="/musicapp", origin="https://music.example.com"
    )
    expected = "https://music.example.com/musicapp" + CALLBACK_URI_PATH

    resp = client.get("/me/connections/spotify/auth/url", headers=SPOOF_HEADERS)
    query = parse_qs(urlsplit(resp.json()["auth_url"]).query)
    client.get(CALLBACK_REQUEST_PATH + "?code=c&state=s", headers=SPOOF_HEADERS, follow_redirects=False)
    display = client.get("/settings/spotify/redirect-uri", headers=SPOOF_HEADERS)

    assert query["redirect_uri"] == [expected]
    assert token_exchange.posts[0]["data"]["redirect_uri"] == expected
    assert display.json()["redirect_uri"] == expected


@pytest.mark.parametrize("base_path", ["", "/musicapp"])
def test_fallback_derivation_ignores_raw_forwarded_headers(tmp_path, base_path):
    client = _clients(tmp_path, base_path=base_path)
    resp = client.get("/me/connections/spotify/auth/url", headers=FORWARDED_ONLY_HEADERS)
    query = parse_qs(urlsplit(resp.json()["auth_url"]).query)
    prefix = "/musicapp" if base_path else ""
    assert query["redirect_uri"] == [BASE_ORIGIN + prefix + CALLBACK_URI_PATH]


def test_callback_success_redirects_to_prefixed_profile(tmp_path, token_exchange):
    client = _clients(tmp_path, base_path="/musicapp")
    resp = client.get(CALLBACK_REQUEST_PATH + "?code=c&state=s", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/musicapp/profile?spotify=connected"


@pytest.mark.parametrize(
    "query,knobs,location_suffix",
    [
        ("?x=y", {}, "/profile?spotify=error"),
        ("?code=c&state=s", {"consume_returns": None}, "/profile?spotify=error&reason=state"),
        ("?code=c&state=s", {"status_code": 400}, "/profile?spotify=error&reason=token"),
        ("?code=c&state=s", {"fail": True}, "/profile?spotify=error&reason=network"),
    ],
)
def test_callback_error_branches_prefixed_once(
    tmp_path, token_exchange, query, knobs, location_suffix
):
    for attr, value in knobs.items():
        setattr(token_exchange, attr, value)
    client = _clients(
        tmp_path,
        base_path="/musicapp",
        consume_returns=knobs.get("consume_returns", "user-a"),
    )
    resp = client.get(CALLBACK_REQUEST_PATH + query, follow_redirects=False)
    assert resp.headers["location"] == "/musicapp" + location_suffix


def test_error_branches_unprefixed_under_empty_base(tmp_path, token_exchange):
    client = _clients(tmp_path)
    missing = client.get(CALLBACK_REQUEST_PATH + "?x=y", follow_redirects=False)
    bad_state = _clients(tmp_path, consume_returns=None).get(
        CALLBACK_REQUEST_PATH + "?code=c&state=s", follow_redirects=False
    )
    assert missing.headers["location"] == "/profile?spotify=error"
    assert bad_state.headers["location"] == "/profile?spotify=error&reason=state"
