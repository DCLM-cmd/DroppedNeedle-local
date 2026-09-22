"""Plugin routes: admin list/install/update/uninstall, with the secret mask-sentinel.

v1 content surface: read-only GET /plugins/sources plus plugin HTTP under
/plugins/ext/{name}/{subpath} (GET+POST+DELETE)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter, FastAPI, HTTPException

from api.v1.routes import plugins as plugins_routes
from api.v1.schemas.plugins import PLUGIN_SECRET_MASK
from api.v1.schemas.settings import PluginConfig
from core.dependencies import get_plugin_host, get_preferences_service
from infrastructure.plugins.host import LoadedPlugin, PluginRouteResult
from infrastructure.plugins.manifest import (
    PluginCapabilityConfig,
    PluginManifest,
    PluginRouteSpec,
    PluginSettingField,
)
from middleware import _get_current_admin, _get_current_user
from tests.helpers import build_test_client, mock_admin_user, mock_user


def _plugin(name: str = "demo", capabilities: list[str] | None = None) -> LoadedPlugin:
    return LoadedPlugin(
        manifest=PluginManifest(
            name=name,
            version="1.0.0",
            api_version=0,
            entrypoint="plugin:Demo",
            capabilities=capabilities or ["scrobbler"],
            display_name="Demo",
            settings=[
                PluginSettingField(key="url", label="URL"),
                PluginSettingField(key="token", label="Token", secret=True),
            ],
        ),
        enabled=True,
        active_capabilities=capabilities or ["scrobbler"],
    )


@pytest.fixture
def harness(tmp_path):
    host = MagicMock()
    plugin = _plugin()
    host.list_plugins = MagicMock(return_value=[plugin])
    host.get = MagicMock(return_value=plugin)
    host.load_all = MagicMock()
    prefs = MagicMock()
    prefs.get_plugin_config = MagicMock(
        return_value=PluginConfig(enabled=True, settings={"url": "https://x", "token": "s3cret"})
    )
    prefs.save_plugin_config = MagicMock()

    app = FastAPI()
    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(plugins_routes.router)
    app.include_router(v1)
    app.dependency_overrides[get_plugin_host] = lambda: host
    app.dependency_overrides[get_preferences_service] = lambda: prefs
    app.dependency_overrides[_get_current_admin] = mock_admin_user
    return build_test_client(app), host, prefs


def test_list_masks_secret_settings(harness):
    client, _, _ = harness
    response = client.get("/api/v1/plugins")
    assert response.status_code == 200
    plugin = response.json()["plugins"][0]
    assert plugin["settings_values"]["url"] == "https://x"
    assert plugin["settings_values"]["token"] == PLUGIN_SECRET_MASK
    assert "s3cret" not in response.text


def test_update_keeps_secret_when_mask_returned(harness):
    client, host, prefs = harness
    response = client.put(
        "/api/v1/plugins/demo",
        json={"enabled": True, "settings": {"url": "https://new", "token": PLUGIN_SECRET_MASK}},
    )
    assert response.status_code == 200
    saved = prefs.save_plugin_config.call_args.args[1]
    assert saved.settings["url"] == "https://new"
    assert saved.settings["token"] == "s3cret"  # mask means keep-existing
    host.load_all.assert_called_once()
    host.sync_ticks.assert_called_once()


def test_update_unknown_plugin_404s(harness):
    client, host, _ = harness
    host.get = MagicMock(return_value=None)
    response = client.put("/api/v1/plugins/nope", json={"enabled": False, "settings": {}})
    assert response.status_code == 404


def test_plugin_content_surface_is_sources_plus_ext(harness):
    """v1 content surface: read-only GET /plugins/sources plus plugin HTTP
    under /plugins/ext (GET+POST+DELETE). The old D22 search/fetch probes stay
    404: no route acquires content on a plugin's behalf."""
    client, _, _ = harness
    by_path: dict[str, set[str]] = {}
    for route in plugins_routes.router.routes:
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", set()) or set())
        by_path.setdefault(path, set()).update(methods)
    assert by_path.get("/plugins/sources") == {"GET"}
    assert by_path.get("/plugins/ext/{plugin_name}/{subpath:path}") == {"GET", "POST", "DELETE"}
    assert not [p for p in by_path if "search" in p or "fetch" in p], by_path

    assert client.post("/api/v1/plugins/sources/x/search", json={"query": "q"}).status_code == 404
    assert client.post("/api/v1/plugins/sources/x/fetch", json={"item_id": "i"}).status_code == 404


