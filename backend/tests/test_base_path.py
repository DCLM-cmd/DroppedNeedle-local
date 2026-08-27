"""Base-path contract: strict normalizer, raw-ASGI middleware, Settings wiring."""

import json
from typing import Any

import pytest
from pydantic import ValidationError
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from core.base_path import (
    MAX_BASE_PATH_LENGTH,
    BasePathError,
    BasePathMiddleware,
    application_path,
    normalize_base_path,
    scope_base_path,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("/music", "/music"),
        ("/music/app", "/music/app"),
        ("/a/b/c", "/a/b/c"),
        ("/_private.2~x-", "/_private.2~x-"),
        (
            "/" + "a" * (MAX_BASE_PATH_LENGTH - 1),
            "/" + "a" * (MAX_BASE_PATH_LENGTH - 1),
        ),
    ],
)
def test_normalize_accepts_canonical_values(raw: str | None, expected: str) -> None:
    assert normalize_base_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "music",
        "/",
        "/music/",
        "//",
        "//host",
        "x/",
        ".",
        "..",
        "/.",
        "/..",
        "/./x",
        "/a/../b",
        "/a/./b",
        "/%20",
        "/mu%73ic",
        "\\music",
        "/mu\\sic",
        "?q",
        "#f",
        " /music",
        "/music ",
        "/mu\t sic",
        "/mu\nsic",
        "/x\x00",
        "/x\x7f",
        "/caf\xc3\xa9",
        "/\U0001f3b5",
        "/" + "a" * MAX_BASE_PATH_LENGTH,
    ],
)
def test_normalize_rejects_non_canonical_values(raw: str) -> None:
    with pytest.raises(BasePathError):
        normalize_base_path(raw)


def test_base_path_error_names_the_violation_class() -> None:
    with pytest.raises(BasePathError, match="must be an absolute path"):
        normalize_base_path("music")
    with pytest.raises(BasePathError, match="must not end with"):
        normalize_base_path("/music/")
    with pytest.raises(BasePathError, match="empty path segment"):
        normalize_base_path("//host")
    with pytest.raises(BasePathError, match=r"\.\."):
        normalize_base_path("/a/../b")
    with pytest.raises(BasePathError, match="allowed characters"):
        normalize_base_path("/café")
    assert issubclass(BasePathError, ValueError)


def test_scope_base_path_uses_fallback_and_degrades_invalid_root() -> None:
    assert scope_base_path({"root_path": "/music"}, "/fallback") == "/music"
    assert scope_base_path({"root_path": ""}, "/fallback") == "/fallback"
    assert scope_base_path({"root_path": "/invalid/"}, "/fallback") == ""


class RecordingApp:
    """Inner ASGI app that records the scopes it receives."""

    def __init__(
        self,
        body: bytes = b"ok",
        exc: BaseException | None = None,
    ) -> None:
        self.scopes: list[dict[str, Any]] = []
        self.body = body
        self.exc = exc

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        self.scopes.append(scope)
        if self.exc is not None:
            raise self.exc
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": self.body})


def _http_scope(path: str, **extra: Any) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"example.test")],
    }
    scope.update(extra)
    return scope


def _ws_scope(path: str, **extra: Any) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "websocket",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "subprotocols": [],
    }
    scope.update(extra)
    return scope


async def _run(inner: RecordingApp, base_path: str, scope: dict) -> list[dict]:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    await BasePathMiddleware(inner, base_path=base_path)(scope, receive, send)
    return sent


def _body_of(sent: list[dict]) -> bytes:
    return b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )


def _start_of(sent: list[dict]) -> dict:
    return next(m for m in sent if m["type"] == "http.response.start")


HOUSE_404 = {"error": {"code": "NOT_FOUND", "message": "Not found", "details": None}}


@pytest.mark.asyncio
async def test_prefixed_request_sets_root_path_and_preserves_full_path() -> None:
    inner = RecordingApp()
    scope = _http_scope("/music/artists")
    sent = await _run(inner, "/music", scope)

    seen = inner.scopes[0]
    assert seen["path"] == "/music/artists"
    assert application_path(seen) == "/artists"
    assert seen["root_path"] == "/music"
    assert seen["method"] == "GET"
    assert seen["headers"] == [(b"host", b"example.test")]
    assert _start_of(sent)["status"] == 200
    assert scope["path"] == "/music/artists"
    assert scope["raw_path"] == b"/music/artists"


@pytest.mark.asyncio
async def test_stale_raw_path_is_dropped_from_forwarded_scope() -> None:
    inner = RecordingApp()
    await _run(inner, "/music", _http_scope("/music/x", query_string=b"a=1"))

    seen = inner.scopes[0]
    assert "raw_path" not in seen
    assert seen["query_string"] == b"a=1"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/music", "/music/"])
async def test_base_exact_paths_map_to_root(path: str) -> None:
    inner = RecordingApp()
    await _run(inner, "/music", _http_scope(path))
    assert application_path(inner.scopes[0]) == "/"


