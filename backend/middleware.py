import logging
import time
from hashlib import sha256
from typing import Annotated, Callable

from fastapi import Depends, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from core.base_path import application_path

from infrastructure.degradation import (
    init_degradation_context,
    try_get_degradation_context,
    clear_degradation_context,
)
from infrastructure.persistence.auth_store import TokenRecord, UserRecord
from infrastructure.resilience.rate_limiter import (
    BoundedTTLMap,
    TokenBucketRateLimiter,
)
from infrastructure.msgspec_fastapi import MsgSpecJSONResponse

logger = logging.getLogger(__name__)

SLOW_REQUEST_THRESHOLD = 1.0

# Exact paths that require no authentication.
# Keep this list narrow, the old "/api/v1/auth/" prefix exempted admin routes too.
_PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    # Auth bootstrap
    "/api/v1/auth/setup/status",
    "/api/v1/auth/setup",
    "/api/v1/auth/providers",
    "/api/v1/auth/login",
    "/api/v1/auth/password-recovery/reset",
    # Logout is public so an expired session can still clear the cookie
    "/api/v1/auth/logout",
    # Third-party login flows
    "/api/v1/auth/plex/pin",
    "/api/v1/auth/plex/poll",
    "/api/v1/auth/jellyfin/login",
    "/api/v1/auth/oidc/authorize",
    "/api/v1/auth/oidc/callback",
    "/api/v1/auth/oidc/exchange",
    # Spotify OAuth callback identifies the user from the single-use `state` token
    # (like the OIDC callback), so it must work without a session cookie - an expired
    # cookie or a bearer-token client then lands on the graceful /profile?spotify=error
    # redirect instead of a raw 401.
    "/api/v1/me/connections/spotify/auth/callback",
    # MusicBrainz returns with a one-time token; only this exact callback is public.
    "/api/v1/library/contributions/musicbrainz/callback",
    # OpenAPI spec (single file)
    "/api/v1/openapi.json",
})

# Prefix matches for paths that have sub-routes (Swagger UI assets, etc.)
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/api/v1/docs",
    "/api/v1/redoc",
    "/api/v1/wrapped",
)

