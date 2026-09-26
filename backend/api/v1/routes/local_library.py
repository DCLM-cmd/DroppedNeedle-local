import logging
from typing import Literal

import msgspec.structs
from fastapi import APIRouter, Depends, HTTPException, Query

from api.v1.schemas.local_files import (
    CrateResponse,
    DecadesResponse,
    LocalAlbumMatch,
    LocalAlbumSummary,
    LocalLyricLine,
    LocalLyricsResponse,
    LocalPaginatedResponse,
    LocalSearchResponse,
    LocalStorageStats,
    LocalTrackInfo,
)
from core.dependencies import (
    CurrentUserDep,
    NativeLyricsServiceDep,
    get_local_files_service,
    get_preferences_service,
)
from core.exceptions import ExternalServiceError
from infrastructure.msgspec_fastapi import MsgSpecRoute
from services.local_files_service import LocalFilesService
from services.preferences_service import PreferencesService

logger = logging.getLogger(__name__)

router = APIRouter(route_class=MsgSpecRoute, prefix="/local", tags=["local-files"])


def _with_download_allowed(
    albums: list[LocalAlbumSummary], allowed: bool
) -> list[LocalAlbumSummary]:
    """Stamp the per-request viewer download bit onto summary items."""
    return [
        msgspec.structs.replace(album, download_allowed=allowed) for album in albums
    ]


@router.get("/tracks/{file_id}/lyrics", response_model=LocalLyricsResponse)
async def get_local_track_lyrics(
    file_id: str,
    current_user: CurrentUserDep,
    service: NativeLyricsServiceDep,
) -> LocalLyricsResponse:
    lyrics = await service.get(file_id)
    if lyrics is None:
        raise HTTPException(status_code=404, detail="Lyrics not available")
    return LocalLyricsResponse(
        text="\n".join(line.value for line in lyrics.lines),
        is_synced=lyrics.synced,
        lines=[
            LocalLyricLine(
                text=line.value,
                start_seconds=(
                    line.start_ms / 1000 if line.start_ms is not None else None
                ),
            )
            for line in lyrics.lines
        ],
    )


@router.get("/albums", response_model=LocalPaginatedResponse)
async def get_local_albums(
    current_user: CurrentUserDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort_by: Literal["name", "date_added", "year", "random", "rediscover"] = "name",
    sort_order: Literal["asc", "desc"] = Query(default="asc"),
    q: str | None = Query(default=None, min_length=1),
    decade: int | None = Query(default=None),
    service: LocalFilesService = Depends(get_local_files_service),
    preferences: PreferencesService = Depends(get_preferences_service),
) -> LocalPaginatedResponse:
    try:
        page = await service.get_albums(
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_order=sort_order,
            search_query=q,
            decade=decade,
        )
        return msgspec.structs.replace(
            page,
            items=_with_download_allowed(
                page.items,
                preferences.is_library_download_allowed(current_user.role),
            ),
        )
    except ExternalServiceError as e:
        logger.error("Failed to get local albums: %s", e)
        raise HTTPException(status_code=502, detail="Failed to get local albums")


@router.get("/albums/match/{musicbrainz_id}", response_model=LocalAlbumMatch)
async def match_local_album(
    musicbrainz_id: str,
    current_user: CurrentUserDep,
    service: LocalFilesService = Depends(get_local_files_service),
    preferences: PreferencesService = Depends(get_preferences_service),
) -> LocalAlbumMatch:
    try:
        match = await service.match_album_by_mbid(musicbrainz_id)
        return msgspec.structs.replace(
            match,
            download_allowed=preferences.is_library_download_allowed(current_user.role),
        )
    except ExternalServiceError as e:
        logger.error("Failed to match local album %s: %s", musicbrainz_id, e)
        raise HTTPException(status_code=502, detail="Failed to match local album")


@router.get("/albums/{mbid}/tracks", response_model=list[LocalTrackInfo])
async def get_local_album_tracks(
    mbid: str,
    service: LocalFilesService = Depends(get_local_files_service),
) -> list[LocalTrackInfo]:
    try:
        return await service.get_album_tracks_by_id(mbid)
    except ExternalServiceError as e:
        logger.error("Failed to get local album tracks %s: %s", mbid, e)
        raise HTTPException(status_code=502, detail="Failed to get local album tracks")


@router.get("/search", response_model=LocalSearchResponse)
async def search_local(
    current_user: CurrentUserDep,
    q: str = Query(min_length=1),
    service: LocalFilesService = Depends(get_local_files_service),
    preferences: PreferencesService = Depends(get_preferences_service),
) -> LocalSearchResponse:
    try:
        result = await service.search(q)
        return msgspec.structs.replace(
            result,
            albums=_with_download_allowed(
                result.albums,
                preferences.is_library_download_allowed(current_user.role),
            ),
        )
    except ExternalServiceError as e:
        logger.error("Failed to search local files: %s", e)
        raise HTTPException(status_code=502, detail="Failed to search local files")


@router.get("/recent", response_model=list[LocalAlbumSummary])
async def get_local_recent(
    current_user: CurrentUserDep,
    limit: int = Query(default=20, ge=1, le=50),
    service: LocalFilesService = Depends(get_local_files_service),
    preferences: PreferencesService = Depends(get_preferences_service),
) -> list[LocalAlbumSummary]:
    try:
        items = await service.get_recently_added(limit=limit)
        return _with_download_allowed(
            items, preferences.is_library_download_allowed(current_user.role)
        )
    except ExternalServiceError as e:
        logger.error("Failed to get recent local albums: %s", e)
        raise HTTPException(status_code=502, detail="Failed to get recent local albums")


@router.get("/stats", response_model=LocalStorageStats)
async def get_local_stats(
    service: LocalFilesService = Depends(get_local_files_service),
) -> LocalStorageStats:
    return await service.get_storage_stats()


@router.get("/suggestions", response_model=CrateResponse)
async def get_local_suggestions(
    limit: int = Query(default=12, ge=1, le=40),
    decade: int | None = Query(default=None),
    service: LocalFilesService = Depends(get_local_files_service),
) -> CrateResponse:
    try:
        items = await service.get_crate_suggestions(limit=limit, decade=decade)
        return CrateResponse(items=items)
    except ExternalServiceError as e:
        logger.error("Failed to get crate suggestions: %s", e)
        raise HTTPException(status_code=502, detail="Failed to get suggestions")


@router.get("/decades", response_model=DecadesResponse)
async def get_local_decades(
    service: LocalFilesService = Depends(get_local_files_service),
) -> DecadesResponse:
    try:
        return DecadesResponse(items=await service.get_decades())
    except ExternalServiceError as e:
        logger.error("Failed to get decades: %s", e)
        raise HTTPException(status_code=502, detail="Failed to get decades")
