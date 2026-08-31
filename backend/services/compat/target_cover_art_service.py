"""Local-ID cover adapter for the isolated target compatibility composition."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infrastructure.persistence.native_library_store import NativeLibraryStore
    from repositories.coverart_repository import CoverArtRepository
    from repositories.coverart_disk_cache import CoverDiskCache
    from services.home.cached_local_artwork_service import CachedLocalArtworkService


class TargetCoverArtService:
    def __init__(
        self,
        store: "NativeLibraryStore",
        provider_covers: "CoverArtRepository",
        local_artwork: "CachedLocalArtworkService",
    ) -> None:
        self._store = store
        self._provider = provider_covers
        self._local = local_artwork
        self._album_provider_ids: dict[str, str] = {}
        self._artist_provider_ids: dict[str, str] = {}

    @property
    def disk_cache(self) -> "CoverDiskCache":
        return self._provider.disk_cache

    def is_rg_cover_warming(self, album_id: str, size: str | None = "500") -> bool:
        checker = getattr(self._provider, "is_rg_cover_warming", None)
        if not callable(checker):
            return False
        return bool(checker(self._album_provider_ids.get(album_id, album_id), size))

    def is_release_cover_warming(self, release_id: str) -> bool:
        checker = getattr(self._provider, "is_release_cover_warming", None)
        if not callable(checker):
            return False
        return bool(checker(release_id))

    def is_artist_cover_warming(self, artist_id: str, size: int | None = None) -> bool:
        checker = getattr(self._provider, "is_artist_cover_warming", None)
        if not callable(checker):
            return False
        return bool(checker(self._artist_provider_ids.get(artist_id, artist_id), size))

    @staticmethod
    def _remember_provider_id(
        mappings: dict[str, str], identifier: str, context: dict
    ) -> str | None:
        provider_id = context.get("provider_id")
        if provider_id:
            value = str(provider_id)
            mappings[identifier] = value
            mappings[value] = value
            return value
        return None

    async def get_release_group_cover(
        self,
        album_id: str,
        size: str | None = "500",
        **kwargs,
    ) -> tuple[bytes, str, str] | None:
        context = await self._store.get_target_artwork_context("album", album_id)
        if context is None:
            return await self._provider.get_release_group_cover(
                album_id, size, **kwargs
            )
        provider_id = self._remember_provider_id(
            self._album_provider_ids, album_id, context
        )
        local = await self._local.read(context)
        if local is not None:
            return local[:3]
        if provider_id:
            return await self._provider.get_release_group_cover(
                provider_id, size, **kwargs
            )
        return None

    async def get_release_group_cover_etag(
        self, album_id: str, size: str | None = "500"
    ) -> str | None:
        context = await self._store.get_target_artwork_context("album", album_id)
        if context is None:
            return await self._provider.get_release_group_cover_etag(album_id, size)
        provider_id = self._remember_provider_id(
            self._album_provider_ids, album_id, context
        )
        # Identity only: a listing wants the tag, not the picture. Reading the bytes
        # here loaded 181 MB of covers for one 100-album page.
        identity = await self._local.read_identity(context)
        if identity is not None:
            return identity
        if provider_id:
            return await self._provider.get_release_group_cover_etag(provider_id, size)
        return None

    async def get_release_group_cover_blurhash(
        self, album_id: str, size: str | None = "500"
    ) -> str | None:
        """Mirrors ``get_release_group_cover_etag``: local album id -> provider id.

        Without this the compat builders' ``except Exception`` swallowed an
        AttributeError and every album reported no blurhash, which Finamp surfaces as
        "the server is misconfigured" and which costs it image de-duplication.
        """
        context = await self._store.get_target_artwork_context("album", album_id)
        if context is None:
            return await self._provider.get_release_group_cover_blurhash(album_id, size)
        provider_id = self._remember_provider_id(
            self._album_provider_ids, album_id, context
        )
        identity = await self._local.read_identity(context)
        if identity is not None:
            # Look the hash up, never compute it here. Jellyfin's DTO layer only reads
            # BaseItemImageInfos.Blurhash; the value is produced while scanning. Ours
            # is produced by the organization run - see LibraryImageHashService - and
            # keyed by the artwork's content hash, the same value the etag returns.
            stored = await self._store.get_image_blurhashes([identity])
            return stored.get(identity)
        if provider_id:
            return await self._provider.get_release_group_cover_blurhash(
                provider_id, size
            )
        return None

    async def get_release_group_cover_image_info(
        self, album_id: str, size: str | None = "500"
    ) -> tuple[str | None, str | None]:
        """The album cover's tag and blurhash, resolving the image record once.

        Asking for the two separately cost every listed album two catalog lookups and
        two identity reads for one and the same picture - 358 ms of a 378 ms
        hundred-album page. Jellyfin reads its ItemImageInfo once and takes both the
        tag and the hash off that single record; this is the same shape.
        """
        context = await self._store.get_target_artwork_context("album", album_id)
        if context is None:
            return (
                await self._provider.get_release_group_cover_etag(album_id, size),
                await self._provider.get_release_group_cover_blurhash(album_id, size),
            )
        provider_id = self._remember_provider_id(
            self._album_provider_ids, album_id, context
        )
        identity = await self._local.read_identity(context)
        if identity is not None:
            stored = await self._store.get_image_blurhashes([identity])
            return identity, stored.get(identity)
        if provider_id:
            return (
                await self._provider.get_release_group_cover_etag(provider_id, size),
                await self._provider.get_release_group_cover_blurhash(
                    provider_id, size
                ),
            )
        return None, None

    async def get_artist_image_info(
        self, artist_id: str, size: int | None = None
    ) -> tuple[str | None, str | None]:
        """The artist picture's tag and blurhash. See the album variant above."""
        identity = await self.get_artist_image_etag(artist_id, size)
        if not identity:
            return None, None
        stored = await self._store.get_image_blurhashes([identity])
        return identity, stored.get(identity)

    async def get_release_cover(
        self,
        release_id: str,
        size: str | None = "500",
        **kwargs,
    ) -> tuple[bytes, str, str] | None:
        return await self._provider.get_release_cover(release_id, size, **kwargs)

    async def get_release_cover_etag(
        self, release_id: str, size: str | None = "500"
    ) -> str | None:
        return await self._provider.get_release_cover_etag(release_id, size)

    async def batch_prefetch_covers(
        self,
        album_ids: list[str],
        size: str = "250",
        max_concurrent: int = 5,
    ) -> None:
        provider_ids: list[str] = []
        seen: set[str] = set()
        for album_id in album_ids:
            context = await self._store.get_target_artwork_context("album", album_id)
            provider_id = (
                album_id
                if context is None
                else self._remember_provider_id(
                    self._album_provider_ids, album_id, context
                )
            )
            if provider_id and provider_id not in seen:
                seen.add(provider_id)
                provider_ids.append(provider_id)
        await self._provider.batch_prefetch_covers(provider_ids, size, max_concurrent)

    async def get_artist_image(
        self, artist_id: str, size: int | None = None, **kwargs
    ) -> tuple[bytes, str, str] | None:
        context = await self._store.get_target_artwork_context("artist", artist_id)
        if context is None:
            return await self._provider.get_artist_image(artist_id, size, **kwargs)
        provider_id = self._remember_provider_id(
            self._artist_provider_ids, artist_id, context
        )
        if not provider_id:
            return None
        return await self._provider.get_artist_image(provider_id, size, **kwargs)

    async def get_artist_image_etag(
        self, artist_id: str, size: int | None = None
    ) -> str | None:
        context = await self._store.get_target_artwork_context("artist", artist_id)
        if context is None:
            return await self._provider.get_artist_image_etag(artist_id, size)
        provider_id = self._remember_provider_id(
            self._artist_provider_ids, artist_id, context
        )
        if not provider_id:
            return None
        return await self._provider.get_artist_image_etag(provider_id, size)

    async def get_artist_image_blurhash(
        self, artist_id: str, size: int | None = None
    ) -> str | None:
        """Looked up, never computed - see the album variant above.

        Keyed on the picture's etag, which is its content hash, so an artist's hash is
        stored by the organization run at the same moment the picture itself is
        materialised. A tag without a hash is what makes Finamp declare the server
        misconfigured, so the two are produced together or not at all.
        """
        identity = await self.get_artist_image_etag(artist_id, size)
        if not identity:
            return None
        stored = await self._store.get_image_blurhashes([identity])
        return stored.get(identity)

    async def debug_artist_image(self, artist_id: str, debug_info: dict) -> dict:
        context = await self._store.get_target_artwork_context("artist", artist_id)
        if context is not None:
            provider_id = self._remember_provider_id(
                self._artist_provider_ids, artist_id, context
            )
            if provider_id:
                artist_id = provider_id
        return await self._provider.debug_artist_image(artist_id, debug_info)
