"""Scheduler library writes: path/quota/root rejects + success accounting."""

import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from services import plugin_sources
from services.plugin_sources import (
    _PLUGIN_LIBRARY_QUOTA_BYTES_PER_DAY,
    PluginLibraryWriteError,
    plugin_library_write_health,
    plugin_write_library_file,
)


class _Roots:
    def __init__(self, roots: list) -> None:
        self.library_roots = roots


class _Root:
    def __init__(self, root_id: str, path: str) -> None:
        self.id = root_id
        self.path = path


class _FakePrefs:
    def __init__(self, roots: list) -> None:
        self._roots = roots

    def get_typed_library_settings(self) -> _Roots:
        return _Roots(self._roots)


class _FakePublisher:
    """Minimal stand-in: stores bytes under the given root like the real
    publisher's atomic path, minus the journal machinery."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str, bytes]] = []

    def write_plugin_managed_file(
        self, *, root_id: str, root: Path, rel_path: str, data: bytes
    ) -> Path:
        assert Path(root) == self.root
        target = self.root.joinpath(*rel_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.calls.append((root_id, rel_path, data))
        return target


class _NullCoordinator:
    @asynccontextmanager
    async def write(self, root_id: str):  # noqa: ANN001, ANN202 - test fake
        assert root_id
        yield


@pytest.fixture
def library_harness(tmp_path, monkeypatch):
    plugin_sources._reset_plugin_library_write_state()
    root = tmp_path / "music"
    root.mkdir()
    prefs = _FakePrefs([_Root("root-1", str(root))])
    publisher = _FakePublisher(root)
    monkeypatch.setattr(
        plugin_sources, "_get_preferences_service", lambda: prefs
    )
    monkeypatch.setattr(
        plugin_sources, "_get_library_management_publisher", lambda: publisher
    )
    monkeypatch.setattr(
        plugin_sources, "_get_filesystem_coordinator", _NullCoordinator
    )
    yield prefs, publisher, root
    plugin_sources._reset_plugin_library_write_state()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/absolute/path",
        "../escape",
        "a/../../b",
        "UPPER",
        "has space",
        "dot.mp3",
        "trailing/",
        "double//slash",
        "a" * 65,
        ".hidden",
    ],
)
@pytest.mark.asyncio
async def test_library_write_rejects_unsafe_rel_paths(library_harness, bad):
    _, publisher, _ = library_harness
    with pytest.raises(PluginLibraryWriteError):
        await plugin_write_library_file(
            plugin_name="tick-toy", rel_path=bad, data=b"x"
        )
    assert publisher.calls == []


@pytest.mark.asyncio
async def test_library_write_rejects_non_bytes_data(library_harness):
    with pytest.raises(PluginLibraryWriteError, match="bytes"):
        await plugin_write_library_file(
            plugin_name="tick-toy", rel_path="ok/path", data="nope"
        )


@pytest.mark.asyncio
async def test_library_write_writes_under_first_root(library_harness):
    _, publisher, root = library_harness
    stored = await plugin_write_library_file(
        plugin_name="tick-toy", rel_path="tick-toy/note-1", data=b"hello"
    )
    assert stored == str(root / "tick-toy" / "note-1")
    assert Path(stored).read_bytes() == b"hello"
    assert len(publisher.calls) == 1
    assert plugin_library_write_health("tick-toy") is None


@pytest.mark.asyncio
async def test_library_write_quota_reject_records_health(library_harness):
    _, publisher, _ = library_harness
    plugin_sources._plugin_library_usage["tick-toy"] = deque(
        [(time.monotonic(), _PLUGIN_LIBRARY_QUOTA_BYTES_PER_DAY - 10)]
    )
    with pytest.raises(PluginLibraryWriteError, match="quota"):
        await plugin_write_library_file(
            plugin_name="tick-toy", rel_path="tick-toy/big", data=b"x" * 11
        )
    assert publisher.calls == []
    assert "quota" in (plugin_library_write_health("tick-toy") or "")
    # A later in-quota write clears the health signal.
    await plugin_write_library_file(
        plugin_name="tick-toy", rel_path="tick-toy/small", data=b"ok"
    )
    assert plugin_library_write_health("tick-toy") is None


@pytest.mark.asyncio
async def test_library_write_single_write_over_quota_rejected(library_harness):
    _, publisher, _ = library_harness
    with pytest.raises(PluginLibraryWriteError, match="quota"):
        await plugin_write_library_file(
            plugin_name="tick-toy",
            rel_path="tick-toy/huge",
            data=b"x" * (_PLUGIN_LIBRARY_QUOTA_BYTES_PER_DAY + 1),
        )
    assert publisher.calls == []


@pytest.mark.asyncio
async def test_library_write_without_roots_rejected(monkeypatch):
    plugin_sources._reset_plugin_library_write_state()
    monkeypatch.setattr(
        plugin_sources, "_get_preferences_service", lambda: _FakePrefs([])
    )
    with pytest.raises(PluginLibraryWriteError, match="no library roots"):
        await plugin_write_library_file(
            plugin_name="tick-toy", rel_path="tick-toy/x", data=b"x"
        )
    assert "no library roots" in (plugin_library_write_health("tick-toy") or "")
    plugin_sources._reset_plugin_library_write_state()
