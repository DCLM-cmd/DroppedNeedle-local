"""Plugin API routes (phase 01b).

Admin-only: list plugins, install one from GitHub, enable/disable, edit their
settings (mask-sentinel for secret fields), uninstall. There is deliberately no
route that makes a plugin acquire content (D22).
"""

import asyncio
import hashlib
import logging
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from api.v1.schemas.plugins import (
    PLUGIN_SECRET_MASK,
    PluginInfo,
    PluginInstallRequest,
    PluginListResponse,
    PluginSettingFieldInfo,
    PluginSourceInfo,
    PluginSourcesResponse,
    PluginUpdateRequest,
)
from api.v1.schemas.settings import PluginConfig
from core.dependencies import get_plugin_host, get_preferences_service
from api.v1.schemas.common import StatusMessageResponse
from core.exceptions import ResourceNotFoundError, ValidationError
from infrastructure.msgspec_fastapi import MsgSpecBody, MsgSpecJSONResponse, MsgSpecRoute
from middleware import CurrentAdminDep, CurrentUserDep

logger = logging.getLogger(__name__)

router = APIRouter(route_class=MsgSpecRoute, prefix="/plugins", tags=["plugins"])

_EXT_BODY_MAX_BYTES = 1024 * 1024
_EXT_RATE_CAP = 10_000
_EXT_RATE_WINDOW = 60.0
_EXT_RATE: dict[tuple[str, str, str, str], list[float]] = {}
def _ext_declared_length(request: Request) -> int | None:
    try:
        headers = getattr(request, "headers", None)
        if headers is None:
            return None
        get = getattr(headers, "get", None)
        if get is None:
            return None
        raw = get("content-length")
        if raw is None:
            return None
        value = int(str(raw).strip())
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


async def _read_ext_body_capped(request: Request, cap: int) -> bytes | None:
    stream = getattr(request, "stream", None)
    if callable(stream):
        parts: list[bytes] = []
        total = 0
        try:
            async for chunk in stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > cap:
                    return None
                parts.append(chunk)
        except Exception:  # noqa: BLE001 - truncated read behaves as received bytes
            return b"".join(parts)
        return b"".join(parts)
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001 - unreadable body reads as empty
        return b""
    if len(raw) > cap:
        return None
    return raw


_PLUGIN_KEY_RE = re.compile(r"^plugin:[a-z0-9][a-z0-9-]{0,31}$")
_SOURCE_HEALTHS = frozenset({"ok", "degraded", "error", "unknown"})


def _to_info(plugin, prefs) -> PluginInfo:  # noqa: ANN001
    manifest = plugin.manifest
    config = prefs.get_plugin_config(manifest.name)
    values: dict[str, str] = {}
    for field in manifest.settings:
        stored = config.settings.get(field.key, "")
        values[field.key] = (PLUGIN_SECRET_MASK if stored else "") if field.secret else stored
    sources: list[str] = []
    targets: list[str] = []
    for cap in getattr(manifest, "capability_configs", []) or []:
        source = str(getattr(cap, "source", "") or "")
        target = str(getattr(cap, "target_source", "") or "")
        if source and source not in sources:
            sources.append(source)
        if target and target not in targets:
            targets.append(target)
    return PluginInfo(
        name=manifest.name,
        display_name=manifest.display_name,
        version=manifest.version,
        enabled=plugin.enabled,
        capabilities=manifest.capabilities,
        active_capabilities=plugin.active_capabilities,
        description=manifest.description,
        author=manifest.author,
        homepage=manifest.homepage,
        error=plugin.error,
        settings_fields=[
            PluginSettingFieldInfo(
                key=f.key, label=f.label, help=f.help, secret=f.secret
            )
            for f in manifest.settings
        ],
        settings_values=values,
        ui_entry=getattr(manifest, "ui_entry", "") or "",
        ui_pages=list(getattr(manifest, "ui_pages", []) or []),
        ui_external_url=getattr(manifest, "ui_external_url", "") or "",
        sources=sources,
        targets=targets,
    )


