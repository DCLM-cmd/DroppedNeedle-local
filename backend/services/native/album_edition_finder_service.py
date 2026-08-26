"""Read-only MusicBrainz edition discovery for a local album."""

from core.exceptions import ResourceNotFoundError, ValidationError
from infrastructure.persistence.native_library_store import NativeLibraryStore
from infrastructure.queue.priority_queue import RequestPriority
from models.identification import ReleaseEditionSearchPage
from repositories.protocols.identification import IdentificationProviderProtocol


class AlbumEditionFinderService:
    def __init__(
        self,
        store: NativeLibraryStore,
        provider: IdentificationProviderProtocol,
    ) -> None:
        self._store = store
        self._provider = provider

    async def search(
        self,
        album_id: str,
        *,
        title: str,
        artist: str,
        limit: int,
        offset: int,
    ) -> tuple[str, str, str | None, str | None, ReleaseEditionSearchPage]:
        context = await self._store.get_album_identification_context(album_id)
        if context is None:
            raise ResourceNotFoundError("Library album not found.")
        if not any(track["availability"] == "indexed" for track in context["tracks"]):
            raise ResourceNotFoundError("Library album has no indexed tracks.")

        title_query = " ".join(title.split())
        artist_query = " ".join(artist.split())
        if not title_query:
            raise ValidationError("A release title is required.")
        page = await self._provider.search_release_editions(
            title_query,
            artist_query,
            limit,
            offset,
            RequestPriority.USER_INITIATED,
        )
        identity = context["identity"] or {}
        return (
            title_query,
            artist_query,
            identity.get("release_group_mbid"),
            identity.get("release_mbid"),
            page,
        )
