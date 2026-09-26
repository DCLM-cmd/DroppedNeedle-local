import asyncio
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from weakref import WeakValueDictionary

import httpx
import msgspec

from api.v1.schemas.settings import YouTubeConnectionSettings
from core.exceptions import ConfigurationError, ExternalServiceError, InvalidExternalPayloadError, RateLimitedError
from infrastructure.observability.provider_counters import record_provider_call
from infrastructure.observability.optional_work import OptionalWorkDeferred, OptionalWorkReservation, check_optional_dispatch, reserve_optional_operation
from infrastructure.persistence.youtube_quota_store import YouTubeQuotaStore, utc_today
from models.youtube import YouTubeQuotaResponse

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
DEFAULT_DAILY_QUOTA_LIMIT = 80
PREVIEW_CACHE_MAX = 100
SearchKind = Literal["album", "track"]
SearchKey = tuple[SearchKind, str, str]


def get_quota_file_path() -> Path:
    from core.config import get_settings
    return get_settings().cache_dir / "youtube_quota.json"


class _YouTubeSearchId(msgspec.Struct):
    videoId: str | None = None


class _YouTubeSearchItem(msgspec.Struct):
    id: _YouTubeSearchId | None = None


class _YouTubeSearchResponse(msgspec.Struct):
    items: list[_YouTubeSearchItem]


class _SearchOwner:
    def __init__(self, task: asyncio.Task[str | None]):
        self.task = task
        self.waiters = 0


class _SearchState:
    def __init__(self, path: Path):
        self.quota = YouTubeQuotaStore(path)
        self.cache: OrderedDict[SearchKey, str | None] = OrderedDict()
        self.owners: dict[SearchKey, _SearchOwner] = {}


_states: WeakValueDictionary[Path, _SearchState] = WeakValueDictionary()


