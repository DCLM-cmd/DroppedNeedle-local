import logging
from typing import Optional

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
from services.preferences_service import PreferencesService

logger = logging.getLogger(__name__)

MAX_ENRICHMENT = 10


def _record_optional_degradation(source: str) -> None:
    ctx = try_get_degradation_context()
    if ctx is not None:
        ctx.record(
            IntegrationResult.error(
                source=source,
                msg=f"{source} search popularity is temporarily unavailable",
            )
        )


class SearchEnrichmentService:
    def __init__(
        self,
        lb_repo: ListenBrainzRepositoryProtocol,
        preferences_service: PreferencesService,
        lastfm_repo: Optional[LastFmRepositoryProtocol] = None,
    ):
        self._lb_repo = lb_repo
        self._preferences_service = preferences_service
        self._lastfm_repo = lastfm_repo

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
            artists = []
            for req in artist_requests:
                artists.append(
                    await self._enrich_artist(
                        req.musicbrainz_id,
                        source,
                        name=req.name,
                        is_disconnected=is_disconnected,
                    )
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
            albums = []
            for req in album_requests:
                albums.append(
                    await self._enrich_album_lastfm(
                        req.musicbrainz_id,
                        req.artist_name,
                        req.album_name,
                        is_disconnected=is_disconnected,
                    )
                )
        else:
            albums = [
                AlbumEnrichment(musicbrainz_id=req.musicbrainz_id)
                for req in album_requests
            ]

        await check_disconnected(is_disconnected)
        return EnrichmentResponse(artists=artists, albums=albums, source=source)

    async def _enrich_artist(
        self,
        mbid: str,
        source: EnrichmentSource,
        name: str = "",
        *,
        is_disconnected: DisconnectCallable | None = None,
    ) -> ArtistEnrichment:
        listen_count: Optional[int] = None

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
                if info and info.listeners is not None:
                    listen_count = info.listeners
            except ClientDisconnectedError:
                raise
            except Exception as e:  # noqa: BLE001 - optional popularity degrades
                logger.debug(
                    "Last.fm search artist enrichment failed (%s)",
                    type(e).__name__,
                )
                _record_optional_degradation("lastfm")

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

        if self._lastfm_repo and artist_name and album_name:
            await check_disconnected(is_disconnected)
            try:
                info = await self._lastfm_repo.get_album_info(
                    artist=artist_name,
                    album=album_name,
                    mbid=mbid,
                )
                if info and info.playcount is not None:
                    listen_count = info.playcount
            except ClientDisconnectedError:
                raise
            except Exception as e:  # noqa: BLE001 - optional popularity degrades
                logger.debug(
                    "Last.fm search album enrichment failed (%s)",
                    type(e).__name__,
                )
                _record_optional_degradation("lastfm")

        return AlbumEnrichment(musicbrainz_id=mbid, listen_count=listen_count)
