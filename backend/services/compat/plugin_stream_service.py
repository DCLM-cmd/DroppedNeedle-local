"""Plugin streaming fallback (v1 streaming_source capability).

Local-first: compat shims call :meth:`PluginStreamService.resolve` only after
the local library misses, so a plugin can never hijack a locally owned track.
``resolve`` is first-hit-wins over name-sorted enabled plugins with per-plugin
isolation (``asyncio.timeout(5)`` + ``try/except`` -> ``None``).

Path refs are contained to exactly the plugin dir + configured downloads dir
+ library roots (``resolve()`` + ``is_relative_to``; symlink escape -> ``None``).
URL refs are proxied through a dedicated ``plugin-stream-proxy`` factory client
(timeout 10, ``follow_redirects=False``, max 3 re-validated redirects) with
SSRF blocking AFTER DNS on the connected IP.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urljoin, urlsplit

from fastapi import Depends
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

try:
    from core.dependencies._registry import singleton
except ImportError:
    from functools import lru_cache as _lru_cache
    from typing import Callable as _Callable, TypeVar as _TypeVar
    _F = _TypeVar("_F", bound=_Callable)
    def singleton(fn: _F) -> _F:
        return _lru_cache(maxsize=1)(fn)  # type: ignore[return-value]

if TYPE_CHECKING:
    from api.compat.common.deps import CompatServices
    from infrastructure.plugins.host import PluginHost
    from infrastructure.plugins.protocols import PluginStreamRef

logger = logging.getLogger(__name__)
_RESOLVE_TIMEOUT_S = 5.0
_DNS_TIMEOUT_S = 5.0
_PROXY_TIMEOUT_S = 10.0
_MAX_REDIRECTS = 3
_PROXY_MAX_BYTES = 500 * 1024 * 1024
_PROXY_IDLE_TIMEOUT_S = 30.0


class PluginStreamService:
    """Byte-source fallback over ``streaming_source`` plugins.

    ``compat_services`` is an optional back-reference (default ``None``) so the
    service stays constructible from the host alone in tests; routers pass
    explicit services to the module helpers below instead of relying on it.
    """

    def __init__(
        self,
        host: PluginHost | None,
        compat_services: CompatServices | None = None,
    ) -> None:
        self._host = host
        self._compat = compat_services

    async def resolve(
        self, recording_mbid: str, user_id: str
    ) -> PluginStreamRef | None:
        """First plugin claiming ``recording_mbid`` wins (name-sorted).

        Args are plain strings only (already-authed user id) - never the user
        record, never the token. Unknown MBID, timeout, or plugin error all
        read as ``None`` (fall through to the local 404) with a log line.
        """
        if not recording_mbid or not isinstance(recording_mbid, str):
            return None
        if not user_id or not isinstance(user_id, str):
            return None
        mbid = str(recording_mbid)
        uid = str(user_id)
        host = self._host
        if host is None:
            return None
        try:
            plugins = _streaming_plugins(host)
        except Exception as exc:  # noqa: BLE001 - absence reads as no plugins
            logger.warning("plugin_stream.list_failed: %s", exc)
            return None
        for plugin in plugins:
            name = getattr(getattr(plugin, "manifest", None), "name", "?")
            try:
                instance = getattr(plugin, "instance", None)
                if instance is None:
                    continue
                handler = getattr(instance, "resolve_stream", None)
                if handler is None:
                    continue
                async with asyncio.timeout(_RESOLVE_TIMEOUT_S):
                    ref = await handler(mbid, uid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one plugin never breaks fallback
                logger.warning("plugin_stream.resolve_failed name=%s: %s", name, exc)
                continue
            if ref is None:
                continue
            if not _is_valid_ref(ref, name):
                continue
            return ref
        return None

    def allowed_roots(self) -> list[Path]:
        """Exactly plugin dir + configured downloads dir + library roots."""
        return _allowed_roots(self._host, self._compat)


def _streaming_plugins(host: Any) -> list[Any]:
    getter = getattr(host, "streaming_sources", None)
    if callable(getter):
        try:
            plugins = list(getter())
            plugins.sort(key=lambda p: getattr(getattr(p, "manifest", None), "name", ""))
            return plugins
        except Exception:  # noqa: BLE001 - fall through to filtered list
            pass
    try:
        plugins = list(host.list_plugins())
    except Exception:  # noqa: BLE001 - absence reads as no plugins
        return []
    out = [
        p
        for p in plugins
        if getattr(p, "enabled", False)
        and getattr(p, "instance", None) is not None
        and "streaming_source" in (getattr(p, "active_capabilities", None) or [])
    ]
    out.sort(key=lambda p: getattr(getattr(p, "manifest", None), "name", ""))
    return out


def _is_valid_ref(ref: Any, plugin_name: str) -> bool:
    try:
        if isinstance(ref, dict):
            has_path = bool(str(ref.get("path") or "").strip())
            has_url = bool(str(ref.get("url") or "").strip())
        else:
            has_path = bool(str(getattr(ref, "path", "") or "").strip())
            has_url = bool(str(getattr(ref, "url", "") or "").strip())
    except Exception:  # noqa: BLE001 - malformed ref reads as invalid
        logger.warning("plugin_stream.invalid_ref name=%s reason=unreadable", plugin_name)
        return False
    if has_path == has_url:  # both or neither: exactly one must be set
        logger.warning(
            "plugin_stream.invalid_ref name=%s reason=path-url-exclusive", plugin_name
        )
        return False
    return True


def _ref_path(ref: Any) -> str:
    if isinstance(ref, dict):
        return str(ref.get("path") or "")
    return str(getattr(ref, "path", "") or "")


def _ref_url(ref: Any) -> str:
    if isinstance(ref, dict):
        return str(ref.get("url") or "")
    return str(getattr(ref, "url") or "")


def _ref_content_type(ref: Any) -> str:
    if isinstance(ref, dict):
        return str(ref.get("content_type") or "")
    return str(getattr(ref, "content_type", "") or "")


def _ref_duration(ref: Any) -> float:
    try:
        value = ref.get("duration_seconds") if isinstance(ref, dict) else getattr(ref, "duration_seconds", None)
    except Exception:  # noqa: BLE001 - malformed reads as unknown
        return 0.0
    return float(value) if isinstance(value, (int, float)) and value > 0 else 0.0


def _allowed_roots(host: Any | None, compat: Any | None) -> list[Path]:
    roots: list[Path] = []
    try:
        host_dir = getattr(host, "_dir", None) if host is not None else None
        if host_dir is not None:
            roots.append(Path(host_dir).resolve())
        else:
            from core.config import get_settings

            roots.append((get_settings().root_app_dir / "plugins").resolve())
    except Exception as exc:  # noqa: BLE001 - unresolvable root is skipped
        logger.warning("plugin_stream.roots_plugin_dir_failed: %s", exc)
    try:
        from core.config import get_settings

        roots.append(Path(get_settings().slskd_downloads_path).resolve())
    except Exception as exc:  # noqa: BLE001 - unresolvable root is skipped
        logger.warning("plugin_stream.roots_downloads_failed: %s", exc)
    lib_paths: list[str] = []
    try:
        if compat is not None:
            try:
                typed = compat.preferences.get_typed_library_settings()
                lib_paths = [r.path for r in typed.library_roots if r.path]
            except Exception as exc:  # noqa: BLE001 - fall through to global prefs
                logger.warning("plugin_stream.roots_library_compat_failed: %s", exc)
        if not lib_paths:
            from core.dependencies import get_preferences_service

            prefs = get_preferences_service()
            typed = prefs.get_typed_library_settings()
            lib_paths = [r.path for r in typed.library_roots if r.path]
    except Exception as exc:  # noqa: BLE001 - no library roots is not fatal
        logger.warning("plugin_stream.roots_library_failed: %s", exc)
    for raw in lib_paths:
        try:
            roots.append(Path(raw).resolve())
        except Exception:  # noqa: BLE001 - one bad root never blocks the rest
            continue
    seen: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.append(root)
    return seen


async def validated_plugin_path(
    service: PluginStreamService, raw_path: str
) -> Path | None:
    """Contained-file check: ``resolve()`` + ``is_relative_to`` over exactly the
    allowed roots. Symlink escape, missing file, or outside-roots all read as
    ``None`` with a log line (fall through to the local 404)."""
    if not raw_path or not isinstance(raw_path, str):
        return None
    try:
        resolved = Path(raw_path).expanduser().resolve()
    except Exception as exc:  # noqa: BLE001 - unresolvable reads as miss
        logger.warning("plugin_stream.path_invalid: %s", exc)
        return None
    try:
        roots = service.allowed_roots()
    except Exception as exc:  # noqa: BLE001 - roots failure reads as miss
        logger.warning("plugin_stream.path_roots_failed: %s", exc)
        return None
    allowed = False
    for root in roots:
        try:
            if resolved.is_relative_to(root):
                allowed = True
                break
        except Exception:  # noqa: BLE001 - one bad root never blocks the rest
            continue
    if not allowed:
        logger.warning("plugin_stream.path_outside_roots path=%s", resolved.name)
        return None
    try:
        is_file = await asyncio.to_thread(resolved.is_file)
    except Exception as exc:  # noqa: BLE001 - stat failure reads as miss
        logger.warning("plugin_stream.path_stat_failed path=%s: %s", resolved.name, exc)
        return None
    if not is_file:
        logger.warning("plugin_stream.path_not_file path=%s", resolved.name)
        return None
    return resolved


def get_plugin_stream_proxy_client() -> Any:
    """Dedicated ``plugin-stream-proxy`` factory client (timeout 10, no redirects).

    A separate name is required because the factory caches by name and the
    first caller's kwargs win - the shared default client cannot be retuned
    without affecting MusicBrainz et al.
    """
    from infrastructure.http.client import HttpClientFactory

    return HttpClientFactory.get_client(
        name="plugin-stream-proxy",
        timeout=_PROXY_TIMEOUT_S,
        follow_redirects=False,
    )


def _ip_is_blocked(addr: ipaddress._BaseAddress) -> bool:
    return bool(
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or not addr.is_global
    )
async def _resolve_host_ips(hostname: str, port: int) -> list[str] | None:
    try:
        async with asyncio.timeout(_DNS_TIMEOUT_S):
            infos = await asyncio.to_thread(
                socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM
            )
    except TimeoutError:
        logger.warning("plugin_stream.dns_timeout host=%s", hostname)
        return None
    except OSError:
        return None
    if not infos:
        return None
    return [str(sockaddr[0]).split("%", 1)[0] for _, _, _, _, sockaddr in infos]


def _ip_str_blocked(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return _ip_is_blocked(addr)

def _connected_peer_ip(response: Any) -> str | None:
    """Peer IP of the CONNECTED socket (closes the DNS-rebind TOCTOU window).

    Reads the httpx/httpcore network-stream extension (``server_addr``) left on
    the response by the transport. ``None`` when the transport exposes no peer
    (non-network fakes) - the pre-connect DNS checks stay the gate there; a
    present-but-blocked peer reads as blocked.
    """
    try:
        extensions = getattr(response, "extensions", None) or {}
        network_stream = (
            extensions.get("network_stream") if isinstance(extensions, dict) else None
        )
        if network_stream is None:
            getter = getattr(getattr(response, "stream", None), "get_extra_info", None)
            if not callable(getter):
                return None
            server_addr = getter("server_addr")
        else:
            server_addr = network_stream.get_extra_info("server_addr")
    except Exception:  # noqa: BLE001 - no peer info reads as unknown, not blocked
        return None
    try:
        if isinstance(server_addr, (tuple, list)):
            host = server_addr[0] if server_addr else None
        else:
            host = server_addr
        if not host or not isinstance(host, str):
            return None
        return host.split("%", 1)[0]
    except Exception:  # noqa: BLE001 - unparsable reads as unknown
        return None


async def _hostname_allowed(hostname: str, port: int) -> bool:
    if not hostname or not isinstance(hostname, str):
        return False
    lowered = hostname.lower().strip().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        logger.warning("plugin_stream.ssrf_blocked host=%s reason=localhost", hostname)
        return False
    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        pass
    else:
        if _ip_is_blocked(literal):
            logger.warning(
                "plugin_stream.ssrf_blocked host=%s reason=literal-private", hostname
            )
            return False
        return True
    ips = await _resolve_host_ips(hostname, port)
    if ips is None:
        logger.warning("plugin_stream.ssrf_blocked host=%s reason=dns-failed", hostname)
        return False
    for ip_str in ips:
        if _ip_str_blocked(ip_str):
            logger.warning(
                "plugin_stream.ssrf_blocked host=%s reason=connected-private", hostname
            )
            return False
    return True


async def _recheck_host_before_connect(hostname: str, port: int) -> bool:
    if not hostname or not isinstance(hostname, str):
        return False
    lowered = hostname.lower().strip().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        logger.warning("plugin_stream.ssrf_blocked host=%s reason=rebind-localhost", hostname)
        return False
    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        pass
    else:
        if _ip_is_blocked(literal):
            logger.warning(
                "plugin_stream.ssrf_blocked host=%s reason=rebind-literal-private", hostname
            )
            return False
        return True
    ips = await _resolve_host_ips(hostname, port)
    if ips is None:
        logger.warning("plugin_stream.ssrf_blocked host=%s reason=rebind-dns-failed", hostname)
        return False
    for ip_str in ips:
        if _ip_str_blocked(ip_str):
            logger.warning(
                "plugin_stream.ssrf_blocked host=%s reason=rebind-private", hostname
            )
            return False
    return True


async def validated_plugin_url(raw_url: str) -> str | None:
    """SSRF-checked URL (AFTER DNS on the connected IP). ``None`` + log on any
    block: bad scheme, localhost hostname, loopback/link-local/RFC1918
    connected IP (covers 127.0.0.1 and 169.254.169.254), or DNS failure."""
    if not raw_url or not isinstance(raw_url, str):
        return None
    candidate = raw_url.strip()
    if not candidate:
        return None
    try:
        parts = urlsplit(candidate)
    except Exception as exc:  # noqa: BLE001 - malformed reads as miss
        logger.warning("plugin_stream.url_invalid: %s", exc)
        return None
    if parts.scheme.lower() not in ("http", "https"):
        logger.warning("plugin_stream.url_blocked reason=scheme")
        return None
    hostname = parts.hostname
    if not hostname:
        logger.warning("plugin_stream.url_blocked reason=no-host")
        return None
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    try:
        allowed = await _hostname_allowed(hostname, port)
    except Exception as exc:  # noqa: BLE001 - check failure reads as blocked
        logger.warning("plugin_stream.url_check_failed: %s", exc)
        return None
    if not allowed:
        return None
    return candidate


def synthetic_track_for_ref(recording_mbid: str, ref: Any, hint: str) -> Any:
    """Minimal ViewTrack so the shared ``decide()``/``stream()`` policy applies
    to plugin bytes too (url MAY carry transcode hints - owner override)."""
    from services.compat.view_models import ViewTrack

    fmt = ""
    try:
        path_part = urlsplit(hint).path if "://" in hint else hint
        suffix = Path(path_part).suffix.lower().lstrip(".")
        if suffix:
            fmt = suffix
    except Exception:  # noqa: BLE001 - suffix failure falls through to content-type
        pass
    if not fmt:
        content_type = _ref_content_type(ref).lower().split(";", 1)[0].strip()
        mapping = {
            "audio/mpeg": "mp3",
            "audio/mp3": "mp3",
            "audio/ogg": "opus",
            "audio/opus": "opus",
            "audio/flac": "flac",
            "audio/x-flac": "flac",
            "audio/wav": "wav",
            "audio/x-wav": "wav",
            "audio/mp4": "mp4",
            "audio/aac": "aac",
        }
        fmt = mapping.get(content_type, "")
        if not fmt and content_type.startswith("audio/"):
            fmt = content_type.split("/", 1)[1].split("+", 1)[0].strip()
    return ViewTrack(
        file_id=f"plugin:{recording_mbid}",
        title="",
        album_title="",
        file_format=fmt,
        duration_seconds=_ref_duration(ref),
        recording_mbid=recording_mbid,
        musicbrainz_recording_id=recording_mbid,
    )


def _content_type_for_path(path: Path, fallback: str = "") -> str:
    try:
        from services.local_files_service import CONTENT_TYPE_MAP

        found = CONTENT_TYPE_MAP.get(path.suffix.lower(), "")
        if found:
            return found
    except Exception:  # noqa: BLE001 - map failure falls through to suffix guess
        pass
    suffix = path.suffix.lower()
    mapping = {
        ".mp3": "audio/mpeg",
        ".opus": "audio/ogg",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
    }
    return mapping.get(suffix, fallback or "application/octet-stream")

def _post_open_within_roots(fd: int, original: Path, roots: list[Path]) -> bool:
    """Post-open containment: the OPEN fd must still resolve inside ``roots``.

    Linux: ``fstat`` (must be a regular file) + ``/proc/self/fd`` target within
    roots. Elsewhere (or with no /proc): re-resolve the path after opening
    (doubled resolve check). ``False`` on any failure so the caller closes the
    fd and reads the ref as a miss.
    """
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
    except OSError:
        return False
    try:
        target = os.readlink(f"/proc/self/fd/{fd}")
        resolved = Path(target).resolve()
    except OSError:
        try:
            resolved = Path(original).resolve()
        except OSError:
            return False
    for root in roots:
        try:
            if resolved.is_relative_to(root):
                return True
        except Exception:  # noqa: BLE001 - one bad root never blocks the rest
            continue
    return False


async def _path_post_open_ok(path: Path, roots: list[Path]) -> bool:
    """Open-check-close gate: open ``path``, run the post-open containment
    check on the live fd, close it. ``False`` when the open itself fails."""
    try:
        fd = await asyncio.to_thread(os.open, path, os.O_RDONLY)
    except OSError:
        return False
    try:
        return await asyncio.to_thread(_post_open_within_roots, fd, path, roots)
    finally:
        try:
            await asyncio.to_thread(os.close, fd)
        except OSError:
            pass


async def stream_plugin_path_response(
    *,
    service: PluginStreamService,
    recording_mbid: str,
    user_id: str,
    validated_path: Path,
    ref: Any,
    requested_format: str | None,
    max_bitrate_kbps: int | None,
    force_original: bool,
    start_seconds: float,
    settings: Any,
    concurrency: Any,
    transcode: Any,
    range_header: str | None,
    is_disconnected: Any | None,
    estimate: bool = False,
) -> Response | None:
    """Serve a contained plugin file through the shared decide/stream policy."""
    from services.compat.stream_concurrency import StreamCapacityError, leased_chunks
    from services.compat.transcode_service import decide, ffmpeg_available

    track = synthetic_track_for_ref(recording_mbid, ref, str(validated_path))
    try:
        plan = decide(
            track,
            requested_format=requested_format,
            max_bitrate_kbps=max_bitrate_kbps,
            force_original=force_original,
            start_seconds=start_seconds,
            settings=settings,
            ffmpeg_available=ffmpeg_available(),
        )
    except Exception as exc:  # noqa: BLE001 - decide failure reads as miss
        logger.warning("plugin_stream.decide_failed: %s", exc)
        return None
    roots = service.allowed_roots()
    if plan.transcode:
        # Re-check the live fd right before ffmpeg opens the path: the file may
        # have been swapped between validation and use. The transcode lease is
        # only acquired inside transcode.stream, after this gate passes.
        if not await _path_post_open_ok(validated_path, roots):
            logger.warning(
                "plugin_stream.path_post_open_escape path=%s", validated_path.name
            )
            return None
        try:
            return await transcode.stream(
                str(validated_path),
                plan,
                principal=user_id,
                is_disconnected=is_disconnected,
                estimate=estimate,
            )
        except StreamCapacityError:
            return Response(status_code=429, headers={"Retry-After": "1"})
        except Exception as exc:  # noqa: BLE001 - transcode failure reads as miss
            logger.warning("plugin_stream.transcode_failed: %s", exc)
            return None
    try:
        chunks, headers, status = await _stream_validated_file(
            validated_path, range_header, roots
        )
    except _RangeNotSatisfiable as exc:
        return Response(
            status_code=416, headers={"Content-Range": f"bytes */{exc.file_size}"}
        )
    except Exception as exc:  # noqa: BLE001 - read failure reads as miss
        logger.warning("plugin_stream.path_read_failed: %s", exc)
        return None
    try:
        lease = await concurrency.acquire_direct(user_id)
    except StreamCapacityError:
        return Response(status_code=429, headers={"Retry-After": "1"})
    out_headers = {**headers, "Content-Encoding": "identity"}
    try:
        return StreamingResponse(
            leased_chunks(chunks, lease),
            status_code=status,
            headers=out_headers,
            media_type=headers.get("Content-Type", "application/octet-stream"),
            background=BackgroundTask(lease.release),
        )
    except BaseException:
        await lease.release()
        raise


class _RangeNotSatisfiable(Exception):
    def __init__(self, file_size: int) -> None:
        super().__init__(file_size)
        self.file_size = file_size


async def _stream_validated_file(
    path: Path, range_header: str | None, roots: list[Path]
) -> tuple[Any, dict[str, str], int]:
    import re

    try:
        stat_result = await asyncio.to_thread(path.stat)
    except OSError as exc:
        raise FileNotFoundError(str(exc)) from exc
    file_size = stat_result.st_size
    # Post-open gate before any byte is promised: the fd opened here must still
    # resolve inside the roots. Failure raises (the caller reads it as a miss)
    # so no lease is acquired and no headers are emitted for an escaped file.
    if not await _path_post_open_ok(path, roots):
        logger.warning("plugin_stream.path_post_open_escape path=%s", path.name)
        raise FileNotFoundError(str(path))
    content_type = _content_type_for_path(path)
    if range_header:
        match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", range_header.strip())
        if match is None or not any(match.groups()) or file_size == 0:
            raise _RangeNotSatisfiable(file_size)
        start_str, end_str = match.groups()
        if not start_str:
            suffix_len = int(end_str)
            if suffix_len <= 0:
                raise _RangeNotSatisfiable(file_size)
            start = max(0, file_size - suffix_len)
            end = file_size - 1
        elif not end_str:
            start = int(start_str)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str)
        end = min(end, file_size - 1)
        if start < 0 or start > end or start >= file_size:
            raise _RangeNotSatisfiable(file_size)
        length = end - start + 1
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
        }
        return _iter_validated_file(path, start, length, roots), headers, 206
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(file_size),
        "Accept-Ranges": "bytes",
    }
    return _iter_validated_file(path, 0, file_size, roots), headers, 200


async def _iter_validated_file(path: Path, offset: int, length: int, roots: list[Path]):  # type: ignore[no-untyped-def]
    from infrastructure.constants import STREAM_CHUNK_SIZE

    try:
        fd = await asyncio.to_thread(os.open, path, os.O_RDONLY)
    except OSError as exc:
        logger.warning("plugin_stream.path_read_error path=%s: %s", path.name, exc)
        return
    try:
        # The fd actually being read must still resolve inside the roots: a
        # swap between validation and this open yields silence, not bytes.
        ok = await asyncio.to_thread(_post_open_within_roots, fd, path, roots)
        if not ok:
            logger.warning("plugin_stream.path_post_open_escape path=%s", path.name)
            return
        await asyncio.to_thread(os.lseek, fd, offset, os.SEEK_SET)
        remaining = length
        while remaining > 0:
            data = await asyncio.to_thread(
                os.read, fd, min(STREAM_CHUNK_SIZE, remaining)
            )
            if not data:
                break
            remaining -= len(data)
            yield data
    except OSError as exc:
        logger.warning("plugin_stream.path_read_error path=%s: %s", path.name, exc)
    finally:
        try:
            await asyncio.to_thread(os.close, fd)
        except OSError:
            pass


def _temp_suffix_for(ref: Any, validated_url: str) -> str:
    try:
        suffix = Path(urlsplit(validated_url).path).suffix.lower()
        if suffix and len(suffix) <= 8 and suffix[1:].isalnum():
            return suffix
    except Exception:  # noqa: BLE001 - suffix failure falls back to .bin
        pass
    return ".bin"


async def _write_chunks_to_fd(fd: int, chunks: Any) -> None:
    """Drain proxied ``chunks`` into the open ``fd``; always closes the fd."""
    try:
        async for chunk in chunks:
            data = bytes(chunk)
            offset = 0
            while offset < len(data):
                written = await asyncio.to_thread(os.write, fd, data[offset:])
                if written <= 0:
                    raise OSError("short write to plugin-stream temp file")
                offset += written
    finally:
        try:
            await asyncio.to_thread(os.close, fd)
        except OSError:
            pass


def _with_temp_cleanup(response: Any, tmppath: str) -> Any:
    """Remove ``tmppath`` once the transcode body drains, errors, or is
    closed early - never before ffmpeg opens it."""
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        try:
            os.unlink(tmppath)
        except OSError:
            pass
        return response

    async def _cleaning_body():  # type: ignore[no-untyped-def]
        try:
            async for chunk in body_iterator:
                yield chunk
        finally:
            try:
                await asyncio.to_thread(os.unlink, tmppath)
            except OSError:
                pass

    try:
        response.body_iterator = _cleaning_body()
    except Exception:  # noqa: BLE001 - wrapper failure still cleans up
        try:
            os.unlink(tmppath)
        except OSError:
            pass
    return response


async def _transcode_plugin_url(
    *,
    validated_url: str,
    ref: Any,
    user_id: str,
    plan: Any,
    transcode: Any,
    is_disconnected: Any | None,
    estimate: bool = False,
) -> Response | None:
    """Fetch a plugin URL through the capped proxy into a temp file, then hand
    the FILE to ``transcode.stream``. ffmpeg never touches the network; the
    proxy's byte cap and per-redirect re-validation apply to the fetch. The
    transcode lease is acquired inside ``transcode.stream`` - after the fetch
    validates - and the temp file is removed once the body drains (or on any
    error before ffmpeg takes ownership)."""
    from services.compat.stream_concurrency import StreamCapacityError

    try:
        proxied = await _proxy_direct(validated_url, None, ref)
    except Exception as exc:  # noqa: BLE001 - fetch setup failure reads as miss
        logger.warning("plugin_stream.url_fetch_failed: %s", exc)
        return None
    if proxied is None:
        return None
    chunks, _headers, _status, _media_type, upstream = proxied
    try:
        fd, tmppath = tempfile.mkstemp(
            prefix="plugin-stream-", suffix=_temp_suffix_for(ref, validated_url)
        )
    except OSError as exc:
        logger.warning("plugin_stream.url_temp_failed: %s", exc)
        try:
            await upstream.aclose()
        except Exception:  # noqa: BLE001 - close failure is not a miss
            pass
        return None
    try:
        try:
            await _write_chunks_to_fd(fd, chunks)
        finally:
            try:
                await upstream.aclose()
            except Exception:  # noqa: BLE001 - close failure is not a miss
                pass
        try:
            response = await transcode.stream(
                tmppath,
                plan,
                principal=user_id,
                is_disconnected=is_disconnected,
                estimate=estimate,
            )
        except StreamCapacityError:
            return Response(status_code=429, headers={"Retry-After": "1"})
        except Exception as exc:  # noqa: BLE001 - transcode failure reads as miss
            logger.warning("plugin_stream.url_transcode_failed: %s", exc)
            return None
        owned = tmppath
        tmppath = ""
        return _with_temp_cleanup(response, owned)
    finally:
        if tmppath:
            try:
                await asyncio.to_thread(os.unlink, tmppath)
            except OSError:
                pass


async def stream_plugin_url_response(
    *,
    recording_mbid: str,
    user_id: str,
    ref: Any,
    validated_url: str,
    requested_format: str | None,
    max_bitrate_kbps: int | None,
    force_original: bool,
    start_seconds: float,
    settings: Any,
    concurrency: Any,
    transcode: Any,
    range_header: str | None,
    is_disconnected: Any | None,
    estimate: bool = False,
) -> Response | None:
    """Proxy a plugin URL (range-passthrough) or transcode it via the shared
    decide/stream policy when the client asked for a transcode."""
    from services.compat.stream_concurrency import StreamCapacityError
    from services.compat.transcode_service import decide, ffmpeg_available

    track = synthetic_track_for_ref(recording_mbid, ref, validated_url)
    try:
        plan = decide(
            track,
            requested_format=requested_format,
            max_bitrate_kbps=max_bitrate_kbps,
            force_original=force_original,
            start_seconds=start_seconds,
            settings=settings,
            ffmpeg_available=ffmpeg_available(),
        )
    except Exception as exc:  # noqa: BLE001 - decide failure reads as miss
        logger.warning("plugin_stream.decide_failed: %s", exc)
        return None
    if plan.transcode:
        # ffmpeg never fetches network: the URL is pulled through the capped,
        # re-validating proxy into a temp file and ffmpeg reads the FILE.
        return await _transcode_plugin_url(
            validated_url=validated_url,
            ref=ref,
            user_id=user_id,
            plan=plan,
            transcode=transcode,
            is_disconnected=is_disconnected,
            estimate=estimate,
        )
    # The concurrency lease is acquired AFTER the proxy validates, connects,
    # and returns bytes - never before. A blocked/over-cap upstream costs no
    # lease and never surfaces as a 429.
    try:
        proxied = await _proxy_direct(validated_url, range_header, ref)
    except Exception as exc:  # noqa: BLE001 - proxy setup failure reads as miss
        logger.warning("plugin_stream.proxy_failed: %s", exc)
        return None
    if proxied is None:
        return None
    chunks, headers, status, media_type, upstream = proxied
    try:
        lease = await concurrency.acquire_direct(user_id)
    except StreamCapacityError:
        try:
            await upstream.aclose()
        except Exception:  # noqa: BLE001 - close failure is not a miss
            pass
        return Response(status_code=429, headers={"Retry-After": "1"})

    async def _body():  # type: ignore[no-untyped-def]
        try:
            async for chunk in chunks:
                yield chunk
        finally:
            try:
                await upstream.aclose()
            except Exception:  # noqa: BLE001 - close failure is not a miss
                pass
            await lease.release()

    out_headers = {**headers, "Content-Encoding": "identity"}
    try:
        return StreamingResponse(
            _body(),
            status_code=status,
            headers=out_headers,
            media_type=media_type,
            background=BackgroundTask(lease.release),
        )
    except BaseException:
        await lease.release()
        try:
            await upstream.aclose()
        except Exception:  # noqa: BLE001 - close failure is not a miss
            pass
        raise


async def _proxy_direct(
    initial_url: str, range_header: str | None, ref: Any
) -> tuple[Any, dict[str, str], int, str, Any] | None:
    """GET ``initial_url`` with Range passthrough; follow at most 3 re-validated
    redirects. Returns ``(chunks, headers, status, media_type, response)`` where
    ``response`` stays open until the caller drains/closes it. ``None`` + log on
    SSRF block, redirect-to-loopback, or non-200/206."""
    client = get_plugin_stream_proxy_client()
    current = initial_url
    for _ in range(_MAX_REDIRECTS + 1):
        checked = await validated_plugin_url(current)
        if checked is None:
            return None
        try:
            hop = urlsplit(checked)
        except Exception as exc:  # noqa: BLE001 - malformed reads as miss
            logger.warning("plugin_stream.proxy_url_invalid: %s", exc)
            return None
        hop_port = hop.port or (443 if hop.scheme.lower() == "https" else 80)
        try:
            pinned = await _recheck_host_before_connect(hop.hostname or "", hop_port)
        except Exception as exc:  # noqa: BLE001 - recheck failure reads as blocked
            logger.warning("plugin_stream.proxy_recheck_failed: %s", exc)
            return None
        if not pinned:
            return None
        out_headers: dict[str, str] = {}
        if range_header:
            out_headers["Range"] = range_header
        try:
            request = client.build_request("GET", checked, headers=out_headers)
            response = await client.send(request, stream=True)
        except Exception as exc:  # noqa: BLE001 - network failure reads as miss
            logger.warning("plugin_stream.proxy_request_failed: %s", exc)
            return None
        # Connected-IP gate: the socket's actual peer must be public even when
        # pre-connect DNS looked clean (DNS-rebind TOCTOU, incl. the ffmpeg
        # path which no longer fetches at all). Unknown peer (no extension)
        # keeps the pre-connect verdict.
        peer_ip = _connected_peer_ip(response)
        if peer_ip is not None and _ip_str_blocked(peer_ip):
            logger.warning(
                "plugin_stream.proxy_connected_private host=%s", hop.hostname or ""
            )
            try:
                await response.aclose()
            except Exception:  # noqa: BLE001 - close failure is not a miss
                pass
            return None
        if response.is_redirect:
            location = response.headers.get("location")
            try:
                await response.aclose()
            except Exception:  # noqa: BLE001 - close failure is not a miss
                pass
            if not location:
                logger.warning("plugin_stream.proxy_redirect_no_location")
                return None
            try:
                current = urljoin(checked, location)
            except Exception as exc:  # noqa: BLE001 - bad Location reads as miss
                logger.warning("plugin_stream.proxy_redirect_bad: %s", exc)
                return None
            continue
        if response.status_code not in (200, 206):
            logger.warning(
                "plugin_stream.proxy_bad_status status=%s", response.status_code
            )
            try:
                await response.aclose()
            except Exception:  # noqa: BLE001 - close failure is not a miss
                pass
            return None
        upstream_type = (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        hinted = _ref_content_type(ref).split(";", 1)[0].strip() or upstream_type
        media_type = hinted or "application/octet-stream"
        headers: dict[str, str] = {"Accept-Ranges": "bytes"}
        for key in ("Content-Length", "Content-Range", "Accept-Ranges"):
            value = response.headers.get(key)
            if value is not None:
                headers[key] = value
        headers["Content-Type"] = media_type
        if response.status_code == 206 and "Content-Range" not in headers:
            content_range = response.headers.get("content-range")
            if content_range:
                headers["Content-Range"] = content_range
        declared_length = response.headers.get("Content-Length")
        if declared_length is not None:
            try:
                if int(str(declared_length).strip()) > _PROXY_MAX_BYTES:
                    logger.warning(
                        "plugin_stream.proxy_over_cap bytes=%s cap=%s",
                        declared_length,
                        _PROXY_MAX_BYTES,
                    )
                    try:
                        await response.aclose()
                    except Exception:  # noqa: BLE001 - close failure is not a miss
                        pass
                    return None
            except (TypeError, ValueError):
                pass

        async def _chunks(resp: Any = response):  # type: ignore[no-untyped-def]
            total = 0
            stream = resp.aiter_bytes()
            while True:
                try:
                    async with asyncio.timeout(_PROXY_IDLE_TIMEOUT_S):
                        chunk = await stream.__anext__()
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    logger.warning(
                        "plugin_stream.proxy_idle_timeout cap_s=%s", _PROXY_IDLE_TIMEOUT_S
                    )
                    try:
                        await resp.aclose()
                    except Exception:  # noqa: BLE001 - close failure is not a miss
                        pass
                    return
                total += len(chunk)
                if total > _PROXY_MAX_BYTES:
                    logger.warning(
                        "plugin_stream.proxy_over_cap bytes=%s cap=%s",
                        total,
                        _PROXY_MAX_BYTES,
                    )
                    try:
                        await resp.aclose()
                    except Exception:  # noqa: BLE001 - close failure is not a miss
                        pass
                    return
                yield chunk

        return _chunks(), headers, response.status_code, media_type, response


async def stream_plugin_ref_response(
    *,
    service: PluginStreamService,
    ref: Any,
    recording_mbid: str,
    user_id: str,
    requested_format: str | None,
    max_bitrate_kbps: int | None,
    force_original: bool,
    start_seconds: float,
    settings: Any,
    concurrency: Any,
    transcode: Any,
    range_header: str | None,
    is_disconnected: Any | None,
    estimate: bool = False,
) -> Response | None:
    """Dispatch one resolved ref to the path or url branch. ``None`` + log when
    the ref fails containment/SSRF so the caller falls through to local."""
    raw_path = _ref_path(ref)
    raw_url = _ref_url(ref)
    if raw_path:
        validated_path = await validated_plugin_path(service, raw_path)
        if validated_path is None:
            return None
        return await stream_plugin_path_response(
            service=service,
            recording_mbid=recording_mbid,
            user_id=user_id,
            validated_path=validated_path,
            ref=ref,
            requested_format=requested_format,
            max_bitrate_kbps=max_bitrate_kbps,
            force_original=force_original,
            start_seconds=start_seconds,
            settings=settings,
            concurrency=concurrency,
            transcode=transcode,
            range_header=range_header,
            is_disconnected=is_disconnected,
            estimate=estimate,
        )
    if raw_url:
        validated_url = await validated_plugin_url(raw_url)
        if validated_url is None:
            return None
        return await stream_plugin_url_response(
            recording_mbid=recording_mbid,
            user_id=user_id,
            ref=ref,
            validated_url=validated_url,
            requested_format=requested_format,
            max_bitrate_kbps=max_bitrate_kbps,
            force_original=force_original,
            start_seconds=start_seconds,
            settings=settings,
            concurrency=concurrency,
            transcode=transcode,
            range_header=range_header,
            is_disconnected=is_disconnected,
            estimate=estimate,
        )
    return None


@singleton
def get_plugin_stream_service() -> PluginStreamService:
    """Singleton over the live plugin host; absence reads as no plugins."""
    try:
        from core.dependencies.service_providers import get_plugin_host

        host = get_plugin_host()
    except Exception:  # noqa: BLE001 - absence reads as no plugins
        host = None
    return PluginStreamService(host)


PluginStreamServiceDep = Annotated[
    PluginStreamService, Depends(get_plugin_stream_service)
]
