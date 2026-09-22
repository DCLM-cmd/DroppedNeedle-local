import asyncio
import logging
from collections.abc import Awaitable, Sequence
from typing import Any, Optional, TypeVar

from api.v1.schemas.search import (
    AlbumEnrichment,
    AlbumEnrichmentRequest,
    ArtistEnrichment,
    ArtistEnrichmentRequest,
    EnrichmentBatchRequest,
    EnrichmentResponse,
    EnrichmentSource,
)
from core.exceptions import ClientDisconnectedError
from infrastructure.degradation import try_get_degradation_context
from infrastructure.http.disconnect import DisconnectCallable, check_disconnected
from infrastructure.integration_result import IntegrationResult
from repositories.protocols import (
    LastFmRepositoryProtocol,
    ListenBrainzRepositoryProtocol,
)
from infrastructure.plugins.protocols import (
    PluginAlbumEnrichment,
    PluginArtistEnrichment,
)
from repositories.lastfm_models import LastFmAlbumInfo, LastFmArtistInfo
from services.preferences_service import PreferencesService

logger = logging.getLogger(__name__)

MAX_ENRICHMENT = 10

# B4: equals the repository's own _request_semaphore, so the service cannot
# amplify in-flight pressure beyond what ListenBrainzRepository already
# tolerates; <=10-item batches never stress the 5/s token bucket either.
_ENRICH_CONCURRENCY = 2
_METADATA_TIMEOUT = 30.0

_T = TypeVar("_T")


async def _gather_bounded(
    coros: Sequence[Awaitable[_T]],
) -> list[_T | BaseException]:
    """Run coroutines with at most _ENRICH_CONCURRENCY in flight, preserving
    input order (house pattern: library_service._resolve_one,
    homepage_service warmers). Exceptions surface as entries, not raises."""
    semaphore = asyncio.Semaphore(_ENRICH_CONCURRENCY)

    async def _run(coro: Awaitable[_T]) -> _T:
        async with semaphore:
            return await coro

    return list(
        await asyncio.gather(*(_run(coro) for coro in coros), return_exceptions=True)
    )


def _record_optional_degradation(source: str) -> None:
    ctx = try_get_degradation_context()
    if ctx is not None:
        ctx.record(
            IntegrationResult.error(
                source=source,
                msg=f"{source} search popularity is temporarily unavailable",
            )
        )
def _record_plugin_degradation(plugin_name: str) -> None:
    """Record one plugin metadata failure without ever raising.

    Background tasks have no request scope: ``try_get`` returns ``None``
    and the failure is silently dropped (the caller still degrades to
    ``None``). Any unexpected ``record`` error is swallowed so plugins
    never break enrichment."""
    ctx = try_get_degradation_context()
    if ctx is None:
        return
    try:
        ctx.record(
            IntegrationResult.error(
                source=f"plugin:{plugin_name}",
                msg=f"plugin:{plugin_name} metadata is temporarily unavailable",
            )
        )
    except Exception:  # noqa: BLE001 - degradation bookkeeping never fails the request
        logger.debug(
            "Plugin degradation record failed name=%s",
            plugin_name,
        )


def _is_bio_gap(value: str | None) -> bool:
    """A biography gap is ``None`` or empty ``""``."""
    return value is None or value == ""


def _union_first_party_order(
    first: list[str] | None, *candidates: list[str] | None
) -> list[str]:
    """Union preserving first-party order, then plugin-only entries in order.

    Empty-string entries are gaps and never added; ``None`` lists are gaps."""
    seen: set[str] = set()
    merged: list[str] = []
    for entry in first or []:
        if entry is None or entry == "" or entry in seen:
            continue
        seen.add(entry)
        merged.append(entry)
    for candidate in candidates:
        for entry in candidate or []:
            if entry is None or entry == "" or entry in seen:
                continue
            seen.add(entry)
            merged.append(entry)
    return merged


def _first_plugin_image(
    candidates: Sequence[PluginArtistEnrichment | PluginAlbumEnrichment | None],
) -> list[str]:
    """First non-empty plugin ``image_urls[0]`` (single element) or ``[]``."""
    for candidate in candidates:
        if candidate is None:
            continue
        for url in candidate.image_urls or []:
            if url is not None and url != "":
                return [url]
    return []


