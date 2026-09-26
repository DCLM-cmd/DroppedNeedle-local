"""Security-fix regressions: ext body cap, publish principal spoof, proxy cap,
SSRF DNS-rebind pinning, install cache/tick invalidation."""

import asyncio
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.v1.schemas.settings import PluginConfig
from infrastructure.plugins.host import PluginHost

_CAP = 1024 * 1024

EXT_MANIFEST = """[plugin]
name = "{name}"
version = "1.0.0"
api_version = 1
entrypoint = "plugin:Toy"
capabilities = ["subscriber", "publisher"]

[[route]]
path = "note"
method = "DELETE"
auth = "user"

[[route]]
path = "status"
method = "GET"
auth = "user"
"""

EXT_CODE = """
from infrastructure.plugins.protocols import PluginRouteResponse


class Toy:
    def __init__(self, context):
        self.ctx = context

    async def on_event(self, event):
        return None

    async def handle_route(self, method, subpath, query, body):
        return PluginRouteResponse(status=200, body={"ok": True})
"""

PUB_MANIFEST = """[plugin]
name = "{name}"
version = "1.0.0"
api_version = 1
entrypoint = "plugin:Toy"
capabilities = ["subscriber", "publisher"]
"""

PUB_CODE = """
class Toy:
    def __init__(self, context):
        self.ctx = context

    async def on_event(self, event):
        return None
"""


class FakePrefs:
    def __init__(self) -> None:
        self.configs: dict[str, PluginConfig] = {}

    def get_plugin_config(self, name: str) -> PluginConfig:
        return self.configs.get(name, PluginConfig())

    def enable(self, name: str, settings: dict | None = None) -> None:
        self.configs[name] = PluginConfig(enabled=True, settings=settings or {})


def _write_plugin(root: Path, name: str, manifest: str, code: str) -> None:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.toml").write_text(manifest)
    (plugin_dir / "plugin.py").write_text(code)


def _ext_host(tmp_path: Path, name: str) -> PluginHost:
    prefs = FakePrefs()
    _write_plugin(tmp_path, name, EXT_MANIFEST.format(name=name), EXT_CODE)
    prefs.enable(name)
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    return host


def _pub_host(tmp_path: Path, *names: str) -> PluginHost:
    prefs = FakePrefs()
    for name in names:
        _write_plugin(tmp_path, name, PUB_MANIFEST.format(name=name), PUB_CODE)
        prefs.enable(name)
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    return host


def _user() -> SimpleNamespace:
    return SimpleNamespace(id="sec-u1", role="user")


# -- 1. ext body cap on every method --


