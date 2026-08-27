"""Base-path-aware Jellyfin advertisement (PR178 / DN-BASE-002).

SystemInfo and PlaybackInfo hand out addresses built from the request itself
(origin + scope root_path), never raw forwarded headers. Under a non-empty
deployment base path every advertised URL carries the prefix exactly once, so
token-bearing stream URLs cannot escape it and their unprefixed twins do not
resolve."""

from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from core.base_path import BasePathMiddleware

pytestmark = pytest.mark.asyncio

_BASE = "/app/base"


def _h(env):
    return {"Authorization": f'MediaBrowser Token="{env.secret}", Client="jellyfin-test"'}


def _spoof_headers():
    return {
        "X-Forwarded-Host": "evil.example",
        "X-Forwarded-Proto": "https",
        "Forwarded": "host=evil.example;proto=https",
    }


def _prefixed_client(env):
    """Wrap the real compat app in the production base-path middleware."""
    return TestClient(
        BasePathMiddleware(env.app, _BASE),
        raise_server_exceptions=False,
    )


def _track_ids(env, client, base=""):
    album = client.get(
        f"{base}/jellyfin/Items",
        params={"IncludeItemTypes": "MusicAlbum"},
        headers=_h(env),
    ).json()["Items"][0]["Id"]
    track = client.get(
        f"{base}/jellyfin/Items", params={"ParentId": album}, headers=_h(env)
    ).json()["Items"][0]["Id"]
    return track


# ----- empty base: byte-identical with today's URLs -----


async def test_system_info_local_address_empty_base(compat_env):
    pub = compat_env.client.get("/jellyfin/System/Info/Public")
    assert pub.status_code == 200
    assert pub.json()["LocalAddress"] == "http://testserver/jellyfin"

    authed = compat_env.client.get("/jellyfin/System/Info", headers=_h(compat_env))
    assert authed.status_code == 200
    assert authed.json()["LocalAddress"] == "http://testserver/jellyfin"


async def test_playback_info_direct_stream_url_empty_base(compat_env):
    tid = _track_ids(compat_env, compat_env.client)
    r = compat_env.client.get(
        f"/jellyfin/Items/{tid}/PlaybackInfo",
        params={"userId": "user-alice"},
        headers=_h(compat_env),
    )
    assert r.status_code == 200
    src = r.json()["MediaSources"][0]
    # No client bitrate ceiling sent -> the server never forces a transcode,
    # whatever ffmpeg is installed on the machine.
    assert "TranscodingUrl" not in src
    assert src["DirectStreamUrl"] == (
        f"http://testserver/jellyfin/Audio/{tid}/stream.flac"
        f"?static=true&mediaSourceId={tid}&api_key={compat_env.secret}"
    )


async def test_raw_forwarded_headers_cannot_shape_advertised_urls(compat_env):
    spoof = {**_h(compat_env), **_spoof_headers()}
    tid = _track_ids(compat_env, compat_env.client)

    pub = compat_env.client.get("/jellyfin/System/Info/Public", headers=_spoof_headers())
    assert pub.status_code == 200
    assert pub.json()["LocalAddress"] == "http://testserver/jellyfin"

    pb = compat_env.client.get(
        f"/jellyfin/Items/{tid}/PlaybackInfo",
        params={"userId": "user-alice"},
        headers=spoof,
    )
    assert pb.status_code == 200
    body = pb.text
    assert "evil.example" not in body
    assert '"https:' not in body


# ----- multi-segment base: prefixed exactly once -----


async def test_prefixed_system_info_and_playback_info_advertise_prefix_once(compat_env):
    client = _prefixed_client(compat_env)
    pub = client.get(f"{_BASE}/jellyfin/System/Info/Public")
    assert pub.status_code == 200
    assert pub.json()["LocalAddress"] == f"http://testserver{_BASE}/jellyfin"

    tid = _track_ids(compat_env, client, _BASE)
    r = client.get(
        f"{_BASE}/jellyfin/Items/{tid}/PlaybackInfo",
        params={"userId": "user-alice"},
        headers=_h(compat_env),
    )
    assert r.status_code == 200
    body = r.json()
    src = body["MediaSources"][0]
    assert src["DirectStreamUrl"] == (
        f"http://testserver{_BASE}/jellyfin/Audio/{tid}/stream.flac"
        f"?static=true&mediaSourceId={tid}&api_key={compat_env.secret}"
    )
    text = r.text
    assert "http://testserver/jellyfin/" not in text


async def test_transcoding_url_lives_inside_prefix(compat_env, monkeypatch):
    from api.v1.schemas.settings import ConnectAppsSettings

    compat_env.preferences.save_connect_apps_settings(
        ConnectAppsSettings(
            subsonic_enabled=True, jellyfin_enabled=True, transcoding_enabled=True
        )
    )
    monkeypatch.setattr(
        "services.compat.transcode_service.ffmpeg_available", lambda: True
    )
    client = _prefixed_client(compat_env)
    tid = _track_ids(compat_env, client, _BASE)
    r = client.get(
        f"{_BASE}/jellyfin/Items/{tid}/PlaybackInfo",
        params={"userId": "user-alice", "maxStreamingBitrate": 32000},
        headers=_h(compat_env),
    )
    assert r.status_code == 200
    body = r.json()
    src = body["MediaSources"][0]
    assert src["SupportsTranscoding"] is True
    psid = body["PlaySessionId"]
    assert src["TranscodingUrl"] == (
        f"{_BASE}/jellyfin/Audio/{tid}/universal"
        f"?AudioCodec=mp3&Container=mp3&PlaySessionId={psid}"
    )
    assert f"http://testserver/jellyfin/" not in r.text


# ----- token-bearing URL usable only inside the prefix -----


async def test_direct_stream_token_url_plays_only_within_prefix(streaming_env):
    client = _prefixed_client(streaming_env)
    pb = client.post(
        f"{_BASE}/jellyfin/Items/{streaming_env.jf_track_id}/PlaybackInfo",
        headers={"X-Emby-Token": streaming_env.secret},
        json={},
    )
    assert pb.status_code == 200
    parts = urlsplit(pb.json()["MediaSources"][0]["DirectStreamUrl"])
    assert parts.path.startswith(_BASE)

    # Exactly what a native player does: fetch the advertised URL verbatim,
    # no auth header - the embedded api_key must carry it.
    ok = client.get(f"{parts.path}?{parts.query}")
    assert ok.status_code == 200
    assert ok.content == streaming_env.raw

    # Same tokens served without the configured prefix must not resolve; the
    # cookie/token-bearing surface stays unreachable outside the prefix.
    escaped = client.get(f"{parts.path.removeprefix(_BASE)}?{parts.query}")
    assert escaped.status_code == 404
    assert streaming_env.secret.encode() not in escaped.content
