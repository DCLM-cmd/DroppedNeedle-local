"""``ProwlarrIndexer`` - the ``IndexerProtocol`` impl for one Prowlarr instance.

Either/or with the native Newznab list: when the ``usenet_search_backend``
selector is ``"prowlarr"`` this is the composite primary (and native rows never
search); otherwise it sits idle. Shape mirrors ``NewznabIndexer``: query ladder
(canonical, then one punctuation-normalized free-text retry only on a genuine
clean empty), short-TTL search cache keyed ``("prowlarr", query)``, rate-limit
backoff-skip, per-call timeout. Member failure semantics (Newznab ``_search_one``
precedent): every member error maps to ``[]`` - the fan-out never fails because
of Prowlarr.

No ``from __future__ import annotations`` (the conformance test compares real
signatures).
"""

import logging
import time

from core.exceptions import ProwlarrAuthError, RateLimitedError
from models.common import ServiceStatus
from repositories.newznab.newznab_indexer import normalize_newznab_query
from repositories.protocols.indexer import IndexerResult, UsenetRelease

from .prowlarr_client import ProwlarrClient

logger = logging.getLogger(__name__)


class ProwlarrIndexer:
    def __init__(
        self,
        client: ProwlarrClient | None,
        *,
        categories: list[int] | None = None,
        enabled: bool = True,
        search_cache_ttl: float = 300.0,
        rate_limit_backoff: float = 300.0,
        per_indexer_timeout: float = 30.0,
    ) -> None:
        self._client = client
        self._categories = list(categories) if categories else []
        self._enabled = enabled
        self._search_cache_ttl = search_cache_ttl
        self._rate_limit_backoff = rate_limit_backoff
        self._timeout = per_indexer_timeout
        self._search_cache: dict[tuple[str, str], tuple[float, list[UsenetRelease]]] = {}
        self._backoff_until: float = 0.0

    @property
    def indexer_name(self) -> str:
        return "usenet"

    def is_configured(self) -> bool:
        return self._enabled and self._client is not None

    async def health_check(self) -> ServiceStatus:
        if not self.is_configured():
            return ServiceStatus(status="error", message="Prowlarr not configured")
        assert self._client is not None
        try:
            status = await self._client.system_status(timeout=self._timeout)
            indexers = await self._client.list_indexers(timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - health check never raises
            logger.warning("prowlarr health: instance unreachable: %s", exc)
            return ServiceStatus(status="error", message="Prowlarr unreachable")
        # system_status is degraded-optional (A0-VERIFY): a None (404) still counts
        # as reachable when the indexer list answers. Count enabled rows only
        # (disabled rows are never searched).
        version = status.version if status is not None else None
        enabled = sum(1 for i in indexers if i.enable)
        return ServiceStatus(
            status="ok",
            version=version,
            message=f"Prowlarr OK - {enabled} enabled indexer(s)",
        )

    async def search_album(
        self,
        artist_name: str,
        album_title: str,
        year: int | None = None,
        track_count: int | None = None,
        *,
        timeout: float = 30.0,
    ) -> list[IndexerResult]:
        query = f"{artist_name} {album_title}".strip()
        releases = await self._search_with_ladder(query, timeout=timeout)
        return [IndexerResult(source="usenet", usenet=r) for r in releases]

    async def search_track(
        self,
        artist_name: str,
        track_title: str,
        album_title: str | None = None,
        duration_seconds: int | None = None,
        *,
        timeout: float = 30.0,
    ) -> list[IndexerResult]:
        # No reliable single-track Usenet search (Newznab comment): free-text
        # artist+track, same as the Newznab track path.
        query = f"{artist_name} {track_title}".strip()
        releases = await self._search_with_ladder(query, timeout=timeout)
        return [IndexerResult(source="usenet", usenet=r) for r in releases]

    async def _search_with_ladder(self, query: str, *, timeout: float) -> list[UsenetRelease]:
        """Canonical query first; on a genuine clean empty, one normalized retry
        (#259 ladder, same contract as ``NewznabIndexer``)."""
        if not query:
            return []
        releases, clean = await self._search_one(query, timeout=timeout)
        if releases:
            return releases
        normalized = normalize_newznab_query(query)
        if normalized == query or not clean:
            return releases
        logger.info(
            "prowlarr.query_normalized_retry",
            extra={"query": query, "normalized_query": normalized},
        )
        releases, _ = await self._search_one(normalized, timeout=timeout)
        return releases

    async def _search_one(self, query: str, *, timeout: float) -> tuple[list[UsenetRelease], bool]:
        """One Prowlarr search. The bool reports a successful ANSWER (results or a
        genuine empty); backoff skips, auth failures, and rate limits report False
        so the ladder never retries on their silence. Never raises."""
        if not self.is_configured():
            return [], False
        assert self._client is not None
        now = time.monotonic()
        if self._backoff_until > now:
            logger.info("prowlarr instance in rate-limit backoff; skipping")
            return [], False
        cache_key = ("prowlarr", query)
        cached = self._search_cache.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1], True
        if len(self._search_cache) > 256:
            self._search_cache = {k: v for k, v in self._search_cache.items() if v[0] > now}
        per_call = min(timeout, self._timeout)
        try:
            releases = await self._client.search(
                query, self._categories, timeout=per_call
            )
        except RateLimitedError as exc:
            backoff = exc.retry_after_seconds or self._rate_limit_backoff
            self._backoff_until = now + backoff
            logger.warning("prowlarr rate-limited; backing off %.0fs", backoff)
            return [], False
        except ProwlarrAuthError as exc:
            logger.warning("prowlarr auth failed: %s", exc)
            return [], False
        except Exception as exc:  # noqa: BLE001 - one member error never fails the fan-out
            logger.warning("prowlarr search failed: %s", exc)
            return [], False
        self._search_cache[cache_key] = (now + self._search_cache_ttl, releases)
        return releases, True