def _clear_plugin_cache() -> None:
    from core.dependencies import (
        get_acquisition_cleanup_service,
        get_acquisition_dispatcher,
        get_album_preflight_scorer,
        get_discover_service,
        get_download_orchestrator,
        get_download_service,
        get_file_processor,
        get_home_service,
        get_newznab_indexer,
        get_newznab_release_scorer,
        get_plugin_release_scorer,
        get_plugin_source_registry,
        get_status_service,
        get_target_acquisition_dispatcher,
        get_target_discover_service,
        get_target_download_orchestrator,
        get_target_download_service,
        get_target_file_processor,
        get_target_home_service,
        get_target_status_service,
        get_track_matcher,
    )

    for provider in (
        get_album_preflight_scorer,
        get_download_orchestrator,
        get_target_download_orchestrator,
        get_download_service,
        get_target_download_service,
        get_acquisition_dispatcher,
        get_target_acquisition_dispatcher,
        get_plugin_source_registry,
        get_plugin_release_scorer,
        get_newznab_indexer,
        get_newznab_release_scorer,
        get_track_matcher,
        get_file_processor,
        get_target_file_processor,
        get_status_service,
        get_target_status_service,
        get_acquisition_cleanup_service,
        get_home_service,
        get_target_home_service,
        get_discover_service,
        get_target_discover_service,
    ):
        try:
            provider.cache_clear()
        except Exception:  # noqa: BLE001
            pass


def _sanitize_health(value: object) -> str:
    return str(value) if value in _SOURCE_HEALTHS else "unknown"


def _sanitize_target_source(value: object, fallback: str) -> str:
    text = str(value or "")
    if text == "usenet" or _PLUGIN_KEY_RE.match(text):
        return text
    if _PLUGIN_KEY_RE.match(fallback):
        return fallback
    return "unknown"


def _build_sources(host) -> list[PluginSourceInfo]:  # noqa: ANN001
    by_key: dict[str, PluginSourceInfo] = {}
    try:
        clients = list(host.download_clients())
    except Exception:  # noqa: BLE001
        clients = []
    try:
        indexers = list(host.indexers())
    except Exception:  # noqa: BLE001
        indexers = []
    client_ids = {id(p) for p in clients}
    indexer_ids = {id(p) for p in indexers}
    for plugin in clients + indexers:
        try:
            manifest = plugin.manifest
            name = manifest.name
            enabled = bool(plugin.enabled)
            is_client = id(plugin) in client_ids
            is_indexer = id(plugin) in indexer_ids
            source = ""
            target_source = ""
            display = manifest.display_name or name
            for cap in getattr(manifest, "capability_configs", []) or []:
                if getattr(cap, "id", "") == "download_client" and getattr(cap, "source", ""):
                    source = str(cap.source)
                    if getattr(cap, "display_name", ""):
                        display = str(cap.display_name)
                if getattr(cap, "id", "") == "indexer" and getattr(cap, "target_source", ""):
                    target_source = str(cap.target_source)
            key = f"plugin:{source}" if source else (target_source or "")
            if not _PLUGIN_KEY_RE.match(key):
                continue
            existing = by_key.get(key)
            health = "ok" if enabled and not plugin.error else ("degraded" if enabled else "unknown")
            if existing is None:
                by_key[key] = PluginSourceInfo(
                    key=key,
                    plugin=name,
                    display_name=display,
                    has_client=is_client,
                    has_indexer=is_indexer,
                    target_source=_sanitize_target_source(target_source or key, key),
                    configured=enabled and (is_client or is_indexer),
                    health=_sanitize_health(health),
                )
            else:
                existing.has_client = existing.has_client or is_client
                existing.has_indexer = existing.has_indexer or is_indexer
                existing.configured = existing.configured or (enabled and (is_client or is_indexer))
        except Exception:  # noqa: BLE001
            continue
    return sorted(by_key.values(), key=lambda item: item.key)


def _ext_route_spec(plugin, method: str, subpath: str):  # noqa: ANN001
    for route in getattr(plugin.manifest, "routes", []) or []:
        if getattr(route, "path", None) == subpath and (
            getattr(route, "method", "GET") or "GET"
        ).upper() == method:
            return route
    return None


