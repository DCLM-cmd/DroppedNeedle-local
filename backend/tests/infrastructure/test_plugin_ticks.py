"""Scheduler ticks: TaskRegistry loop semantics + plugin-dir state files."""

import asyncio
from pathlib import Path

import pytest

from api.v1.schemas.settings import PluginConfig
from core.task_registry import TaskRegistry
from infrastructure.plugins.host import (
    LoadedPlugin,
    PluginHost,
    PluginStateFileError,
)
from infrastructure.plugins.manifest import PluginManifest


class FakePrefs:
    def __init__(self) -> None:
        self.configs: dict[str, PluginConfig] = {}

    def get_plugin_config(self, name: str) -> PluginConfig:
        return self.configs.get(name, PluginConfig())

    def enable(self, name: str) -> None:
        self.configs[name] = PluginConfig(enabled=True)

    def disable(self, name: str) -> None:
        self.configs[name] = PluginConfig(enabled=False)


SCHEDULER_MANIFEST = """
[plugin]
name = "tick-toy"
version = "1.0.0"
api_version = 1
entrypoint = "plugin:TickToy"
capabilities = ["scheduler"]

[schedule]
interval_minutes = 5
"""


def _write_plugin(root: Path, manifest: str, code: str) -> Path:
    plugin_dir = root / "tick-toy"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.toml").write_text(manifest)
    (plugin_dir / "plugin.py").write_text(code)
    return plugin_dir


def _host_with_instance(tmp_path, instance) -> PluginHost:
    host = PluginHost(plugins_dir=tmp_path, preferences_service=FakePrefs())
    host._plugins["tick-toy"] = LoadedPlugin(
        manifest=PluginManifest(
            name="tick-toy",
            version="1.0.0",
            api_version=1,
            entrypoint="plugin:TickToy",
            capabilities=["scheduler"],
        ),
        enabled=True,
        directory=str(tmp_path / "tick-toy"),
        instance=instance,
        active_capabilities=["scheduler"],
    )
    return host


class _Counter:
    def __init__(self, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first

    async def on_tick(self) -> None:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("boom")


async def _run_loop(host: PluginHost, interval: float, seconds: float) -> None:
    task = asyncio.create_task(host._tick_loop("tick-toy", interval, True))
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_tick_exception_logs_and_continues(tmp_path):
    host = _host_with_instance(tmp_path, _Counter(fail_first=True))
    await _run_loop(host, 0.01, 0.06)
    assert host._plugins["tick-toy"].instance.calls >= 2


@pytest.mark.asyncio
async def test_tick_cancel_breaks_loop(tmp_path):
    host = _host_with_instance(tmp_path, _Counter())
    task = asyncio.create_task(host._tick_loop("tick-toy", 60.0, True))
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()


@pytest.mark.asyncio
async def test_tick_hung_tick_cancelled_at_interval(tmp_path):
    class Hung:
        def __init__(self) -> None:
            self.calls = 0

        async def on_tick(self) -> None:
            self.calls += 1
            await asyncio.sleep(10.0)

    host = _host_with_instance(tmp_path, Hung())
    await _run_loop(host, 0.02, 0.09)
    assert host._plugins["tick-toy"].instance.calls >= 2


@pytest.mark.asyncio
async def test_tick_no_overlap(tmp_path):
    class Slow:
        def __init__(self) -> None:
            self.calls = 0
            self.active = 0
            self.max_active = 0

        async def on_tick(self) -> None:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.03)
            finally:
                self.active -= 1

    host = _host_with_instance(tmp_path, Slow())
    await _run_loop(host, 0.01, 0.12)
    instance = host._plugins["tick-toy"].instance
    assert instance.calls >= 2
    assert instance.max_active == 1