def test_install_forwards_to_host_and_returns_disabled_plugin(harness, monkeypatch):
    client, host, _ = harness
    installed = _plugin("fresh")
    installed.enabled = False
    host.install_from_github = AsyncMock(return_value="fresh")
    host.get = MagicMock(return_value=installed)

    response = client.post(
        "/api/v1/plugins/install", json={"repository_url": "https://github.com/o/r"}
    )
    assert response.status_code == 201
    assert response.json()["enabled"] is False
    assert host.install_from_github.await_args.args[0] == "https://github.com/o/r"


def test_install_surfaces_a_bad_url_as_400(harness):
    from infrastructure.plugins.host import PluginInstallError

    client, host, _ = harness
    host.install_from_github = AsyncMock(side_effect=PluginInstallError("Enter a public GitHub URL"))

    response = client.post("/api/v1/plugins/install", json={"repository_url": "nope"})
    assert response.status_code == 400
    assert "GitHub" in response.json()["error"]["message"]


def test_uninstall_calls_the_host(harness):
    client, host, _ = harness
    host.uninstall = MagicMock()

    response = client.delete("/api/v1/plugins/demo")
    assert response.status_code == 200
    host.uninstall.assert_called_once_with("demo")


def _deny_admin():
    raise HTTPException(status_code=403, detail="Admin access required")


def _plugin_client(host, *, user=None, admin=None):
    """A plugins-router client with explicit auth overrides (the shared harness
    only overrides the admin dep, but /sources and /ext hang off the user dep)."""
    app = FastAPI()
    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(plugins_routes.router)
    app.include_router(v1)
    app.dependency_overrides[get_plugin_host] = lambda: host
    app.dependency_overrides[get_preferences_service] = lambda: MagicMock()
    if user is not None:
        app.dependency_overrides[_get_current_user] = user
    if admin is not None:
        app.dependency_overrides[_get_current_admin] = admin
    return build_test_client(app)


def _source_plugin(
    name: str = "demo",
    source: str = "demo-src",
    *,
    enabled: bool = True,
    error: str | None = None,
) -> LoadedPlugin:
    plugin = LoadedPlugin(
        manifest=PluginManifest(
            name=name,
            version="1.0.0",
            api_version=1,
            entrypoint="plugin:Demo",
            capabilities=["download_client", "indexer"],
            display_name="Demo",
            capability_configs=[
                PluginCapabilityConfig(
                    id="download_client", source=source, display_name="Demo Src"
                ),
                PluginCapabilityConfig(id="indexer", target_source=f"plugin:{source}"),
            ],
        ),
        enabled=enabled,
        active_capabilities=["download_client", "indexer"],
        error=error,
    )
    plugin.instance = object()
    return plugin


def _ext_plugin() -> LoadedPlugin:
    plugin = LoadedPlugin(
        manifest=PluginManifest(
            name="demo",
            version="1.0.0",
            api_version=1,
            entrypoint="plugin:Demo",
            capabilities=["publisher"],
            display_name="Demo",
            routes=[
                PluginRouteSpec(path="lookup", method="GET", auth="user"),
                PluginRouteSpec(path="admin-thing", method="POST", auth="admin"),
            ],
        ),
        enabled=True,
        active_capabilities=["publisher"],
    )
    plugin.instance = object()
    return plugin


def test_sources_list_shape():
    plugin = _source_plugin()
    host = MagicMock()
    host.download_clients = MagicMock(return_value=[plugin])
    host.indexers = MagicMock(return_value=[plugin])
    client = _plugin_client(host, user=mock_admin_user, admin=mock_admin_user)

    response = client.get("/api/v1/plugins/sources")
    assert response.status_code == 200
    assert response.json() == {
        "sources": [
            {
                "key": "plugin:demo-src",
                "plugin": "demo",
                "display_name": "Demo Src",
                "has_client": True,
                "has_indexer": True,
                "target_source": "plugin:demo-src",
                "configured": True,
                "health": "ok",
            }
        ]
    }


