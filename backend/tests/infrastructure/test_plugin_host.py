"""PluginHost + manifest: validation, the disabled-by-default trust model,
failure isolation, capability dispatch, and the shipped example plugins as
executable fixtures."""

import asyncio
from pathlib import Path

import pytest

from api.v1.schemas.settings import PluginConfig
from infrastructure.plugins.host import PluginHost
from infrastructure.plugins.manifest import ManifestError, load_manifest
from infrastructure.plugins.protocols import PluginContext, ScrobbleEvent

EXAMPLES = Path(__file__).parent.parent.parent.parent / "examples" / "plugins"

VALID_MANIFEST = """
[plugin]
name = "test-plugin"
version = "1.0.0"
api_version = 0
entrypoint = "plugin:TestPlugin"
capabilities = ["scrobbler"]
"""

SCROBBLER_CODE = """
SEEN = []

class TestPlugin:
    def __init__(self, context):
        self.ctx = context

    async def on_scrobble(self, event):
        SEEN.append(event.track)
"""


class FakePrefs:
    def __init__(self) -> None:
        self.configs: dict[str, PluginConfig] = {}

    def get_plugin_config(self, name: str) -> PluginConfig:
        return self.configs.get(name, PluginConfig())

    def enable(self, name: str, settings: dict | None = None) -> None:
        self.configs[name] = PluginConfig(enabled=True, settings=settings or {})


def _write_plugin(root: Path, name: str, manifest: str, code: str) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(manifest)
    (plugin_dir / "plugin.py").write_text(code)
    return plugin_dir


# -- manifest validation --


def test_manifest_rejects_wrong_api_version(tmp_path):
    _write_plugin(tmp_path, "p", VALID_MANIFEST.replace("api_version = 0", "api_version = 99"), "")
    with pytest.raises(ManifestError, match="api_version"):
        load_manifest(tmp_path / "p")


def test_manifest_rejects_unknown_capability(tmp_path):
    bad = VALID_MANIFEST.replace('["scrobbler"]', '["mind_reader"]')
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="unknown capabilities"):
        load_manifest(tmp_path / "p")


def test_manifest_accepts_reserved_capabilities(tmp_path):
    reserved = VALID_MANIFEST.replace('["scrobbler"]', '["metadata_provider"]')
    _write_plugin(tmp_path, "p", reserved, "")
    manifest = load_manifest(tmp_path / "p")
    assert manifest.capabilities == ["metadata_provider"]


def test_manifest_requires_entrypoint_shape(tmp_path):
    bad = VALID_MANIFEST.replace('"plugin:TestPlugin"', '"plugin.TestPlugin"')
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="entrypoint"):
        load_manifest(tmp_path / "p")

V1_MANIFEST = """
[plugin]
name = "v1-plugin"
version = "1.0.0"
api_version = 1
entrypoint = "plugin:TestPlugin"
capabilities = ["scrobbler"]
"""


def test_manifest_v0_rejects_v1_only_capability(tmp_path):
    bad = VALID_MANIFEST.replace('["scrobbler"]', '["download_client"]')
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="unknown capabilities"):
        load_manifest(tmp_path / "p")


def test_manifest_api_version_2_names_supported_versions(tmp_path):
    bad = VALID_MANIFEST.replace("api_version = 0", "api_version = 2")
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match=r"\(0, 1\)"):
        load_manifest(tmp_path / "p")


def test_manifest_v1_accepts_all_nine_capabilities(tmp_path):
    manifest = V1_MANIFEST.replace(
        'capabilities = ["scrobbler"]',
        'capabilities = ["scrobbler", "purchase_links", "download_client", "indexer",'
        ' "subscriber", "publisher", "metadata_provider", "scheduler", "streaming_source"]',
    ) + '\n[schedule]\ninterval_minutes = 60\n'
    _write_plugin(tmp_path, "p", manifest, "")
    loaded = load_manifest(tmp_path / "p")
    assert len(loaded.capabilities) == 9


