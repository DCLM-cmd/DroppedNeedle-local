"""Base-path awareness of the Spotify redirect_uri builder and relative
browser redirects (PR #178).

``Settings.base_path`` (canonical: "/seg" or "", validated at Settings load)
must appear exactly once between the effective origin and the callback path,
the derivation must stay byte-stable across repeated calls, and
``with_base_path`` must prefix app-relative profile redirects without ever
doubling the prefix.
"""

import json

import pytest

from api.v1.schemas.settings import SpotifySettings
from core.config import Settings
from infrastructure.crypto import init_crypto
from services.preferences_service import PreferencesService

CALLBACK_URI_PATH = "/api/v1/me/connections/spotify/auth/callback"
BASE_ORIGIN = "http://testserver"


@pytest.fixture(autouse=True)
def _crypto(tmp_path):
    init_crypto(tmp_path / "config")


def _service(tmp_path, base_path="", stored_spotify=None):
    """Build a service; ``stored_spotify`` seeds legacy on-disk config verbatim."""
    config_file = tmp_path / "config.json"
    if stored_spotify is not None:
        config_file.write_text(json.dumps({"spotify_settings": stored_spotify}))
    settings = Settings(
        config_file_path=config_file, root_app_dir=tmp_path, base_path=base_path
    )
    return PreferencesService(settings)


def _configured(tmp_path, base_path="", origin=""):
    svc = _service(tmp_path, base_path=base_path)
    svc.save_spotify_settings(
        SpotifySettings(
            client_id="cid",
            client_secret="csecret",
            enabled=True,
            spotify_redirect_origin=origin,
        )
    )
    return svc


def test_empty_base_preserves_historical_bytes(tmp_path):
    svc = _service(tmp_path)
    assert svc.spotify_redirect_uri(BASE_ORIGIN + "/") == BASE_ORIGIN + CALLBACK_URI_PATH


def test_non_empty_base_appended_once_to_request_fallback(tmp_path):
    svc = _service(tmp_path, base_path="/musicapp")
    assert svc.spotify_redirect_uri(BASE_ORIGIN + "/") == (
        BASE_ORIGIN + "/musicapp" + CALLBACK_URI_PATH
    )


def test_base_appended_once_to_configured_origin(tmp_path):
    svc = _configured(tmp_path, base_path="/musicapp", origin="https://music.example.com")
    assert svc.spotify_redirect_uri(BASE_ORIGIN + "/") == (
        "https://music.example.com/musicapp" + CALLBACK_URI_PATH
    )


def test_request_fallback_already_carrying_base_not_doubled(tmp_path):
    # Under the mounted app request.base_url ends with the deployment prefix.
    svc = _service(tmp_path, base_path="/musicapp")
    assert svc.spotify_redirect_uri(BASE_ORIGIN + "/musicapp/") == (
        BASE_ORIGIN + "/musicapp" + CALLBACK_URI_PATH
    )


def test_configured_origin_with_prefixed_request_base_not_doubled(tmp_path):
    # Saved origins stay bare by contract; deployment prefixes arrive via the
    # request base instead and must not double there either.
    svc = _configured(tmp_path, base_path="/musicapp", origin="https://music.example.com")
    assert svc.spotify_redirect_uri(BASE_ORIGIN + "/musicapp/") == (
        "https://music.example.com/musicapp" + CALLBACK_URI_PATH
    )


def test_multi_segment_base_appended_once(tmp_path):
    svc = _service(tmp_path, base_path="/apps/droppedneedle")
    assert svc.spotify_redirect_uri("https://h.test/") == (
        "https://h.test/apps/droppedneedle" + CALLBACK_URI_PATH
    )


def test_legacy_stored_origin_with_trailing_slash_normalized_once(tmp_path):
    # Saved configs only persist rstripped origins, but a hand-edited or
    # pre-ratchet file may still carry the trailing slash.
    svc = _service(
        tmp_path,
        base_path="/musicapp",
        stored_spotify={
            "enabled": True,
            "spotify_redirect_origin": "https://music.example.com/musicapp/",
        },
    )
    assert svc.spotify_redirect_uri("") == (
        "https://music.example.com/musicapp" + CALLBACK_URI_PATH
    )
    assert "//api" not in svc.spotify_redirect_uri("")


def test_repeated_derivation_is_byte_identical(tmp_path):
    svc = _configured(tmp_path, base_path="/musicapp", origin="https://music.example.com")
    first = svc.spotify_redirect_uri(BASE_ORIGIN + "/")
    assert first == svc.spotify_redirect_uri(BASE_ORIGIN + "/musicapp/")
    assert first == "https://music.example.com/musicapp" + CALLBACK_URI_PATH


@pytest.mark.parametrize(
    "base,target,expected",
    [
        ("", "/profile?spotify=connected", "/profile?spotify=connected"),
        ("/musicapp", "/profile?spotify=connected", "/musicapp/profile?spotify=connected"),
        ("/apps/dn", "/profile?x=1&y=2", "/apps/dn/profile?x=1&y=2"),
    ],
)
def test_with_base_path_prefixes_relative_redirects_once(tmp_path, base, target, expected):
    svc = _service(tmp_path, base_path=base)
    assert svc.with_base_path(target) == expected
