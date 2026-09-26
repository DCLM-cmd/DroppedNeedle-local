"""Publisher ingress + /ext route guards at the host boundary.

Publisher allowlist, strict per-kind schema, per-principal rate isolation,
unspoofable source stamping with no existence oracle, the depth-1 +
causation-dedup loop guard, and the non-auth /ext matrix (shape, timeout,
status clamp, oversize body, generic failures) including the route layer's
request cap (413) and crash mapping (generic 500).
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.v1.schemas.settings import PluginConfig
from infrastructure.plugins.host import PluginHost, _EVENT_DEPTH
from infrastructure.plugins.protocols import PluginEvent

ECHO_CODE = """
from infrastructure.plugins.protocols import PluginRouteResponse


class Toy:
    def __init__(self, context):
        self.ctx = context
        self.seen = []

    async def on_event(self, event):
        self.seen.append((event.kind, event.causation_id))
        await self.ctx.publish(
            "download_note",
            {"task_id": "task-a", "note": "echo"},
            causation_id=event.causation_id or None,
        )

    async def handle_route(self, method, subpath, query, body):
        return PluginRouteResponse(status=200, body={"ok": True})
"""

ROUTE_CODE = """
import asyncio

from infrastructure.plugins.protocols import PluginRouteResponse


class Toy:
    def __init__(self, context):
        self.ctx = context

    async def on_event(self, event):
        return None

    async def handle_route(self, method, subpath, query, body):
        mode = (self.ctx.settings.get("mode") or "ok").strip()
        if subpath == "status":
            return PluginRouteResponse(status=200, body={"mode": mode})
        if mode == "hang":
            await asyncio.sleep(10)
            return PluginRouteResponse(status=200, body={})
        if mode == "boom":
            raise RuntimeError("toy blew up")
        if mode == "big":
            return PluginRouteResponse(status=200, body={"blob": "x" * (1024 * 1024 + 1)})
        if mode == "status-500":
            return PluginRouteResponse(status=500, body={"x": 1})
        if mode == "status-302":
            return PluginRouteResponse(status=302, body={"x": 1})
        if mode == "status-400":
            return PluginRouteResponse(
                status=400,
                body={"error": {"code": "BAD", "message": "bad", "details": None}},
            )
        if mode == "status-404":
            return PluginRouteResponse(
                status=404,
                body={"error": {"code": "NOT_FOUND", "message": "Not found", "details": None}},
            )
        if mode == "status-201":
            return PluginRouteResponse(status=201, body={"created": True})
        if mode == "unserialisable":
            return PluginRouteResponse(status=200, body={"bad": object()})
        return PluginRouteResponse(status=200, body={"ok": True, "method": method, "subpath": subpath})
"""

SUB_ONLY_CODE = """
class Toy:
    def __init__(self, context):
        self.ctx = context

    async def on_event(self, event):
        return None
"""

_ROUTES = """
[[route]]
path = "note"
method = "POST"
auth = "user"

[[route]]
path = "note"
method = "DELETE"
auth = "user"

