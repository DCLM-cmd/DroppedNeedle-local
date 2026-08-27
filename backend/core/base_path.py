"""Deployment base-path handling shared across settings, runtime rewrite, and ASGI serving.

One strict normalizer plus one raw-ASGI middleware power every base-aware
surface so the security contract (canonical ASCII path, segment-safe matching,
prefix answered before routing/auth/static) lives in exactly one place.
"""

import re
from collections.abc import Mapping

from models.error import NOT_FOUND, error_response

MAX_BASE_PATH_LENGTH = 256

# Canonical URL path segment: RFC 3986 unreserved characters except '%'.
_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9._~-]+")


class BasePathError(ValueError):
    """Raised when a configured deployment base path is not canonical."""


def normalize_base_path(raw: str | None) -> str:
    """Return ``''`` or a strict canonical ``/seg[/seg...]`` base path.

    Fails closed instead of coercing: surrounding whitespace, escapes
    (``%xx``, ``\\``), query/fragment characters, control or non-ASCII bytes,
    dot segments, empty segments, and anything over ``MAX_BASE_PATH_LENGTH``
    characters raise :class:`BasePathError`. The returned value is safe to
    embed verbatim in static assets and byte-compare against server paths.
    """
    if raw is None:
        return ""
    value = str(raw)
    if value == "":
        return ""
    if not value.startswith("/"):
        raise BasePathError(
            f"Invalid BASE_PATH {value!r}: must be an absolute path starting with '/'."
        )
    if len(value) > MAX_BASE_PATH_LENGTH:
        raise BasePathError(
            f"Invalid BASE_PATH: exceeds the {MAX_BASE_PATH_LENGTH}-character limit."
        )
    if value.endswith("/"):
        raise BasePathError(f"Invalid BASE_PATH {value!r}: must not end with '/'.")

    segments: list[str] = []
    for segment in value.split("/")[1:]:
        if not segment:
            raise BasePathError(
                f"Invalid BASE_PATH {value!r}: contains an empty path segment."
            )
        if segment == "." or segment == "..":
            raise BasePathError(
                f"Invalid BASE_PATH {value!r}: relative '.'/'..' segments are forbidden."
            )
        if _SEGMENT_PATTERN.fullmatch(segment) is None:
            raise BasePathError(
                f"Invalid BASE_PATH segment {segment!r}: allowed characters are "
                "A-Z a-z 0-9 . _ ~ -"
            )
        segments.append(segment)
    return "/" + "/".join(segments)


def scope_base_path(
    scope: Mapping[str, object],
    fallback: str = "",
) -> str:
    """Return a canonical ASGI root path, degrading invalid upstream state."""
    raw = scope.get("root_path") or fallback
    try:
        return normalize_base_path(str(raw))
    except BasePathError:
        return ""


def application_path(scope: Mapping[str, object]) -> str:
    """Return the request path relative to the ASGI ``root_path``."""
    path = scope.get("path", "")
    root_path = scope.get("root_path", "")
    if not isinstance(path, str) or not isinstance(root_path, str):
        return ""
    if not root_path:
        return path
    if path == root_path:
        return "/"
    if path.startswith(root_path + "/"):
        return path[len(root_path) :]
    return path


class BasePathMiddleware:
    """Raw-ASGI middleware that serves the wrapped app under a fixed base path.

    Matching is byte-exact and segment-aware. Accepted requests keep their full
    ASGI path and append the base to ``root_path`` so Starlette can route nested
    mounts correctly. Unmatched requests never reach routing, auth, or static
    mounts; lifespans and empty bases delegate untouched.
    """

    def __init__(self, app: object, base_path: str) -> None:
        self.app = app
        self.base_path = base_path

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] == "lifespan" or not self.base_path:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            await self._handle_http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._handle_websocket(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    def _strip(self, path: str) -> str | None:
        """Return the stripped request path, or ``None`` when it does not match."""
        if path == self.base_path:
            return "/"
        if not path.startswith(self.base_path + "/"):
            return None
        rest = path[len(self.base_path) :]
        # Exactly one strip per hop: a remainder that still spells the base is
        # a doubled prefix, refused instead of recursed into.
        if rest == self.base_path or rest.startswith(self.base_path + "/"):
            return None
        for segment in rest.split("/"):
            if segment == "." or segment == "..":
                return None
        return rest

    def _raw_prefix_matches(self, scope: Mapping[str, object]) -> bool:
        raw_path = scope.get("raw_path")
        if raw_path is None:
            return True
        root_path = scope.get("root_path", "")
        if not isinstance(raw_path, bytes) or not isinstance(root_path, str):
            return False
        try:
            prefix = f"{root_path}{self.base_path}".encode("ascii")
        except UnicodeEncodeError:
            return False
        return raw_path == prefix or raw_path.startswith(prefix + b"/")

    async def _handle_http(self, scope: dict, receive: object, send: object) -> None:
        if (
            not self._raw_prefix_matches(scope)
            or self._strip(application_path(scope)) is None
        ):
            response = error_response(404, NOT_FOUND, "Not found")
            await response(scope, receive, send)
            return
        forwarded = dict(scope)
        forwarded["root_path"] = scope.get("root_path", "") + self.base_path
        forwarded.pop("raw_path", None)
        await self.app(forwarded, receive, send)

    async def _handle_websocket(
        self, scope: dict, receive: object, send: object
    ) -> None:
        if (
            not self._raw_prefix_matches(scope)
            or self._strip(application_path(scope)) is None
        ):
            await send({"type": "websocket.close", "code": 1008})
            return
        forwarded = dict(scope)
        forwarded["root_path"] = scope.get("root_path", "") + self.base_path
        forwarded.pop("raw_path", None)
        await self.app(forwarded, receive, send)
