from typing import Protocol

from models.library import LibraryAlbum


class LibraryRepositoryProtocol(Protocol):
    """Forward-looking contract for the native library.

    Phase 3's ``LibraryManager`` implements this against ``library_files``.
    Now satisfied by ``services.native.target_library_repository.TargetLibraryRepository``
    via ``NativeLibraryStore``.
    """

    def is_configured(self) -> bool:
        ...


    async def has_album(self, mbid: str) -> bool:
        ...

    async def get_library_albums(self) -> list[LibraryAlbum]:
        ...

    async def get_library_album_mbids(self) -> set[str]:
        ...

    async def get_library_artist_mbids(self) -> set[str]:
        ...