[[route]]
path = "status"
method = "GET"
auth = "user"
"""


def _manifest(name: str, caps: str, routes: bool = True) -> str:
    doc = (
        "[plugin]\n"
        f'name = "{name}"\n'
        'version = "1.0.0"\n'
        "api_version = 1\n"
        'entrypoint = "plugin:Toy"\n'
        f"capabilities = [{caps}]\n"
    )
    return doc + (_ROUTES if routes else "")


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


def _host(tmp_path: Path, specs: dict[str, tuple[str, str]], settings: dict[str, dict] | None = None) -> PluginHost:
    prefs = FakePrefs()
    for name, (manifest, code) in specs.items():
        _write_plugin(tmp_path, name, manifest, code)
        prefs.enable(name, (settings or {}).get(name))
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    return host


def _pub_host(tmp_path: Path, *names: str) -> PluginHost:
    return _host(tmp_path, {name: (_manifest(name, '"subscriber", "publisher"'), ECHO_CODE) for name in names})


# -- publisher allowlist + strict schema --


def test_unknown_publish_kind_raises_loudly(tmp_path):
    host = _pub_host(tmp_path, "pub")
    with pytest.raises(ValueError, match="unknown publish kind"):
        host.publish_from_plugin("pub", "grant_admin", {"anything": 1})


@pytest.mark.parametrize(
    ("kind", "payload", "ok", "status"),
    [
        ("download_note", {"task_id": "t", "note": "n"}, True, 200),
        ("download_note", {"task_id": "t"}, True, 200),
        ("download_note", {}, False, 422),
        ("download_note", {"task_id": "t", "note": "n", "source_plugin": "evil"}, False, 422),
        ("download_note", ["nope"], False, 422),
        ("download_note", {"task_id": "t", "note": "x" * 1025}, False, 422),
        ("indexer_invalidate", {"target_source": "plugin:x"}, True, 200),
        ("indexer_invalidate", {}, False, 422),
        ("plugin_notice", {"title": "t", "body": "b"}, True, 200),
        ("plugin_notice", {"title": "t"}, True, 200),
    ],
)
def test_publish_schema_is_strict_per_kind(tmp_path, kind, payload, ok, status):
    host = _pub_host(tmp_path, "pub")
    result = host.publish_from_plugin("pub", kind, payload)
    assert result.ok is ok and result.status == status
    assert (host.drain_published() != []) is ok


def test_publish_without_publisher_capability_is_404(tmp_path):
    host = _host(
        tmp_path,
        {
            "sub": (_manifest("sub", '"subscriber"', routes=False), SUB_ONLY_CODE),
            "pub": (_manifest("pub", '"subscriber", "publisher"'), ECHO_CODE),
        },
    )
    result = host.publish_from_plugin("sub", "download_note", {"task_id": "t", "note": "n"})
    assert result.ok is False and result.status == 404
    unknown = host.publish_from_plugin("ghost", "download_note", {"task_id": "t", "note": "n"})
    assert unknown.ok is False and unknown.status == 404
    assert host.drain_published() == []


def test_publish_from_disabled_plugin_is_404(tmp_path):
    prefs = FakePrefs()
    _write_plugin(tmp_path, "pub", _manifest("pub", '"publisher"', routes=False), SUB_ONLY_CODE)
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    result = host.publish_from_plugin("pub", "plugin_notice", {"title": "t", "body": "b"})
    assert result.ok is False and result.status == 404


# -- rate isolation + queue bound --


def test_publish_rate_limit_is_per_principal(tmp_path):
    host = _pub_host(tmp_path, "pub-a", "pub-b")
    for _ in range(30):
        assert host.publish_from_plugin("pub-a", "plugin_notice", {"title": "t", "body": "b"}, principal="alice").ok
    limited = host.publish_from_plugin("pub-a", "plugin_notice", {"title": "t", "body": "b"}, principal="alice")
    assert limited.ok is False and limited.status == 429
    assert isinstance(limited.retry_after, int) and limited.retry_after >= 1
    assert limited.error == "rate_limited"
    assert host.publish_from_plugin("pub-a", "plugin_notice", {"title": "t", "body": "b"}, principal="bob").ok
    assert host.publish_from_plugin("pub-b", "plugin_notice", {"title": "t", "body": "b"}, principal="alice").ok


def test_publish_queue_drops_oldest_when_full(tmp_path):
    host = _pub_host(tmp_path, "pub")
    for index in range(105):
        result = host.publish_from_plugin(
            "pub", "plugin_notice", {"title": f"t{index}", "body": "b"}, principal=f"user-{index}"
        )
        assert result.ok
    records = host.drain_published()
    assert len(records) == 100 and host.publish_dropped_count == 5
    assert records[0]["payload"] == {"title": "t5", "body": "b"}
    assert host.drain_published() == []


# -- ownership stamp without oracle + depth + dedup loop guard --


def test_download_note_is_stamped_without_task_oracle(tmp_path):
    host = _pub_host(tmp_path, "pub")
    result = host.publish_from_plugin("pub", "download_note", {"task_id": "no-such-task", "note": "hi"})
    assert result.ok is True and result.status == 200
    (record,) = host.drain_published()
    assert record["source_plugin"] == "pub"
    assert record["payload"] == {"task_id": "no-such-task", "note": "hi"}


def test_publish_beyond_depth_1_is_dropped(tmp_path):
    host = _pub_host(tmp_path, "pub")
    token = _EVENT_DEPTH.set(2)
    try:
        deep = host.publish_from_plugin("pub", "download_note", {"task_id": "t", "note": "n"})
    finally:
        _EVENT_DEPTH.reset(token)
    assert deep.ok is False and deep.status == 409
    assert host.drain_published() == []

    token = _EVENT_DEPTH.set(1)
    try:
        shallow = host.publish_from_plugin("pub", "download_note", {"task_id": "t", "note": "n"})
    finally:
        _EVENT_DEPTH.reset(token)
    assert shallow.ok is True
    (record,) = host.drain_published()
    assert record["depth"] == 2


@pytest.mark.asyncio
async def test_bounded_causation_dedup_breaks_subscriber_publish_loops(tmp_path):
    host = _pub_host(tmp_path, "echo")
    plugin = host.get("echo")
    assert plugin is not None and plugin.instance is not None

    await host.dispatch_event(PluginEvent(kind="download_note", payload={"task_id": "task-a"}, causation_id="loop-1"))
    await asyncio.sleep(0.3)
    await host.dispatch_event(PluginEvent(kind="download_note", payload={"task_id": "task-a"}, causation_id="loop-1"))
    await asyncio.sleep(0.3)
    assert plugin.instance.seen == [("download_note", "loop-1")]
    assert len(host.drain_published()) == 1

    await host.dispatch_event(PluginEvent(kind="download_note", payload={"task_id": "task-a"}, causation_id="loop-2"))
    await asyncio.sleep(0.3)
    assert len(plugin.instance.seen) == 2

ROTATE_CODE = """
import uuid