@pytest.mark.asyncio
async def test_ext_delete_oversized_content_length_is_413_without_reading_body(
    tmp_path, monkeypatch
):
    import api.v1.routes.plugins as ext

    host = _ext_host(tmp_path, "extcapdel")
    monkeypatch.setattr(ext, "get_plugin_host", lambda: host)
    calls: list = []
    original = host.handle_plugin_route

    async def _spy(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(host, "handle_plugin_route", _spy)
    body_calls: list = []

    async def _body() -> bytes:
        body_calls.append(1)
        return b"{}"

    request = SimpleNamespace(
        body=_body, query_params={}, headers={"content-length": str(_CAP + 1)}
    )
    response = await ext._serve_ext(request, _user(), "extcapdel", "note", "DELETE")
    assert response.status_code == 413
    assert b"PAYLOAD_TOO_LARGE" in response.body
    assert body_calls == []
    assert calls == []


@pytest.mark.asyncio
async def test_ext_get_oversized_streamed_body_is_413_without_invoking_plugin(
    tmp_path, monkeypatch
):
    import api.v1.routes.plugins as ext

    host = _ext_host(tmp_path, "extcapget")
    monkeypatch.setattr(ext, "get_plugin_host", lambda: host)
    calls: list = []
    original = host.handle_plugin_route

    async def _spy(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(host, "handle_plugin_route", _spy)

    async def _stream():
        yield b"x" * 700_000
        yield b"x" * 700_000

    request = SimpleNamespace(stream=_stream, query_params={}, headers={})
    response = await ext._serve_ext(request, _user(), "extcapget", "status", "GET")
    assert response.status_code == 413
    assert b"PAYLOAD_TOO_LARGE" in response.body
    assert calls == []


@pytest.mark.asyncio
async def test_ext_small_bodies_still_reach_plugin(tmp_path, monkeypatch):
    import api.v1.routes.plugins as ext

    host = _ext_host(tmp_path, "extcapsmall")
    monkeypatch.setattr(ext, "get_plugin_host", lambda: host)

    async def _stream():
        yield b'{"a": 1}'

    request = SimpleNamespace(
        stream=_stream, query_params={}, headers={"content-length": "8"}
    )
    response = await ext._serve_ext(request, _user(), "extcapsmall", "status", "GET")
    assert response.status_code == 200


# -- 2. publish principal spoof --


def test_publish_rotating_plugin_principals_share_one_bucket(tmp_path):
    host = _pub_host(tmp_path, "spoofpub")
    publish = host._publisher_for("spoofpub")
    for index in range(30):
        result = publish(
            "plugin_notice", {"title": "t", "body": "b"}, principal=f"user-{index}"
        )
        assert result.ok, index
    limited = publish(
        "plugin_notice", {"title": "t", "body": "b"}, principal="someone-new"
    )
    assert limited.ok is False and limited.status == 429
    assert isinstance(limited.retry_after, int) and limited.retry_after >= 1


def test_publish_plugin_principal_is_dropped_from_record(tmp_path):
    host = _pub_host(tmp_path, "dropub")
    publish = host._publisher_for("dropub")
    result = publish(
        "plugin_notice", {"title": "t", "body": "b"}, principal="mallory"
    )
    assert result.ok is True
    (record,) = host.drain_published()
    assert record["principal"] == ""
    assert record["source_plugin"] == "dropub"


def test_publish_direct_engine_principals_stay_isolated(tmp_path):
    host = _pub_host(tmp_path, "honestpub")
    for _ in range(30):
        assert host.publish_from_plugin(
            "honestpub", "plugin_notice", {"title": "t", "body": "b"}, principal="alice"
        ).ok
    limited = host.publish_from_plugin(
        "honestpub", "plugin_notice", {"title": "t", "body": "b"}, principal="alice"
    )
    assert limited.ok is False and limited.status == 429
    assert host.publish_from_plugin(
        "honestpub", "plugin_notice", {"title": "t", "body": "b"}, principal="bob"
    ).ok


# -- 3 + 4. proxy cap + rebind pinning --


class _FakeUpstream:
    def __init__(self, *, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.is_redirect = False
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class _FakeProxyClient:
    def __init__(self, response):
        self.response = response
        self.sends: list = []

    def build_request(self, method, url, headers=None):
        return SimpleNamespace(method=method, url=url, headers=headers)

    async def send(self, request, stream=False):
        self.sends.append(request)
        return self.response


@pytest.mark.asyncio
async def test_proxy_content_length_over_cap_aborts(monkeypatch):
    import services.compat.plugin_stream_service as pss

    monkeypatch.setattr(pss, "_PROXY_MAX_BYTES", 1024)
    upstream = _FakeUpstream(
        headers={"Content-Length": "2048", "content-type": "audio/mpeg"},
        chunks=(b"x" * 2048,),
    )
    client = _FakeProxyClient(upstream)
    monkeypatch.setattr(pss, "get_plugin_stream_proxy_client", lambda: client)
    result = await pss._proxy_direct("http://93.184.216.34/file.mp3", None, {})
    assert result is None
    assert upstream.closed is True


@pytest.mark.asyncio
async def test_proxy_streamed_bytes_truncate_at_cap(monkeypatch):
    import services.compat.plugin_stream_service as pss

    monkeypatch.setattr(pss, "_PROXY_MAX_BYTES", 1024)
    upstream = _FakeUpstream(
        headers={"content-type": "audio/mpeg"},
        chunks=(b"x" * 600, b"y" * 600, b"z" * 600),
    )
    client = _FakeProxyClient(upstream)
    monkeypatch.setattr(pss, "get_plugin_stream_proxy_client", lambda: client)
    proxied = await pss._proxy_direct("http://93.184.216.34/file.mp3", None, {})
    assert proxied is not None
    chunks, _headers, status, _media, _resp = proxied
    total = 0
    async for chunk in chunks:
        total += len(chunk)
    assert 0 < total <= 1024
    assert status == 200


@pytest.mark.asyncio
async def test_proxy_idle_stream_aborts_without_hanging(monkeypatch):
    import services.compat.plugin_stream_service as pss

    monkeypatch.setattr(pss, "_PROXY_IDLE_TIMEOUT_S", 0.05)

    class _Stalled(_FakeUpstream):
        async def aiter_bytes(self):
            yield b"head"
            await asyncio.sleep(30)
            yield b"tail"

    upstream = _Stalled(headers={"content-type": "audio/mpeg"})
    client = _FakeProxyClient(upstream)
    monkeypatch.setattr(pss, "get_plugin_stream_proxy_client", lambda: client)
    proxied = await pss._proxy_direct("http://93.184.216.34/file.mp3", None, {})
    assert proxied is not None
    chunks, _headers, _status, _media, _resp = proxied
    seen = [chunk async for chunk in chunks]
    assert seen == [b"head"]
    assert upstream.closed is True


@pytest.mark.asyncio
async def test_proxy_rebind_to_private_blocked_before_send(monkeypatch):
    import services.compat.plugin_stream_service as pss

    resolutions = {"n": 0}

    def _flapping(host, port, *args, **kwargs):
        resolutions["n"] += 1
        ip = "93.184.216.34" if resolutions["n"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", _flapping)
    upstream = _FakeUpstream(
        headers={"content-type": "audio/mpeg"}, chunks=(b"evil",)
    )
    client = _FakeProxyClient(upstream)
    monkeypatch.setattr(pss, "get_plugin_stream_proxy_client", lambda: client)
    result = await pss._proxy_direct("http://flapping.test/file.mp3", None, {})
    assert result is None
    assert client.sends == []
    assert resolutions["n"] >= 2


# -- 5. install invalidates --


def test_install_clears_provider_cache_and_syncs_ticks(monkeypatch):
    from fastapi import APIRouter, FastAPI

    from api.v1.routes import plugins as plugins_routes
    from core.dependencies import get_plugin_host, get_preferences_service
    from infrastructure.plugins.manifest import PluginManifest
    from infrastructure.plugins.host import LoadedPlugin
    from middleware import _get_current_admin
    from tests.helpers import build_test_client, mock_admin_user

    host = MagicMock()
    host.install_from_github = AsyncMock(return_value="fresh")
    host.get = MagicMock(
        return_value=LoadedPlugin(
            manifest=PluginManifest(
                name="fresh",
                version="1.0.0",
                api_version=0,
                entrypoint="plugin:Demo",
                capabilities=[],
                display_name="Fresh",
                settings=[],
            ),
            enabled=False,
            active_capabilities=[],
        )
    )
    host.sync_ticks = MagicMock(return_value=None)
    prefs = MagicMock()
    prefs.get_plugin_config = MagicMock(
        return_value=PluginConfig(enabled=False, settings={})
    )
    cleared: list = []
    monkeypatch.setattr(
        plugins_routes, "_clear_plugin_cache", lambda: cleared.append(1)
    )

    app = FastAPI()
    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(plugins_routes.router)
    app.include_router(v1)
    app.dependency_overrides[get_plugin_host] = lambda: host
    app.dependency_overrides[get_preferences_service] = lambda: prefs
    app.dependency_overrides[_get_current_admin] = mock_admin_user
    client = build_test_client(app)

    response = client.post(
        "/api/v1/plugins/install", json={"repository_url": "https://github.com/o/r"}
    )
    assert response.status_code == 201
    assert cleared == [1]
    host.sync_ticks.assert_called_once()


# -- 6. ext rate buckets are per (plugin, principal, method, subpath) --


@pytest.mark.asyncio
async def test_ext_rate_limit_buckets_are_per_route(tmp_path, monkeypatch):
    import api.v1.routes.plugins as ext

    host = _ext_host(tmp_path, "extperroute")
    monkeypatch.setattr(ext, "get_plugin_host", lambda: host)
    monkeypatch.setattr(ext, "_EXT_RATE", {})
    user = SimpleNamespace(id="route-u1", role="user")

    async def _stream():
        yield b"{}"

    async def _body() -> bytes:
        return b"{}"

    response = None
    for _ in range(61):
        request = SimpleNamespace(body=_body, stream=_stream, query_params={}, headers={})
        response = await ext._serve_ext(request, user, "extperroute", "status", "GET")
    assert response is not None and response.status_code == 429
    assert b"RATE_LIMITED" in response.body

    # Same plugin + principal, different method+subpath: unaffected bucket.
    request = SimpleNamespace(body=_body, stream=_stream, query_params={}, headers={})
    other = await ext._serve_ext(request, user, "extperroute", "note", "DELETE")
    assert other.status_code == 200
    assert ("extperroute", "route-u1", "GET", "status") in ext._EXT_RATE
    assert ("extperroute", "route-u1", "DELETE", "note") in ext._EXT_RATE


# -- 7. priority/policy + plugin saves invalidate the scorer/registry chain --


def test_clear_plugin_cache_clears_scorer_registry_chain(monkeypatch):
    from api.v1.routes import plugins as plugins_routes
    from core.dependencies import (
        get_newznab_indexer,
        get_newznab_release_scorer,
        get_plugin_release_scorer,
        get_plugin_source_registry,
        get_track_matcher,
    )

    spies = []
    for provider in (
        get_plugin_source_registry,
        get_plugin_release_scorer,
        get_newznab_indexer,
        get_newznab_release_scorer,
        get_track_matcher,
    ):
        spy = MagicMock()
        monkeypatch.setattr(provider, "cache_clear", spy)
        spies.append(spy)
    plugins_routes._clear_plugin_cache()
    for spy in spies:
        spy.assert_called_once()


def _download_clients_app(prefs):
    from fastapi import FastAPI

    from api.v1.routes import download_clients
    from core.dependencies import get_preferences_service

    app = FastAPI()
    app.include_router(download_clients.router)
    app.dependency_overrides[get_preferences_service] = lambda: prefs
    return app


def test_put_source_priority_clears_acquisition_dispatchers(monkeypatch):
    from core.dependencies import (
        get_acquisition_dispatcher,
        get_target_acquisition_dispatcher,
    )
    from middleware import _get_current_admin
    from tests.helpers import build_test_client, mock_admin_user

    spies = []
    for provider in (get_acquisition_dispatcher, get_target_acquisition_dispatcher):
        spy = MagicMock()
        monkeypatch.setattr(provider, "cache_clear", spy)
        spies.append(spy)
    prefs = MagicMock()
    prefs.get_source_priority.return_value = []
    prefs.save_source_priority.return_value = None
    app = _download_clients_app(prefs)
    app.dependency_overrides[_get_current_admin] = mock_admin_user
    response = build_test_client(app).put(
        "/download-clients/source-priority", json={"order": []}
    )
    assert response.status_code == 200
    for spy in spies:
        spy.assert_called_once()


def test_put_policy_clears_acquisition_dispatchers(monkeypatch):
    from api.v1.schemas.settings import DownloadPolicySettings
    from core.dependencies import (
        get_acquisition_dispatcher,
        get_target_acquisition_dispatcher,
    )
    from middleware import _get_current_admin
    from tests.helpers import build_test_client, mock_admin_user

    spies = []
    for provider in (get_acquisition_dispatcher, get_target_acquisition_dispatcher):
        spy = MagicMock()
        monkeypatch.setattr(provider, "cache_clear", spy)
        spies.append(spy)
    prefs = MagicMock()
    prefs.get_download_policy.return_value = DownloadPolicySettings()
    prefs.save_download_policy.return_value = None
    app = _download_clients_app(prefs)
    app.dependency_overrides[_get_current_admin] = mock_admin_user
    response = build_test_client(app).put(
        "/download-clients/policy", json={"preflight_score_auto_accept": 0.8}
    )
    assert response.status_code == 200
    for spy in spies:
        spy.assert_called_once()


# -- 8. transcode-via-temp: ffmpeg never fetches network --


@pytest.mark.asyncio
async def test_url_transcode_fetches_via_proxy_into_temp_file(monkeypatch):
    import services.compat.plugin_stream_service as pss
    from services.compat import transcode_service as ts

    payload = b"ID3" + bytes(4096)
    upstream = _FakeUpstream(
        headers={"content-type": "audio/mpeg"},
        chunks=(payload[:1000], payload[1000:]),
    )
    client = _FakeProxyClient(upstream)
    monkeypatch.setattr(pss, "get_plugin_stream_proxy_client", lambda: client)
    monkeypatch.setattr(ts, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(
        ts, "decide", lambda *args, **kwargs: SimpleNamespace(transcode=True)
    )
    seen = {}

    class _Transcode:
        async def stream(
            self, path, plan, *, principal, is_disconnected=None, estimate=False
        ):
            seen["path"] = path
            # The handed-off input is a real file holding the proxied bytes.
            assert Path(path).read_bytes() == payload
            from starlette.responses import StreamingResponse

            async def _out():
                yield b"transcoded"

            return StreamingResponse(_out(), media_type="audio/mpeg")

    response = await pss.stream_plugin_url_response(
        recording_mbid="mbid-1",
        user_id="u1",
        ref={"content_type": "audio/mpeg", "duration": 200.0},
        validated_url="http://93.184.216.34/file.mp3",
        requested_format="mp3",
        max_bitrate_kbps=128,
        force_original=False,
        start_seconds=0.0,
        settings=object(),
        concurrency=SimpleNamespace(),
        transcode=_Transcode(),
        range_header=None,
        is_disconnected=None,
    )
    assert response is not None
    assert not seen["path"].startswith("http")
    assert client.sends != []  # fetched through the capped proxy
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert body == b"transcoded"
    assert not Path(seen["path"]).exists()  # temp cleaned up after drain


# -- 9. connected-IP gate: the peer socket must be public --


def _proxied_upstream(ip):
    upstream = _FakeUpstream(
        headers={"content-type": "audio/mpeg"}, chunks=(b"evil",)
    )
    upstream.extensions = {
        "network_stream": SimpleNamespace(
            get_extra_info=lambda name: (ip, 80) if name == "server_addr" else None
        )
    }
    return upstream


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "::1", "10.1.2.3", "192.168.1.10", "169.254.169.254", "fe80::1"]
)
async def test_proxy_aborts_when_connected_ip_is_private(monkeypatch, ip):
    import services.compat.plugin_stream_service as pss

    upstream = _proxied_upstream(ip)
    client = _FakeProxyClient(upstream)
    monkeypatch.setattr(pss, "get_plugin_stream_proxy_client", lambda: client)
    result = await pss._proxy_direct("http://93.184.216.34/file.mp3", None, {})
    assert result is None
    assert upstream.closed is True
    assert len(client.sends) == 1  # connected first, then the peer gate fired


@pytest.mark.asyncio
async def test_proxy_allows_public_connected_ip(monkeypatch):
    import services.compat.plugin_stream_service as pss

    upstream = _proxied_upstream("93.184.216.34")
    upstream._chunks = [b"ok"]
    client = _FakeProxyClient(upstream)
    monkeypatch.setattr(pss, "get_plugin_stream_proxy_client", lambda: client)
    proxied = await pss._proxy_direct("http://93.184.216.34/file.mp3", None, {})
    assert proxied is not None


# -- 10. post-open containment: the open fd must stay inside the roots --


def test_post_open_within_roots_allows_contained_fd(tmp_path):
    import services.compat.plugin_stream_service as pss

    target = tmp_path / "tone.flac"
    target.write_bytes(b"audio")
    fd = os.open(target, os.O_RDONLY)
    try:
        assert pss._post_open_within_roots(fd, target, [tmp_path]) is True
        assert (
            pss._post_open_within_roots(fd, target, [tmp_path / "elsewhere"]) is False
        )
    finally:
        os.close(fd)


def test_post_open_within_roots_rejects_non_regular_file(tmp_path):
    import services.compat.plugin_stream_service as pss

    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        assert pss._post_open_within_roots(fd, tmp_path, [tmp_path]) is False
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_path_response_post_open_escape_returns_none_without_lease(
    tmp_path, monkeypatch
):
    import services.compat.plugin_stream_service as pss
    from services.compat import transcode_service as ts

    target = tmp_path / "tone.flac"
    target.write_bytes(b"audio-bytes")
    monkeypatch.setattr(
        ts, "decide", lambda *args, **kwargs: SimpleNamespace(transcode=False)
    )
    monkeypatch.setattr(
        pss, "_post_open_within_roots", lambda fd, original, roots: False
    )
    service = pss.PluginStreamService(None)
    monkeypatch.setattr(service, "allowed_roots", lambda: [tmp_path])
    concurrency = SimpleNamespace(acquire_direct=AsyncMock())
    response = await pss.stream_plugin_path_response(
        service=service,
        recording_mbid="mbid-1",
        user_id="u1",
        validated_path=target,
        ref={},
        requested_format=None,
        max_bitrate_kbps=None,
        force_original=True,
        start_seconds=0.0,
        settings=object(),
        concurrency=concurrency,
        transcode=object(),
        range_header=None,
        is_disconnected=None,
    )
    assert response is None
    concurrency.acquire_direct.assert_not_called()


@pytest.mark.asyncio
async def test_iter_validated_file_post_open_escape_yields_nothing(
    tmp_path, monkeypatch
):
    import services.compat.plugin_stream_service as pss

    target = tmp_path / "tone.flac"
    target.write_bytes(b"audio-bytes")
    monkeypatch.setattr(
        pss, "_post_open_within_roots", lambda fd, original, roots: False
    )
    seen = [chunk async for chunk in pss._iter_validated_file(target, 0, 11, [tmp_path])]
    assert seen == []


@pytest.mark.asyncio
async def test_iter_validated_file_streams_range_when_contained(tmp_path):
    import services.compat.plugin_stream_service as pss

    target = tmp_path / "tone.flac"
    target.write_bytes(b"0123456789abcdef")
    seen = [chunk async for chunk in pss._iter_validated_file(target, 4, 6, [tmp_path])]
    assert b"".join(seen) == b"456789"


# -- 11. lease after validation + bounded DNS --


@pytest.mark.asyncio
async def test_direct_proxy_acquires_lease_only_after_validation(monkeypatch):
    import services.compat.plugin_stream_service as pss
    from services.compat import transcode_service as ts

    monkeypatch.setattr(
        ts, "decide", lambda *args, **kwargs: SimpleNamespace(transcode=False)
    )

    async def _direct(**kwargs):
        return await pss.stream_plugin_url_response(
            recording_mbid="mbid-1",
            user_id="u1",
            ref={},
            requested_format=None,
            max_bitrate_kbps=None,
            force_original=True,
            start_seconds=0.0,
            settings=object(),
            transcode=object(),
            range_header=None,
            is_disconnected=None,
            **kwargs,
        )

    # Blocked upstream (loopback literal): miss, and no lease is consumed.
    concurrency = SimpleNamespace(acquire_direct=AsyncMock())
    missed = await _direct(
        validated_url="http://127.0.0.1/file.mp3", concurrency=concurrency
    )
    assert missed is None
    concurrency.acquire_direct.assert_not_called()

    # Healthy upstream: exactly one lease, then bytes flow.
    upstream = _FakeUpstream(
        headers={"content-type": "audio/mpeg"}, chunks=(b"ok",)
    )
    client = _FakeProxyClient(upstream)
    monkeypatch.setattr(pss, "get_plugin_stream_proxy_client", lambda: client)
    lease = SimpleNamespace(release=AsyncMock())
    concurrency = SimpleNamespace(acquire_direct=AsyncMock(return_value=lease))
    response = await _direct(
        validated_url="http://93.184.216.34/file.mp3", concurrency=concurrency
    )
    assert response is not None and response.status_code == 200
    concurrency.acquire_direct.assert_awaited_once_with("u1")


@pytest.mark.asyncio
async def test_dns_resolution_has_timeout(monkeypatch):
    import time

    import services.compat.plugin_stream_service as pss

    monkeypatch.setattr(pss, "_DNS_TIMEOUT_S", 0.05)

    def _slow(*args, **kwargs):
        time.sleep(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", _slow)
    started = time.monotonic()
    assert await pss._resolve_host_ips("slow.test", 80) is None
    assert time.monotonic() - started < 4
