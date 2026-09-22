"""CompositeIndexer - pool the SELECTED Usenet backend + usenet-targeting plugins.

Wraps ``[<selected: NewznabIndexer | ProwlarrIndexer>,
*registry.indexers_for_target('usenet')]`` as one ``IndexerProtocol`` with
``indexer_name == "usenet"`` (either/or: the unselected backend never searches,
so Prowlarr and native rows can't duplicate each other). Fans one logical
search out across every member with ``return_exceptions`` gather, pools the
``UsenetRelease`` results, and dedups by the cross-indexer
:func:`models.download_identity.usenet_identity` key (first-seen-wins, so the
primary wins over pooled plugin copies). One member erroring never fails the
fan-out. Health aggregates every member (any-ok reads ok) so no healthy member
is hidden behind the primary.
"""

import asyncio
import logging

from models.common import ServiceStatus
from models.download_identity import usenet_identity

logger = logging.getLogger(__name__)


class CompositeIndexer:
    """One ``usenet`` indexer pooling Newznab + plugin usenet indexers."""

    def __init__(self, primary_indexer=None, extra_indexers=None) -> None:
        self._primary = primary_indexer
        self._extras = list(extra_indexers or [])

    @property
    def indexer_name(self) -> str:
        return "usenet"

    def is_configured(self) -> bool:
        try:
            if self._primary is not None and self._primary.is_configured():
                return True
        except Exception:  # noqa: BLE001 - absence, not failure
            pass
        for extra in self._extras:
            try:
                if extra.is_configured():
                    return True
            except Exception:  # noqa: BLE001 - one bad plugin never blocks
                continue
        return False

    async def health_check(self) -> ServiceStatus:
        """Aggregate every member (primary + extras): any-ok reads ok with a
        reachable/total count, so a healthy Prowlarr is visible even when the
        Newznab primary is empty, and vice versa. Never raises."""
        members = self._members()
        if not members:
            return ServiceStatus(status="error", message="No indexers configured")
        reachable = 0
        version: str | None = None
        for member in members:
            try:
                status = await member.health_check()
            except Exception:  # noqa: BLE001 - one bad member never blocks
                continue
            if getattr(status, "status", "") == "ok":
                reachable += 1
                version = version or getattr(status, "version", None)
        if reachable:
            return ServiceStatus(
                status="ok",
                version=version,
                message=f"{reachable}/{len(members)} indexer(s) reachable",
            )
        return ServiceStatus(status="error", message="No indexer reachable")

    def _members(self) -> list:
        members = []
        if self._primary is not None:
            members.append(self._primary)
        members.extend(self._extras)
        return members

    def _dedup(self, releases: list) -> list:
        seen: set[str] = set()
        out = []
        dropped = 0
        for release in releases:
            try:
                key = usenet_identity(release.title, release.size_bytes)
            except Exception:  # noqa: BLE001 - one bad release never blocks
                continue
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            out.append(release)
        if dropped:
            logger.info("usenet composite deduped %d cross-indexer duplicate(s)", dropped)
        return out

    async def search_album(
        self,
        artist_name: str,
        album_title: str,
        year: int | None = None,
        track_count: int | None = None,
        *,
        timeout: float = 30.0,
    ) -> list:
        members = self._members()
        if not members:
            return []
        results = await asyncio.gather(
            *(
                member.search_album(
                    artist_name, album_title, year, track_count, timeout=timeout
                )
                for member in members
            ),
            return_exceptions=True,
        )
        pooled = []
        for member, res in zip(members, results):
            if isinstance(res, Exception):
                logger.warning(
                    "usenet composite member %s search failed: %s",
                    getattr(member, "indexer_name", "?"),
                    res,
                )
                continue
            for row in res or []:
                release = getattr(row, "usenet", None)
                if release is not None:
                    pooled.append(release)
        deduped = self._dedup(pooled)
        from repositories.protocols.indexer import IndexerResult

        return [IndexerResult(source="usenet", usenet=r) for r in deduped]

    async def search_track(
        self,
        artist_name: str,
        track_title: str,
        album_title: str | None = None,
        duration_seconds: int | None = None,
        *,
        timeout: float = 30.0,
    ) -> list:
        members = self._members()
        if not members:
            return []
        results = await asyncio.gather(
            *(
                member.search_track(
                    artist_name,
                    track_title,
                    album_title,
                    duration_seconds,
                    timeout=timeout,
                )
                for member in members
            ),
            return_exceptions=True,
        )
        pooled = []
        for member, res in zip(members, results):
            if isinstance(res, Exception):
                logger.warning(
                    "usenet composite member %s search failed: %s",
                    getattr(member, "indexer_name", "?"),
                    res,
                )
                continue
            for row in res or []:
                release = getattr(row, "usenet", None)
                if release is not None:
                    pooled.append(release)
        deduped = self._dedup(pooled)
        from repositories.protocols.indexer import IndexerResult

        return [IndexerResult(source="usenet", usenet=r) for r in deduped]