@pytest.mark.parametrize("bad_name", ["Upper-Case", "has_underscore", "..", "a" * 33])
def test_manifest_v1_rejects_non_kebab_names(tmp_path, bad_name):
    bad = V1_MANIFEST.replace('"v1-plugin"', f'"{bad_name}"')
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="invalid plugin name"):
        load_manifest(tmp_path / "p")


def test_manifest_v0_legacy_name_check_is_unchanged(tmp_path):
    ok = VALID_MANIFEST.replace('"test-plugin"', '"legacy_Name"')
    _write_plugin(tmp_path, "p", ok, "")
    assert load_manifest(tmp_path / "p").name == "legacy_Name"


def test_manifest_rejects_unknown_plugin_key(tmp_path):
    bad = VALID_MANIFEST.replace('capabilities = ["scrobbler"]', 'capabilities = ["scrobbler"]\nsoruce = "x"')
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="unknown"):
        load_manifest(tmp_path / "p")


def test_manifest_rejects_unknown_top_level_table(tmp_path):
    bad = VALID_MANIFEST + '\n[capability_typo]\nfoo = 1\n'
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="unknown"):
        load_manifest(tmp_path / "p")


def test_manifest_route_requires_publisher_capability(tmp_path):
    bad = V1_MANIFEST + '\n[[route]]\npath = "lookup"\nmethod = "GET"\nauth = "user"\n'
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="publisher"):
        load_manifest(tmp_path / "p")


def test_manifest_route_requires_api_1(tmp_path):
    bad = VALID_MANIFEST + '\n[[route]]\npath = "lookup"\nmethod = "GET"\nauth = "user"\n'
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="api_version 1"):
        load_manifest(tmp_path / "p")


def test_manifest_plugin_ui_requires_api_1(tmp_path):
    bad = VALID_MANIFEST + '\n[plugin_ui]\nentry = "ui/dist/panel.js"\n'
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="api_version 1"):
        load_manifest(tmp_path / "p")


def test_manifest_scheduler_requires_schedule_table(tmp_path):
    bad = V1_MANIFEST.replace('["scrobbler"]', '["scheduler"]')
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="schedule"):
        load_manifest(tmp_path / "p")


@pytest.mark.parametrize("interval", [1, 4, 1441, 100000])
def test_manifest_scheduler_rejects_out_of_range_interval(tmp_path, interval):
    bad = V1_MANIFEST.replace('["scrobbler"]', '["scheduler"]') + f"\n[schedule]\ninterval_minutes = {interval}\n"
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="interval_minutes"):
        load_manifest(tmp_path / "p")


def test_manifest_schedule_requires_api_1(tmp_path):
    bad = VALID_MANIFEST + "\n[schedule]\ninterval_minutes = 60\n"
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="api_version 1"):
        load_manifest(tmp_path / "p")


def test_manifest_capability_id_must_be_declared(tmp_path):
    bad = V1_MANIFEST + '\n[[capability]]\nid = "indexer"\ntarget_source = "usenet"\n'
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="capabilities"):
        load_manifest(tmp_path / "p")


def test_manifest_rejects_unknown_capability_key(tmp_path):
    bad = V1_MANIFEST.replace('["scrobbler"]', '["scrobbler", "indexer"]') + (
        '\n[[capability]]\nid = "indexer"\ntarget_source = "usenet"\nsoruce = "x"\n'
    )
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="unknown"):
        load_manifest(tmp_path / "p")


def test_manifest_rejects_bad_source_alias(tmp_path):
    bad = V1_MANIFEST.replace('["scrobbler"]', '["download_client"]') + (
        '\n[[capability]]\nid = "download_client"\nsource = "Bad_Name"\n'
    )
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="source"):
        load_manifest(tmp_path / "p")


@pytest.mark.parametrize("target", ["soulseek", "plugin:Bad_Name", "http://x"])
def test_manifest_rejects_bad_target_source(tmp_path, target):
    bad = V1_MANIFEST.replace('["scrobbler"]', '["indexer"]') + (
        f'\n[[capability]]\nid = "indexer"\ntarget_source = "{target}"\n'
    )
    _write_plugin(tmp_path, "p", bad, "")
    with pytest.raises(ManifestError, match="target_source"):
        load_manifest(tmp_path / "p")


