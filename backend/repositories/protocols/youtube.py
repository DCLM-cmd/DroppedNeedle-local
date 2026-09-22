from typing import Literal, Protocol

from models.youtube import YouTubeQuotaResponse


class YouTubeRepositoryProtocol(Protocol):

    def configure(self, api_key: str) -> None:
        ...

    @property
    def is_configured(self) -> bool:
        ...

    @property
    def search_available(self) -> bool:
        ...

    def is_cached(self, artist: str, album: str, *, kind: Literal["album", "track"] = "album") -> bool:
        ...

    def are_cached(self, pairs: list[tuple[str, str]]) -> dict[str, bool]:
        ...

    @property
    def quota_remaining(self) -> int:
        ...

    async def search_video(self, artist: str, album: str) -> str | None:
        ...

    async def search_track(self, artist: str, track_name: str) -> str | None:
        ...

    def get_quota_status(self) -> YouTubeQuotaResponse:
        ...