class YouTubeRepository:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_key: str = "",
        daily_quota_limit: int = DEFAULT_DAILY_QUOTA_LIMIT,
        settings_getter: Callable[[], YouTubeConnectionSettings] | None = None,
    ):
        self._http_client = http_client
        self._api_key = api_key
        self._daily_quota_limit = daily_quota_limit
        self._settings_getter = settings_getter
        path = get_quota_file_path().resolve()
        state = _states.get(path)
        if state is None:
            state = _SearchState(path)
            _states[path] = state
        self._state = state

    def configure(self, api_key: str) -> None:
        self._api_key = api_key

    def _settings(self) -> YouTubeConnectionSettings:
        if self._settings_getter is not None:
            return self._settings_getter()
        return YouTubeConnectionSettings(
            api_key=self._api_key,
            enabled=bool(self._api_key),
            api_enabled=bool(self._api_key),
            daily_quota_limit=self._daily_quota_limit,
        )

    @property
    def is_configured(self) -> bool:
        settings = self._settings()
        return bool(settings.enabled and settings.api_enabled and settings.api_key.strip())

    @property
    def search_available(self) -> bool:
        return self.is_configured and self.quota_remaining > 0

    @property
    def quota_remaining(self) -> int:
        return max(0, self._settings().daily_quota_limit - self._state.quota.count)

    def get_quota_status(self) -> YouTubeQuotaResponse:
        limit = self._settings().daily_quota_limit
        count = self._state.quota.count
        return YouTubeQuotaResponse(used=count, limit=limit, remaining=max(0, limit - count), date=utc_today())

    def is_cached(self, artist: str, album: str, *, kind: SearchKind = "album") -> bool:
        return (kind, artist.lower(), album.lower()) in self._state.cache

    def are_cached(self, pairs: list[tuple[str, str]]) -> dict[str, bool]:
        return {
            f"{artist.lower()}|{track.lower()}": self.is_cached(artist, track, kind="track")
            for artist, track in pairs
        }

    async def search_video(self, artist: str, album: str) -> str | None:
        return await self._search("album", artist, album)

    async def search_track(self, artist: str, track_name: str) -> str | None:
        return await self._search("track", artist, track_name)

    async def _search(self, kind: SearchKind, artist: str, title: str) -> str | None:
        key = (kind, artist.lower(), title.lower())
        if key in self._state.cache:
            self._state.cache.move_to_end(key)
            return self._state.cache[key]
        owner = self._state.owners.get(key)
        joined = owner is not None
        if owner is None:
            token = reserve_optional_operation()
            task = asyncio.create_task(self._dispatch(key, artist, title, token))
            owner = _SearchOwner(task)
            self._state.owners[key] = owner
            def settled(done: asyncio.Task[str | None]) -> None:
                if token is not None:
                    token.refund()
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(settled)
        owner.waiters += 1
        try:
            return await asyncio.shield(owner.task)
        except OptionalWorkDeferred:
            if not joined:
                raise
            if self._state.owners.get(key) is owner:
                del self._state.owners[key]
            return await self._search(kind, artist, title)
        finally:
            owner.waiters -= 1
            if owner.waiters == 0:
                if self._state.owners.get(key) is owner:
                    del self._state.owners[key]
                if not owner.task.done():
                    owner.task.cancel()
                    try:
                        await asyncio.shield(owner.task)
                    except asyncio.CancelledError:
                        pass

    def _dispatch_settings(self) -> YouTubeConnectionSettings:
        settings = self._settings()
        if not (settings.enabled and settings.api_enabled and settings.api_key.strip()):
            raise ConfigurationError("YouTube API search is disabled or not configured")
        return settings

    async def _dispatch(self, key: SearchKey, artist: str, title: str, token: OptionalWorkReservation | None) -> str | None:
        date = None
        dispatched = False
        try:
            settings = self._dispatch_settings()
            check_optional_dispatch()
            date = await self._state.quota.reserve(settings.daily_quota_limit)
            while date != utc_today():
                await self._state.quota.refund(date)
                date = None
                settings = self._dispatch_settings()
                check_optional_dispatch()
                date = await self._state.quota.reserve(settings.daily_quota_limit)
            settings = self._dispatch_settings()
            check_optional_dispatch()
            if self._state.quota.count > settings.daily_quota_limit:
                raise RateLimitedError(
                    "YouTube daily quota exceeded",
                    details={"reason": "youtube_daily_quota"},
                )
            query = f"{artist} {title}" + (" full album" if key[0] == "album" else "")
            if token is not None:
                token.mark_dispatched()
            dispatched = True
            try:
                response = await self._http_client.get(
                    YOUTUBE_SEARCH_URL,
                    params={"part": "id", "type": "video", "maxResults": 1, "q": query, "key": settings.api_key.strip()},
                    timeout=10.0,
                )
            except httpx.HTTPError as exc:
                record_provider_call("youtube", None, None, category="search")
                raise ExternalServiceError("YouTube search transport failed") from exc
            record_provider_call("youtube", None, response.status_code, response=response, category="search")
            if response.status_code == 429:
                raise RateLimitedError("YouTube search rate limited upstream")
            if response.status_code < 200 or response.status_code >= 300:
                raise ExternalServiceError("YouTube search failed", details={"status_code": response.status_code})
            try:
                data = msgspec.json.decode(response.content, type=_YouTubeSearchResponse)
                video_id = data.items[0].id.videoId if data.items and data.items[0].id else None
                if data.items and not video_id:
                    raise InvalidExternalPayloadError("YouTube search returned an invalid video")
            except msgspec.DecodeError as exc:
                raise InvalidExternalPayloadError("YouTube search returned an invalid payload") from exc
            self._state.cache[key] = video_id
            self._state.cache.move_to_end(key)
            if len(self._state.cache) > PREVIEW_CACHE_MAX:
                self._state.cache.popitem(last=False)
            return video_id
        finally:
            if not dispatched:
                if token is not None:
                    token.refund()
                if date is not None:
                    await self._state.quota.refund(date)

    async def verify_api_key(self, api_key: str) -> tuple[bool, str]:
        try:
            response = await self._http_client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "id", "id": "dQw4w9WgXcQ", "key": api_key},
                timeout=10.0,
            )
            if response.status_code == 200:
                return True, "YouTube API key is valid"
            elif response.status_code == 403:
                return False, "API key is invalid or YouTube Data API is not enabled"
            else:
                return False, f"Unexpected response: {response.status_code}"
        except Exception as e:  # noqa: BLE001
            return False, f"Connection error: {e}"