def test_manifest_v1_full_tables_accept(tmp_path):
    manifest = (
        V1_MANIFEST.replace('"v1-plugin"', '"full-plugin"').replace(
            '["scrobbler"]', '["download_client", "indexer", "publisher", "scheduler"]'
        )
        + '\n[[capability]]\nid = "download_client"\nsource = "full-plugin"\ndisplay_name = "Full Plugin"\n'
        + '\n[[capability]]\nid = "indexer"\ntarget_source = "plugin:full-plugin"\n'
        + "\n[schedule]\ninterval_minutes = 60\nrun_on_load = true\n"
        + '\n[[route]]\npath = "lookup"\nmethod = "POST"\nauth = "user"\nrate_limit_per_minute = 60\n'
        + '\n[plugin_ui]\nentry = "ui/dist/panel.js"\npages = ["panel"]\n'
    )
    _write_plugin(tmp_path, "p", manifest, "")
    loaded = load_manifest(tmp_path / "p")
    assert [c.id for c in loaded.capability_configs] == ["download_client", "indexer"]
    assert loaded.schedule is not None and loaded.schedule.interval_minutes == 60
    assert loaded.routes[0].path == "lookup" and loaded.routes[0].method == "POST"
    assert loaded.ui_entry == "ui/dist/panel.js" and loaded.ui_pages == ["panel"]


# -- trust model --


def test_disabled_plugin_runs_no_code(tmp_path):
    """Dropping a folder in must be inert: the module is never imported until
    an admin enables the plugin."""
    booby_trap = tmp_path / "boom.txt"
    code = f"open({str(booby_trap)!r}, 'w').write('ran')\n\nclass TestPlugin:\n    def __init__(self, ctx): ...\n"
    _write_plugin(tmp_path, "test-plugin", VALID_MANIFEST, code)
    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())

    host.load_all()

    assert not booby_trap.exists()
    plugin = host.get("test-plugin")
    assert plugin is not None and plugin.enabled is False and plugin.instance is None


def test_enabled_scrobbler_loads_and_dispatches(tmp_path):
    prefs = FakePrefs()
    prefs.enable("test-plugin")
    _write_plugin(tmp_path, "test-plugin", VALID_MANIFEST, SCROBBLER_CODE)
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()

    plugin = host.get("test-plugin")
    assert plugin is not None and plugin.active_capabilities == ["scrobbler"]

    asyncio.run(host.dispatch_scrobble(ScrobbleEvent(artist="A", track="Song")))
    module = type(plugin.instance).__module__
    import sys

    assert sys.modules[module].SEEN == ["Song"]


def test_broken_plugin_is_isolated(tmp_path):
    prefs = FakePrefs()
    prefs.enable("test-plugin")
    _write_plugin(tmp_path, "test-plugin", VALID_MANIFEST, "raise RuntimeError('boom')")
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)

    host.load_all()  # must not raise

    plugin = host.get("test-plugin")
    assert plugin is not None
    assert plugin.enabled is False
    assert "Failed to load" in (plugin.error or "")


def test_invalid_manifest_is_surfaced_not_fatal(tmp_path):
    plugin_dir = tmp_path / "broken"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text("not [valid toml")
    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())

    host.load_all()

    plugin = host.get("broken")
    assert plugin is not None and plugin.error


def test_one_crashing_scrobbler_does_not_stop_the_next(tmp_path):
    prefs = FakePrefs()
    prefs.enable("a-crasher")
    prefs.enable("b-worker")
    crasher = VALID_MANIFEST.replace('"test-plugin"', '"a-crasher"')
    worker = VALID_MANIFEST.replace('"test-plugin"', '"b-worker"')
    _write_plugin(
        tmp_path, "a-crasher", crasher,
        "class TestPlugin:\n    def __init__(self, ctx): ...\n"
        "    async def on_scrobble(self, event):\n        raise RuntimeError('boom')\n",
    )
    _write_plugin(tmp_path, "b-worker", worker, SCROBBLER_CODE)
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()

    asyncio.run(host.dispatch_scrobble(ScrobbleEvent(artist="A", track="Song")))

    import sys

    worker_plugin = host.get("b-worker")
    module = type(worker_plugin.instance).__module__
    assert sys.modules[module].SEEN == ["Song"]