def test_sources_health_is_sanitized():
    assert plugins_routes._sanitize_health("ok") == "ok"
    assert plugins_routes._sanitize_health("bogus") == "unknown"
    assert plugins_routes._sanitize_health(None) == "unknown"

    off = _source_plugin(name="off", source="off-src", enabled=False)
    broken = _source_plugin(name="broken", source="broken-src", enabled=True, error="boom")
    host = MagicMock()
    host.download_clients = MagicMock(return_value=[off, broken])
    host.indexers = MagicMock(return_value=[])
    client = _plugin_client(host, user=mock_admin_user, admin=mock_admin_user)

    response = client.get("/api/v1/plugins/sources")
    assert response.status_code == 200
    by_key = {s["key"]: s for s in response.json()["sources"]}
    assert by_key["plugin:off-src"]["health"] == "unknown"
    assert by_key["plugin:off-src"]["configured"] is False
    assert by_key["plugin:broken-src"]["health"] == "degraded"


def test_sources_ext_and_panel_require_auth(monkeypatch):
    host = MagicMock()
    host.download_clients = MagicMock(return_value=[])
    host.indexers = MagicMock(return_value=[])
    host.get = MagicMock(return_value=None)
    monkeypatch.setattr(plugins_routes, "get_plugin_host", lambda: host)
    client = _plugin_client(host)

    assert client.get("/api/v1/plugins/sources").status_code == 401
    assert client.get("/api/v1/plugins/ext/demo/lookup").status_code == 401
    assert client.post("/api/v1/plugins/ext/demo/lookup", json={}).status_code == 401
    assert client.get("/api/v1/plugins/demo/ui/panel.js").status_code == 401


def test_ext_method_matrix_enforces_auth_and_declared_routes(monkeypatch):
    plugin = _ext_plugin()
    host = MagicMock()
    host.get = MagicMock(side_effect=lambda name: plugin if name == "demo" else None)
    host.handle_plugin_route = AsyncMock(
        return_value=PluginRouteResult(status=200, body={"ok": True})
    )
    monkeypatch.setattr(plugins_routes, "get_plugin_host", lambda: host)

    admin_client = _plugin_client(host, user=mock_admin_user, admin=mock_admin_user)
    user_client = _plugin_client(host, user=mock_user, admin=_deny_admin)

    response = user_client.get("/api/v1/plugins/ext/demo/lookup")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert user_client.post("/api/v1/plugins/ext/demo/admin-thing", json={}).status_code == 403
    assert admin_client.post("/api/v1/plugins/ext/demo/admin-thing", json={}).status_code == 200
    assert admin_client.get("/api/v1/plugins/ext/nope/lookup").status_code == 404
    assert admin_client.get("/api/v1/plugins/ext/demo/nope").status_code == 404
    assert admin_client.post("/api/v1/plugins/ext/demo/lookup", json={}).status_code == 404


def test_panel_js_serves_bundle_bytes_to_admin(tmp_path):
    plugin_dir = tmp_path / "demo"
    (plugin_dir / "ui" / "dist").mkdir(parents=True)
    bundle = b"console.log('demo panel');"
    (plugin_dir / "ui" / "dist" / "panel.js").write_bytes(bundle)

    plugin = LoadedPlugin(
        manifest=PluginManifest(
            name="demo",
            version="1.0.0",
            api_version=1,
            entrypoint="plugin:Demo",
            capabilities=["publisher"],
            display_name="Demo",
            ui_entry="ui/dist/panel.js",
        ),
        enabled=True,
        active_capabilities=["publisher"],
        directory=str(plugin_dir),
    )
    plugin.instance = object()
    host = MagicMock()
    host.get = MagicMock(return_value=plugin)
    client = _plugin_client(host, user=mock_admin_user, admin=mock_admin_user)

    response = client.get("/api/v1/plugins/demo/ui/panel.js")
    assert response.status_code == 200
    assert response.content == bundle
    assert response.headers["content-type"].startswith("text/javascript")

    host.get = MagicMock(return_value=None)
    assert client.get("/api/v1/plugins/demo/ui/panel.js").status_code == 404

    user_client = _plugin_client(host, user=mock_user, admin=_deny_admin)
    assert user_client.get("/api/v1/plugins/demo/ui/panel.js").status_code == 403