class Toy:
    def __init__(self, context):
        self.ctx = context
        self.seen = []

    async def on_event(self, event):
        self.seen.append((event.kind, event.causation_id))
        await self.ctx.publish(
            "download_note",
            {"task_id": "task-a", "note": "echo"},
            causation_id=uuid.uuid4().hex,
        )

    async def handle_route(self, method, subpath, query, body):
        from infrastructure.plugins.protocols import PluginRouteResponse

        return PluginRouteResponse(status=200, body={"ok": True})
"""


def _rotate_host(tmp_path: Path, *names: str) -> PluginHost:
    return _host(
        tmp_path,
        {name: (_manifest(name, '"subscriber", "publisher"'), ROTATE_CODE) for name in names},
    )


@pytest.mark.asyncio
async def test_derived_causation_breaks_rotating_ping_pong(tmp_path):
    """Two subscribers that rotate the causation on every publish cannot
    ping-pong: a depth>0 publish inherits the parent causation, so the
    re-dispatch dedups against both plugins that already saw the parent."""
    host = _rotate_host(tmp_path, "ping", "pong")

    await host.dispatch_event(
        PluginEvent(kind="download_note", payload={"task_id": "task-a"}, causation_id="seed-1")
    )
    await asyncio.sleep(0.3)
    assert host.get("ping").instance.seen == [("download_note", "seed-1")]
    assert host.get("pong").instance.seen == [("download_note", "seed-1")]
    records = host.drain_published()
    assert len(records) == 2
    assert {record["causation_id"] for record in records} == {"seed-1"}

    for record in records:
        await host.dispatch_event(
            PluginEvent(
                kind=record["kind"], payload=record["payload"], causation_id=record["causation_id"]
            )
        )
    await asyncio.sleep(0.3)
    assert host.get("ping").instance.seen == [("download_note", "seed-1")]
    assert host.get("pong").instance.seen == [("download_note", "seed-1")]
    assert host.drain_published() == []


# -- /ext route matrix at the host boundary --


@pytest.mark.asyncio
async def test_undeclared_paths_methods_and_disabled_plugins_share_one_404(tmp_path):
    host = _host(tmp_path, {"ext": (_manifest("ext", '"subscriber", "publisher"'), ROUTE_CODE)})
    bodies = [
        (await host.handle_plugin_route("ext", "GET", "missing", {}, None)).body,
        (await host.handle_plugin_route("ext", "PUT", "note", {}, None)).body,
        (await host.handle_plugin_route("ghost", "POST", "note", {}, None)).body,
    ]
    statuses = [
        (await host.handle_plugin_route("ext", "GET", "missing", {}, None)).status,
        (await host.handle_plugin_route("ext", "PUT", "note", {}, None)).status,
        (await host.handle_plugin_route("ghost", "POST", "note", {}, None)).status,
    ]
    assert statuses == [404, 404, 404]
    assert bodies[0] == bodies[1] == bodies[2] == {
        "error": {"code": "NOT_FOUND", "message": "Not found", "details": None}
    }


@pytest.mark.asyncio
async def test_ext_delete_passes_when_declared(tmp_path):
    host = _host(tmp_path, {"ext": (_manifest("ext", '"subscriber", "publisher"'), ROUTE_CODE)})
    result = await host.handle_plugin_route("ext", "DELETE", "note", {}, None)
    assert result.status == 200 and result.body["method"] == "DELETE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("status-201", 201),
        ("status-400", 400),
        ("status-404", 404),
        ("status-302", 200),
        ("status-500", 502),
    ],
)
async def test_ext_status_clamp(tmp_path, mode, status):
    prefs = FakePrefs()
    _write_plugin(tmp_path, "ext", _manifest("ext", '"subscriber", "publisher"'), ROUTE_CODE)
    prefs.enable("ext", {"mode": mode})
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    result = await host.handle_plugin_route("ext", "POST", "note", {}, None)
    assert result.status == status


@pytest.mark.asyncio
async def test_ext_hung_route_maps_to_generic_502(tmp_path):
    prefs = FakePrefs()
    _write_plugin(tmp_path, "ext", _manifest("ext", '"subscriber", "publisher"'), ROUTE_CODE)
    prefs.enable("ext", {"mode": "hang"})
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    result = await host.handle_plugin_route("ext", "POST", "note", {}, None)
    assert result.status == 502
    assert result.body == {
        "error": {"code": "EXTERNAL_SERVICE_UNAVAILABLE", "message": "Plugin route failed", "details": None}
    }


@pytest.mark.asyncio
async def test_ext_oversize_response_body_maps_to_502(tmp_path):
    prefs = FakePrefs()
    _write_plugin(tmp_path, "ext", _manifest("ext", '"subscriber", "publisher"'), ROUTE_CODE)
    prefs.enable("ext", {"mode": "big"})
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    result = await host.handle_plugin_route("ext", "POST", "note", {}, None)
    assert result.status == 502


@pytest.mark.asyncio
async def test_ext_exception_and_unserialisable_body_never_leak(tmp_path):
    prefs = FakePrefs()
    _write_plugin(tmp_path, "ext", _manifest("ext", '"subscriber", "publisher"'), ROUTE_CODE)
    prefs.enable("ext", {"mode": "boom"})
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()

    result = await host.handle_plugin_route("ext", "POST", "note", {}, None)
    assert result.status == 502
    assert "Traceback" not in str(result.body) and "toy blew up" not in str(result.body)

    prefs.configs["ext"].settings["mode"] = "unserialisable"
    result = await host.handle_plugin_route("ext", "POST", "note", {}, None)
    assert result.status == 502


# -- route-layer guards: auth first, request cap, generic 500 --


def _fake_request(body: bytes):
    async def _body() -> bytes:
        return body

    return SimpleNamespace(body=_body, query_params={})


@pytest.mark.asyncio
async def test_ext_rejects_unauthenticated_and_traversal_before_touching_plugins(tmp_path, monkeypatch):
    import api.v1.routes.plugins as ext
    from core.exceptions import ValidationError
    from fastapi import HTTPException

    host = _host(tmp_path, {"ext": (_manifest("ext", '"subscriber", "publisher"'), ROUTE_CODE)})
    monkeypatch.setattr(ext, "get_plugin_host", lambda: host)
    request = _fake_request(b"{}")
    with pytest.raises(HTTPException) as unauthed:
        await ext._serve_ext(request, None, "ext", "status", "GET")
    assert unauthed.value.status_code == 401
    with pytest.raises(ValidationError):
        await ext._serve_ext(request, SimpleNamespace(id="u1", role="user"), "ext", "../escape", "GET")
    assert host.drain_published() == []


@pytest.mark.asyncio
async def test_ext_post_body_over_cap_is_413(tmp_path, monkeypatch):
    import api.v1.routes.plugins as ext

    host = _host(tmp_path, {"ext413": (_manifest("ext413", '"subscriber", "publisher"'), ROUTE_CODE)})
    monkeypatch.setattr(ext, "get_plugin_host", lambda: host)
    request = _fake_request(b"x" * (1024 * 1024 + 1))
    response = await ext._serve_ext(request, SimpleNamespace(id="u1", role="user"), "ext413", "note", "POST")
    assert response.status_code == 413
    assert b"PAYLOAD_TOO_LARGE" in response.body


@pytest.mark.asyncio
async def test_ext_rate_limited_returns_429_with_retry_after(tmp_path, monkeypatch):
    import api.v1.routes.plugins as ext

    host = _host(tmp_path, {"extrl": (_manifest("extrl", '"subscriber", "publisher"'), ROUTE_CODE)})
    monkeypatch.setattr(ext, "get_plugin_host", lambda: host)
    user = SimpleNamespace(id="u-rl", role="user")
    response = None
    for _ in range(61):
        response = await ext._serve_ext(_fake_request(b""), user, "extrl", "status", "GET")
    assert response is not None and response.status_code == 429
    assert response.headers["retry-after"]
    assert b"RATE_LIMITED" in response.body


@pytest.mark.asyncio
async def test_ext_host_crash_is_generic_500(tmp_path, monkeypatch):
    import api.v1.routes.plugins as ext

    host = _host(tmp_path, {"ext500": (_manifest("ext500", '"subscriber", "publisher"'), ROUTE_CODE)})
    monkeypatch.setattr(ext, "get_plugin_host", lambda: host)

    async def _boom(*args, **kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(host, "handle_plugin_route", _boom)
    response = await ext._serve_ext(
        _fake_request(b""), SimpleNamespace(id="u2", role="user"), "ext500", "status", "GET"
    )
    assert response.status_code == 500
    assert b"INTERNAL_ERROR" in response.body
    assert b"db gone" not in response.body