def _merge_artist_enrichments(
    first: PluginArtistEnrichment | None,
    candidates: Sequence[PluginArtistEnrichment | None],
) -> PluginArtistEnrichment | None:
    """Merge plugin artist metadata BELOW first-party (never overwrite).

    Gap table: a gap is ``None`` or empty ``""``/``[]``. Biography keeps
    first-party when present, else the first non-empty plugin biography.
    Links/tags union first-party order then plugin-only entries. Images
    keep first-party, else the first plugin ``image_urls[0]``. Returns
    ``None`` when the merged result carries no data (all gaps)."""
    baseline = first if first is not None else PluginArtistEnrichment()
    live = [c for c in candidates if c is not None]
    biography = baseline.biography
    if _is_bio_gap(biography):
        biography = None
        for candidate in live:
            if not _is_bio_gap(candidate.biography):
                biography = candidate.biography
                break
    links = _union_first_party_order(baseline.links, *(c.links for c in live))
    tags = _union_first_party_order(baseline.tags, *(c.tags for c in live))
    if (baseline.image_urls or []) and any(
        url is not None and url != "" for url in (baseline.image_urls or [])
    ):
        image_urls = [url for url in (baseline.image_urls or []) if url is not None and url != ""]
    else:
        image_urls = _first_plugin_image(live)
    if _is_bio_gap(biography) and not links and not tags and not image_urls:
        return None
    return PluginArtistEnrichment(
        biography=biography,
        links=links,
        tags=tags,
        image_urls=image_urls,
    )


def _merge_album_enrichments(
    first: PluginAlbumEnrichment | None,
    candidates: Sequence[PluginAlbumEnrichment | None],
) -> PluginAlbumEnrichment | None:
    """Album twin of :func:`_merge_artist_enrichments` (same gap table)."""
    baseline = first if first is not None else PluginAlbumEnrichment()
    live = [c for c in candidates if c is not None]
    biography = baseline.biography
    if _is_bio_gap(biography):
        biography = None
        for candidate in live:
            if not _is_bio_gap(candidate.biography):
                biography = candidate.biography
                break
    links = _union_first_party_order(baseline.links, *(c.links for c in live))
    tags = _union_first_party_order(baseline.tags, *(c.tags for c in live))
    if (baseline.image_urls or []) and any(
        url is not None and url != "" for url in (baseline.image_urls or [])
    ):
        image_urls = [url for url in (baseline.image_urls or []) if url is not None and url != ""]
    else:
        image_urls = _first_plugin_image(live)
    if _is_bio_gap(biography) and not links and not tags and not image_urls:
        return None
    return PluginAlbumEnrichment(
        biography=biography,
        links=links,
        tags=tags,
        image_urls=image_urls,
    )


def _first_party_artist_baseline(
    info: LastFmArtistInfo | None,
) -> PluginArtistEnrichment | None:
    """Map first-party Last.fm artist info into the plugin enrichment shape."""
    if info is None:
        return None
    return PluginArtistEnrichment(
        biography=info.bio_summary or None,
        links=[info.url] if info.url else [],
        tags=[t.name for t in (info.tags or []) if t.name],
        image_urls=[],
    )


def _first_party_album_baseline(
    info: LastFmAlbumInfo | None,
) -> PluginAlbumEnrichment | None:
    """Map first-party Last.fm album info into the plugin enrichment shape."""
    if info is None:
        return None
    return PluginAlbumEnrichment(
        biography=info.summary or None,
        links=[info.url] if info.url else [],
        tags=[t.name for t in (info.tags or []) if t.name],
        image_urls=[info.image_url] if info.image_url else [],
    )


