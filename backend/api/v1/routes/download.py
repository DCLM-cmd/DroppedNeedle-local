import logging

from fastapi import APIRouter, Depends, HTTPException
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from api.v1.schemas.local_files import DownloadAccessResponse
from core.dependencies import get_local_files_service, get_preferences_service
from core.exceptions import (
    ExternalServiceError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from infrastructure.msgspec_fastapi import MsgSpecRoute
from middleware import CurrentUserDep
from services.local_files_service import LocalFilesService
from services.preferences_service import PreferencesService

logger = logging.getLogger(__name__)

router = APIRouter(route_class=MsgSpecRoute, prefix="/download", tags=["download"])

_FORBIDDEN_DOWNLOAD_MESSAGE = "Library downloads are restricted by the administrator"


def _require_library_download(preferences: PreferencesService, role: str) -> None:
    if not preferences.is_library_download_allowed(role):
        raise PermissionDeniedError(_FORBIDDEN_DOWNLOAD_MESSAGE)


@router.get("/access", response_model=DownloadAccessResponse)
async def download_access(
    current_user: CurrentUserDep,
    preferences: PreferencesService = Depends(get_preferences_service),
) -> DownloadAccessResponse:
    """Viewer capability for card/menu download items (no album in hand)."""
    return DownloadAccessResponse(
        allowed=preferences.is_library_download_allowed(current_user.role)
    )


@router.get("/local/track/{track_id}")
async def download_track(
    track_id: str,
    current_user: CurrentUserDep,
    local_service: LocalFilesService = Depends(get_local_files_service),
    preferences: PreferencesService = Depends(get_preferences_service),
) -> FileResponse:
    _require_library_download(preferences, current_user.role)
    try:
        file_path, filename, media_type = await local_service.get_download_track(track_id)
        return FileResponse(
            path=file_path,
            filename=filename,
            media_type=media_type,
        )
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail="Track file not found")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Track file not found on disk")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Access denied: path is outside the music directory")
    except ExternalServiceError as e:
        logger.error("Download error for track %s: %s", track_id, e)
        raise HTTPException(status_code=502, detail="Failed to retrieve track file")
    except OSError as e:
        logger.error("OS error downloading track %s: %s", track_id, e)
        raise HTTPException(status_code=500, detail="Failed to read track file")


@router.get("/local/album/{album_id}")
async def download_album(
    album_id: str,
    current_user: CurrentUserDep,
    local_service: LocalFilesService = Depends(get_local_files_service),
    preferences: PreferencesService = Depends(get_preferences_service),
) -> FileResponse:
    _require_library_download(preferences, current_user.role)
    try:
        zip_path, zip_filename = await local_service.create_album_zip(album_id)
        return FileResponse(
            path=zip_path,
            filename=zip_filename,
            media_type="application/zip",
            headers={"Content-Encoding": "identity"},
            background=BackgroundTask(zip_path.unlink, missing_ok=True),
        )
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail="Album or track files not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Access denied: path is outside the music directory")
    except ExternalServiceError as e:
        logger.error("Download error for album %s: %s", album_id, e)
        raise HTTPException(status_code=502, detail="Failed to retrieve album data")
    except OSError as e:
        logger.error("OS error creating album ZIP %s: %s", album_id, e)
        raise HTTPException(status_code=500, detail="Failed to create album archive")


@router.get("/local/album/mbid/{mbid}")
async def download_album_by_mbid(
    mbid: str,
    current_user: CurrentUserDep,
    local_service: LocalFilesService = Depends(get_local_files_service),
    preferences: PreferencesService = Depends(get_preferences_service),
) -> FileResponse:
    _require_library_download(preferences, current_user.role)
    try:
        zip_path, zip_filename = await local_service.create_album_zip_by_mbid(mbid)
        return FileResponse(
            path=zip_path,
            filename=zip_filename,
            media_type="application/zip",
            headers={"Content-Encoding": "identity"},
            background=BackgroundTask(zip_path.unlink, missing_ok=True),
        )
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail="Album or track files not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Access denied: path is outside the music directory")
    except ExternalServiceError as e:
        logger.error("Download error for album MBID %s: %s", mbid, e)
        raise HTTPException(status_code=502, detail="Failed to retrieve album data")
    except OSError as e:
        logger.error("OS error creating album ZIP for MBID %s: %s", mbid, e)
        raise HTTPException(status_code=500, detail="Failed to create album archive")
