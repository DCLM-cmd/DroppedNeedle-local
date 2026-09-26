"""Plugin acquisition sources: enabled-only view over PluginHost.

Reads the host live on every call (post-swap ``download_clients`` /
``indexers`` plus the ``generation`` snapshot) so a ``load_all`` rebuild is
never observed half-populated. Free Music stays out of here entirely - it is
a separate service/store, never a CHECK/strategy-map entry.
"""

import asyncio
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

from infrastructure.msgspec_fastapi import AppStruct
from infrastructure.plugins.adapters import PluginClientAdapter, PluginIndexerAdapter

logger = logging.getLogger(__name__)


class PluginSourceSpec(AppStruct):
    """One enabled plugin acquisition source."""

    key: str
    plugin: str
    display_name: str = ""
    has_client: bool = False
    has_indexer: bool = False
    target_source: str = ""
    configured: bool = False
    generation: int = 0


def _plugin_target(plugin: Any, fallback_key: str) -> str:
    configs = getattr(getattr(plugin, "manifest", None), "capability_configs", None) or []
    for config in configs:
        if getattr(config, "id", "") == "indexer" and getattr(config, "target_source", ""):
            return str(config.target_source)
    for config in configs:
        if getattr(config, "id", "") == "download_client" and getattr(config, "source", ""):
            return str(config.source)
    return fallback_key


def _display_name(plugin: Any, fallback: str) -> str:
    configs = getattr(getattr(plugin, "manifest", None), "capability_configs", None) or []
    for config in configs:
        if getattr(config, "id", "") in ("download_client", "indexer"):
            display = str(getattr(config, "display_name", "") or "")
            if display:
                return display
    manifest_display = str(getattr(getattr(plugin, "manifest", None), "display_name", "") or "")
    return manifest_display or fallback


def _is_configured(plugin: Any) -> bool:
    instance = getattr(plugin, "instance", None)
    if instance is None:
        return False
    try:
        return bool(instance.is_configured())
    except Exception:  # noqa: BLE001 - absence, not failure
        return False


class PluginSourceRegistry:
    """Enabled-only plugin source view; every read hits the host live."""

    def __init__(self, plugin_host: Any | None = None) -> None:
        self._host = plugin_host

    @property
    def generation(self) -> int:
        if self._host is None:
            return 0
        try:
            return int(self._host.generation)
        except Exception:  # noqa: BLE001 - host without a counter reads as 0
            return 0

    @property
    def specs(self) -> list[PluginSourceSpec]:
        if self._host is None:
            return []
        generation = self.generation
        out: list[PluginSourceSpec] = []
        for plugin in sorted(self._host.download_clients() + self._host.indexers(), key=lambda p: p.manifest.name):
            name = plugin.manifest.name
            if any(spec.plugin == name for spec in out):
                continue
            active = set(getattr(plugin, "active_capabilities", None) or [])
            has_client = "download_client" in active
            has_indexer = "indexer" in active
            if not (has_client or has_indexer):
                continue
            if not getattr(plugin, "enabled", False) or getattr(plugin, "instance", None) is None:
                continue
            key = f"plugin:{name}"
            out.append(
                PluginSourceSpec(
                    key=key,
                    plugin=name,
                    display_name=_display_name(plugin, name),
                    has_client=has_client,
                    has_indexer=has_indexer,
                    target_source=_plugin_target(plugin, key),
                    configured=_is_configured(plugin),
                    generation=generation,
                )
            )
        out.sort(key=lambda spec: spec.plugin)
        return out

    def spec_for(self, key_or_name: str) -> PluginSourceSpec | None:
        name = key_or_name[7:] if key_or_name.startswith("plugin:") else key_or_name
        for spec in self.specs:
            if spec.plugin == name or spec.key == key_or_name:
                return spec
        return None

    def client_for(self, key_or_name: str) -> PluginClientAdapter | None:
        if self._host is None:
            return None
        name = key_or_name[7:] if key_or_name.startswith("plugin:") else key_or_name
        for plugin in self._host.download_clients():
            if plugin.manifest.name != name:
                continue
            if getattr(plugin, "instance", None) is None:
                return None
            return PluginClientAdapter(name, plugin.instance)
        return None

    def indexers_for_target(self, target: str) -> list[PluginIndexerAdapter]:
        if self._host is None:
            return []
        out: list[PluginIndexerAdapter] = []
        for plugin in sorted(self._host.indexers(), key=lambda p: p.manifest.name):
            if getattr(plugin, "instance", None) is None:
                continue
            if _plugin_target(plugin, f"plugin:{plugin.manifest.name}") != target:
                continue
            out.append(
                PluginIndexerAdapter(
                    plugin_name=plugin.manifest.name,
                    target=target,
                    instance=plugin.instance,
                )
            )
        return out

    def is_any_source_ready(self) -> bool:
        if self._host is None:
            return False
        for plugin in self._host.download_clients():
            if _is_configured(plugin):
                return True
        return False

    def is_any_configured(self) -> bool:
        return any(spec.configured for spec in self.specs)