def _ext_rate_hit(plugin_name: str, principal: str, method: str, subpath: str, limit_per_minute: int) -> int:
    now = time.time()
    cutoff = now - _EXT_RATE_WINDOW
    key = (plugin_name, principal, method, subpath)
    hits = _EXT_RATE.get(key)
    if hits is None:
        if len(_EXT_RATE) >= _EXT_RATE_CAP:
            for old_key, old_hits in list(_EXT_RATE.items()):
                if not old_hits or old_hits[-1] <= cutoff:
                    del _EXT_RATE[old_key]
            while len(_EXT_RATE) >= _EXT_RATE_CAP:
                _EXT_RATE.pop(next(iter(_EXT_RATE)))
        hits = _EXT_RATE[key] = []
    while hits and hits[0] <= cutoff:
        hits.pop(0)
    try:
        limit = max(1, int(limit_per_minute))
    except (TypeError, ValueError):
        limit = 60
    if len(hits) >= limit:
        return max(1, int(hits[0] + _EXT_RATE_WINDOW - now) + 1)
    hits.append(now)
    return 0


def _ext_not_found() -> MsgSpecJSONResponse:
    return MsgSpecJSONResponse(
        status_code=404,
        content={"error": {"code": "NOT_FOUND", "message": "Not found", "details": None}},
    )


async def _serve_ext(request: Request, current_user, plugin_name: str, subpath: str, method: str):  # noqa: ANN001
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not plugin_name or not subpath or ".." in subpath.split("/") or subpath.startswith("/"):
        raise ValidationError("Invalid plugin route")
    host = get_plugin_host()
    plugin = host.get(plugin_name)
    if plugin is None or not plugin.enabled or plugin.instance is None:
        return _ext_not_found()
    spec = _ext_route_spec(plugin, method, subpath)
    if spec is None:
        return _ext_not_found()
    if (getattr(spec, "auth", "admin") or "admin") == "admin" and getattr(current_user, "role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    principal = getattr(current_user, "id", "") or ""
    try:
        limit = int(getattr(spec, "rate_limit_per_minute", 60) or 60)
    except (TypeError, ValueError):
        limit = 60
    retry_after = _ext_rate_hit(plugin_name, principal, method, subpath, limit)
    if retry_after:
        return MsgSpecJSONResponse(
            status_code=429,
            content={"error": {"code": "RATE_LIMITED", "message": "Too many requests", "details": None}},
            headers={"Retry-After": str(retry_after)},
        )
    declared = _ext_declared_length(request)
    if declared is not None and declared > _EXT_BODY_MAX_BYTES:
        return MsgSpecJSONResponse(
            status_code=413,
            content={"error": {"code": "PAYLOAD_TOO_LARGE", "message": "Request body too large", "details": None}},
        )
    raw = await _read_ext_body_capped(request, _EXT_BODY_MAX_BYTES)
    if raw is None:
        return MsgSpecJSONResponse(
            status_code=413,
            content={"error": {"code": "PAYLOAD_TOO_LARGE", "message": "Request body too large", "details": None}},
        )
    body: object = None
    if raw:
        try:
            import msgspec

            body = msgspec.json.decode(raw)
        except Exception:  # noqa: BLE001
            try:
                body = raw.decode("utf-8")
            except Exception:  # noqa: BLE001
                body = None
    try:
        query = dict(request.query_params)
    except Exception:  # noqa: BLE001
        query = {}
    try:
        result = await host.handle_plugin_route(plugin_name, method, subpath, query, body)
    except Exception:  # noqa: BLE001
        logger.exception("plugins.ext_failed")
        return MsgSpecJSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "Internal server error", "details": None}},
        )
    try:
        return MsgSpecJSONResponse(status_code=result.status, content=result.body)
    except Exception:  # noqa: BLE001
        logger.warning("plugins.ext_unserialisable")
        return MsgSpecJSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "Internal server error", "details": None}},
        )


