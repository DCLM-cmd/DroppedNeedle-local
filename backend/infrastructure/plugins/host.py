"""PluginHost - discover, validate, and run plugins from the plugins directory.

Trust model (documented, not enforced): a plugin is Python imported in-process
with the app's full privileges. That is why nothing loads until BOTH the
manifest validates AND the admin has explicitly enabled the plugin in
Settings -> Plugins. Dropping a folder in the directory alone runs no code.

Failure isolation: a plugin that fails to import, instantiate, or execute is
recorded (and surfaced in the admin UI) - it never crashes the host or the
flow that invoked it.
"""

import asyncio
import importlib.util
import io
import logging
import re
import shutil
import sys
import time
import uuid
import zipfile
from collections import deque
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec

from core.task_registry import TaskRegistry

from infrastructure.msgspec_fastapi import AppStruct
from infrastructure.plugins.manifest import (
    ManifestError,
    PluginManifest,
    load_manifest,
)
from infrastructure.plugins.protocols import (
    CAPABILITY_PROTOCOLS,
    PluginContext,
    PluginPurchaseLink,
    ScrobbleEvent,
)

if TYPE_CHECKING:
    from services.preferences_service import PreferencesService


def _route_not_found() -> dict[str, Any]:
    return {"error": {"code": "NOT_FOUND", "message": "Not found", "details": None}}


def _route_failed() -> dict[str, Any]:
    return {
        "error": {
            "code": "EXTERNAL_SERVICE_UNAVAILABLE",
            "message": "Plugin route failed",
            "details": None,
        }
    }


logger = logging.getLogger(__name__)


# https://github.com/<owner>/<repo>[/tree/<ref>][.git][/]
_GITHUB_URL = re.compile(
    r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?"
    r"(?:/tree/(?P<ref>[\w./-]+))?/?$"
)
_CODELOAD = "https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{ref}"
_MAX_PLUGIN_ZIP_BYTES = 32 * 2**20
_MAX_PLUGIN_ZIP_ENTRIES = 2000
_MAX_PLUGIN_ZIP_FILE_BYTES = 32 * 2**20
_MAX_PLUGIN_ZIP_DECOMPRESSED_BYTES = 256 * 2**20
_TICK_PREFIX = "plugin-tick:"
_EVENT_NOTIFY_TIMEOUT = 5.0
_ROUTE_TIMEOUT = 5.0
# Owner override 2026-09-05: ext routes allow GET+POST+DELETE.
_ROUTE_METHODS = ("GET", "POST", "DELETE")
_ROUTE_BODY_MAX_BYTES = 1024 * 1024
# Plugin-dir state files (v1, no SQLite): same relative-only charset as routes;
# every read/write resolves symlinks and must stay inside the plugin's own
# directory. 10 MiB cap per file (read or write).
_STATE_PATH = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,63}$")
_STATE_FILE_MAX_BYTES = 10 * 1024 * 1024
# Publisher allowlist (v1 closed set; unknown kind is a loud call error).
_PUBLISH_KINDS = ("indexer_invalidate", "download_note", "plugin_notice")
_PUBLISH_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "indexer_invalidate": ("target_source",),
    "download_note": ("task_id", "note"),
    "plugin_notice": ("title", "body"),
}
_PUBLISH_NOTE_MAX_BYTES = 1024
_PUBLISH_RATE_LIMIT = 30
_PUBLISH_RATE_WINDOW = 60.0
_PUBLISH_RATE_CAP = 10_000
_CAUSATION_CAP = 10_000
_CAUSATION_TTL = 10 * 60.0
_CAUSATION_SWEEP_INTERVAL = 60.0
_PUBLISH_QUEUE_MAX = 100

# Depth of the current subscriber fan-out: a publish from inside ``on_event``
# runs at depth 1 and is enqueued once; anything deeper is dropped + logged.
_EVENT_DEPTH: ContextVar[int] = ContextVar("plugin_publish_depth", default=0)
# Causation of the event currently being delivered to a subscriber (None outside
# fan-out): a depth>0 publish inherits it instead of the caller-supplied id, so
# rotating causations cannot dodge the (causation, subscriber) dedup and
# ping-pong forever under depth-1 + rate limit alone.
_EVENT_CAUSATION: ContextVar[str | None] = ContextVar(
    "plugin_publish_causation", default=None
)


