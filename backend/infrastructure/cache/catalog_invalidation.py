"""ST1: scoped catalog invalidation.

Replaces wholesale ``library_identification_prefixes()`` sweeps at commit
paths that already know which entities they touched. Entity-bearing keys
(``catalog_entity_prefixes()`` × the supplied ids) are gather-deleted
synchronously; cheap locally-rebuilt list snapshots keep their bulk sweep and
can be skipped with ``include_lists=False`` where a caller only touched
entities. Disk-mirror and cover-cleanup legs mirror the precedents in
``library_management_post_commit_service`` and ``library_service``.

In-flight futures are deliberately NOT touched here: they belong to their
services (album_service.refresh_album pops its own maps).

No new cache prefixes: both groups partition library_identification_prefixes().
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from infrastructure.cache.cache_keys import (
    catalog_entity_prefixes,
    catalog_list_prefixes,
)

logger = logging.getLogger(__name__)


async def invalidate_catalog_scope(
    cache,
    *,
    disk_cache=None,
    cover_repo=None,
    album_mbids: Iterable[str] = (),
    artist_mbids: Iterable[str] = (),
    local_album_ids: Iterable[str] = (),
    include_lists: bool = True,
) -> None:
    """Delete entity-keyed caches for *album_mbids* / *artist_mbids* /
    *local_album_ids* across every ``catalog_entity_prefixes()`` family, then
    - when *include_lists* - sweep ``catalog_list_prefixes()``.

    The id sets are folded into one casefolded pool before the cross-product:
    deleting ``{prefix}{id}`` for a family an id never uses is a no-op read on
    the cache, so over-deletion is safe while under-deletion would risk stale
    identity reads (the automatic ST1 fail condition).

    *local_album_ids* is accepted for call-site symmetry with hooks that hold
    local ids; entity prefixes are all MBID-shaped, so local ids contribute no
    deletions here - callers resolve them to provider ids first.
    """
    pool = {
        str(mbid).casefold()
        for mbid in (*album_mbids, *artist_mbids, *local_album_ids)
        if mbid
    }

    tasks = []
    if pool:
        tasks.extend(
            cache.delete(f"{prefix}{mbid}")
            for prefix in catalog_entity_prefixes()
            for mbid in sorted(pool)
        )
    if include_lists:
        tasks.extend(cache.clear_prefix(prefix) for prefix in catalog_list_prefixes())
    if disk_cache is not None:
        tasks.extend(
            [
                *(
                    disk_cache.delete_album(album_mbid)
                    for album_mbid in {str(m).casefold() for m in album_mbids if m}
                ),
                *(
                    disk_cache.delete_artist(artist_mbid)
                    for artist_mbid in {str(a).casefold() for a in artist_mbids if a}
                ),
            ]
        )
    if cover_repo is not None:
        # Cover-cleanup leg mirrors
        # library_service._invalidate_caches_after_removal (warn-and-swallow:
        # covers are cosmetic).
        try:
            cover_tasks = [
                cover_repo.delete_covers_for_album(album_mbid)
                for album_mbid in sorted({str(m).casefold() for m in album_mbids if m})
            ]
            cover_tasks.extend(
                cover_repo.delete_covers_for_artist(artist_mbid)
                for artist_mbid in sorted(
                    {str(a).casefold() for a in artist_mbids if a}
                )
            )
            if cover_tasks:
                await asyncio.gather(*cover_tasks)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to clean up cover images after catalog invalidation",
                exc_info=True,
            )

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=False)
