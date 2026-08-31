import asyncio
import hashlib
import httpx
import time
from typing import Any, Awaitable, Callable

import msgspec
from core.exceptions import (
    RateLimitBlockedError,
    ExternalServiceError,
    RateLimitedError,
    ServiceDisabledUpstreamError,
)
from infrastructure.cache.cache_keys import (
    LB_PREFIX,
    listenbrainz_management_genres_key,
)
from infrastructure.cache.memory_cache import CacheInterface
from infrastructure.resilience.retry import with_retry, CircuitBreaker
from infrastructure.resilience.rate_limiter import TokenBucketRateLimiter
from repositories.listenbrainz_models import (
    ListenBrainzArtist,
    ListenBrainzReleaseGroup,
    ListenBrainzRecording,
    ListenBrainzListen,
    ListenBrainzGenreActivity,
    ListenBrainzSimilarArtist,
    ListenBrainzFeedbackRecording,
    ListenBrainzRecommendationTrack,
    ListenBrainzRecommendationPlaylist,
    ALLOWED_STATS_RANGE,
    parse_artist,
    parse_release_group,
    parse_recording,
    parse_listen,
    parse_artist_recording,
    parse_feedback_recording,
    parse_similar_artist,
    parse_recommendation_track,
)
from models.library_management_genres import GenreCandidate
from repositories.listenbrainz_management_models import (
    LbManagementReleaseGroupMetadata,
)
from infrastructure.degradation import try_get_degradation_context
from infrastructure.http.deduplication import RequestDeduplicator
from infrastructure.integration_result import IntegrationResult

_SOURCE = "listenbrainz"


def _record_degradation(msg: str) -> None:
    ctx = try_get_degradation_context()
    if ctx is not None:
        ctx.record(IntegrationResult.error(source=_SOURCE, msg=msg))


def _parse_retry_after(response: httpx.Response) -> float | None:
    """The delay ListenBrainz asked for, or None when it named none.

    None is the important case: it used to default to 2 seconds, so a 429 carrying no
    headers - an edge proxy refusing the address outright, which is what a block looks
    like - was re-sent twice more at two-second intervals. Telling the two apart is
    what lets the caller wait politely for a throttle and stop for a refusal.
    """
    for header in ("X-RateLimit-Reset-In", "Retry-After"):
        value = response.headers.get(header)
        if value is not None:
            try:
                seconds = float(value)
                if seconds > 0:
                    return min(seconds, 10.0)
            except (TypeError, ValueError):
                continue
    return None


# LB popularity outages last hours; a short TTL would expire during any idle gap (no calls
# to re-mark it) and the NEXT build would wrongly take the dead LB path. Keep the flag alive
# well past idle gaps, and heal it INSTANTLY the moment a popularity call succeeds again.
_POPULARITY_DEGRADED_TTL = 1800.0  # 30 minutes


def _mark_popularity_degraded() -> None:
    """Flag LB popularity as genuinely degraded (drives fallbacks + the UI status
    dot). Only ever called on LB's own explicit "disabled"/"auth-gate" replies."""
    from infrastructure.service_health import service_health

    service_health.mark_degraded(
        "listenbrainz",
        "popularity",
        message="ListenBrainz's popularity data is temporarily unavailable.",
        fallback="lastfm",
        severity="degraded",
        ttl_seconds=_POPULARITY_DEGRADED_TTL,
    )


def _heal_popularity() -> None:
    """LB popularity answered successfully - clear the degraded flag immediately."""
    from infrastructure.service_health import service_health

    service_health.heal("listenbrainz", "popularity")


def lb_popularity_degraded() -> bool:
    """True ONLY when ListenBrainz's popularity API is DEFINITELY degraded - i.e. LB
    itself has recently returned an explicit outage response ("Popularity API currently
    disabled due to high load" 500, or the anti-scraper 401), recorded via
    _mark_popularity_degraded() with a sliding TTL that auto-heals. It is NOT set by
    timeouts, network blips, or empty results. Callers use this as the single, shared
    gate for 'may I fall back to Last.fm?' - the answer defaults to NO (prefer LB)."""
    from infrastructure.service_health import service_health

    return service_health.is_degraded("listenbrainz", "popularity")


def _is_upstream_policy_block(response: httpx.Response) -> bool:
    """LB deterministically refuses some endpoints for an outage's duration; these
    must fail fast (no retry storm) and NOT trip the shared breaker (they'd take
    down endpoints that still work, e.g. authenticated similar-artists):
      - popularity feature-flag 500 ("currently disabled due to high load"),
      - anti-scraper 401 ("...please provide an Auth token", added 2026-07 when LB
        began gating anonymous popularity calls) - retrying/tripping on this let one
        token-less caller open the shared breaker and blind every other LB feature.
    """
    text = response.text
    if response.status_code == 500 and "currently disabled" in text:
        return True
    if response.status_code == 401 and (
        "provide an Auth token" in text or "AI scrapers" in text
    ):
        return True
    return False