def _log_task_error(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("plugins.background_failed: %s", exc)

async def _read_capped_archive(response: Any) -> bytes | None:
    """None on non-200, else the body capped at ``_MAX_PLUGIN_ZIP_BYTES``.

    A lying-or-missing ``content-length`` never lets an unbounded body into
    memory: streamed responses are accumulated chunk by chunk against the cap,
    buffered ones (``.get`` doubles) are measured before they are kept."""
    if response.status_code != 200:
        return None
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_PLUGIN_ZIP_BYTES:
        raise PluginInstallError("That repository is too large to install as a plugin")
    aiter = getattr(response, "aiter_bytes", None)
    if aiter is None:  # buffered ``.get`` double without streaming: measure, then keep
        body = response.content
        if len(body) > _MAX_PLUGIN_ZIP_BYTES:
            raise PluginInstallError("That repository is too large to install as a plugin")
        return body
    chunks: list[bytes] = []
    total = 0
    async for chunk in aiter():
        total += len(chunk)
        if total > _MAX_PLUGIN_ZIP_BYTES:
            raise PluginInstallError("That repository is too large to install as a plugin")
        chunks.append(chunk)
    return b"".join(chunks)

class PluginPublishResult(AppStruct):
    """Outcome of ``publish_from_plugin`` for the service layer to map to HTTP."""

    ok: bool
    status: int  # 200 accepted; 404 unknown/disabled; 409 max-depth; 422 invalid; 429 rate_limited
    retry_after: int = 0
    error: str | None = None

    def __await__(self):  # type: ignore[no-untyped-def]
        """Let plugins ``await ctx.publish(...)``: the result is already computed,
        so awaiting just yields it. Sync callers keep using ``.ok`` directly."""

        async def _self():  # noqa: ANN202
            return self

        return _self().__await__()


class PluginRouteResult(AppStruct):
    """Outcome of ``handle_plugin_route``; ``body`` already passed the 1 MiB cap."""

    status: int
    body: Any = None


class PluginInstallError(Exception):
    """The repository is not a usable plugin (bad URL, no manifest, unsafe zip)."""


class PluginStateFileError(Exception):
    """A plugin-dir state read/write was rejected (bad path, symlink escape,
    missing file, over the 10 MiB cap)."""


class LoadedPlugin(AppStruct):
    manifest: PluginManifest
    enabled: bool
    error: str | None = None
    active_capabilities: list[str] = []
    # the on-disk folder, which need not equal the manifest name (a hand-copied
    # plugin can live in any directory); uninstall removes exactly this one
    directory: str = ""

    instance: object | None = None


class PluginHost:
    def __init__(self, *, plugins_dir: Path, preferences_service: "PreferencesService") -> None:
        self._dir = plugins_dir
        self._prefs = preferences_service
        self._plugins: dict[str, LoadedPlugin] = {}
        self._generation: int = 0
        self._event_inflight: set[str] = set()
        self._dropped_events: dict[str, int] = {}
        self._seen_causations: dict[tuple[str, str], float] = {}
        self._causation_sweep_at: float = 0.0
        self._publish_rate: dict[tuple[str, str], list[float]] = {}
        self._publish_queue: deque[dict[str, Any]] = deque()
        self._publish_dropped: int = 0
        self._tick_intervals: dict[str, float] = {}

    @property
    def generation(self) -> int:
        """Monotonic rebuild counter, bumped after every atomic ``load_all`` swap."""
        return self._generation

    @property
    def dropped_event_counts(self) -> dict[str, int]:
        """Per-plugin skip-if-pending drops, surfaced in plugin health."""
        return dict(self._dropped_events)

    @property
    def publish_dropped_count(self) -> int:
        """Queue-full drop-oldest evictions from ``publish_from_plugin``."""
        return self._publish_dropped

    # -- lifecycle --

    def load_all(self) -> None:
        """Discover every plugin folder; import only manifest-valid, admin-enabled
        ones. Runs once at startup and on explicit reload - in a worker thread,
        so the registry is built aside and swapped in atomically: a dispatch on
        the event loop never sees a half-populated dict."""
        plugins: dict[str, LoadedPlugin] = {}
        if self._dir.is_dir():
            for child in sorted(self._dir.iterdir()):
                if not child.is_dir() or child.name.startswith(("_", ".")):
                    continue
                self._load_one(child, plugins)
        self._plugins = plugins
        self._generation += 1
        if plugins:
            summary = {
                name: (p.active_capabilities if p.enabled else "disabled")
                for name, p in plugins.items()
            }
            counts: dict[str, int] = {}
            for p in plugins.values():
                if p.enabled:
                    for capability in p.active_capabilities:
                        counts[capability] = counts.get(capability, 0) + 1
            logger.info(
                "plugins.loaded generation=%s capabilities=%s",
                self._generation,
                counts,
                extra={"plugins": str(summary)},
            )

    # -- install from GitHub --

    async def install_from_github(self, url: str, http) -> str:  # noqa: ANN001 - httpx.AsyncClient
        """Download a public GitHub repo's default (or given) branch as a zip and
        unpack it into the plugins directory. Returns the installed plugin name.

        NOTE: this downloads and stores third-party code; it does NOT run it.
        The installed plugin stays disabled until an admin enables it, which is
        the same trust gate as a hand-copied folder.
        """
        match = _GITHUB_URL.match((url or "").strip())
        if match is None:
            raise PluginInstallError(
                "Enter a public GitHub repository URL, e.g. https://github.com/owner/repo"
            )
        owner, repo = match["owner"], match["repo"]
        refs = [match["ref"]] if match["ref"] else ["main", "master"]
        # the character classes permit dots, so a '..' component could still walk
        # the codeload path; reject it rather than rely on URL normalisation
        if any(".." in part for part in (owner, repo, *refs)):
            raise PluginInstallError("That repository URL is not valid")

        archive: bytes | None = None
        for ref in refs:
            url_format = _CODELOAD.format(owner=owner, repo=repo, ref=ref)
            archive = await self._fetch_archive(http, url_format)
            if archive is not None:
                break
        if archive is None:
            raise PluginInstallError(
                "Could not download that repository - check the URL is public and the branch exists"
            )

        name = await asyncio.to_thread(self._unpack_plugin_zip, archive)
        await asyncio.to_thread(self.load_all)
        logger.info("plugins.installed name=%s source=%s", name, url)
        return name

    @staticmethod
    async def _fetch_archive(http, url: str) -> bytes | None:  # noqa: ANN001, ANN204
        """GET one codeload URL with the install cap enforced WHILE streaming.

        Returns the body, or None when the ref does not exist (try the next).
        A declared or streamed body over ``_MAX_PLUGIN_ZIP_BYTES`` refuses the
        install instead of buffering an unbounded repo into memory. Doubles that
        expose only ``.get`` keep the old buffered path (cap still enforced).
        """
        streamer = getattr(http, "stream", None)
        if streamer is not None:
            try:
                async with streamer("GET", url, follow_redirects=True) as response:
                    return await _read_capped_archive(response)
            except PluginInstallError:
                raise
            except (AttributeError, TypeError):
                pass  # no real streaming support; fall back to .get below
        response = await http.get(url, follow_redirects=True)
        return await _read_capped_archive(response)


    def _unpack_plugin_zip(self, archive: bytes) -> str:
        """Extract the archive's single top-level dir into the plugins folder,
        named after the manifest. Refuses traversal, absolute paths, symlinks,
        oversized entries, and anything without a valid plugin.toml. Total
        decompressed bytes are capped too, so a zip bomb cannot fill the disk."""
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            entries = [e for e in zf.infolist() if not e.is_dir()]
            if len(entries) > _MAX_PLUGIN_ZIP_ENTRIES:
                raise PluginInstallError("That repository has too many files")
            for entry in entries:
                if entry.file_size > _MAX_PLUGIN_ZIP_FILE_BYTES:
                    raise PluginInstallError("That repository contains an oversized file")
            roots = {Path(e.filename).parts[0] for e in entries if Path(e.filename).parts}
            if len(roots) != 1:
                raise PluginInstallError("Unexpected archive layout")
            root = roots.pop()

            manifest_entry = next(
                (e for e in entries if Path(e.filename).parts[1:] == ("plugin.toml",)), None
            )
            if manifest_entry is None:
                raise PluginInstallError(
                    "No plugin.toml at the repository root - this is not a DroppedNeedle plugin"
                )

            staging = self._dir / f".installing-{root}"
            shutil.rmtree(staging, ignore_errors=True)
            total_written = 0
            try:
                for entry in entries:
                    parts = Path(entry.filename).parts[1:]  # strip the repo-ref root
                    if not parts:
                        continue
                    raw = Path(*parts)
                    if raw.is_absolute() or ".." in raw.parts:
                        raise PluginInstallError("The archive contains unsafe paths")
                    # 0xA000 = symlink in the zip's external attrs (unix mode)
                    if (entry.external_attr >> 16) & 0xF000 == 0xA000:
                        raise PluginInstallError("The archive contains symlinks")
                    target = staging.joinpath(raw)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(entry) as src, open(target, "wb") as dst:
                        written = 0
                        while True:
                            chunk = src.read(65536)
                            if not chunk:
                                break
                            written += len(chunk)
                            total_written += len(chunk)
                            if (
                                written > _MAX_PLUGIN_ZIP_FILE_BYTES
                                or total_written > _MAX_PLUGIN_ZIP_DECOMPRESSED_BYTES
                            ):
                                raise PluginInstallError(
                                    "That repository is too large to install as a plugin"
                                )
                            dst.write(chunk)

                manifest = load_manifest(staging)  # validates before it can be enabled
                final = self._dir / manifest.name
                shutil.rmtree(final, ignore_errors=True)
                staging.replace(final)
                return manifest.name
            except ManifestError as exc:
                raise PluginInstallError(str(exc)) from exc
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def uninstall(self, plugin_name: str) -> None:
        """Disable-by-deletion: remove the plugin's folder. The admin's saved
        settings stay in config.json, so a reinstall keeps its configuration."""
        plugin = self._plugins.get(plugin_name)
        if plugin is None:
            from core.exceptions import ResourceNotFoundError

            raise ResourceNotFoundError("Plugin not found")
        target = Path(plugin.directory) if plugin.directory else self._dir / plugin_name
        if target.is_dir() and target.parent == self._dir:
            shutil.rmtree(target, ignore_errors=True)
        self.load_all()

    # -- plugin-dir state files (v1 durable state; no SQLite) --

    def _plugin_state_target(self, plugin_name: str, rel_path: str) -> Path:
        """Resolve one state path inside the plugin's own directory. Symlink
        escapes (a component pointing outside the directory) are an error +
        warning log, never a write elsewhere."""
        plugin = self._plugins.get(plugin_name)
        if plugin is None or not plugin.directory:
            raise PluginStateFileError(f"unknown plugin {plugin_name!r}")
        if (
            not isinstance(rel_path, str)
            or not _STATE_PATH.match(rel_path)
            or rel_path.endswith("/")
            or any(part in ("", ".", "..") for part in rel_path.split("/"))
        ):
            raise PluginStateFileError(
                f"state path {rel_path!r} is not a safe relative path"
            )
        base = Path(plugin.directory).resolve()
        target = (base / rel_path).resolve()
        if target != base and not target.is_relative_to(base):
            logger.warning(
                "plugins.state_rejected name=%s path=%s reason=symlink-escape",
                plugin_name,
                rel_path,
            )
            raise PluginStateFileError(
                f"state path {rel_path!r} escapes the plugin directory"
            )
        return target

    async def write_plugin_state_file(
        self,
        plugin_name: str,
        rel_path: str,
        data: bytes,
        *,
        timeout: float = 30.0,
    ) -> str:
        """Store plugin-owned state bytes under the plugin's own directory.
        Returns the stored absolute path. Over the 10 MiB per-file cap, a
        symlink escape, or an unsafe path is an error (escape also logs).
        Blocking I/O runs in a worker thread under ``timeout`` seconds."""
        if isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        if not isinstance(data, bytes):
            raise PluginStateFileError("state data must be bytes")
        if len(data) > _STATE_FILE_MAX_BYTES:
            logger.warning(
                "plugins.state_rejected name=%s path=%s reason=over-cap",
                plugin_name,
                rel_path,
            )
            raise PluginStateFileError("state file exceeds the 10 MiB cap")
        target = self._plugin_state_target(plugin_name, rel_path)

        def _write() -> None:
            if target.exists() and not target.is_file():
                raise PluginStateFileError(f"state path {rel_path!r} is not a file")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f"{target.name}.tmp-{uuid.uuid4().hex}")
            tmp.write_bytes(data)
            tmp.replace(target)

        async with asyncio.timeout(timeout):
            await asyncio.to_thread(_write)
        return str(target)

    async def read_plugin_state_file(
        self, plugin_name: str, rel_path: str, *, timeout: float = 30.0
    ) -> bytes:
        """Read back bytes stored with :meth:`write_plugin_state_file`. A missing
        file, an over-cap file, a symlink escape, or an unsafe path is an
        error (escape also logs). Blocking I/O runs in a worker thread under
        ``timeout`` seconds."""
        target = self._plugin_state_target(plugin_name, rel_path)

        def _read() -> bytes:
            size = target.stat().st_size
            if size > _STATE_FILE_MAX_BYTES:
                raise PluginStateFileError("state file exceeds the 10 MiB cap")
            return target.read_bytes()

        try:
            async with asyncio.timeout(timeout):
                return await asyncio.to_thread(_read)
        except FileNotFoundError as exc:
            raise PluginStateFileError(f"state file {rel_path!r} not found") from exc
        except OSError as exc:
            raise PluginStateFileError(f"state file {rel_path!r} unreadable") from exc

    def _load_one(self, plugin_dir: Path, plugins: dict[str, LoadedPlugin]) -> None:
        try:
            manifest = load_manifest(plugin_dir)
        except ManifestError as exc:
            logger.warning("plugins.manifest_invalid: %s", exc)
            plugins[plugin_dir.name] = LoadedPlugin(
                manifest=PluginManifest(
                    name=plugin_dir.name,
                    version="",
                    api_version=0,
                    entrypoint="",
                    capabilities=[],
                ),
                enabled=False,
                error=str(exc),
                directory=str(plugin_dir),
            )
            return

        enabled = self._prefs.get_plugin_config(manifest.name).enabled
        plugin = LoadedPlugin(manifest=manifest, enabled=enabled, directory=str(plugin_dir))
        plugins[manifest.name] = plugin
        if not enabled:
            return

        try:
            instance = self._instantiate(plugin_dir, manifest)
        except Exception as exc:  # noqa: BLE001 - a broken plugin must never crash the host
            logger.error("plugins.load_failed name=%s: %s", manifest.name, exc)
            plugin.error = f"Failed to load: {exc}"
            plugin.enabled = False
            return

        active: list[str] = []
        for capability in manifest.capabilities:
            protocol = CAPABILITY_PROTOCOLS.get(capability)
            if protocol is None:
                logger.info(
                    "plugins.capability_reserved name=%s capability=%s (not active yet)",
                    manifest.name,
                    capability,
                )
                continue
            if isinstance(instance, protocol):
                active.append(capability)
            else:
                logger.warning(
                    "plugins.capability_unimplemented name=%s capability=%s",
                    manifest.name,
                    capability,
                )
        plugin.instance = instance
        plugin.active_capabilities = active

    def _instantiate(self, plugin_dir: Path, manifest: PluginManifest) -> object:
        module_name, _, class_name = manifest.entrypoint.partition(":")
        module_path = plugin_dir / f"{module_name}.py"
        if not module_path.is_file():
            raise ManifestError(f"entrypoint module {module_name}.py not found")
        # namespaced so two plugins may both ship e.g. plugin.py
        full_name = f"droppedneedle_plugin.{manifest.name}.{module_name}"
        spec = importlib.util.spec_from_file_location(full_name, module_path)
        if spec is None or spec.loader is None:
            raise ManifestError("entrypoint module could not be prepared")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        entry_cls = getattr(module, class_name, None)
        if entry_cls is None:
            raise ManifestError(f"entrypoint class {class_name} not found")
        context = PluginContext(
            plugin_name=manifest.name,
            settings=self._settings_getter(manifest.name),
            http=self._plugin_http_client(manifest.name),
            publish=self._publisher_for(manifest.name),
        )
        return entry_cls(context)

    def _publisher_for(self, name: str) -> Any:
        """Host-bound ``ctx.publish`` for one plugin: stamps the caller name so
        a plugin can never spoof ``source_plugin``. A plugin without the
        ``publisher`` capability gets an ``ok=False`` result (never a raise),
        so ``on_tick`` code needs no capability check before publishing."""

        def _publish(
            kind: str,
            payload: Any = None,
            *,
            principal: str = "",
            causation_id: str | None = None,
        ) -> PluginPublishResult:
            if principal:
                logger.warning(
                    "plugins.publish_principal_dropped name=%s kind=%s",
                    name,
                    kind,
                )
            return self.publish_from_plugin(
                name, kind, payload, causation_id=causation_id
            )

        return _publish

    def _settings_getter(self, name: str):
        def _get() -> dict[str, str]:
            return self._prefs.get_plugin_config(name).settings

        return _get

    @staticmethod
    def _plugin_http_client(name: str):
        from infrastructure.http.client import HttpClientFactory

        # one named client per plugin: the factory caches by name and the
        # first caller's kwargs win, so plugins never share timeout surprises
        return HttpClientFactory.get_client(name=f"plugin-{name}", timeout=30.0)

    # -- queries --

    def list_plugins(self) -> list[LoadedPlugin]:
        return list(self._plugins.values())

    def get(self, name: str) -> LoadedPlugin | None:
        return self._plugins.get(name)

    def _active(self, capability: str) -> list[LoadedPlugin]:
        return [
            p
            for p in self._plugins.values()
            if p.enabled and p.instance is not None and capability in p.active_capabilities
        ]

    def purchase_providers(self) -> list[LoadedPlugin]:
        return self._active("purchase_links")

    def download_clients(self) -> list[LoadedPlugin]:
        return self._active("download_client")

    def indexers(self) -> list[LoadedPlugin]:
        return self._active("indexer")

    def subscribers(self) -> list[LoadedPlugin]:
        return self._active("subscriber")

    def publishers(self) -> list[LoadedPlugin]:
        return self._active("publisher")

    def metadata_providers(self) -> list[LoadedPlugin]:
        return self._active("metadata_provider")


    # -- capability dispatch (all best-effort, per-plugin isolation) --

    async def dispatch_scrobble(self, event: ScrobbleEvent) -> None:
        for plugin in self._active("scrobbler"):
            try:
                await plugin.instance.on_scrobble(event)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - plugin errors never break scrobbling
                logger.warning(
                    "plugins.scrobble_failed name=%s: %s", plugin.manifest.name, exc
                )

    async def gather_purchase_links(
        self, artist: str, album: str, release_group_mbid: str
    ) -> list[PluginPurchaseLink]:
        links: list[PluginPurchaseLink] = []
        for plugin in self._active("purchase_links"):
            try:
                async with asyncio.timeout(10):
                    links.extend(
                        await plugin.instance.purchase_links(  # type: ignore[union-attr]
                            artist, album, release_group_mbid
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - plugin errors never break Get-it
                logger.warning(
                    "plugins.purchase_links_failed name=%s: %s", plugin.manifest.name, exc
                )
        return links

    # -- subscriber fan-out (true per-plugin tasks, unlike dispatch_scrobble) --

    async def dispatch_event(self, event: Any) -> None:
        """Fan one engine event out to every subscriber plugin without awaiting
        any of them: each notification is its own task with a done-callback,
        so a slow subscriber never delays the publishing flow. At most one
        in-flight notification per plugin; an overlapped dispatch is skipped
        and counted in ``dropped_event_counts``. ``scrobble``-kind events
        additionally reach v0 ``scrobbler`` plugins via ``dispatch_scrobble``."""
        kind = getattr(event, "kind", None) or "unknown"
        causation = getattr(event, "causation_id", None)
        now = time.time()
        self._sweep_causations(now)
        for plugin in self.subscribers():
            name = plugin.manifest.name
            dedup_key = (causation, name) if causation else None
            if dedup_key is not None and dedup_key in self._seen_causations:
                continue
            if name in self._event_inflight:
                self._dropped_events[name] = self._dropped_events.get(name, 0) + 1
                logger.warning(
                    "plugins.event_dropped name=%s kind=%s reason=slow-subscriber",
                    name,
                    kind,
                )
                continue
            if dedup_key is not None:
                self._seen_causations[dedup_key] = now
            self._event_inflight.add(name)
            task = asyncio.create_task(self._notify_one(plugin, event, kind))
            task.add_done_callback(_log_task_error)
        if kind == "scrobble":
            payload = getattr(event, "payload", None)
            if (
                payload is not None
                and hasattr(payload, "artist")
                and hasattr(payload, "track")
            ):
                task = asyncio.create_task(self.dispatch_scrobble(payload))
                task.add_done_callback(_log_task_error)

    async def _notify_one(
        self, plugin: LoadedPlugin, event: Any, kind: str
    ) -> None:
        name = plugin.manifest.name
        start = time.monotonic()
        token = _EVENT_DEPTH.set(_EVENT_DEPTH.get(0) + 1)
        causation_token = _EVENT_CAUSATION.set(getattr(event, "causation_id", None))
        try:
            try:
                async with asyncio.timeout(_EVENT_NOTIFY_TIMEOUT):
                    await plugin.instance.on_event(event)  # type: ignore[union-attr]
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one subscriber never breaks fan-out
                logger.warning(
                    "plugins.event_failed name=%s kind=%s: %s", name, kind, exc
                )
                return
            duration_ms = (time.monotonic() - start) * 1000
            logger.info(
                "plugins.event_notified name=%s kind=%s duration_ms=%.1f",
                name,
                kind,
                duration_ms,
            )
        finally:
            _EVENT_DEPTH.reset(token)
            _EVENT_CAUSATION.reset(causation_token)
            self._event_inflight.discard(name)

    def _sweep_causations(self, now: float) -> None:
        """Bounded causation-dedup set: opportunistic sweep when the cap is
        hit, plus a lazy 60 s periodic sweep (no background task)."""
        if (
            now - self._causation_sweep_at < _CAUSATION_SWEEP_INTERVAL
            and len(self._seen_causations) < _CAUSATION_CAP
        ):
            return
        self._causation_sweep_at = now
        cutoff = now - _CAUSATION_TTL
        for key, seen_at in list(self._seen_causations.items()):
            if seen_at <= cutoff:
                del self._seen_causations[key]
        while len(self._seen_causations) > _CAUSATION_CAP:
            self._seen_causations.pop(next(iter(self._seen_causations)))

    # -- publisher ingress (plugin -> engine hints, allowlisted and guarded) --

    def publish_from_plugin(
        self,
        plugin_name: str,
        kind: str,
        payload: Any = None,
        *,
        principal: str = "",
        causation_id: str | None = None,
    ) -> PluginPublishResult:
        """Validate and enqueue one plugin hint for the engine to consume via
        ``drain_published``. Unknown kinds raise loudly; field, depth, rate,
        and queue overflows return a result (never raise into the caller).
        Own-task ownership (``download_note``) is enforced by the queue
        consumer, which can see the task store - the host only stamps
        ``source_plugin`` so the consumer can check it. ``principal`` is
        trusted engine input for rate keying only: the plugin-bound
        ``ctx.publish`` closure never forwards a caller-supplied value, so
        rotating plugin-supplied principals share one per-plugin bucket."""
        plugin = self._plugins.get(plugin_name)
        if (
            plugin is None
            or not plugin.enabled
            or plugin.instance is None
            or "publisher" not in plugin.active_capabilities
        ):
            logger.warning(
                "plugins.publish_rejected name=%s kind=%s reason=unknown-or-disabled",
                plugin_name,
                kind,
            )
            return PluginPublishResult(
                ok=False, status=404, error="unknown or disabled publisher"
            )
        if kind not in _PUBLISH_KINDS:
            raise ValueError(f"unknown publish kind {kind!r}")
        data, error = self._coerce_publish_payload(kind, payload)
        if error is not None:
            logger.warning(
                "plugins.publish_rejected name=%s kind=%s reason=%s",
                plugin_name,
                kind,
                error,
            )
            return PluginPublishResult(ok=False, status=422, error=error)
        depth = _EVENT_DEPTH.get(0)
        if depth > 1:
            logger.warning(
                "plugins.publish_dropped name=%s kind=%s reason=max-depth",
                plugin_name,
                kind,
            )
            return PluginPublishResult(
                ok=False, status=409, error="max publish depth exceeded"
            )
        retry_after = self._publish_rate_hit(plugin_name, principal)
        if retry_after:
            logger.warning(
                "plugins.publish_ratelimited name=%s kind=%s retry_after=%s",
                plugin_name,
                kind,
                retry_after,
            )
            return PluginPublishResult(
                ok=False, status=429, retry_after=retry_after, error="rate_limited"
            )
        parent_causation = _EVENT_CAUSATION.get(None)
        if depth > 0 and parent_causation:
            # Inside a subscriber notification: ignore the caller-supplied id and
            # inherit the parent's, so the re-dispatch dedups against every
            # subscriber that already saw the parent. A rotating caller id dodged
            # that dedup and ping-ponged forever under depth-1 + 30/min alone.
            # Depth-0 publishes (routes, ticks) keep fresh ids.
            causation_id = parent_causation
        else:
            causation_id = causation_id or uuid.uuid4().hex
        record = {
            "source_plugin": plugin_name,
            "kind": kind,
            "payload": data,
            "principal": principal,
            "causation_id": causation_id,
            "depth": depth + 1,
            "queued_at": time.time(),
        }
        if len(self._publish_queue) >= _PUBLISH_QUEUE_MAX:
            self._publish_queue.popleft()
            self._publish_dropped += 1
            logger.warning(
                "plugins.publish_dropped name=%s kind=%s reason=queue-full",
                plugin_name,
                kind,
            )
        self._publish_queue.append(record)
        return PluginPublishResult(ok=True, status=200)

    @staticmethod
    def _coerce_publish_payload(
        kind: str, payload: Any
    ) -> tuple[dict[str, Any] | None, str | None]:
        if payload is None:
            data: dict[str, Any] = {}
        elif isinstance(payload, dict):
            data = dict(payload)
        elif isinstance(payload, AppStruct):
            data = dict(payload)
        else:
            return None, f"payload must be an object for kind {kind!r}"
        allowed = set(_PUBLISH_REQUIRED_FIELDS[kind])
        unknown = sorted(key for key in data if key not in allowed)
        if unknown:
            return None, f"unknown fields for kind {kind!r}: {unknown}"
        # Free text defaults like the protocol structs (note/body = ""); references
        # (target_source/task_id/title) must be present and non-empty.
        for optional in ("note", "body"):
            if optional in allowed:
                value = data.get(optional, "")
                if not isinstance(value, str):
                    return None, f"missing fields for kind {kind!r}: [{optional!r}]"
                data.setdefault(optional, "")
        missing = [
            field
            for field in _PUBLISH_REQUIRED_FIELDS[kind]
            if field not in ("note", "body")
            and (not isinstance(data.get(field), str) or not data[field].strip())
        ]
        if missing:
            return None, f"missing fields for kind {kind!r}: {missing}"
        if kind == "download_note" and len(data["note"].encode("utf-8")) > (
            _PUBLISH_NOTE_MAX_BYTES
        ):
            return None, "note exceeds 1 KiB"
        return {key: data[key] for key in allowed}, None

    def _publish_rate_hit(self, plugin_name: str, principal: str) -> int:
        """Record a publish; return 0 when allowed, else Retry-After seconds."""
        now = time.time()
        cutoff = now - _PUBLISH_RATE_WINDOW
        key = (plugin_name, principal)
        hits = self._publish_rate.get(key)
        if hits is None:
            if len(self._publish_rate) >= _PUBLISH_RATE_CAP:
                for old_key, old_hits in list(self._publish_rate.items()):
                    if not old_hits or old_hits[-1] <= cutoff:
                        del self._publish_rate[old_key]
                while len(self._publish_rate) >= _PUBLISH_RATE_CAP:
                    self._publish_rate.pop(next(iter(self._publish_rate)))
            hits = self._publish_rate[key] = []
        while hits and hits[0] <= cutoff:
            hits.pop(0)
        if len(hits) >= _PUBLISH_RATE_LIMIT:
            return max(1, int(hits[0] + _PUBLISH_RATE_WINDOW - now) + 1)
        hits.append(now)
        return 0

    def drain_published(self) -> list[dict[str, Any]]:
        """Take every queued hint for the engine consumer (Wave 2+ services)."""
        records = list(self._publish_queue)
        self._publish_queue.clear()
        return records

    # -- guarded custom routes (plugin HTTP under /ext/) --

    async def handle_plugin_route(
        self,
        plugin_name: str,
        method: str,
        subpath: str,
        query: dict[str, str] | None = None,
        body: Any = None,
    ) -> PluginRouteResult:
        """Run one declared plugin route with timeout + isolation. Disabled
        plugins, undeclared paths, and non-GET/POST/DELETE methods are 404
        (no oracle); plugin-chosen statuses pass only when 200-299, 400, or
        404, else map to 200/502 per the fixed table; any failure is a
        generic 502 (never a traceback). Auth and request rate limits live
        in the route/service layer, which owns the principal."""
        plugin = self._plugins.get(plugin_name)
        if (
            plugin is None
            or not plugin.enabled
            or plugin.instance is None
            or "publisher" not in plugin.active_capabilities
        ):
            return PluginRouteResult(status=404, body=_route_not_found())
        verb = (method or "").upper()
        if verb not in _ROUTE_METHODS:
            return PluginRouteResult(status=404, body=_route_not_found())
        declared = False
        for route in getattr(plugin.manifest, "routes", []) or []:
            if getattr(route, "path", None) == subpath and (
                getattr(route, "method", "GET") or "GET"
            ).upper() == verb:
                declared = True
                break
        if not declared:
            return PluginRouteResult(status=404, body=_route_not_found())
        handler = getattr(plugin.instance, "handle_route", None)
        if handler is None:
            logger.warning("plugins.route_no_handler name=%s", plugin_name)
            return PluginRouteResult(status=502, body=_route_failed())
        try:
            async with asyncio.timeout(_ROUTE_TIMEOUT):
                response = await handler(verb, subpath, dict(query or {}), body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolation: never a traceback
            logger.warning(
                "plugins.route_failed name=%s method=%s path=%s: %s",
                plugin_name,
                verb,
                subpath,
                exc,
            )
            return PluginRouteResult(status=502, body=_route_failed())
        try:
            status = int(getattr(response, "status", 200))
        except (TypeError, ValueError):
            status = 200
        if not (200 <= status <= 299 or status in (400, 404)):
            status = 502 if 500 <= status <= 599 else 200
        response_body = getattr(response, "body", None)
        try:
            raw = msgspec.json.encode(response_body)
        except Exception:  # noqa: BLE001 - unserialisable body is a plugin failure
            logger.warning(
                "plugins.route_failed name=%s method=%s path=%s: unserialisable body",
                plugin_name,
                verb,
                subpath,
            )
            return PluginRouteResult(status=502, body=_route_failed())
        if len(raw) > _ROUTE_BODY_MAX_BYTES:
            logger.warning(
                "plugins.route_failed name=%s method=%s path=%s: body over 1 MiB",
                plugin_name,
                verb,
                subpath,
            )
            return PluginRouteResult(status=502, body=_route_failed())
        return PluginRouteResult(status=status, body=response_body)

    # -- scheduler ticks (one TaskRegistry loop per scheduler plugin) --

    def _desired_ticks(self) -> dict[str, tuple[float, bool]]:
        """Enabled scheduler plugins mapped to (interval_seconds, run_on_load).
        ``load_all`` only records this set via the swap + ``generation`` bump;
        loops are rebuilt solely by ``sync_ticks``."""
        desired: dict[str, tuple[float, bool]] = {}
        for name, plugin in self._plugins.items():
            if (
                not plugin.enabled
                or plugin.instance is None
                or "scheduler" not in plugin.active_capabilities
            ):
                continue
            schedule = getattr(plugin.manifest, "schedule", None)
            try:
                interval_minutes = int(
                    getattr(schedule, "interval_minutes", 60) or 60
                )
            except (TypeError, ValueError):
                interval_minutes = 60
            interval_minutes = min(max(interval_minutes, 5), 1440)
            run_on_load = bool(getattr(schedule, "run_on_load", False))
            desired[name] = (interval_minutes * 60.0, run_on_load)
        return desired

    def start_scheduled_ticks(self) -> None:
        """Start a loop per desired scheduler plugin; live names keep theirs."""
        for name, (interval_s, run_on_load) in sorted(self._desired_ticks().items()):
            self._start_tick_loop(name, interval_s, run_on_load)

    async def stop_scheduled_ticks(self) -> None:
        """Cancel every scheduler loop (shutdown path; ``cancel_all`` covers them)."""
        registry = TaskRegistry.get_instance()
        for name in sorted(
            task_name
            for task_name in registry.get_all()
            if task_name.startswith(_TICK_PREFIX)
        ):
            await registry.cancel(name)
        self._tick_intervals.clear()

    async def sync_ticks(self) -> None:
        """Rebuild loops to match the desired set: cancel removed/changed
        intervals, start added ones. The sole rebuild choke point, called
        after ``load_all`` from the plugin save route and startup lifecycle."""
        registry = TaskRegistry.get_instance()
        desired = self._desired_ticks()
        live = {
            task_name[len(_TICK_PREFIX):]
            for task_name in registry.get_all()
            if task_name.startswith(_TICK_PREFIX)
        }
        for name in sorted(live - set(desired)):
            await registry.cancel(f"{_TICK_PREFIX}{name}")
            self._tick_intervals.pop(name, None)
        for name in sorted(set(desired) - live):
            interval_s, run_on_load = desired[name]
            self._start_tick_loop(name, interval_s, run_on_load)
        for name in sorted(set(desired) & live):
            interval_s, run_on_load = desired[name]
            if self._tick_intervals.get(name) != interval_s:
                await registry.cancel(f"{_TICK_PREFIX}{name}")
                self._tick_intervals.pop(name, None)
                self._start_tick_loop(name, interval_s, run_on_load)

    def _start_tick_loop(
        self, plugin_name: str, interval_s: float, run_on_load: bool
    ) -> None:
        registry = TaskRegistry.get_instance()
        task = asyncio.create_task(
            self._tick_loop(plugin_name, interval_s, run_on_load)
        )
        try:
            registry.register(f"{_TICK_PREFIX}{plugin_name}", task)
        except RuntimeError:
            task.cancel()  # a live loop owns this name; keep it
        else:
            self._tick_intervals[plugin_name] = interval_s

    async def _tick_loop(
        self, plugin_name: str, interval_s: float, run_on_load: bool
    ) -> None:
        """One plugin's schedule: no overlap (an overrun delays itself plus
        one full interval), a hung tick is cancelled at the interval, an
        exception logs and continues, a missed tick is skipped (no backfill).
        The plugin instance resolves fresh every sweep, never captured, so a
        settings-save singleton rebuild applies on the next tick; a disabled
        or removed plugin exits its loop (``sync_ticks`` owns rebuilds)."""
        if not run_on_load:
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                return
        while True:
            plugin = self._plugins.get(plugin_name)
            if (
                plugin is None
                or not plugin.enabled
                or plugin.instance is None
                or "scheduler" not in plugin.active_capabilities
            ):
                return
            tick = getattr(plugin.instance, "on_tick", None)
            if tick is None:
                return
            try:
                async with asyncio.timeout(interval_s):
                    await tick()
            except asyncio.CancelledError:
                break
            except TimeoutError:
                logger.warning(
                    "plugins.tick_timeout name=%s interval_s=%s",
                    plugin_name,
                    interval_s,
                )
            except Exception as exc:  # noqa: BLE001 - a tick failure never kills the loop
                logger.warning(
                    "plugins.tick_failed name=%s: %s", plugin_name, exc, exc_info=True
                )
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                break