@router.get("/sources", response_model=PluginSourcesResponse)
async def list_plugin_sources(
    _: CurrentUserDep,
    host=Depends(get_plugin_host),
):
    try:
        return PluginSourcesResponse(sources=_build_sources(host))
    except Exception:  # noqa: BLE001
        logger.warning("plugins.sources_failed")
        return PluginSourcesResponse(sources=[])


@router.get("/ext/{plugin_name}/{subpath:path}")
async def plugin_ext_get(plugin_name: str, subpath: str, request: Request, current_user: CurrentUserDep):
    return await _serve_ext(request, current_user, plugin_name, subpath, "GET")


@router.post("/ext/{plugin_name}/{subpath:path}")
async def plugin_ext_post(plugin_name: str, subpath: str, request: Request, current_user: CurrentUserDep):
    return await _serve_ext(request, current_user, plugin_name, subpath, "POST")


@router.delete("/ext/{plugin_name}/{subpath:path}")
async def plugin_ext_delete(plugin_name: str, subpath: str, request: Request, current_user: CurrentUserDep):
    return await _serve_ext(request, current_user, plugin_name, subpath, "DELETE")

@router.get("/{plugin_name}/ui/panel.js")
async def plugin_panel_js(
    plugin_name: str,
    request: Request,
    _: CurrentAdminDep,
    host=Depends(get_plugin_host),
):
    if (
        not plugin_name
        or "/" in plugin_name
        or "\\" in plugin_name
        or ".." in plugin_name
        or plugin_name.startswith(".")
        or "\x00" in plugin_name
    ):
        raise ResourceNotFoundError("Plugin not found")
    try:
        plugin = host.get(plugin_name)
    except Exception:  # noqa: BLE001
        logger.exception("plugins.panel_lookup_failed")
        return MsgSpecJSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "Internal server error", "details": None}},
        )
    if plugin is None or not plugin.enabled or plugin.instance is None:
        raise ResourceNotFoundError("Plugin not found")
    ui_entry = str(getattr(plugin.manifest, "ui_entry", "") or "")
    if not ui_entry:
        raise ResourceNotFoundError("Plugin not found")
    normalized = ui_entry.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.startswith("http://")
        or normalized.startswith("https://")
        or ".." in normalized.split("/")
        or "\x00" in normalized
    ):
        raise ResourceNotFoundError("Plugin not found")
    directory = str(getattr(plugin, "directory", "") or "")
    if not directory:
        raise ResourceNotFoundError("Plugin not found")
    response = await asyncio.to_thread(
        _plugin_panel_response, directory, normalized, plugin_name, request.headers.get("if-none-match", "")
    )
    if not plugin.enabled or plugin.instance is None:
        raise ResourceNotFoundError("Plugin not found")
    return response


def _plugin_panel_response(directory: str, normalized: str, plugin_name: str, if_none_match: str):
    import errno
    import os
    import stat as stat_module
    from email.utils import formatdate
    from pathlib import Path

    from fastapi.responses import Response

    try:
        base = Path(directory).resolve()
        target = (base / normalized).resolve()
        if not target.is_relative_to(base):
            raise ResourceNotFoundError("Plugin not found")
        # Resolve internal symlinks first, then pin each directory without following
        # replacements. Metadata and bytes come from the same opened descriptor.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        parent_fd = os.open(target.anchor, flags)
        try:
            for component in target.parts[1:-1]:
                child_fd = os.open(component, flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child_fd
            file_fd = os.open(
                target.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
            )
        finally:
            os.close(parent_fd)
        try:
            stat = os.fstat(file_fd)
            if not stat_module.S_ISREG(stat.st_mode):
                raise ResourceNotFoundError("Plugin not found")
            etag = '"' + hashlib.sha1(f"{stat.st_mtime_ns}:{stat.st_size}:{plugin_name}".encode()).hexdigest() + '"'
            headers = {
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
                "Cache-Control": "private, max-age=60, must-revalidate",
                "ETag": etag,
                "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
            }
            candidates = [token.strip() for token in if_none_match.split(",")] if if_none_match else []
            if "*" in candidates or etag in candidates or f"W/{etag}" in candidates:
                return Response(status_code=304, headers=headers)
            with os.fdopen(file_fd, "rb", closefd=False) as bundle:
                return Response(content=bundle.read(), media_type="text/javascript", headers=headers)
        finally:
            os.close(file_fd)
    except ResourceNotFoundError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
            raise ResourceNotFoundError("Plugin not found") from exc
        logger.exception("plugins.panel_read_failed")
    except Exception:  # noqa: BLE001
        logger.exception("plugins.panel_read_failed")
    return MsgSpecJSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "Internal server error", "details": None}},
    )