@pytest.mark.asyncio
async def test_pre_existing_root_path_gets_base_appended_once() -> None:
    inner = RecordingApp()
    await _run(inner, "/music", _http_scope("/sub/music/health", root_path="/sub"))
    assert inner.scopes[0]["root_path"] == "/sub/music"
    assert application_path(inner.scopes[0]) == "/health"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api/v1/artists",
        "/foobar",
        "/musics",
        "/music.exe",
        "/MUSIC",
        "/music2/x",
    ],
)
async def test_unmatched_requests_get_house_envelope_before_inner(path: str) -> None:
    inner = RecordingApp()
    sent = await _run(inner, "/music", _http_scope(path))

    assert not inner.scopes
    start = _start_of(sent)
    assert start["status"] == 404
    content_type = dict(start["headers"])[b"content-type"]
    assert content_type.startswith(b"application/json")
    assert json.loads(_body_of(sent)) == HOUSE_404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_path",
    [b"/%6dusic/health", b"/music%2Fhealth", b"/music%2fhealth"],
)
async def test_encoded_base_prefix_spellings_are_rejected(raw_path: bytes) -> None:
    inner = RecordingApp()
    sent = await _run(
        inner,
        "/music",
        _http_scope("/music/health", raw_path=raw_path),
    )

    assert not inner.scopes
    assert _start_of(sent)["status"] == 404
    assert json.loads(_body_of(sent)) == HOUSE_404


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/music/music", "/music/music/", "/music/music/x"])
async def test_double_prefixed_paths_are_rejected(path: str) -> None:
    inner = RecordingApp()
    sent = await _run(inner, "/music", _http_scope(path))

    assert not inner.scopes
    start = _start_of(sent)
    assert start["status"] == 404
    content_type = dict(start["headers"])[b"content-type"]
    assert content_type.startswith(b"application/json")
    assert json.loads(_body_of(sent)) == HOUSE_404


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/music/../x", "/music/./y", "/music/a/../b"])
async def test_dot_segments_after_base_are_rejected(path: str) -> None:
    inner = RecordingApp()
    sent = await _run(inner, "/music", _http_scope(path))

    assert not inner.scopes
    assert _start_of(sent)["status"] == 404
    assert json.loads(_body_of(sent)) == HOUSE_404


@pytest.mark.asyncio
async def test_empty_base_is_identity_passthrough() -> None:
    inner = RecordingApp(body=b"passthrough")
    scope = _http_scope("/odd//path/../thing", extra_key=123)
    sent = await _run(inner, "", scope)

    assert inner.scopes == [scope]
    assert inner.scopes[0] is scope
    assert _body_of(sent) == b"passthrough"


@pytest.mark.asyncio
async def test_lifespan_passthrough_even_with_base() -> None:
    inner = RecordingApp()

    async def receive() -> dict:
        return {"type": "lifespan.startup"}

    await BasePathMiddleware(inner, base_path="/music")(
        {"type": "lifespan"}, receive, lambda m: None
    )
    assert inner.scopes == [{"type": "lifespan"}]


@pytest.mark.asyncio
async def test_prefixed_websocket_is_forwarded() -> None:
    inner = RecordingApp()
    await _run(inner, "/music", _ws_scope("/music/ws"))
    seen = inner.scopes[0]
    assert seen["type"] == "websocket"
    assert seen["path"] == "/music/ws"
    assert application_path(seen) == "/ws"
    assert seen["root_path"] == "/music"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/other/ws", "/music2"])
async def test_unmatched_websocket_closes_1008_before_inner(path: str) -> None:
    inner = RecordingApp()
    sent = await _run(inner, "/music", _ws_scope(path))

    assert not inner.scopes
    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.asyncio
async def test_inner_exceptions_propagate() -> None:
    inner = RecordingApp(exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await _run(inner, "/music", _http_scope("/music/x"))


def test_application_path_is_segment_aware() -> None:
    assert application_path({"path": "/music/api", "root_path": "/music"}) == "/api"
    assert application_path({"path": "/music", "root_path": "/music"}) == "/"
    assert (
        application_path({"path": "/museum/api", "root_path": "/music"})
        == "/museum/api"
    )
    assert application_path({"path": "/api", "root_path": ""}) == "/api"


def test_prefixed_static_mount_serves_assets(tmp_path) -> None:
    (tmp_path / "app.js").write_text("console.log('ok')", encoding="utf-8")
    app = FastAPI()
    app.mount("/assets", StaticFiles(directory=tmp_path))
    client = TestClient(BasePathMiddleware(app, "/music"))

    response = client.get("/music/assets/app.js")
    assert response.status_code == 200
    assert response.text == "console.log('ok')"
    assert client.get("/assets/app.js").status_code == 404
    assert client.get("/music/music/assets/app.js").status_code == 404


def _settings(**kwargs: Any):
    from core.config import Settings

    return Settings(_env_file=None, **kwargs)


def test_settings_base_path_defaults_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BASE_PATH", raising=False)
    assert _settings().base_path == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", ""), ("/music", "/music"), ("/a/b", "/a/b")],
)
def test_settings_normalizes_valid_base_path_env(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("BASE_PATH", value)
    assert _settings().base_path == expected


@pytest.mark.parametrize("bad", ["music", "/music/", "//", "/a/../b", "/café", "?"])
def test_settings_rejects_invalid_base_path_env(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("BASE_PATH", bad)
    with pytest.raises(ValidationError) as excinfo:
        _settings()
    assert "Invalid BASE_PATH" in str(excinfo.value)


def test_max_base_path_length_constant() -> None:
    assert MAX_BASE_PATH_LENGTH == 256


def test_module_imports_are_cycle_free() -> None:
    import core.config
    import target_application

    assert hasattr(core.config.Settings.model_fields, "__getitem__")
    assert "base_path" in core.config.Settings.model_fields
    assert hasattr(target_application, "create_production_target_application")