_listenbrainz_circuit_breaker = CircuitBreaker(
    failure_threshold=10, success_threshold=2, timeout=60.0, name="listenbrainz"
)

_listenbrainz_rate_limiter = TokenBucketRateLimiter(rate=5.0, capacity=10)
_metadata_deduplicator = RequestDeduplicator()

# Coalesces concurrent reads of the same key. The discover/home builders fan out
# over hundreds of artist MBIDs with asyncio.gather, and several builders can run
# at once (one per user, plus the background warmers). Without this, every one of
# those overlapping calls is its own request to ListenBrainz, because none of them
# has written to the cache yet when the others look.
_read_deduplicator = RequestDeduplicator()

# "This artist has nothing" is an answer worth remembering. Caching only non-empty
# results meant every empty answer was re-requested on every single rebuild,
# forever - and on a personal library the dud MBIDs are a large share of the set.
# Shorter than the positive TTL so a genuinely new release still shows up promptly.
NEGATIVE_TTL_SECONDS = 900

LISTENBRAINZ_API_URL = "https://api.listenbrainz.org"

ListenBrainzJsonObject = dict[str, Any]
ListenBrainzJsonArray = list[ListenBrainzJsonObject]
ListenBrainzJson = ListenBrainzJsonObject | ListenBrainzJsonArray


def _decode_json_response(response: httpx.Response) -> ListenBrainzJson:
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray, memoryview)):
        return msgspec.json.decode(content, type=ListenBrainzJson)
    return response.json()