@router.get("", response_model=PluginListResponse)
async def list_plugins(
    _: CurrentAdminDep,
    host=Depends(get_plugin_host),
    prefs=Depends(get_preferences_service),
):
    return PluginListResponse(plugins=[_to_info(p, prefs) for p in host.list_plugins()])


@router.post("/install", response_model=PluginInfo, status_code=201)
async def install_plugin(
    _: CurrentAdminDep,
    body: PluginInstallRequest = MsgSpecBody(PluginInstallRequest),
    host=Depends(get_plugin_host),
    prefs=Depends(get_preferences_service),
):
    """Download a public GitHub repo into the plugins folder. The code is stored,
    never executed: the plugin arrives DISABLED and an admin must enable it,
    exactly like a hand-copied folder."""
    from infrastructure.http.client import HttpClientFactory
    from infrastructure.plugins.host import PluginInstallError

    http = HttpClientFactory.get_client(name="plugin-install", timeout=60.0)
    try:
        name = await host.install_from_github(body.repository_url, http)
    except PluginInstallError as exc:
        raise ValidationError(str(exc)) from exc
    plugin = host.get(name)
    if plugin is None:
        raise ValidationError("The plugin installed but could not be read back")
    _clear_plugin_cache()
    try:
        ticks = host.sync_ticks()
        if asyncio.isfuture(ticks) or asyncio.iscoroutine(ticks):
            await ticks
    except Exception:  # noqa: BLE001
        logger.warning("plugins.sync_ticks_failed")
    return _to_info(plugin, prefs)


@router.put("/{plugin_name}", response_model=PluginInfo)
async def update_plugin(
    plugin_name: str,
    _: CurrentAdminDep,
    body: PluginUpdateRequest = MsgSpecBody(PluginUpdateRequest),
    host=Depends(get_plugin_host),
    prefs=Depends(get_preferences_service),
):
    plugin = host.get(plugin_name)
    if plugin is None:
        raise ResourceNotFoundError("Plugin not found")
    current = prefs.get_plugin_config(plugin_name)
    secret_keys = {f.key for f in plugin.manifest.settings if f.secret}
    merged: dict[str, str] = {}
    for key, value in body.settings.items():
        if key in secret_keys and value == PLUGIN_SECRET_MASK:
            merged[key] = current.settings.get(key, "")
        else:
            merged[key] = value
    prefs.save_plugin_config(plugin_name, PluginConfig(enabled=body.enabled, settings=merged))
    await asyncio.to_thread(host.load_all)
    _clear_plugin_cache()
    try:
        ticks = host.sync_ticks()
        if asyncio.isfuture(ticks) or asyncio.iscoroutine(ticks):
            await ticks
    except Exception:  # noqa: BLE001
        logger.warning("plugins.sync_ticks_failed")
    refreshed = host.get(plugin_name)
    if refreshed is None:
        raise ResourceNotFoundError("Plugin not found")
    return _to_info(refreshed, prefs)


@router.delete("/{plugin_name}", response_model=StatusMessageResponse)
async def uninstall_plugin(
    plugin_name: str,
    _: CurrentAdminDep,
    host=Depends(get_plugin_host),
):
    """Remove the plugin's folder. Its saved settings stay in config.json, so a
    reinstall picks them back up."""
    await asyncio.to_thread(host.uninstall, plugin_name)
    _clear_plugin_cache()
    try:
        ticks = host.sync_ticks()
        if asyncio.isfuture(ticks) or asyncio.iscoroutine(ticks):
            await ticks
    except Exception:  # noqa: BLE001
        logger.warning("plugins.sync_ticks_failed")
    return StatusMessageResponse(status="ok", message=f"Removed {plugin_name}")
