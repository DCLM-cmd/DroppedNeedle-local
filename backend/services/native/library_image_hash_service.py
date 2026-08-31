"""Computing artwork blurhashes once, while organizing - the way Jellyfin does it.

A blurhash is a low-frequency summary of an image that clients draw as a placeholder
while the real artwork loads, and that Finamp additionally uses to tell two images
apart without downloading them. Computing one is pure CPU, and cheap only in isolation:
a hundred of them is a hundred image decodes.

Jellyfin never pays that on a request. Its LibraryManager.UpdateImagesAsync computes
the hash when an image is first seen during a scan, stores it on the image record
(BaseItemImageInfos.Blurhash) next to the dimensions, and its DTO layer only ever
reads the stored value. Its ImageNeedsRefresh decides what to redo: an image with no
hash, no dimensions, or a changed file. Checked against the Jellyfin instance next
door, that gives 5643 images with 5643 hashes - complete coverage, zero request cost.

DroppedNeedle used to compute them inline instead, which made a 100-album listing
take 5.1 seconds against Jellyfin's 0.2. This service moves the work to where
Jellyfin has it: the organization run fills the store, and serving only looks up.

The staleness rule is Jellyfin's, expressed against a stronger key. Jellyfin keys an
image by path and modification time; we key it by the artwork's content hash, which
the codebase already derives for every cover because it is what we publish as the
image's ETag. Different art therefore always means a different key, so unlike a
path-based key it cannot go stale while looking current.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from infrastructure.images.blurhash import encode_image_bytes_with_size

if TYPE_CHECKING:
    from infrastructure.persistence.native_library_store import NativeLibraryStore
    from services.compat.target_cover_art_service import TargetCoverArtService
    from services.home.cached_local_artwork_service import CachedLocalArtworkService

logger = logging.getLogger(__name__)

# One organization run should not turn into an unbounded image-decoding job. A run
# touches a handful of albums; the rest of the library is caught by later runs, the
# same way Jellyfin only rehashes what the current scan walks over.
_DEFAULT_BACKFILL_LIMIT = 500
# Album ids are read in pages so a large library never lands in memory at once.
_PAGE_SIZE = 200
# Pause between artwork FETCHES (not between hashes). Materialising a cover reaches the
# Cover Art Archive, which answers on the same address as MusicBrainz and ListenBrainz -
# a burst here is felt by all three, and this installation has already been refused at
# that edge. Background work has no deadline, so it goes at a courteous pace.
_FETCH_PAUSE_SECONDS = 1.0
# What list_target_albums calls an album's identifier. It has no plain "id" column,
# and reading one would quietly yield nothing at all.
_ALBUM_ID_COLUMN = "release_group_mbid"
_ARTIST_ID_COLUMN = "artist_mbid"


class LibraryImageHashService:
    """Fills the stored blurhashes for album artwork."""

    def __init__(
        self,
        store: "NativeLibraryStore",
        local_artwork: "CachedLocalArtworkService",
        covers: "TargetCoverArtService | None" = None,
    ) -> None:
        self._store = store
        self._local = local_artwork
        # Artist pictures are fetched on demand rather than sitting in the catalog, so
        # hashing one means materialising it first - which is exactly what Jellyfin's
        # scan does before it hashes anything (ConvertImageToLocal in UpdateImagesAsync).
        self._covers = covers

    async def ensure_for_albums(self, album_ids: list[str]) -> int:
        """Store a blurhash for each of these albums' covers. Returns how many were new.

        Already-hashed artwork is skipped on its content hash, so calling this twice
        over the same albums costs one cheap batch read and no decoding.
        """
        wanted = [value for value in dict.fromkeys(album_ids) if value]
        if not wanted:
            return 0

        pending: list[tuple[str, bytes]] = []
        for album_id in wanted:
            resolved = await self._artwork_of(album_id)
            if resolved is not None:
                pending.append(resolved)
        if not pending:
            return 0

        known = await self._store.get_image_blurhashes([sha for sha, _ in pending])
        written = 0
        for content_sha1, content in pending:
            if content_sha1 in known:
                continue
            # Skip duplicates within this batch: two albums can share one cover.
            known[content_sha1] = ""
            if await self._store_hash(content_sha1, content):
                written += 1
        return written

    async def backfill(self, *, limit: int = _DEFAULT_BACKFILL_LIMIT) -> int:
        """Hash the artwork of albums that have none yet.

        This is the equivalent of Jellyfin rehashing what a scan walks over: it is run
        after an organization run, so the covers that run just placed - and anything an
        earlier run missed - are hashed before a client ever asks for them.
        """
        written = 0
        offset = 0
        while written < limit:
            try:
                rows, _ = await self._store.list_target_albums(
                    limit=_PAGE_SIZE, offset=offset, sort="recent"
                )
            except Exception:  # noqa: BLE001 - a backfill must never fail a run
                logger.warning("Could not list albums to hash artwork", exc_info=True)
                return written
            if not rows:
                break
            offset += len(rows)
            album_ids = [
                str(row[_ALBUM_ID_COLUMN])
                for row in rows
                if row.get(_ALBUM_ID_COLUMN)
            ]
            if not album_ids:
                # Loud on purpose. Reading the wrong column here would be invisible -
                # the backfill would report success, write nothing, and every album
                # would silently lose its blurhash again.
                logger.warning(
                    "No album ids in a listing page; expected column %r, saw %s",
                    _ALBUM_ID_COLUMN,
                    sorted(dict(rows[0]).keys()),
                )
                return written
            written += await self.ensure_for_albums(album_ids)
        written += await self._backfill_artists(limit)
        if written:
            logger.info("Stored %d new artwork blurhashes", written)
        return written

    async def _backfill_artists(self, limit: int) -> int:
        """Materialise and hash artist pictures.

        Without this an artist advertised a picture that did not exist yet: the image
        route answered 404 and no blurhash was stored, which is precisely what Finamp
        reports as "the server does not compute blurhashes" while showing a broken
        image. Jellyfin has no such state - it downloads remote art during the scan,
        then serves a tag and a hash that both refer to something real.
        """
        if self._covers is None:
            return 0
        written = 0
        offset = 0
        while written < limit:
            try:
                rows, _ = await self._store.list_target_artists(
                    limit=_PAGE_SIZE, offset=offset
                )
            except Exception:  # noqa: BLE001 - a backfill must never fail a run
                logger.warning("Could not list artists to hash artwork", exc_info=True)
                return written
            if not rows:
                break
            offset += len(rows)
            for row in rows:
                artist_id = row.get(_ARTIST_ID_COLUMN)
                if artist_id and await self._hash_artist(str(artist_id)):
                    written += 1
        return written

    async def _hash_artist(self, artist_id: str) -> bool:
        try:
            identity = await self._covers.get_artist_image_etag(artist_id)
            if identity is None:
                # Not cached yet. Fetching it is what puts it in the cache, and only
                # then does the artist have an image to advertise at all.
                await asyncio.sleep(_FETCH_PAUSE_SECONDS)
                if await self._covers.get_artist_image(artist_id) is None:
                    return False
                identity = await self._covers.get_artist_image_etag(artist_id)
            if not isinstance(identity, str) or not identity:
                return False
            if await self._store.get_image_blurhashes([identity]):
                return False
            resolved = await self._covers.get_artist_image(artist_id)
        except Exception:  # noqa: BLE001 - artwork is decorative, never fatal
            logger.debug("Could not resolve art for artist %s", artist_id, exc_info=True)
            return False
        if not resolved or not resolved[0]:
            return False
        return await self._store_hash(identity, resolved[0])

    async def _artwork_of(self, album_id: str) -> tuple[str, bytes] | None:
        """The album cover's content hash and bytes, or None when it has no artwork."""
        try:
            context: dict[str, Any] | None = (
                await self._store.get_target_artwork_context("album", album_id)
            )
            if context is None:
                return None
            local = await self._local.read(context)
            if local is None and self._covers is not None:
                # The catalog says this album's art comes from a provider, but nothing
                # has ever fetched it, and nothing would: the cover only downloads when
                # a client asks, and a client only asks when the listing advertises an
                # image. Fetching it here is what breaks that deadlock - the same thing
                # Jellyfin's scan does when it pulls remote art down before hashing it.
                await asyncio.sleep(_FETCH_PAUSE_SECONDS)
                if await self._covers.get_release_group_cover(album_id) is not None:
                    local = await self._local.read(context)
        except Exception:  # noqa: BLE001 - artwork is decorative, never fatal
            logger.debug("Could not read artwork for album %s", album_id, exc_info=True)
            return None
        if local is None:
            return None
        content, _content_type, _source, content_sha1 = local
        if not content or not isinstance(content_sha1, str) or not content_sha1:
            return None
        return content_sha1, content

    async def _store_hash(self, content_sha1: str, content: bytes) -> bool:
        # Off the event loop: this is the one genuinely CPU-bound step, and the whole
        # point of the exercise is that it never happens while a client is waiting.
        measured = await asyncio.to_thread(encode_image_bytes_with_size, content)
        if measured is None:
            return False
        blurhash, width, height = measured
        try:
            await self._store.put_image_blurhash(
                content_sha1=content_sha1,
                blurhash=blurhash,
                width=width,
                height=height,
                computed_at=time.time(),
            )
        except Exception:  # noqa: BLE001 - a failed write must not fail the run
            logger.warning("Could not store an artwork blurhash", exc_info=True)
            return False
        return True