class PluginLibraryWriteError(Exception):
    """A plugin library-file write was rejected (bad path, no roots configured,
    quota exceeded, unsafe root, or a storage failure)."""


# Scheduler library writes (v1): relative-only, same charset as ext routes.
# ``..``/absolute/empty segments are rejected even though the charset already
# excludes most of them (defence in depth - the regex is the contract).
_PLUGIN_REL_PATH = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,63}$")
# 100 MiB per rolling 24 h window, per plugin. In-memory only in v1
# (no SQLite): a restart resets the window, which only ever loosens the cap.
_PLUGIN_LIBRARY_QUOTA_BYTES_PER_DAY = 100 * 1024 * 1024
_PLUGIN_LIBRARY_QUOTA_WINDOW_SECONDS = 24 * 60 * 60

# Rolling usage: plugin name -> deque of (monotonic timestamp, byte count).
# Pruned on every check; bounded by construction (entries older than the
# window are dropped, and one plugin can only add entries by writing).
_plugin_library_usage: dict[str, deque[tuple[float, int]]] = {}
# Last rejection per plugin (quota/path/roots): the health signal surfaced via
# ``plugin_library_write_health`` for the admin UI / status consumers.
_plugin_library_write_health: dict[str, str] = {}


def _get_preferences_service() -> Any:
    from core.dependencies.service_providers import get_preferences_service

    return get_preferences_service()


def _get_library_management_publisher() -> Any:
    from core.dependencies.service_providers import get_library_management_publisher

    return get_library_management_publisher()


def _get_filesystem_coordinator() -> Any:
    from core.dependencies.service_providers import get_library_filesystem_coordinator

    return get_library_filesystem_coordinator()


def _validate_plugin_rel_path(rel_path: str) -> str:
    if (
        not isinstance(rel_path, str)
        or not _PLUGIN_REL_PATH.match(rel_path)
        or rel_path.endswith("/")
        or any(part in ("", ".", "..") for part in rel_path.split("/"))
    ):
        raise PluginLibraryWriteError(
            f"rel_path {rel_path!r} is not a safe relative path"
        )
    return rel_path


def _prune_plugin_library_usage(plugin_name: str, now: float) -> int:
    """Drop out-of-window entries; return bytes used inside the window."""
    entries = _plugin_library_usage.get(plugin_name)
    if not entries:
        return 0
    cutoff = now - _PLUGIN_LIBRARY_QUOTA_WINDOW_SECONDS
    while entries and entries[0][0] <= cutoff:
        entries.popleft()
    used = sum(count for _, count in entries)
    if not entries:
        _plugin_library_usage.pop(plugin_name, None)
    return used


def plugin_library_write_health(plugin_name: str) -> str | None:
    """Last library-write rejection for this plugin, if any (quota/path/roots).

    The health signal for the admin UI: set when a write is rejected, cleared
    on the next successful write."""
    return _plugin_library_write_health.get(plugin_name)