class ListenBrainzRepository:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        cache: CacheInterface,
        username: str = "",
        user_token: str = "",
        fallback_token_provider: "Callable[[], Awaitable[str | None]] | None" = None,
    ):
        self._client = http_client
        self._cache = cache
        self._username = username
        self._user_token = user_token
        self._base_url = LISTENBRAINZ_API_URL
        self._request_semaphore = asyncio.Semaphore(2)
        # borrowed token for PUBLIC reads when this (usually global/enrichment) repo
        # has none of its own; LB now anti-scraper-gates anonymous popularity calls.
        # Resolved once, lazily, and NEVER used for require_auth writes.
        self._fallback_token_provider = fallback_token_provider
        self._fallback_resolved = False

    def configure(self, username: str, user_token: str = "") -> None:
        self._username = username
        self._user_token = user_token

    async def _ensure_read_token(self) -> None:
        if (
            self._user_token
            or self._fallback_token_provider is None
            or self._fallback_resolved
        ):
            return
        self._fallback_resolved = True
        try:
            token = await self._fallback_token_provider()
        except Exception:  # noqa: BLE001 - a missing borrowed token just means anonymous
            token = None
        if token:
            self._user_token = token

    @staticmethod
    def reset_circuit_breaker() -> None:
        _listenbrainz_circuit_breaker.reset()

    async def _cached(
        self,
        cache_key: str,
        loader: "Callable[[], Awaitable[Any]]",
        *,
        ttl_seconds: int,
        negative_ttl_seconds: int = NEGATIVE_TTL_SECONDS,
    ) -> Any:
        """Cache-aside read that remembers empty answers and coalesces duplicates.

        Note the ``is not None`` test: the previous per-method blocks all used a
        truthiness check, so a cached empty list read as a miss and went back to
        ListenBrainz every time.

        A loader returning ``None`` means "I have no answer to cache" - used when a
        capability-wide outage short-circuits the call. Storing that as an empty
        result would hide the artist for the whole negative TTL after LB recovers.
        """
        cached = await self._cache.get(cache_key)
        if cached is not None:
            return cached

        async def load() -> Any:
            result = await loader()
            if result is None:
                return None
            await self._cache.set(
                cache_key,
                result,
                ttl_seconds=ttl_seconds if result else negative_ttl_seconds,
            )
            return result

        return await _read_deduplicator.dedupe(cache_key, load)

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._user_token:
            headers["Authorization"] = f"Token {self._user_token}"
        return headers

    @with_retry(
        max_attempts=3,
        base_delay=1.0,
        max_delay=3.0,
        circuit_breaker=_listenbrainz_circuit_breaker,
        retriable_exceptions=(httpx.HTTPError, ExternalServiceError),
        non_breaking_exceptions=(RateLimitedError,),
        # A throttle that names an interval is waited out (see _get_retry_after_seconds);
        # a 429 with no hint at all is a refusal and is NOT re-sent. Retrying those
        # tripled exactly the traffic the server had asked us to stop: the discover
        # homepage issues three stats calls per load, so one page view became nine
        # requests into a rate limit, and MetaBrainz eventually stopped answering this
        # address entirely - dropped TLS handshakes for ListenBrainz, MusicBrainz and
        # the Cover Art Archive alike, since all three answer on one IP.
        non_retriable_exceptions=(RateLimitBlockedError,),
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        require_auth: bool = False,
    ) -> Any:
        url = f"{self._base_url}{endpoint}"

        # a borrowed fallback token authenticates public reads only; writes must use
        # this repo's own (real user) token, never someone else's
        if not require_auth:
            await self._ensure_read_token()

        if require_auth and not self._user_token:
            raise ExternalServiceError(
                "ListenBrainz user token required for this request"
            )

        await _listenbrainz_rate_limiter.acquire()

        async with self._request_semaphore:
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers=self._get_headers(),
                    params=params,
                    json=json_data,
                    timeout=15.0,
                )

                if response.status_code == 204:
                    return None

                if response.status_code == 404:
                    return None

                if response.status_code == 429:
                    retry_after = _parse_retry_after(response)
                    if retry_after is None:
                        raise RateLimitBlockedError(
                            f"ListenBrainz refused ({method} {endpoint}) without a "
                            "retry hint",
                            response.text,
                        )
                    raise RateLimitedError(
                        f"ListenBrainz rate limited ({method} {endpoint})",
                        response.text,
                        retry_after_seconds=retry_after,
                    )

                if response.status_code != 200:
                    # deterministic upstream policy blocks (disabled-under-load 500,
                    # anti-scraper 401): fail fast, outside the retriable set and outside
                    # ExternalServiceError, so with_retry doesn't storm and the shared LB
                    # breaker stays closed for endpoints that still work
                    if _is_upstream_policy_block(response):
                        _mark_popularity_degraded()
                        raise ServiceDisabledUpstreamError(
                            f"ListenBrainz {method} {endpoint} unavailable upstream "
                            f"({response.status_code})",
                            response.text,
                        )
                    raise ExternalServiceError(
                        f"ListenBrainz {method} failed ({response.status_code})",
                        response.text,
                    )

                # a 200 from a popularity endpoint means LB popularity recovered - heal now
                # so the degraded flag doesn't linger for the full TTL after LB comes back
                if "/popularity/" in endpoint:
                    _heal_popularity()

                try:
                    return _decode_json_response(response)
                except (msgspec.DecodeError, ValueError, TypeError):
                    _record_degradation(
                        f"ListenBrainz returned invalid JSON for {method} {endpoint}"
                    )
                    return None

            except httpx.HTTPError as e:
                # str() on a timeout is EMPTY, so the message used to read
                # "ListenBrainz request failed: " with nothing after the colon - which
                # said neither what went wrong nor where, and sent a live investigation
                # chasing DNS. Name the exception type and the endpoint always.
                detail = str(e) or type(e).__name__
                raise ExternalServiceError(
                    f"ListenBrainz {method} {endpoint} failed: "
                    f"{type(e).__name__}: {detail}"
                )

    async def _get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        require_auth: bool = False,
    ) -> Any:
        return await self._request(
            "GET", endpoint, params=params, require_auth=require_auth
        )

    async def _post(
        self, endpoint: str, data: dict[str, Any], require_auth: bool = False
    ) -> Any:
        return await self._request(
            "POST", endpoint, json_data=data, require_auth=require_auth
        )

    async def validate_username(self, username: str | None = None) -> tuple[bool, str]:
        user = username or self._username
        if not user:
            return False, "No username provided"

        try:
            url = f"{self._base_url}/1/user/{user}/listen-count"
            response = await self._client.request(
                "GET",
                url,
                headers=self._get_headers(),
                timeout=10.0,
            )

            if response.status_code == 404:
                return False, f"User '{user}' not found"

            if response.status_code != 200:
                return False, f"Validation failed (HTTP {response.status_code})"

            result = _decode_json_response(response)
            if result and "payload" in result:
                count = result.get("payload", {}).get("count", 0)
                return True, f"User found with {count:,} listens"
            return False, "User not found"
        except httpx.TimeoutException:
            return False, "Connection timed out"
        except httpx.ConnectError:
            return False, "Could not connect to ListenBrainz"
        except Exception as e:  # noqa: BLE001
            return False, f"Validation failed: {str(e)}"

    async def validate_token(self) -> tuple[bool, str]:
        if not self._user_token:
            return False, "No token provided"

        try:
            url = f"{self._base_url}/1/validate-token"
            headers = self._get_headers()
            response = await self._client.request(
                "GET",
                url,
                headers=headers,
                timeout=10.0,
            )

            if response.status_code != 200:
                return False, "Token invalid or expired"

            result = _decode_json_response(response)
            if result and result.get("valid"):
                username = result.get("user_name", self._username)
                return True, f"Successfully connected as '{username}'"
            return False, "Token invalid"
        except httpx.TimeoutException:
            return False, "Connection timed out"
        except httpx.ConnectError:
            return False, "Could not connect to ListenBrainz"
        except Exception as e:  # noqa: BLE001
            return False, f"Validation failed: {str(e)}"

    async def get_user_listens(
        self,
        username: str | None = None,
        count: int = 25,
        max_ts: int | None = None,
        min_ts: int | None = None,
    ) -> list[ListenBrainzListen]:
        user = username or self._username
        if not user:
            return []

        params: dict[str, Any] = {"count": min(count, 100)}
        if max_ts:
            params["max_ts"] = max_ts
        if min_ts:
            params["min_ts"] = min_ts

        result = await self._get(f"/1/user/{user}/listens", params=params)
        if not result:
            return []
        return [
            parse_listen(item) for item in result.get("payload", {}).get("listens", [])
        ]

    async def get_user_loved_recordings(
        self,
        username: str | None = None,
        count: int = 25,
        offset: int = 0,
    ) -> list[ListenBrainzFeedbackRecording]:
        user = username or self._username
        if not user:
            return []

        cache_key = f"{LB_PREFIX}user_loved_recordings:{user}:{count}:{offset}"
        async def load():
            params: dict[str, Any] = {
                "score": 1,
                "count": min(count, 100),
                "offset": offset,
                "metadata": "true",
            }
            result = await self._get(f"/1/feedback/user/{user}/get-feedback", params=params)
            if not result:
                return []

            payload = result.get("payload", result)
            feedback_items: list[dict[str, Any]]
            if isinstance(payload, dict):
                feedback_raw = payload.get("feedback") or payload.get("recordings") or []
                if isinstance(feedback_raw, list):
                    feedback_items = [
                        item for item in feedback_raw if isinstance(item, dict)
                    ]
                else:
                    feedback_items = []
            elif isinstance(payload, list):
                feedback_items = [item for item in payload if isinstance(item, dict)]
            else:
                feedback_items = []

            loved_recordings = [parse_feedback_recording(item) for item in feedback_items]
            return loved_recordings

        return await self._cached(cache_key, load, ttl_seconds=300)

    async def get_user_top_artists(
        self,
        username: str | None = None,
        range_: str = "this_month",
        count: int = 25,
        offset: int = 0,
    ) -> list[ListenBrainzArtist]:
        user = username or self._username
        if not user:
            return []

        if range_ not in ALLOWED_STATS_RANGE:
            range_ = "this_month"

        cache_key = f"{LB_PREFIX}user_artists:{user}:{range_}:{count}:{offset}"
        async def load():
            params = {"count": min(count, 100), "offset": offset, "range": range_}
            result = await self._get(f"/1/stats/user/{user}/artists", params=params)
            if not result:
                return []
            artists = [
                parse_artist(item) for item in result.get("payload", {}).get("artists", [])
            ]
            return artists

        return await self._cached(cache_key, load, ttl_seconds=300)

    async def get_user_top_release_groups(
        self,
        username: str | None = None,
        range_: str = "this_month",
        count: int = 25,
        offset: int = 0,
    ) -> list[ListenBrainzReleaseGroup]:
        user = username or self._username
        if not user:
            return []

        if range_ not in ALLOWED_STATS_RANGE:
            range_ = "this_month"

        cache_key = f"{LB_PREFIX}user_release_groups:{user}:{range_}:{count}:{offset}"
        async def load():
            params = {"count": min(count, 100), "offset": offset, "range": range_}
            result = await self._get(f"/1/stats/user/{user}/release-groups", params=params)
            if not result:
                return []
            groups = [
                parse_release_group(item)
                for item in result.get("payload", {}).get("release_groups", [])
            ]
            return groups

        return await self._cached(cache_key, load, ttl_seconds=300)

    async def get_user_top_recordings(
        self,
        username: str | None = None,
        range_: str = "this_month",
        count: int = 25,
        offset: int = 0,
    ) -> list[ListenBrainzRecording]:
        user = username or self._username
        if not user:
            return []

        if range_ not in ALLOWED_STATS_RANGE:
            range_ = "this_month"

        # The only user-stats read that had no cache at all, so every caller that
        # wanted the user's top tracks hit ListenBrainz directly.
        cache_key = f"{LB_PREFIX}user_recordings:{user}:{range_}:{count}:{offset}"

        async def load() -> list[ListenBrainzRecording]:
            params = {"count": min(count, 100), "offset": offset, "range": range_}
            result = await self._get(f"/1/stats/user/{user}/recordings", params=params)
            if not result:
                return []
            return [
                parse_recording(item)
                for item in result.get("payload", {}).get("recordings", [])
            ]

        return await self._cached(cache_key, load, ttl_seconds=300)

    async def get_user_genre_activity(
        self, username: str | None = None
    ) -> list[ListenBrainzGenreActivity]:
        user = username or self._username
        if not user:
            return []

        cache_key = f"{LB_PREFIX}user_genres:{user}"
        async def load():
            result = await self._get(f"/1/stats/user/{user}/genre-activity")

            if not result:
                return []

            genre_counts: dict[str, int] = {}
            for item in result.get("result", []):
                genre = item.get("genre", "Unknown")
                count = item.get("listen_count", 0)
                genre_counts[genre] = genre_counts.get(genre, 0) + count

            genres = [
                ListenBrainzGenreActivity(genre=g, listen_count=c)
                for g, c in sorted(genre_counts.items(), key=lambda x: -x[1])
            ]
            return genres

        return await self._cached(cache_key, load, ttl_seconds=300)

    async def get_sitewide_top_artists(
        self, range_: str = "week", count: int = 25, offset: int = 0
    ) -> list[ListenBrainzArtist]:
        if range_ not in ALLOWED_STATS_RANGE:
            range_ = "week"

        cache_key = f"{LB_PREFIX}sitewide_artists:{range_}:{count}:{offset}"
        async def load():
            params = {"count": min(count, 100), "offset": offset, "range": range_}
            result = await self._get("/1/stats/sitewide/artists", params=params)
            if not result:
                return []
            artists = [
                parse_artist(item) for item in result.get("payload", {}).get("artists", [])
            ]
            return artists

        return await self._cached(cache_key, load, ttl_seconds=3600)

    async def get_sitewide_top_release_groups(
        self, range_: str = "week", count: int = 25, offset: int = 0
    ) -> list[ListenBrainzReleaseGroup]:
        if range_ not in ALLOWED_STATS_RANGE:
            range_ = "week"

        cache_key = f"{LB_PREFIX}sitewide_release_groups:{range_}:{count}:{offset}"
        async def load():
            params = {"count": min(count, 100), "offset": offset, "range": range_}
            result = await self._get("/1/stats/sitewide/release-groups", params=params)
            if not result:
                return []
            groups = [
                parse_release_group(item)
                for item in result.get("payload", {}).get("release_groups", [])
            ]
            return groups

        return await self._cached(cache_key, load, ttl_seconds=3600)

    async def get_sitewide_top_recordings(
        self, range_: str = "week", count: int = 25, offset: int = 0
    ) -> list[ListenBrainzRecording]:
        if range_ not in ALLOWED_STATS_RANGE:
            range_ = "week"

        cache_key = f"{LB_PREFIX}sitewide_recordings:{range_}:{count}:{offset}"
        async def load():
            params = {"count": min(count, 100), "offset": offset, "range": range_}
            result = await self._get("/1/stats/sitewide/recordings", params=params)
            if not result:
                return []
            recordings = [
                parse_recording(item)
                for item in result.get("payload", {}).get("recordings", [])
            ]
            return recordings

        return await self._cached(cache_key, load, ttl_seconds=3600)

    async def get_artist_top_recordings(
        self, artist_mbid: str, count: int = 10
    ) -> list[ListenBrainzRecording]:
        if not artist_mbid:
            return []

        # The endpoint takes no count - it returns the artist's full top list and
        # the trim happens here. Keying the cache on count therefore bought nothing
        # and cost a fresh request per distinct count the callers happened to ask
        # for. Cache the upstream answer once, slice it per caller.
        cache_key = f"{LB_PREFIX}artist_recordings:{artist_mbid}"

        async def load() -> list[ListenBrainzRecording] | None:
            # This outage is capability-wide, not artist-specific. Once LB explicitly
            # disables popularity, let the shared health TTL expire before probing
            # again - and don't cache the blank as if it were this artist's answer.
            if lb_popularity_degraded():
                _record_degradation(
                    "ListenBrainz popularity is temporarily unavailable"
                )
                return None
            result = await self._get(
                f"/1/popularity/top-recordings-for-artist/{artist_mbid}"
            )
            if not result or not isinstance(result, list):
                return []
            return [parse_artist_recording(item) for item in result]

        recordings = await self._cached(cache_key, load, ttl_seconds=3600)
        return (recordings or [])[:count]

    async def get_recording_release_groups_batch(
        self,
        recording_mbids: list[str],
    ) -> dict[str, str]:
        """Resolve recordings to release groups through ListenBrainz metadata.

        Live-verified against ListenBrainz 2026-07-17: POST /1/metadata/recording/
        with ``inc=release`` returns an object keyed by recording MBID whose release
        object carries ``release_group_mbid``.
        """
        unique_mbids = list(dict.fromkeys(mbid for mbid in recording_mbids if mbid))
        if not unique_mbids:
            return {}

        resolved: dict[str, str] = {}
        pending: list[str] = []
        for mbid in unique_mbids:
            cache_key = f"{LB_PREFIX}recording_release_group:{mbid}"
            cached = await self._cache.get(cache_key)
            if cached is None:
                pending.append(mbid)
            elif cached:
                resolved[mbid] = cached

        pending.sort()
        for start in range(0, len(pending), 50):
            batch = pending[start : start + 50]
            dedupe_key = "listenbrainz:recording-metadata:" + ",".join(batch)
            result = await _metadata_deduplicator.dedupe(
                dedupe_key,
                lambda batch=batch: self._post(
                    "/1/metadata/recording/",
                    {"recording_mbids": batch, "inc": "release"},
                ),
            )
            if not isinstance(result, dict):
                _record_degradation("ListenBrainz returned no recording metadata")
                continue
            payload = result
            for mbid in batch:
                metadata = payload.get(mbid)
                release = (
                    metadata.get("release") if isinstance(metadata, dict) else None
                )
                release_group_mbid = (
                    release.get("release_group_mbid")
                    if isinstance(release, dict)
                    else None
                )
                cache_value = (
                    release_group_mbid if isinstance(release_group_mbid, str) else ""
                )
                await self._cache.set(
                    f"{LB_PREFIX}recording_release_group:{mbid}",
                    cache_value,
                    ttl_seconds=86400,
                )
                if cache_value:
                    resolved[mbid] = cache_value

        return resolved

    async def get_release_group_genres_batch(
        self, release_group_mbids: list[str]
    ) -> dict[str, tuple[GenreCandidate, ...]]:
        """Fetch live-verified GET-only release-group genre metadata.

        Verified against production on 2026-07-21; see
        ``listenbrainz_MANAGEMENT_API_NOTES.md``. The local 25-ID ceiling bounds
        request URLs and response work and is not an asserted upstream limit.
        """
        unique_mbids = list(
            dict.fromkeys(
                value.strip() for value in release_group_mbids if value.strip()
            )
        )
        if len(unique_mbids) > 500:
            raise ValueError("ListenBrainz genre lookup accepts at most 500 IDs.")
        resolved: dict[str, tuple[GenreCandidate, ...]] = {}
        pending: list[str] = []
        for mbid in unique_mbids:
            cache_key = listenbrainz_management_genres_key(mbid)
            cached = await self._cache.get(cache_key)
            if isinstance(cached, tuple):
                resolved[mbid] = cached
            else:
                pending.append(mbid)

        pending.sort()
        for start in range(0, len(pending), 25):
            batch = pending[start : start + 25]
            dedupe_key = "listenbrainz:management:release-group-genres:" + ",".join(
                batch
            )
            result = await _metadata_deduplicator.dedupe(
                dedupe_key,
                lambda batch=batch: self._get(
                    "/1/metadata/release_group/",
                    params={
                        "release_group_mbids": ",".join(batch),
                        "inc": "artist tag release",
                    },
                ),
            )
            if result is None:
                raise ExternalServiceError(
                    "ListenBrainz returned no release-group metadata."
                )
            try:
                decoded = msgspec.convert(
                    result,
                    type=dict[str, LbManagementReleaseGroupMetadata],
                )
            except (msgspec.ValidationError, TypeError, ValueError) as error:
                _record_degradation(
                    "ListenBrainz returned invalid release-group metadata"
                )
                raise ExternalServiceError(
                    "ListenBrainz returned invalid release-group metadata."
                ) from error

            fetched_at = time.time()
            for mbid in batch:
                metadata = decoded.get(mbid)
                tagged_values = (
                    tuple(
                        ("release_group", value) for value in metadata.tag.release_group
                    )
                    + tuple(("artist", value) for value in metadata.tag.artist)
                    if metadata is not None
                    else ()
                )
                revision_material = "|".join(
                    f"{value.tag}:{value.count}:{value.genre_mbid or ''}"
                    for _entity, value in tagged_values
                )
                revision = hashlib.sha256(revision_material.encode()).hexdigest()
                candidates = tuple(
                    GenreCandidate(
                        display_name=value.tag,
                        folded_name=" ".join(value.tag.split()).casefold(),
                        provider="listenbrainz",
                        provider_entity=entity,
                        genre_mbid=value.genre_mbid,
                        count=value.count,
                        curated=bool(value.genre_mbid),
                        fetched_at=fetched_at,
                        source_document_revision=revision,
                    )
                    for entity, value in tagged_values
                    if value.tag
                )
                resolved[mbid] = candidates
                await self._cache.set(
                    listenbrainz_management_genres_key(mbid),
                    candidates,
                    ttl_seconds=3600,
                )
        return resolved

    async def get_similar_users(
        self, username: str | None = None
    ) -> list[dict[str, Any]]:
        user = username or self._username
        if not user:
            return []

        result = await self._get(f"/1/user/{user}/similar-users")

        if not result:
            return []

        return result.get("payload", [])

    async def get_user_fresh_releases(
        self, username: str | None = None, past: bool = True, future: bool = False
    ) -> list[dict[str, Any]]:
        user = username or self._username
        if not user:
            return []

        cache_key = f"{LB_PREFIX}fresh_releases:{user}:{past}:{future}"
        async def load():
            params = {"past": str(past).lower(), "future": str(future).lower()}
            result = await self._get(f"/1/user/{user}/fresh_releases", params=params)

            if not result:
                return []

            releases = result.get("payload", {}).get("releases", [])
            return releases

        return await self._cached(cache_key, load, ttl_seconds=3600)

    async def get_similar_artists(
        self, artist_mbid: str, max_similar: int = 15, mode: str = "easy"
    ) -> list[ListenBrainzSimilarArtist]:
        cache_key = f"{LB_PREFIX}similar_artists:{artist_mbid}:{max_similar}:{mode}"
        async def load():
            params = {
                "mode": mode,
                "max_similar_artists": max_similar,
                "max_recordings_per_artist": 5,
                "pop_begin": 0,
                "pop_end": 100,
            }
            result = await self._get(f"/1/lb-radio/artist/{artist_mbid}", params=params)
            if not result or "error" in result:
                return []

            similar_artists: list[ListenBrainzSimilarArtist] = []
            for mbid, recordings in result.items():
                if mbid == artist_mbid:
                    continue
                if not isinstance(recordings, list):
                    continue
                similar_artists.append(parse_similar_artist(mbid, recordings))

            similar_artists.sort(key=lambda a: a.listen_count, reverse=True)
            return similar_artists

        return await self._cached(cache_key, load, ttl_seconds=3600)

    async def get_artist_top_release_groups(
        self, artist_mbid: str, count: int = 10
    ) -> list[ListenBrainzReleaseGroup]:
        if not artist_mbid:
            return []

        # By far the most-called ListenBrainz read: the discover and home builders
        # gather over every seed and similar artist. The endpoint ignores count and
        # returns the artist's whole top list, so the old ``:{count}`` key fetched
        # the same artist up to three times over (callers ask for 1, 2 and 10) and
        # stored three copies of it. One key, sliced per caller.
        cache_key = f"{LB_PREFIX}artist_release_groups:{artist_mbid}"

        async def load() -> list[ListenBrainzReleaseGroup] | None:
            if lb_popularity_degraded():
                _record_degradation(
                    "ListenBrainz popularity is temporarily unavailable"
                )
                return None
            result = await self._get(
                f"/1/popularity/top-release-groups-for-artist/{artist_mbid}"
            )
            if not result or not isinstance(result, list):
                return []
            release_groups = []
            for item in result:
                rg = item.get("release_group", {})
                release_groups.append(
                    ListenBrainzReleaseGroup(
                        release_group_name=rg.get("name", "Unknown"),
                        artist_name=item.get("artist", {}).get("name", "Unknown"),
                        listen_count=item.get("total_listen_count", 0),
                        release_group_mbid=item.get("release_group_mbid"),
                        caa_release_mbid=rg.get("caa_release_mbid"),
                        caa_id=rg.get("caa_id"),
                    )
                )
            return release_groups

        release_groups = await self._cached(cache_key, load, ttl_seconds=3600)
        return (release_groups or [])[:count]

    async def get_release_group_popularity_batch(
        self, release_group_mbids: list[str]
    ) -> dict[str, int]:
        """Get listen counts for multiple release groups in a single call.

        Returns a dict mapping mbid -> total_listen_count.
        """
        if not release_group_mbids:
            return {}

        # Cached per MBID, not per batch: the same albums recur across home,
        # discover and search with a different set each time, so a whole-batch key
        # would almost never hit. This endpoint had no cache at all, which meant a
        # POST to ListenBrainz on every listing render.
        counts: dict[str, int] = {}
        missing: list[str] = []
        for mbid in dict.fromkeys(release_group_mbids):
            cached = await self._cache.get(f"{LB_PREFIX}rg_popularity:{mbid}")
            if cached is None:
                missing.append(mbid)
            elif cached >= 0:
                counts[mbid] = cached
        if not missing:
            return counts

        if lb_popularity_degraded():
            _record_degradation("ListenBrainz popularity is temporarily unavailable")
            return counts

        result = await self._post(
            "/1/popularity/release-group", {"release_group_mbids": missing}
        )
        # An empty list is a real answer ("LB has counts for none of these") and must
        # still be remembered below; only a missing/malformed body means we learned
        # nothing and should ask again next time.
        if result is None or not isinstance(result, list):
            return counts

        fetched: dict[str, int] = {}
        for item in result:
            mbid = item.get("release_group_mbid")
            count = item.get("total_listen_count")
            if mbid and count is not None:
                fetched[mbid] = count
        for mbid in missing:
            listen_count = fetched.get(mbid)
            if listen_count is None:
                # -1 records "LB has no count for this one" so the next listing
                # doesn't ask again; it is never surfaced as a real count.
                await self._cache.set(
                    f"{LB_PREFIX}rg_popularity:{mbid}",
                    -1,
                    ttl_seconds=NEGATIVE_TTL_SECONDS,
                )
                continue
            await self._cache.set(
                f"{LB_PREFIX}rg_popularity:{mbid}", listen_count, ttl_seconds=3600
            )
            counts[mbid] = listen_count
        return counts

    def is_configured(self) -> bool:
        return bool(self._username)

    async def submit_now_playing(
        self,
        artist_name: str,
        track_name: str,
        release_name: str = "",
        duration_ms: int = 0,
    ) -> bool:
        track_metadata: dict[str, Any] = {
            "artist_name": artist_name,
            "track_name": track_name,
        }
        if release_name:
            track_metadata["release_name"] = release_name
        if duration_ms > 0:
            track_metadata["additional_info"] = {"duration_ms": duration_ms}

        payload = {
            "listen_type": "playing_now",
            "payload": [{"track_metadata": track_metadata}],
        }
        await self._post("/1/submit-listens", payload, require_auth=True)
        return True

    async def submit_single_listen(
        self,
        artist_name: str,
        track_name: str,
        listened_at: int,
        release_name: str = "",
        duration_ms: int = 0,
    ) -> bool:
        track_metadata: dict[str, Any] = {
            "artist_name": artist_name,
            "track_name": track_name,
        }
        if release_name:
            track_metadata["release_name"] = release_name
        if duration_ms > 0:
            track_metadata["additional_info"] = {"duration_ms": duration_ms}

        payload = {
            "listen_type": "single",
            "payload": [
                {
                    "listened_at": listened_at,
                    "track_metadata": track_metadata,
                }
            ],
        }
        await self._post("/1/submit-listens", payload, require_auth=True)
        return True

    async def get_recommendation_playlists(
        self, username: str | None = None
    ) -> list[dict[str, Any]]:
        user = username or self._username
        if not user:
            return []

        cache_key = f"{LB_PREFIX}rec_playlists:{user}"
        cached = await self._cache.get(cache_key)
        if cached is not None:
            return cached

        result = await self._get(f"/1/user/{user}/playlists/recommendations")
        if not result or not isinstance(result, dict):
            return []

        playlists_raw = result.get("playlists", [])
        playlists: list[dict[str, Any]] = []
        for entry in playlists_raw:
            pl = entry.get("playlist", {})
            if not isinstance(pl, dict):
                continue

            identifier = pl.get("identifier", "")
            playlist_id = identifier.rsplit("/", 1)[-1] if identifier else ""
            if not playlist_id:
                continue

            ext = pl.get("extension", {})
            mb_ext = ext.get("https://musicbrainz.org/doc/jspf#playlist", {})
            algo = mb_ext.get("additional_metadata", {}).get("algorithm_metadata", {})

            playlists.append(
                {
                    "playlist_id": playlist_id,
                    "identifier": identifier,
                    "title": pl.get("title", ""),
                    "date": pl.get("date", ""),
                    "source_patch": algo.get("source_patch", ""),
                }
            )

        if playlists:
            await self._cache.set(cache_key, playlists, ttl_seconds=21600)
        return playlists

    async def get_playlist_tracks(
        self, playlist_id: str
    ) -> ListenBrainzRecommendationPlaylist | None:
        if not playlist_id:
            return None

        cache_key = f"{LB_PREFIX}rec_playlist:{playlist_id}"
        cached = await self._cache.get(cache_key)
        if cached is not None:
            return cached

        result = await self._get(f"/1/playlist/{playlist_id}")
        if not result or not isinstance(result, dict):
            return None

        pl = result.get("playlist", {})
        if not isinstance(pl, dict):
            return None

        ext = pl.get("extension", {})
        mb_ext = ext.get("https://musicbrainz.org/doc/jspf#playlist", {})
        algo = mb_ext.get("additional_metadata", {}).get("algorithm_metadata", {})

        tracks: list[ListenBrainzRecommendationTrack] = []
        for raw_track in pl.get("track", []):
            parsed = parse_recommendation_track(raw_track)
            if parsed:
                tracks.append(parsed)

        playlist = ListenBrainzRecommendationPlaylist(
            identifier=pl.get("identifier", ""),
            title=pl.get("title", ""),
            date=pl.get("date", ""),
            source_patch=algo.get("source_patch", ""),
            tracks=tracks,
        )

        if tracks:
            await self._cache.set(cache_key, playlist, ttl_seconds=21600)

        return playlist
