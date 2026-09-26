"""Prowlarr connection admin routes: single-section get/put + test. All admin-only.
``api_key`` is masked on GET and preserved on PUT when the masked sentinel comes
back. Shape follows the closest single-connection precedent (lidarr-import):
``GET/PUT /config`` + ``POST /test``.

Prowlarr multiplexes its own indexers, so one connection is the complete model -
either/or with the native Newznab list: when selected it is the composite
primary (see ``_build_usenet_indexer``), reusing the scorer, retry, and SABnzbd
handoff unchanged.
"""

import logging

from fastapi import APIRouter, Depends

from api.v1.schemas.download import OperationResult, ProwlarrTestResponse
from api.v1.schemas.settings import PROWLARR_API_KEY_MASK, ProwlarrConnectionSettings
from core.dependencies import build_prowlarr_client, get_preferences_service
from core.exceptions import ProwlarrApiError, RateLimitedError
from infrastructure.msgspec_fastapi import MsgSpecBody, MsgSpecRoute
from middleware import CurrentAdminDep

logger = logging.getLogger(__name__)

router = APIRouter(route_class=MsgSpecRoute, prefix="/prowlarr", tags=["prowlarr"])


def _clear_prowlarr_cache() -> None:
    # The orchestrator/service pool the Prowlarr member at construction, so clear
    # the same downstream chain as the indexer routes - else a saved connection
    # doesn't take effect.
    from core.dependencies import (
        get_download_orchestrator,
        get_download_service,
        get_newznab_release_scorer,
        get_prowlarr_indexer,
        get_target_download_orchestrator,
        get_target_download_service,
    )

    for provider in (
        get_prowlarr_indexer,
        get_newznab_release_scorer,
        get_download_orchestrator,
        get_download_service,
        get_target_download_orchestrator,
        get_target_download_service,
    ):
        provider.cache_clear()


@router.get("/config", response_model=ProwlarrConnectionSettings)
async def get_config(
    _: CurrentAdminDep, preferences=Depends(get_preferences_service)
):
    return preferences.get_prowlarr_connection()


@router.put("/config", response_model=OperationResult)
async def update_config(
    _: CurrentAdminDep,
    settings: ProwlarrConnectionSettings = MsgSpecBody(ProwlarrConnectionSettings),
    preferences=Depends(get_preferences_service),
):
    preferences.save_prowlarr_connection(settings)
    _clear_prowlarr_cache()
    return OperationResult(success=True)


@router.post("/test", response_model=ProwlarrTestResponse)
async def test_connection(
    _: CurrentAdminDep,
    settings: ProwlarrConnectionSettings = MsgSpecBody(ProwlarrConnectionSettings),
    preferences=Depends(get_preferences_service),
):
    """Probe the SUBMITTED url/key (so Test works before the first save); a masked
    key resolves to the stored one. Reachable/bad-key/version distinctions are
    carried in the body - never a leaked 5xx (the URL/host is never echoed)."""
    api_key = settings.api_key
    if api_key == PROWLARR_API_KEY_MASK:
        api_key = preferences.get_prowlarr_connection_raw().api_key

    try:
        # Built inside the try so even a construction-time failure (none expected:
        # the client holds the URL verbatim until first request) degrades to the
        # body instead of a 500. Like the indexers /test route, this is an
        # admin-only reachability oracle by design (submitted URL, not stored).
        client = build_prowlarr_client(settings.url, api_key)
        status = await client.system_status()
        indexers = await client.list_indexers()
    except RateLimitedError:
        return ProwlarrTestResponse(
            valid=False,
            message="Prowlarr rate-limited the test. Try again shortly.",
        )
    except ProwlarrApiError as exc:
        if getattr(exc, "auth", False):
            return ProwlarrTestResponse(
                valid=False,
                message="Prowlarr rejected the API key. Check Settings → General → "
                "Security → API Key in Prowlarr.",
            )
        return ProwlarrTestResponse(
            valid=False,
            message="Couldn't reach Prowlarr. Check the URL and that Prowlarr is running.",
        )
    # Count enabled rows only: disabled indexers are never searched, so the raw
    # list length would over-promise (torrent rows still count - Prowlarr
    # searches them; v1 just skips their results).
    count = sum(1 for i in indexers if i.enable)
    version = status.version if status is not None else None
    if version:
        message = f"Connected - Prowlarr v{version} with {count} enabled indexer(s)"
    else:
        message = f"Connected - {count} enabled indexer(s)"
    return ProwlarrTestResponse(
        valid=True, version=version, message=message, indexer_count=count
    )