class SearchEnrichmentService:
    def __init__(
        self,
        lb_repo: ListenBrainzRepositoryProtocol,
        preferences_service: PreferencesService,
        lastfm_repo: Optional[LastFmRepositoryProtocol] = None,
        plugin_host: Any | None = None,
    ):
        self._lb_repo = lb_repo
        self._preferences_service = preferences_service
        self._lastfm_repo = lastfm_repo
        self._plugin_host = plugin_host

    def _is_listenbrainz_enabled(self) -> bool:
        lb_settings = self._preferences_service.get_listenbrainz_connection()
        return lb_settings.enabled and bool(lb_settings.username)

    def _is_lastfm_enabled(self) -> bool:
        try:
            lfm_settings = self._preferences_service.get_lastfm_connection()
            return lfm_settings.enabled and bool(lfm_settings.api_key)
        except Exception:  # noqa: BLE001
            return False

    def _get_enrichment_source(self) -> EnrichmentSource:
        lb_enabled = self._is_listenbrainz_enabled()
        lfm_enabled = self._is_lastfm_enabled() and self._lastfm_repo is not None

        if not lb_enabled and not lfm_enabled:
            return "none"

        try:
            primary = self._preferences_service.get_primary_music_source()
            preferred = primary.source
        except Exception:  # noqa: BLE001
            preferred = "listenbrainz"

        if preferred == "lastfm" and lfm_enabled:
            return "lastfm"
        if preferred == "listenbrainz" and lb_enabled:
            return "listenbrainz"
        if lb_enabled:
            return "listenbrainz"
        if lfm_enabled:
            return "lastfm"
        return "none"

    async def enrich(
        self,
        artist_mbids: list[str],
        album_mbids: list[str],
        *,
        is_disconnected: DisconnectCallable | None = None,
    ) -> EnrichmentResponse:
        return await self.enrich_batch(
            EnrichmentBatchRequest(
                artists=[
                    ArtistEnrichmentRequest(musicbrainz_id=mbid)
                    for mbid in artist_mbids
                ],
                albums=[
                    AlbumEnrichmentRequest(musicbrainz_id=mbid) for mbid in album_mbids
                ],
            ),
            is_disconnected=is_disconnected,
        )

    async def enrich_batch(
        self,
        request: EnrichmentBatchRequest,
        *,
        is_disconnected: DisconnectCallable | None = None,
    ) -> EnrichmentResponse:
        source = self._get_enrichment_source()
        artist_requests = request.artists[:MAX_ENRICHMENT]
        album_requests = request.albums[:MAX_ENRICHMENT]

        await check_disconnected(is_disconnected)
        if source == "none":
            artists = [
                ArtistEnrichment(musicbrainz_id=req.musicbrainz_id)
                for req in artist_requests
            ]
        else:
            artists = self._resolve_gathered(
                await _gather_bounded(
                    [
                        self._enrich_artist(
                            req.musicbrainz_id,
                            source,
                            name=req.name,
                            is_disconnected=is_disconnected,
                        )
                        for req in artist_requests
                    ]
                ),
                artist_requests,
                lambda req: ArtistEnrichment(musicbrainz_id=req.musicbrainz_id),
            )

        await check_disconnected(is_disconnected)
        albums: list[AlbumEnrichment]
        if source == "listenbrainz" and album_requests:
            mbids = [req.musicbrainz_id for req in album_requests]
            try:
                album_listen_counts = (
                    await self._lb_repo.get_release_group_popularity_batch(mbids)
                )
            except Exception as e:  # noqa: BLE001 - optional popularity degrades
                logger.debug(
                    "ListenBrainz search album enrichment failed (%s)",
                    type(e).__name__,
                )
                _record_optional_degradation("listenbrainz")
                album_listen_counts = {}
            albums = [
                AlbumEnrichment(
                    musicbrainz_id=req.musicbrainz_id,
                    listen_count=album_listen_counts.get(req.musicbrainz_id),
                )
                for req in album_requests
            ]
        elif source == "lastfm" and album_requests and self._lastfm_repo:
            albums = self._resolve_gathered(
                await _gather_bounded(
                    [
                        self._enrich_album_lastfm(
                            req.musicbrainz_id,
                            req.artist_name,
                            req.album_name,
                            is_disconnected=is_disconnected,
                        )
                        for req in album_requests
                    ]
                ),
                album_requests,
                lambda req: AlbumEnrichment(musicbrainz_id=req.musicbrainz_id),
            )
        else:
            albums = [
                AlbumEnrichment(musicbrainz_id=req.musicbrainz_id)
                for req in album_requests
            ]

        await check_disconnected(is_disconnected)
        return EnrichmentResponse(artists=artists, albums=albums, source=source)

    @staticmethod
    def _resolve_gathered(results, requests, make_fallback):
        """B4 post-scan: map gathered results back onto their requests in
        order (gather preserves input order).

        ClientDisconnectedError (and non-Exception BaseExceptions such as
        CancelledError) re-raise to preserve the early-abort contract; any
        other exception entry degrades to the bare fallback object, matching
        what each _enrich_* helper's own except-branch returns today."""
        resolved = []
        for res, req in zip(results, requests):
            if isinstance(res, ClientDisconnectedError):
                raise res
            if isinstance(res, BaseException) and not isinstance(res, Exception):
                raise res
            if isinstance(res, BaseException):
                resolved.append(make_fallback(req))
            else:
                resolved.append(res)
        return resolved

    async def _artist_plugin_metadata(
        self,
        *,
        artist_name: str,
        mbid: str | None,
        baseline: PluginArtistEnrichment | None,
        is_disconnected: DisconnectCallable | None = None,
    ) -> PluginArtistEnrichment | None:
        """Fetch enabled metadata plugins below first-party and merge.

        Per-plugin ``asyncio.timeout`` + try/except degrades to ``None``;
        each failure records ``plugin:<name>`` via the never-raising
        ``try_get`` path. Returns the merged ``PluginArtistEnrichment``
        shape (``None`` when everything is a gap). Search schemas stay
        frozen: the caller keeps its own response shape."""
        host: Any = getattr(self, "_plugin_host", None)
        if host is None:
            return _merge_artist_enrichments(baseline, [])
        await check_disconnected(is_disconnected)
        try:
            providers = host.metadata_providers()
        except Exception as exc:  # noqa: BLE001 - host listing failure degrades
            logger.debug(
                "Plugin metadata providers unavailable (%s)",
                type(exc).__name__,
            )
            return _merge_artist_enrichments(baseline, [])
        candidates: list[PluginArtistEnrichment | None] = []
        for plugin in providers or []:
            name = getattr(getattr(plugin, "manifest", None), "name", None) or "unknown"
            instance = getattr(plugin, "instance", None)
            if instance is None:
                continue
            enrich = getattr(instance, "enrich_artist", None)
            if enrich is None:
                continue
            try:
                async with asyncio.timeout(_METADATA_TIMEOUT):
                    result = await enrich(
                        artist_name=artist_name,
                        mbid=mbid,
                        timeout=_METADATA_TIMEOUT,
                    )
            except TimeoutError as exc:  # noqa: BLE001 - timeout degrades to None
                logger.debug(
                    "Plugin artist enrichment timeout name=%s (%s)",
                    name,
                    type(exc).__name__,
                )
                _record_plugin_degradation(name)
                candidates.append(None)
                continue
            except Exception as exc:  # noqa: BLE001 - plugin errors never break enrichment
                logger.debug(
                    "Plugin artist enrichment failed name=%s (%s)",
                    name,
                    type(exc).__name__,
                )
                _record_plugin_degradation(name)
                candidates.append(None)
                continue
            if result is None:
                candidates.append(None)
            elif isinstance(result, PluginArtistEnrichment):
                candidates.append(result)
            else:
                logger.debug(
                    "Plugin artist enrichment bad shape name=%s type=%s",
                    name,
                    type(result).__name__,
                )
                _record_plugin_degradation(name)
                candidates.append(None)
        return _merge_artist_enrichments(baseline, candidates)

    async def _album_plugin_metadata(
        self,
        *,
        artist_name: str,
        album_title: str,
        mbid: str | None,
        baseline: PluginAlbumEnrichment | None,
        is_disconnected: DisconnectCallable | None = None,
    ) -> PluginAlbumEnrichment | None:
        """Album twin of :meth:`_artist_plugin_metadata` (same isolation)."""
        host: Any = getattr(self, "_plugin_host", None)
        if host is None:
            return _merge_album_enrichments(baseline, [])
        await check_disconnected(is_disconnected)
        try:
            providers = host.metadata_providers()
        except Exception as exc:  # noqa: BLE001 - host listing failure degrades
            logger.debug(
                "Plugin metadata providers unavailable (%s)",
                type(exc).__name__,
            )
            return _merge_album_enrichments(baseline, [])
        candidates: list[PluginAlbumEnrichment | None] = []
        for plugin in providers or []:
            name = getattr(getattr(plugin, "manifest", None), "name", None) or "unknown"
            instance = getattr(plugin, "instance", None)
            if instance is None:
                continue
            enrich = getattr(instance, "enrich_album", None)
            if enrich is None:
                continue
            try:
                async with asyncio.timeout(_METADATA_TIMEOUT):
                    result = await enrich(
                        artist_name=artist_name,
                        album_title=album_title,
                        mbid=mbid,
                        timeout=_METADATA_TIMEOUT,
                    )
            except TimeoutError as exc:  # noqa: BLE001 - timeout degrades to None
                logger.debug(
                    "Plugin album enrichment timeout name=%s (%s)",
                    name,
                    type(exc).__name__,
                )
                _record_plugin_degradation(name)
                candidates.append(None)
                continue
            except Exception as exc:  # noqa: BLE001 - plugin errors never break enrichment
                logger.debug(
                    "Plugin album enrichment failed name=%s (%s)",
                    name,
                    type(exc).__name__,
                )
                _record_plugin_degradation(name)
                candidates.append(None)
                continue
            if result is None:
                candidates.append(None)
            elif isinstance(result, PluginAlbumEnrichment):
                candidates.append(result)
            else:
                logger.debug(
                    "Plugin album enrichment bad shape name=%s type=%s",
                    name,
                    type(result).__name__,
                )
                _record_plugin_degradation(name)
                candidates.append(None)
        return _merge_album_enrichments(baseline, candidates)

    async def _enrich_artist(
        self,
        mbid: str,
        source: EnrichmentSource,
        name: str = "",
        *,
        is_disconnected: DisconnectCallable | None = None,
    ) -> ArtistEnrichment:
        listen_count: Optional[int] = None
        artist_info: LastFmArtistInfo | None = None

        await check_disconnected(is_disconnected)
        if source == "listenbrainz":
            try:
                top_releases = await self._lb_repo.get_artist_top_release_groups(
                    mbid, count=5
                )
                if top_releases:
                    listen_count = sum(release.listen_count for release in top_releases)
            except ClientDisconnectedError:
                raise
            except Exception as e:  # noqa: BLE001 - optional popularity degrades
                logger.debug(
                    "ListenBrainz search artist enrichment failed (%s)",
                    type(e).__name__,
                )
                _record_optional_degradation("listenbrainz")
        elif source == "lastfm" and self._lastfm_repo and name:
            try:
                info = await self._lastfm_repo.get_artist_info(artist=name, mbid=mbid)
                if info is not None:
                    artist_info = info
                    if info.listeners is not None:
                        listen_count = info.listeners
            except ClientDisconnectedError:
                raise
            except Exception as e:  # noqa: BLE001 - optional popularity degrades
                logger.debug(
                    "Last.fm search artist enrichment failed (%s)",
                    type(e).__name__,
                )
                _record_optional_degradation("lastfm")

        # Plugin metadata below first-party (search schemas frozen: the merged
        # PluginArtistEnrichment is computed for degradation/merge coverage;
        # this response keeps its listen_count shape).
        try:
            await self._artist_plugin_metadata(
                artist_name=name,
                mbid=mbid,
                baseline=_first_party_artist_baseline(artist_info),
                is_disconnected=is_disconnected,
            )
        except ClientDisconnectedError:
            raise
        except Exception:  # noqa: BLE001 - plugins never break enrichment
            logger.debug("Plugin artist metadata skipped")

        return ArtistEnrichment(musicbrainz_id=mbid, listen_count=listen_count)

    async def _enrich_album_lastfm(
        self,
        mbid: str,
        artist_name: str,
        album_name: str,
        *,
        is_disconnected: DisconnectCallable | None = None,
    ) -> AlbumEnrichment:
        listen_count: Optional[int] = None
        album_info: LastFmAlbumInfo | None = None

        if self._lastfm_repo and artist_name and album_name:
            await check_disconnected(is_disconnected)
            try:
                info = await self._lastfm_repo.get_album_info(
                    artist=artist_name,
                    album=album_name,
                    mbid=mbid,
                )
                if info is not None:
                    album_info = info
                    if info.playcount is not None:
                        listen_count = info.playcount
            except ClientDisconnectedError:
                raise
            except Exception as e:  # noqa: BLE001 - optional popularity degrades
                logger.debug(
                    "Last.fm search album enrichment failed (%s)",
                    type(e).__name__,
                )
                _record_optional_degradation("lastfm")

        # Plugin metadata below first-party (schemas frozen: the merged
        # PluginAlbumEnrichment is computed for degradation/merge coverage).
        try:
            await self._album_plugin_metadata(
                artist_name=artist_name,
                album_title=album_name,
                mbid=mbid,
                baseline=_first_party_album_baseline(album_info),
                is_disconnected=is_disconnected,
            )
        except ClientDisconnectedError:
            raise
        except Exception:  # noqa: BLE001 - plugins never break enrichment
            logger.debug("Plugin album metadata skipped")

        return AlbumEnrichment(musicbrainz_id=mbid, listen_count=listen_count)
