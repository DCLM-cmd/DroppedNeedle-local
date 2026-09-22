"""Raw httpx + JSON wrapper around one Prowlarr instance. No fan-out logic (that's
``ProwlarrIndexer``); this is the per-instance HTTP + parse + map layer.

Auth is an ``X-Api-Key`` header (lidarr-import precedent) - the same secret the
user pasted; never logged. No ``@with_retry``/``CircuitBreaker`` here by design:
like ``NewznabClient`` (which carries zero), backoff-skip lives in the indexer
layer so retries never spend API budget or inflate fan-out tail latency against
``per_indexer_timeout``.

Shapes follow the inspected devopsarr/prowlarr-py generated-client docs
(``GET/POST /api/v1/search``, ``GET /api/v1/indexer``, ``ReleaseResource``),
live-verified against Prowlarr **2.3.5.5327** (linuxserver docker): ``GET
/api/v1/system/status`` exists with ``version``; ``GET /api/v1/search`` honors
``query`` + repeated ``categories`` + repeated ``indexerIds`` + ``limit``;
``downloadUrl`` embeds ``?apikey=<instance key>`` so it is self-contained for
SABnzbd's server-side fetch (no header transfer needed - and it must therefore
never be logged, same as Newznab's self-authenticating enclosures);
``publishDate`` is ISO-8601 with ``Z``; ``ReleaseResource`` carries no password
signal (``password`` stays 0); a bad key yields HTTP 401.
"""

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
import msgspec

from core.exceptions import ProwlarrApiError, ProwlarrAuthError, RateLimitedError
from repositories.newznab.newznab_client import _parse_date as _rfc2822_to_unix
from repositories.protocols.indexer import UsenetRelease

from .prowlarr_models import ProwlarrIndexerInfo, ProwlarrRelease, ProwlarrSystemStatus

logger = logging.getLogger(__name__)


class ProwlarrClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        *,
        indexer_name: str = "prowlarr",
    ) -> None:
        self._http = http
        # The user pastes the bare origin (ProwlarrConnectionSettings strips any
        # /api/v1 suffix); keep it verbatim and append /api/v1 ourselves.
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._indexer_name = indexer_name

    async def system_status(self, *, timeout: float = 30.0) -> ProwlarrSystemStatus | None:
        """``GET /api/v1/system/status`` (verified live on 2.3.5.5327).
        Returns None on 404 (older/alternate builds) so callers degrade, never hard-fail."""
        try:
            content = await self._get("/system/status", timeout=timeout)
        except ProwlarrApiError as exc:
            if exc.code == 404:
                return None
            raise
        return self._decode(content, ProwlarrSystemStatus)

    async def list_indexers(self, *, timeout: float = 30.0) -> list[ProwlarrIndexerInfo]:
        content = await self._get("/indexer", timeout=timeout)
        return self._decode(content, list[ProwlarrIndexerInfo])

    async def search(
        self,
        query: str,
        categories: list[int],
        *,
        indexer_ids: list[int] | None = None,
        limit: int = 100,
        timeout: float = 30.0,
    ) -> list[UsenetRelease]:
        """Basic (free-text) ``GET /api/v1/search`` - no ``type`` param, which is
        the parameterized-search path. Verified live on 2.3.5.5327: repeated
        ``categories``/``indexerIds`` params and ``limit`` are all honored."""
        params: list[tuple[str, str]] = [("query", query)]
        for cat in categories:
            params.append(("categories", str(cat)))
        for iid in indexer_ids or []:
            params.append(("indexerIds", str(iid)))
        params.append(("limit", str(limit)))
        content = await self._get("/search", params=params, timeout=timeout)
        releases = self._decode(content, list[ProwlarrRelease])
        return [r for r in (self._to_usenet(rel) for rel in releases) if r is not None]

    def _to_usenet(self, release: ProwlarrRelease) -> UsenetRelease | None:
        # v1 is usenet-only: torrent members are skipped, never failed.
        if (release.protocol or "").lower() != "usenet":
            return None
        # Verified live on 2.3.5.5327: downloadUrl embeds ?apikey=, so it is
        # self-contained for SABnzbd's fetch - use as-is. It carries the
        # instance key, so it must never be logged (audited: no nzb_url logging).
        if not release.download_url:
            return None
        return UsenetRelease(
            indexer_id=f"prowlarr:{release.indexer_id}",
            indexer_name=release.indexer or self._indexer_name,
            guid=release.guid or release.download_url,
            title=release.title,
            nzb_url=release.download_url,
            size_bytes=release.size or 0,
            category_ids=[c.id for c in release.categories],
            grabs=release.grabs,
            files=release.files,
            usenet_date=_parse_prowlarr_date(release.publish_date),
            # Verified live on 2.3.5.5327: ReleaseResource carries no password
            # signal, so 0 (not passworded) stands.
            password=0,
        )

    async def _get(
        self,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        timeout: float,
    ) -> bytes:
        url = f"{self._base_url}/api/v1{path}"
        try:
            response = await self._http.get(
                url, params=params, headers={"X-Api-Key": self._api_key}, timeout=timeout
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # httpx.InvalidURL is NOT an HTTPError subclass (lidarr precedent): a
            # malformed base URL must map here, not escape as a raw 500. The class
            # name carries no hosts/paths/keys, so it is safe to surface.
            detail = str(exc) or type(exc).__name__
            raise ProwlarrApiError(f"Prowlarr request failed: {detail}") from exc
        if response.status_code in (401, 403):
            raise ProwlarrAuthError(
                "Prowlarr rejected the API key", code=response.status_code
            )
        if response.status_code == 429:
            raise RateLimitedError(
                "Prowlarr rate limited",
                retry_after_seconds=_retry_after(response),
            )
        if response.status_code >= 400:
            raise ProwlarrApiError(
                f"Prowlarr returned HTTP {response.status_code}",
                code=response.status_code,
            )
        return response.content

    def _decode(self, content: bytes, decode_type: type):
        try:
            return msgspec.json.decode(content, type=decode_type)
        except msgspec.MsgspecError as exc:
            raise ProwlarrApiError(f"Prowlarr response decode failed: {exc}") from exc


def _parse_prowlarr_date(value: str) -> float | None:
    """``publishDate`` (ISO-8601 when present) → unix float for ``usenet_date``.

    Tries ISO first, then falls back to the reused Newznab RFC-2822 parser (which
    returns None on garbage) - never raises, never a new date parser.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _rfc2822_to_unix(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _retry_after(response: httpx.Response) -> float | None:
    """``Retry-After`` seconds when the header carries a plain integer; else None."""
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except (TypeError, ValueError):
        return None