@pytest.mark.asyncio
async def test_scrobble_kind_reaches_subscriber_and_scrobbler_exactly_once(tmp_path):
    """One ``dispatch_event`` with a scrobble kind notifies the subscriber path
    AND fires v0 ``on_scrobble`` exactly once (the scrobble service calls only
    ``dispatch_event``; a second direct ``dispatch_scrobble`` would double it)."""
    from infrastructure.plugins.protocols import PluginEvent

    manifest = V1_MANIFEST.replace('"v1-plugin"', '"both-plugin"').replace(
        'capabilities = ["scrobbler"]', 'capabilities = ["scrobbler", "subscriber"]'
    )
    code = (
        "SEEN = []\nEVENTS = []\n\nclass TestPlugin:\n"
        "    def __init__(self, context):\n        self.ctx = context\n"
        "    async def on_scrobble(self, event):\n        SEEN.append(event.track)\n"
        "    async def on_event(self, event):\n        EVENTS.append(event.kind)\n"
    )
    prefs = FakePrefs()
    prefs.enable("both-plugin")
    _write_plugin(tmp_path, "both-plugin", manifest, code)
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    plugin = host.get("both-plugin")
    assert plugin is not None
    assert set(plugin.active_capabilities) == {"scrobbler", "subscriber"}

    await host.dispatch_event(
        PluginEvent(
            kind="scrobble",
            payload=ScrobbleEvent(artist="A", track="Song"),
            causation_id="once-1",
        )
    )
    await asyncio.sleep(0.3)

    import sys

    module = sys.modules[type(plugin.instance).__module__]
    assert module.SEEN == ["Song"]
    assert module.EVENTS == ["scrobble"]


def test_no_capability_acquires_content(tmp_path):
    """v1 pins its nine capability ids; the old `audio_source` fetch path stays
    gone: unknown at load, with no host dispatch for it."""
    from infrastructure.plugins.manifest import KNOWN_CAPABILITIES

    assert set(KNOWN_CAPABILITIES) == {
        "scrobbler",
        "purchase_links",
        "download_client",
        "indexer",
        "subscriber",
        "publisher",
        "metadata_provider",
        "scheduler",
        "streaming_source",
    }
    assert "audio_source" not in KNOWN_CAPABILITIES
    for gone in ("source_search", "source_fetch"):
        assert not hasattr(PluginHost, gone), f"PluginHost.{gone} came back"

    manifest = VALID_MANIFEST.replace(
        'capabilities = ["scrobbler"]', 'capabilities = ["audio_source"]'
    )
    assert 'capabilities = ["audio_source"]' in manifest  # the replace actually fired

    prefs = FakePrefs()
    prefs.enable("grabby")
    _write_plugin(tmp_path, "grabby", manifest, SCROBBLER_CODE)
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()

    plugin = host.get("grabby")
    assert plugin is not None and plugin.instance is None
    assert "unknown capabilities" in (plugin.error or "")


# -- the shipped examples are executable fixtures --


def test_example_plugins_load_with_their_capabilities():
    prefs = FakePrefs()
    prefs.enable("webhook-scrobbler")
    host = PluginHost(plugins_dir=EXAMPLES, preferences_service=prefs)
    host.load_all()

    scrobbler = host.get("webhook-scrobbler")
    assert scrobbler is not None and scrobbler.active_capabilities == ["scrobbler"]


@pytest.mark.asyncio
async def test_webhook_scrobbler_posts_the_play():
    from unittest.mock import AsyncMock

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "webhook_scrobbler_test", EXAMPLES / "webhook-scrobbler" / "plugin.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    http = AsyncMock()
    http.post = AsyncMock(return_value=type("R", (), {"status_code": 200})())
    context = PluginContext(
        plugin_name="webhook-scrobbler",
        settings=lambda: {"webhook_url": "https://hooks.example/x"},
        http=http,
    )
    plugin = module.WebhookScrobbler(context)

    await plugin.on_scrobble(ScrobbleEvent(artist="A", track="T", timestamp=5))

    http.post.assert_awaited_once()
    assert http.post.await_args.kwargs["json"]["track"] == "T"