def _reset_plugin_library_write_state() -> None:
    """Clear quota usage + health (tests only; production never calls this)."""
    _plugin_library_usage.clear()
    _plugin_library_write_health.clear()


async def plugin_write_library_file(
    *,
    plugin_name: str,
    rel_path: str,
    data: bytes,
    timeout: float = 30.0,
) -> str:
    """Write ONE file into the music library via LibraryManagementPublisher.

    ``rel_path`` is relative-only (``^[a-z0-9][a-z0-9/_-]{0,63}$``; ``..`` /
    absolute rejected); allowed roots are EXACTLY the app music-library roots
    (the write lands in the first configured root, containment re-checked
    against the full set by the publisher's symlink-safe resolver). Quota:
    100 MiB/rolling-day/plugin - over the quota the write is rejected with
    :class:`PluginLibraryWriteError` and the rejection is recorded in
    :func:`plugin_library_write_health`. Returns the stored absolute path.
    ``on_tick`` awaits this inside its tick timeout; blocking I/O runs in a
    worker thread under ``timeout`` seconds. Direct library-dir writes outside
    this helper stay unsupported."""
    if not isinstance(plugin_name, str) or not plugin_name:
        raise PluginLibraryWriteError("plugin_name is required")
    _validate_plugin_rel_path(rel_path)
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise PluginLibraryWriteError("data must be bytes")
    try:
        timeout_s = float(timeout)
    except (TypeError, ValueError) as exc:
        raise PluginLibraryWriteError("timeout must be a number") from exc
    if not timeout_s > 0:
        raise PluginLibraryWriteError("timeout must be positive")

    now = time.monotonic()
    used = _prune_plugin_library_usage(plugin_name, now)
    if used + len(data) > _PLUGIN_LIBRARY_QUOTA_BYTES_PER_DAY:
        message = (
            f"plugin {plugin_name!r} exceeded the library write quota "
            f"({used + len(data)} > {_PLUGIN_LIBRARY_QUOTA_BYTES_PER_DAY} bytes/day)"
        )
        _plugin_library_write_health[plugin_name] = message
        logger.warning("plugins.library_write_rejected reason=over-quota name=%s", plugin_name)
        raise PluginLibraryWriteError(message)

    preferences = _get_preferences_service()
    roots = [
        root
        for root in preferences.get_typed_library_settings().library_roots
        if getattr(root, "path", "")
    ]
    if not roots:
        message = "no library roots configured"
        _plugin_library_write_health[plugin_name] = message
        raise PluginLibraryWriteError(message)
    primary = roots[0]
    root_path = Path(primary.path)
    root_id = str(getattr(primary, "id", "") or "")
    if not root_id:
        message = "the primary library root has no id"
        _plugin_library_write_health[plugin_name] = message
        raise PluginLibraryWriteError(message)

    publisher = _get_library_management_publisher()
    coordinator = _get_filesystem_coordinator()
    from core.exceptions import ValidationError

    try:
        async with asyncio.timeout(timeout_s):
            async with coordinator.write(root_id):
                stored = await asyncio.to_thread(
                    publisher.write_plugin_managed_file,
                    root_id=root_id,
                    root=root_path,
                    rel_path=rel_path,
                    data=data,
                )
    except TimeoutError as exc:
        message = f"library write timed out after {timeout_s}s"
        _plugin_library_write_health[plugin_name] = message
        raise PluginLibraryWriteError(message) from exc
    except PluginLibraryWriteError:
        raise
    except ValidationError as exc:
        message = str(exc)
        _plugin_library_write_health[plugin_name] = message
        logger.warning(
            "plugins.library_write_rejected reason=unsafe-path name=%s", plugin_name
        )
        raise PluginLibraryWriteError(message) from exc
    except OSError as exc:
        message = f"library write failed: {exc}"
        _plugin_library_write_health[plugin_name] = message
        raise PluginLibraryWriteError(message) from exc

    entries = _plugin_library_usage.setdefault(plugin_name, deque())
    entries.append((time.monotonic(), len(data)))
    _plugin_library_write_health.pop(plugin_name, None)
    return str(stored)