@pytest.mark.asyncio
async def test_tick_disabled_plugin_exits_loop(tmp_path):
    host = _host_with_instance(tmp_path, _Counter())
    task = asyncio.create_task(host._tick_loop("tick-toy", 0.01, True))
    await asyncio.sleep(0.03)
    host._plugins["tick-toy"].enabled = False
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()


@pytest.mark.asyncio
async def test_sync_ticks_rebuilds_scheduler_only_plugin(tmp_path):
    prefs = FakePrefs()
    prefs.enable("tick-toy")
    _write_plugin(
        tmp_path,
        SCHEDULER_MANIFEST,
        "class TickToy:\n    def __init__(self, ctx):\n        self.ctx = ctx\n"
        "    async def on_tick(self):\n        return None\n",
    )
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    assert host.get("tick-toy").active_capabilities == ["scheduler"]
    registry = TaskRegistry.get_instance()
    try:
        await host.sync_ticks()
        assert "plugin-tick:tick-toy" in registry.get_all()
        prefs.disable("tick-toy")
        host.load_all()
        await host.sync_ticks()
        assert "plugin-tick:tick-toy" not in registry.get_all()
    finally:
        await host.stop_scheduled_ticks()
        registry.reset()


@pytest.mark.asyncio
async def test_tick_ctx_publish_without_publisher_capability_is_result(tmp_path):
    prefs = FakePrefs()
    prefs.enable("tick-toy")
    _write_plugin(
        tmp_path,
        SCHEDULER_MANIFEST,
        "class TickToy:\n    def __init__(self, ctx):\n        self.ctx = ctx\n"
        "    async def on_tick(self):\n        return None\n",
    )
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    plugin = host.get("tick-toy")
    result = plugin.instance.ctx.publish(
        "plugin_notice", {"title": "hi", "body": "hello"}
    )
    assert result.ok is False


# -- plugin-dir state files --


@pytest.mark.asyncio
async def test_state_file_roundtrip(tmp_path):
    prefs = FakePrefs()
    prefs.enable("tick-toy")
    _write_plugin(tmp_path, SCHEDULER_MANIFEST, "x = 1\n")
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    stored = await host.write_plugin_state_file("tick-toy", "cache/seen", b"abc")
    assert stored.startswith(str(tmp_path / "tick-toy"))
    assert await host.read_plugin_state_file("tick-toy", "cache/seen") == b"abc"


@pytest.mark.asyncio
async def test_state_file_rejects_unsafe_paths(tmp_path):
    prefs = FakePrefs()
    prefs.enable("tick-toy")
    _write_plugin(tmp_path, SCHEDULER_MANIFEST, "x = 1\n")
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    for bad in ("../escape", "/absolute", "UPPER", "trailing/"):
        with pytest.raises(PluginStateFileError):
            await host.write_plugin_state_file("tick-toy", bad, b"x")


@pytest.mark.asyncio
async def test_state_file_rejects_symlink_escape(tmp_path):
    prefs = FakePrefs()
    prefs.enable("tick-toy")
    plugin_dir = _write_plugin(tmp_path, SCHEDULER_MANIFEST, "x = 1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (plugin_dir / "link").symlink_to(outside, target_is_directory=True)
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    with pytest.raises(PluginStateFileError):
        await host.write_plugin_state_file("tick-toy", "link/evil", b"x")
    assert not (outside / "evil").exists()


@pytest.mark.asyncio
async def test_state_file_cap(tmp_path):
    prefs = FakePrefs()
    prefs.enable("tick-toy")
    _write_plugin(tmp_path, SCHEDULER_MANIFEST, "x = 1\n")
    host = PluginHost(plugins_dir=tmp_path, preferences_service=prefs)
    host.load_all()
    with pytest.raises(PluginStateFileError, match="cap"):
        await host.write_plugin_state_file(
            "tick-toy", "big/blob", b"x" * (10 * 1024 * 1024 + 1)
        )
    with pytest.raises(PluginStateFileError, match="not found"):
        await host.read_plugin_state_file("tick-toy", "big/blob")