_NON_INTERACTIVE_API_PATHS: frozenset[str] = frozenset({
    "/api/v1/following/events",
    "/api/v1/now-playing/events",
})


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limiter with per-path overrides, keyed PER CLIENT.

    The budget is per caller, not per process. A single shared bucket makes every
    client compete for one allowance: one browser tab looping on a failing page
    exhausted the whole instance's 30/s and the OTHER pages started answering
    "Too many requests" - a 92 req/s burst from one tab produced 908 rejections
    across the library, artist and connection endpoints at once. Keying by client
    contains a runaway to the client that caused it.

    The key is the caller's credential when it carries one (so two users behind
    one NAT, or a browser and a media client on one machine, get separate
    budgets), else the connecting IP. Credentials are hashed - this map outlives
    the request, and it should not hold raw session tokens. This runs BEFORE
    ``AuthMiddleware``, so the credential is read from the request rather than
    from ``request.state``; an invalid token still gets a stable key and is
    rejected on its own merits a moment later.

    Buckets live in a ``BoundedTTLMap``: idle clients expire and the map is
    count-capped, so a stream of fresh identities cannot grow it without bound.
    Losing a bucket only refills that client's allowance - the failure mode is
    leniency, never a wrongly-rejected request.
    """

    def __init__(
        self,
        app: ASGIApp,
        default_rate: float = 120.0,
        default_capacity: int = 240,
        overrides: dict[str, tuple[float, int]] | None = None,
        *,
        max_clients: int = 10_000,
        entry_ttl_seconds: float = 15 * 60.0,
        clock: "Callable[[], float]" = time.monotonic,
    ):
        super().__init__(app)
        self._clock = clock
        self._default = self._make_map(
            default_rate, default_capacity, max_clients, entry_ttl_seconds
        )
        # First match wins, so a more specific path registered ahead of a broader
        # one keeps its own budget: '/api/v1/auth/setup/status' MUST precede
        # '/api/v1/auth/setup', or the bootstrap call every cold page load makes
        # inherits the deliberately tiny setup budget and the whole app fails to
        # render.
        self._overrides: list[tuple[str, BoundedTTLMap]] = [
            (prefix, self._make_map(rate, capacity, max_clients, entry_ttl_seconds))
            for prefix, (rate, capacity) in (overrides or {}).items()
        ]

    def _make_map(
        self, rate: float, capacity: int, max_clients: int, ttl_seconds: float
    ) -> "BoundedTTLMap":
        return BoundedTTLMap(
            max_entries=max_clients,
            ttl_seconds=ttl_seconds,
            factory=lambda: TokenBucketRateLimiter(rate=rate, capacity=capacity),
            clock=self._clock,
        )

    def _get_bucket_map(self, path: str) -> "BoundedTTLMap":
        for prefix, buckets in self._overrides:
            if path.startswith(prefix):
                return buckets
        return self._default

    @staticmethod
    def _client_key(request: Request) -> str:
        credential = AuthMiddleware._extract_bearer(request)
        if credential:
            return "tok:" + sha256(credential.encode("utf-8")).hexdigest()[:32]
        client = request.client
        return "ip:" + (client.host if client else "unknown")

    async def dispatch(self, request: Request, call_next):
        path = application_path(request.scope)
        if not path.startswith("/api/"):
            return await call_next(request)

        limiter = self._get_bucket_map(path).get(self._client_key(request))
        acquired = await limiter.try_acquire()

        if acquired:
            response = await call_next(request)
            response.headers["X-RateLimit-Limit"] = str(limiter.capacity)
            response.headers["X-RateLimit-Remaining"] = str(limiter.remaining)
            return response

        retry_after = limiter.retry_after()
        return MsgSpecJSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "Too many requests",
                    "details": None,
                }
            },
            headers={
                "Retry-After": str(int(retry_after)),
                "X-RateLimit-Limit": str(limiter.capacity),
                "X-RateLimit-Remaining": "0",
            },
        )


class DegradationMiddleware(BaseHTTPMiddleware):
    """Initialise a per-request DegradationContext and surface results in a header."""

    async def dispatch(self, request: Request, call_next):
        init_degradation_context()
        try:
            response = await call_next(request)
            ctx = try_get_degradation_context()
            if ctx and ctx.has_degradation():
                sources = ",".join(
                    name for name, status in ctx.summary().items() if status != "ok"
                )
                if sources:
                    response.headers["X-Degraded-Services"] = sources
            return response
        finally:
            clear_degradation_context()


class PerformanceMiddleware(BaseHTTPMiddleware):
    
    def __init__(self, app: ASGIApp):
        super().__init__(app)
    
    async def dispatch(self, request: Request, call_next):
        start_time = time.perf_counter()
        response = await call_next(request)
        process_time = time.perf_counter() - start_time
        
        response.headers["X-Response-Time"] = f"{process_time:.3f}s"
        
        if process_time > SLOW_REQUEST_THRESHOLD:
            logger.warning(
                f"Slow request: {request.method} {request.url.path} "
                f"took {process_time:.2f}s"
            )
        
        return response


class AuthMiddleware(BaseHTTPMiddleware):
    """Global Bearer token validation for all /api/* routes.
 
    Non-/api/* paths (frontend SPA, static assets) are skipped entirely.
    Public API routes are allowlisted above. All others return 401 if the
    token is missing, invalid, or expired.
 
    On success, injects into request.state:
        - user: UserRecord
        - token: TokenRecord
    """
 
    async def dispatch(self, request: Request, call_next):
        path = application_path(request.scope)
 
        # Non-API paths: SPA routes, static files, favicons, etc.
        if not path.startswith("/api/") and path != "/health":
            return await call_next(request)
 
        # Allowlisted public API routes
        if self._is_public(path):
            return await call_next(request)
 
        # All other /api/* routes require a valid token
        raw_token = self._extract_bearer(request)
        if not raw_token:
            return self._unauthorized("Not authenticated")
 
        # Lazy import to avoid circular imports at module load time
        from core.dependencies.auth_providers import get_auth_service
        auth_service = get_auth_service()
 
        result = await auth_service.verify_token(raw_token)
        if result is None:
            return self._unauthorized("Invalid or expired token")
 
        user, token = result
        request.state.user = user
        request.state.token = token

        workload_gate = None
        if self._tracks_interactive_activity(path):
            from core.dependencies.service_providers import (
                get_background_workload_gate,
            )

            workload_gate = get_background_workload_gate()
            workload_gate.begin_interactive_request()

        try:
            return await call_next(request)
        finally:
            if workload_gate is not None:
                workload_gate.end_interactive_request()

    @staticmethod
    def _tracks_interactive_activity(path: str) -> bool:
        """Exclude long-lived media and SSE connections from activity tracking."""
        if path in _NON_INTERACTIVE_API_PATHS:
            return False
        if path.startswith("/api/v1/stream/") or "/held-audio/" in path:
            return False
        return not path.endswith("/stream")

    @staticmethod
    def _is_public(path: str) -> bool:
        if path in _PUBLIC_PATHS:
            return True
        for prefix in _PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return True
        return False

    @staticmethod
    def _extract_bearer(request: Request) -> str | None:
        # Bearer token (programmatic / API clients)
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip() or None
        # httpOnly session cookie (browser)
        return request.cookies.get("droppedneedle_session") or None

    @staticmethod
    def _unauthorized(detail: str) -> MsgSpecJSONResponse:
        return MsgSpecJSONResponse(
            status_code = status.HTTP_401_UNAUTHORIZED,
            content = {"error": {"code": "UNAUTHORIZED", "message": detail, "details": None}},
            headers = {"WWW-Authenticate": "Bearer"},
        )


class HSTSMiddleware(BaseHTTPMiddleware):
    """Adds Strict-Transport-Security when hsts_max_age > 0 in security settings."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        is_https = (
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").lower() == "https"
        )
        if not is_https:
            return response
        from core.dependencies.cache_providers import get_preferences_service
        sec = get_preferences_service().get_security_settings()
        if sec.hsts_max_age > 0:
            value = f"max-age={sec.hsts_max_age}"
            if sec.hsts_include_subdomains:
                value += "; includeSubDomains"
            if sec.hsts_preload:
                value += "; preload"
            response.headers["Strict-Transport-Security"] = value
        return response


def _get_current_user(request: Request) -> UserRecord:
    """Extract the already verified user from request.state.

    The middleware has already validated the token by the time any route
    handler runs, so this is a zero-cost lookup with no DB call.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail = "Not authenticated",
            headers = {"WWW-Authenticate": "Bearer"},
        )
    return user


def _get_current_admin(request: Request) -> UserRecord:
    """Like _get_current_user but also enforces admin role."""
    user = _get_current_user(request)
    if user.role != "admin":
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail = "Admin access required",
        )
    return user


def _get_current_curator(request: Request) -> UserRecord:
    """Like _get_current_user but requires the admin OR trusted role - the curator
    surfaces (quality upgrades, edition pins; CollectionManagement D18)."""
    user = _get_current_user(request)
    if user.role not in ("admin", "trusted"):
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail = "Admin or trusted access required",
        )
    return user


def _get_current_token(request: Request) -> TokenRecord:
    """Extract the already verified token record from request.state."""
    token = getattr(request.state, "token", None)
    if token is None:
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail = "Not authenticated",
            headers = {"WWW-Authenticate": "Bearer"},
        )
    return token


CurrentUserDep = Annotated[UserRecord, Depends(_get_current_user)]
CurrentAdminDep = Annotated[UserRecord, Depends(_get_current_admin)]
CurrentCuratorDep = Annotated[UserRecord, Depends(_get_current_curator)]
CurrentTokenDep = Annotated[TokenRecord, Depends(_get_current_token)]