# -- install from GitHub --


def _github_zip(root: str = "repo-main", manifest: str = VALID_MANIFEST, extra: dict | None = None) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(f"{root}/plugin.toml", manifest)
        zf.writestr(f"{root}/plugin.py", SCROBBLER_CODE)
        for name, content in (extra or {}).items():
            zf.writestr(f"{root}/{name}", content)
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", headers: dict | None = None) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    async def aiter_bytes(self, chunk_size: int = 65536):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]


class _FakeStream:
    """Sync-built async context manager mirroring ``httpx.AsyncClient.stream``."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _fake_http(responses: dict[str, _FakeResponse]):
    from unittest.mock import AsyncMock, MagicMock

    http = AsyncMock()

    def _match(url: str) -> _FakeResponse:
        for fragment, response in responses.items():
            if fragment in url:
                return response
        return _FakeResponse(404)

    async def _get(url: str, **_kwargs):
        return _match(url)

    http.get = AsyncMock(side_effect=_get)
    http.stream = MagicMock(side_effect=lambda *args, **kwargs: _FakeStream(_match(args[1])))
    return http


@pytest.mark.asyncio
async def test_install_from_github_lands_disabled(tmp_path):
    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    http = _fake_http({"heads/main": _FakeResponse(200, _github_zip())})

    name = await host.install_from_github("https://github.com/owner/repo", http)

    assert name == "test-plugin"
    assert (tmp_path / "test-plugin" / "plugin.toml").is_file()
    plugin = host.get("test-plugin")
    # installed code is stored, never run: enabling stays the admin's decision
    assert plugin is not None and plugin.enabled is False and plugin.instance is None


@pytest.mark.asyncio
async def test_install_falls_back_to_master_branch(tmp_path):
    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    http = _fake_http({"heads/master": _FakeResponse(200, _github_zip("repo-master"))})

    name = await host.install_from_github("https://github.com/owner/repo", http)
    assert name == "test-plugin"


@pytest.mark.asyncio
async def test_install_rejects_non_github_urls(tmp_path):
    from infrastructure.plugins.host import PluginInstallError

    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    for bad in (
        "https://evil.example.com/owner/repo",
        "http://github.com/owner/repo",
        "https://github.com/owner",
        "not a url",
    ):
        with pytest.raises(PluginInstallError):
            await host.install_from_github(bad, _fake_http({}))


@pytest.mark.asyncio
async def test_install_rejects_traversal_components_in_the_url(tmp_path):
    """'..' survives the URL character classes; it must not reach codeload."""
    from infrastructure.plugins.host import PluginInstallError

    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    http = _fake_http({})
    for bad in (
        "https://github.com/owner/..",
        "https://github.com/../repo",
        "https://github.com/owner/repo/tree/../../etc",
    ):
        with pytest.raises(PluginInstallError):
            await host.install_from_github(bad, http)
    http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_install_refuses_an_oversized_repo_before_buffering(tmp_path):
    from infrastructure.plugins.host import PluginInstallError

    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    http = _fake_http(
        {"heads/main": _FakeResponse(200, b"x", headers={"content-length": str(10**12)})}
    )

    with pytest.raises(PluginInstallError, match="too large"):
        await host.install_from_github("https://github.com/owner/repo", http)

@pytest.mark.asyncio
async def test_install_refuses_a_streamed_body_over_the_cap(tmp_path, monkeypatch):
    """No (or lying) content-length: the streamed accumulation still refuses."""
    import infrastructure.plugins.host as host_module
    from infrastructure.plugins.host import PluginInstallError

    monkeypatch.setattr(host_module, "_MAX_PLUGIN_ZIP_BYTES", 1024)
    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    http = _fake_http({"heads/main": _FakeResponse(200, b"x" * 2048)})

    with pytest.raises(PluginInstallError, match="too large"):
        await host.install_from_github("https://github.com/owner/repo", http)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_install_refuses_a_zip_bomb(tmp_path):
    """A tiny archive decompressing past the per-file cap is refused."""
    import io
    import zipfile

    import infrastructure.plugins.host as host_module
    from infrastructure.plugins.host import PluginInstallError

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("repo-main/plugin.toml", VALID_MANIFEST)
        zf.writestr("repo-main/plugin.py", SCROBBLER_CODE)
        zf.writestr("repo-main/bomb.bin", b"\0" * (host_module._MAX_PLUGIN_ZIP_FILE_BYTES + 1))
    archive = buffer.getvalue()
    assert len(archive) < host_module._MAX_PLUGIN_ZIP_BYTES

    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    http = _fake_http({"heads/main": _FakeResponse(200, archive)})

    with pytest.raises(PluginInstallError, match="oversized|too large"):
        await host.install_from_github("https://github.com/owner/repo", http)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_unpack_refuses_total_decompressed_bytes(tmp_path, monkeypatch):
    """Many small files summing past the total cap are refused mid-extract."""
    import io
    import zipfile

    import infrastructure.plugins.host as host_module
    from infrastructure.plugins.host import PluginInstallError

    monkeypatch.setattr(host_module, "_MAX_PLUGIN_ZIP_DECOMPRESSED_BYTES", 100)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("repo-main/plugin.toml", VALID_MANIFEST)
        zf.writestr("repo-main/plugin.py", SCROBBLER_CODE)
    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())

    with pytest.raises(PluginInstallError, match="too large"):
        host._unpack_plugin_zip(buffer.getvalue())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_install_rejects_repo_without_manifest(tmp_path):
    import io
    import zipfile

    from infrastructure.plugins.host import PluginInstallError

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("repo-main/README.md", "hi")
    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    http = _fake_http({"heads/main": _FakeResponse(200, buffer.getvalue())})

    with pytest.raises(PluginInstallError, match="plugin.toml"):
        await host.install_from_github("https://github.com/owner/repo", http)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_install_rejects_invalid_manifest_and_leaves_no_staging(tmp_path):
    from infrastructure.plugins.host import PluginInstallError

    bad = VALID_MANIFEST.replace("api_version = 0", "api_version = 99")
    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    http = _fake_http({"heads/main": _FakeResponse(200, _github_zip(manifest=bad))})

    with pytest.raises(PluginInstallError, match="api_version"):
        await host.install_from_github("https://github.com/owner/repo", http)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_install_refuses_traversal_entries(tmp_path):
    import io
    import zipfile

    from infrastructure.plugins.host import PluginInstallError

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("repo-main/plugin.toml", VALID_MANIFEST)
        zf.writestr("repo-main/../../escape.py", "pwned")
    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    http = _fake_http({"heads/main": _FakeResponse(200, buffer.getvalue())})

    with pytest.raises(PluginInstallError):
        await host.install_from_github("https://github.com/owner/repo", http)
    assert not (tmp_path.parent / "escape.py").exists()


@pytest.mark.asyncio
async def test_install_over_existing_replaces_it(tmp_path):
    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    http = _fake_http({"heads/main": _FakeResponse(200, _github_zip(extra={"old.txt": "v1"}))})
    await host.install_from_github("https://github.com/owner/repo", http)
    assert (tmp_path / "test-plugin" / "old.txt").is_file()

    http = _fake_http({"heads/main": _FakeResponse(200, _github_zip())})
    await host.install_from_github("https://github.com/owner/repo", http)
    assert not (tmp_path / "test-plugin" / "old.txt").exists()


def test_uninstall_removes_only_that_folder(tmp_path):
    prefs = FakePrefs()
    _write_plugin(tmp_path, "test-plugin", VALID_MANIFEST, SCROBBLER_CODE)
    keep = _write_plugin(
        tmp_path, "other", VALID_MANIFEST.replace('"test-plugin"', '"other"'), SCROBBLER_CODE
    )
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()

    host.uninstall("test-plugin")

    assert not (tmp_path / "test-plugin").exists()
    assert keep.exists()
    assert host.get("test-plugin") is None


def test_uninstall_unknown_plugin_raises(tmp_path):
    from core.exceptions import ResourceNotFoundError

    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    host.load_all()
    with pytest.raises(ResourceNotFoundError):
        host.uninstall("nope")
