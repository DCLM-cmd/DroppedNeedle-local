from typing import Protocol

from infrastructure.queue.priority_queue import RequestPriority
from models.identification import AlbumCandidate, ReleaseEditionSearchPage


class IdentificationProviderProtocol(Protocol):
    async def search_release_editions(
        self,
        title: str,
        artist: str,
        limit: int,
        offset: int,
        priority: RequestPriority,
    ) -> ReleaseEditionSearchPage: ...

    async def search_album_candidate_ids(
        self,
        artist: str,
        title: str,
        limit: int,
        priority: RequestPriority,
    ) -> list[str]: ...

    async def search_recording_candidate_ids(
        self,
        artist: str,
        title: str,
        limit: int,
        priority: RequestPriority,
    ) -> list[str]: ...

    async def get_album_candidate(
        self,
        release_group_mbid: str,
        target_track_count: int,
        priority: RequestPriority,
    ) -> AlbumCandidate | None: ...

    async def get_exact_release_candidate(
        self,
        release_mbid: str,
        priority: RequestPriority,
    ) -> AlbumCandidate | None: ...
