import logging
import math
import time as _time
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

from api.v1.schemas.requests_page import (
    ActiveRequestItem,
    ActiveRequestsResponse,
    CancelRequestResponse,
    RequestHistoryItem,
    RequestHistoryResponse,
    RetryRequestResponse,
)
from core.exceptions import PermissionDeniedError, ValidationError
from infrastructure.cover_urls import prefer_release_group_cover_url
from infrastructure.queue.priority_queue import RequestPriority
from infrastructure.persistence.request_history import (
    RequesterCancelDecision,
    RequestHistoryRecord,
    RequestHistoryStore,
)
from repositories.protocols import LibraryRepositoryProtocol

if TYPE_CHECKING:
    from services.native.download_service import DownloadService

logger = logging.getLogger(__name__)

_CANCELLABLE_STATUSES = {"pending", "downloading", "queued"}
_RETRYABLE_STATUSES = {"failed", "cancelled", "incomplete"}
_CLEARABLE_STATUSES = {"imported", "incomplete", "failed", "cancelled"}
_RETRYABLE_STATUS_ORDER = ("failed", "cancelled", "incomplete")

_LIBRARY_MBIDS_CACHE_TTL = 30


def _generation_of(value: object | None) -> int | None:
    generation = getattr(value, "generation", None)
    return generation if isinstance(generation, int) and not isinstance(generation, bool) else None


def _mutation_won(value: object) -> bool:
    return value is not False


class RequestsPageService:
    def __init__(
        self,
        library_repo: LibraryRepositoryProtocol,
        request_history: RequestHistoryStore,
        library_mbids_fn: Callable[..., Coroutine[Any, Any, set[str]]],
        on_import_callback: Callable[[RequestHistoryRecord], Coroutine[Any, Any, None]]
        | None = None,
        get_download_service: Optional[Callable[[], "DownloadService"]] = None,
        download_store=None,  # DownloadStore | None - native reconciler source of truth
        acquisition=None,  # noqa: ANN001 - AcquisitionDispatcher | None
    ):
        self._library_repo = library_repo
        self._request_history = request_history
        self._library_mbids_fn = library_mbids_fn
        self._on_import_callback = on_import_callback
        # Resolve the DownloadService fresh at every dispatch: a settings save rebuilds
        # its singleton, so capturing an instance would ignore a saved quality change
        # until restart. Kept for cancel_task; new dispatches go via the dispatcher.
        self._get_download_service = get_download_service
        self._download_store = download_store
        # picks the download client or Free Music per approve/retry dispatch
        self._acquisition = acquisition
        self._library_mbids_cache: set[str] | None = None
        self._library_mbids_cache_time: float = 0

    async def get_active_requests(
        self,
        user_id: str | None = None,
        request_kind: str | None = None,
    ) -> ActiveRequestsResponse:
        if user_id is not None:
            active_records = (
                await self._request_history.async_get_active_requests_for_user(
                    user_id, request_kind=request_kind
                )
            )
        else:
            active_records = await self._request_history.async_get_active_requests(
                request_kind=request_kind
            )
        if not active_records:
            return ActiveRequestsResponse(items=[], count=0)

        library_mbids = await self._fetch_library_mbids()

        items: list[ActiveRequestItem] = []
        for record in active_records:
            # awaiting_approval records have no download task yet
            if record.status == "awaiting_approval":
                items.append(self._build_pending_item(record))
                continue

            completed = await self._check_if_completed(record, library_mbids)
            if completed:
                continue
            items.append(self._build_pending_item(record))

        return ActiveRequestsResponse(items=items, count=len(items))

    async def get_request_history(
        self,
        page: int = 1,
        page_size: int = 20,
        status_filter: Optional[str] = None,
        sort: Optional[str] = None,
        user_id: Optional[str] = None,
        request_kind: str | None = None,
    ) -> RequestHistoryResponse:
        if user_id is not None:
            records, total = await self._request_history.async_get_history_for_user(
                user_id=user_id,
                page=page,
                page_size=page_size,
                status_filter=status_filter,
                sort=sort,
                request_kind=request_kind,
            )
        else:
            records, total = await self._request_history.async_get_history(
                page=page,
                page_size=page_size,
                status_filter=status_filter,
                sort=sort,
                request_kind=request_kind,
            )

        library_mbids = await self._fetch_library_mbids()

        task_ids = [r.download_task_id for r in records if r.download_task_id]
        reimportable: set[str] = (
            await self._download_store.get_reimportable_task_ids(task_ids)
            if self._download_store is not None
            else set()
        )

        items = [
            RequestHistoryItem(
                musicbrainz_id=r.musicbrainz_id,
                artist_name=r.artist_name,
                album_title=r.album_title,
                artist_mbid=r.artist_mbid,
                year=r.year,
                cover_url=r.cover_url,
                requested_at=datetime.fromisoformat(r.requested_at),
                completed_at=(
                    datetime.fromisoformat(r.completed_at) if r.completed_at else None
                ),
                status=r.status,
                in_library=r.musicbrainz_id.lower() in library_mbids,
                user_id=r.user_id,
                requested_by_name=r.requested_by_name,
                reviewed_by_name=r.reviewed_by_name,
                reviewed_at=(
                    datetime.fromisoformat(r.reviewed_at) if r.reviewed_at else None
                ),
                download_task_id=r.download_task_id,
                can_reimport=r.status == "failed"
                and r.download_task_id in reimportable,
                request_kind=getattr(r, "request_kind", "album"),
                track_title=r.track_title,
                duration_seconds=r.duration_seconds,
                track_release_group_mbid=r.track_release_group_mbid,
            )
            for r in records
        ]

        total_pages = max(1, math.ceil(total / page_size))

        return RequestHistoryResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    async def get_pending_approvals(
        self, request_kind: str | None = None
    ) -> ActiveRequestsResponse:
        records = await self._request_history.async_get_pending_approvals(
            request_kind=request_kind
        )
        items = [self._build_pending_item(r) for r in records]
        return ActiveRequestsResponse(items=items, count=len(items))

    async def get_pending_approval_count(self, request_kind: str | None = None) -> int:
        return await self._request_history.async_get_pending_approval_count(
            request_kind=request_kind
        )

    async def approve_request(
        self,
        musicbrainz_id: str,
        reviewer_id: str,
        reviewer_name: str | None = None,
        request_kind: str = "album",
    ) -> CancelRequestResponse:
        record = await self._request_history.async_get_record(
            musicbrainz_id, request_kind=request_kind
        )
        if not record:
            return CancelRequestResponse(success=False, message="Request not found")
        if record.status != "awaiting_approval":
            return CancelRequestResponse(
                success=False,
                message=f"Request is not awaiting approval (status: {record.status})",
            )

        expected_generation = _generation_of(record)
        claim_kwargs: dict[str, object] = {
            "reviewer_id": reviewer_id,
            "reviewer_name": reviewer_name,
            "request_kind": request_kind,
        }
        if expected_generation is not None:
            claim_kwargs["expected_generation"] = expected_generation
        claim = await self._request_history.async_claim_approval(
            musicbrainz_id, **claim_kwargs
        )
        if claim is None:
            current = await self._request_history.async_get_record(
                musicbrainz_id, request_kind=request_kind
            )
            status = getattr(current, "status", "changed")
            return CancelRequestResponse(
                success=False,
                message=f"Request is not awaiting approval (status: {status})",
            )
        generation = _generation_of(claim) or expected_generation
        task_id: str | None = None
        try:
            if self._acquisition is not None:
                task_id = await self._dispatch_record(
                    record,
                    origin="user",
                    fallback_user_id=reviewer_id,
                )
        except ValidationError as error:
            await self._restore_request_status(
                musicbrainz_id,
                "awaiting_approval",
                request_kind=request_kind,
                expected_generation=generation,
            )
            return CancelRequestResponse(success=False, message=str(error))
        except Exception:  # noqa: BLE001 - keep provider details out of the response
            logger.exception("Failed to dispatch approved request %s", musicbrainz_id)
            await self._restore_request_status(
                musicbrainz_id,
                "failed",
                request_kind=request_kind,
                completed_at=datetime.now(timezone.utc).isoformat(),
                expected_generation=generation,
            )
            return CancelRequestResponse(
                success=False,
                message=f"Approved but failed to start: {self._record_title(record)}",
            )

        from services.native.download_service import ALREADY_IN_LIBRARY

        if task_id == ALREADY_IN_LIBRARY:
            kwargs: dict[str, object] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "request_kind": request_kind,
            }
            if generation is not None:
                kwargs["expected_generation"] = generation
            await self._request_history.async_update_status(
                musicbrainz_id, "imported", **kwargs
            )
        elif task_id is not None:
            kwargs = {"request_kind": request_kind}
            if generation is not None:
                kwargs["expected_generation"] = generation
            try:
                linked = await self._request_history.async_update_download_task_id(
                    musicbrainz_id, task_id, **kwargs
                )
                if not _mutation_won(linked):
                    await self._cancel_orphan_task(task_id, record.user_id or reviewer_id)
                    return CancelRequestResponse(
                        success=False, message="Approved request became stale"
                    )
            except Exception:  # noqa: BLE001 - provider details stay out of responses
                logger.exception("Failed to link approved request %s", musicbrainz_id)
                await self._cancel_orphan_task(task_id, record.user_id or reviewer_id)
                await self._restore_request_status(
                    musicbrainz_id,
                    "failed",
                    request_kind=request_kind,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    expected_generation=generation,
                )
                return CancelRequestResponse(
                    success=False,
                    message=f"Approved but failed to start: {self._record_title(record)}",
                )
        return CancelRequestResponse(
            success=True, message=f"Approved: {self._record_title(record)}"
        )

    async def reject_request(
        self,
        musicbrainz_id: str,
        reviewer_id: str,
        reviewer_name: str | None = None,
        request_kind: str = "album",
    ) -> CancelRequestResponse:
        record = await self._request_history.async_get_record(
            musicbrainz_id, request_kind=request_kind
        )
        if not record:
            return CancelRequestResponse(success=False, message="Request not found")
        if record.status != "awaiting_approval":
            return CancelRequestResponse(
                success=False,
                message=f"Request is not awaiting approval (status: {record.status})",
            )
        expected_generation = _generation_of(record)
        claim_kwargs: dict[str, object] = {
            "reviewer_id": reviewer_id,
            "reviewer_name": reviewer_name,
            "request_kind": request_kind,
            "target_status": "rejected",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        if expected_generation is not None:
            claim_kwargs["expected_generation"] = expected_generation
        claim = await self._request_history.async_claim_approval(
            musicbrainz_id, **claim_kwargs
        )
        if claim is None:
            current = await self._request_history.async_get_record(
                musicbrainz_id, request_kind=request_kind
            )
            status = getattr(current, "status", "changed")
            return CancelRequestResponse(
                success=False,
                message=f"Request is not awaiting approval (status: {status})",
            )
        return CancelRequestResponse(
            success=True, message=f"Rejected: {self._record_title(record)}"
        )

    async def cancel_request(
        self,
        musicbrainz_id: str,
        *,
        user_id: str,
        user_role: str,
        request_kind: str = "album",
    ) -> CancelRequestResponse:
        record = await self._request_history.async_get_record(
            musicbrainz_id, request_kind=request_kind
        )
        if not record:
            return CancelRequestResponse(success=False, message="Request not found")

        is_admin = user_role == "admin"
        prior_status = record.status
        generation = _generation_of(record)
        task_owner = record.user_id or user_id
        decision: RequesterCancelDecision | None = None
        if not is_admin:
            decision = await self._request_history.async_prepare_requester_cancel(
                user_id, musicbrainz_id, request_kind=request_kind
            )
            prior_status = decision.prior_status or prior_status
            generation = _generation_of(decision) or generation
            if decision.action == "denied":
                if prior_status not in (*_CANCELLABLE_STATUSES, "awaiting_approval"):
                    return CancelRequestResponse(
                        success=False,
                        message=f"Cannot cancel request with status '{prior_status}'",
                    )
                raise PermissionDeniedError("Cannot cancel another user's request")
            if decision.action == "detached":
                return CancelRequestResponse(
                    success=True,
                    message=(
                        "Removed from your requests. The shared server request "
                        "continues for another listener."
                    ),
                )
            if decision.action == "cancelled":
                return CancelRequestResponse(
                    success=True,
                    message=f"Cancelled request for {self._record_title(record)}",
                )
            if decision.action != "cancel_task":
                return CancelRequestResponse(
                    success=False,
                    message=f"Cannot cancel request with status '{prior_status}'",
                )
            current = await self._request_history.async_get_record(
                musicbrainz_id, request_kind=request_kind
            )
            if current is not None:
                record = current
                task_owner = record.user_id or user_id
        elif prior_status == "awaiting_approval":
            kwargs: dict[str, object] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "request_kind": request_kind,
            }
            if generation is not None:
                kwargs["expected_generation"] = generation
            changed = await self._request_history.async_update_status(
                musicbrainz_id, "cancelled", **kwargs
            )
            if not _mutation_won(changed):
                return CancelRequestResponse(
                    success=False, message="Request changed while cancelling"
                )
            await self._request_history.async_update_dispatch_authorized(
                musicbrainz_id,
                False,
                request_kind=request_kind,
                **(
                    {"expected_generation": generation}
                    if generation is not None
                    else {}
                ),
            )
            return CancelRequestResponse(
                success=True,
                message=f"Cancelled request for {self._record_title(record)}",
            )

        if prior_status not in _CANCELLABLE_STATUSES:
            return CancelRequestResponse(
                success=False,
                message=f"Cannot cancel request with status '{prior_status}'",
            )

        download_service = None
        try:
            if self._get_download_service is not None:
                download_service = self._get_download_service()
            if record.download_task_id and download_service is not None:
                await download_service.cancel_task(
                    record.download_task_id,
                    user_id if is_admin else task_owner,
                    "admin" if is_admin else "user",
                )
            kwargs = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "request_kind": request_kind,
            }
            if generation is not None:
                kwargs["expected_generation"] = generation
            changed = await self._request_history.async_update_status(
                musicbrainz_id, "cancelled", **kwargs
            )
            if not _mutation_won(changed):
                return CancelRequestResponse(
                    success=False, message="Request changed while cancelling"
                )
        except Exception:  # noqa: BLE001 - provider details are never user-facing
            logger.exception("Failed to cancel request %s", musicbrainz_id)
            if decision is not None and decision.prior_status is not None:
                try:
                    await self._request_history.async_restore_request_status(
                        musicbrainz_id,
                        decision.prior_status,
                        request_kind=request_kind,
                        expected_status="cancelling",
                        expected_generation=generation,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to restore request %s", musicbrainz_id)
            return CancelRequestResponse(
                success=False, message="Unable to cancel request"
            )

        return CancelRequestResponse(
            success=True,
            message=f"Cancelled download of {self._record_title(record)}",
        )

    async def retry_request(
        self,
        musicbrainz_id: str,
        *,
        user_id: str,
        user_role: str,
        request_kind: str = "album",
    ) -> RetryRequestResponse:
        record = await self._request_history.async_get_record(
            musicbrainz_id, request_kind=request_kind
        )
        if not record:
            return RetryRequestResponse(success=False, message="Request not found")
        if record.status not in _RETRYABLE_STATUSES:
            return RetryRequestResponse(
                success=False,
                message=f"Cannot retry request with status '{record.status}'",
            )

        if self._acquisition is None:
            return RetryRequestResponse(success=False, message="Downloads unavailable")

        # The transaction verifies requester membership and claims exactly one
        # retryable generation. An ordinary user may dispatch only a generation
        # that already carries approval provenance.
        can_dispatch = bool(record.dispatch_authorized) or user_role in (
            "admin",
            "trusted",
        )
        target_status = "pending" if can_dispatch else "awaiting_approval"
        claim_kwargs: dict[str, object] = {
            "user_id": user_id,
            "request_kind": request_kind,
            "target_status": target_status,
            "dispatch_authorized": can_dispatch,
            "require_membership": user_role != "admin",
        }
        expected_generation = _generation_of(record)
        if expected_generation is not None:
            claim_kwargs["expected_generation"] = expected_generation
        claim = await self._request_history.async_claim_retry(
            musicbrainz_id, **claim_kwargs
        )
        if claim is None:
            current = await self._request_history.async_get_record(
                musicbrainz_id, request_kind=request_kind
            )
            current_status = getattr(current, "status", None)
            if (
                user_role != "admin"
                and current_status in _RETRYABLE_STATUSES
                and not await self._request_history.async_is_requester(
                    user_id, musicbrainz_id, request_kind=request_kind
                )
            ):
                raise PermissionDeniedError("Cannot retry another user's request")
            return RetryRequestResponse(
                success=False,
                message=(
                    "Request not found"
                    if current is None
                    else f"Cannot retry request with status '{current_status}'"
                ),
            )

        generation = _generation_of(claim) or expected_generation
        if target_status == "awaiting_approval":
            return RetryRequestResponse(
                success=True,
                message="Retry submitted, awaiting admin approval",
            )

        task_id: str | None = None
        try:
            task_id = await self._dispatch_record(
                record,
                origin="retry",
                fallback_user_id=user_id,
            )
        except ValidationError as error:
            await self._restore_request_status(
                musicbrainz_id,
                record.status,
                request_kind=request_kind,
                expected_generation=generation,
            )
            return RetryRequestResponse(success=False, message=str(error))
        except Exception:  # noqa: BLE001 - keep provider details out of the response
            logger.exception("Retry dispatch failed for request %s", musicbrainz_id)
            await self._restore_request_status(
                musicbrainz_id,
                record.status,
                request_kind=request_kind,
                expected_generation=generation,
            )
            return RetryRequestResponse(
                success=False, message="Retry failed to start download"
            )

        from services.native.download_service import ALREADY_IN_LIBRARY

        if task_id == ALREADY_IN_LIBRARY:
            kwargs: dict[str, object] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "request_kind": request_kind,
            }
            if generation is not None:
                kwargs["expected_generation"] = generation
            await self._request_history.async_update_status(
                musicbrainz_id, "imported", **kwargs
            )
        else:
            kwargs = {"request_kind": request_kind}
            if generation is not None:
                kwargs["expected_generation"] = generation
            try:
                linked = await self._request_history.async_update_download_task_id(
                    musicbrainz_id, task_id, **kwargs
                )
                if not _mutation_won(linked):
                    await self._cancel_orphan_task(task_id, record.user_id or user_id)
                    return RetryRequestResponse(
                        success=False, message="Retry request became stale"
                    )
            except Exception:  # noqa: BLE001 - provider details stay out of responses
                logger.exception("Failed to link retried request %s", musicbrainz_id)
                await self._cancel_orphan_task(task_id, record.user_id or user_id)
                await self._restore_request_status(
                    musicbrainz_id,
                    "failed",
                    request_kind=request_kind,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    expected_generation=generation,
                )
                return RetryRequestResponse(
                    success=False, message="Retry failed to start download"
                )
        return RetryRequestResponse(
            success=True, message=f"Re-requested {self._record_title(record)}"
        )

    async def clear_history_item(
        self,
        musicbrainz_id: str,
        *,
        user_id: str,
        user_role: str,
        request_kind: str = "album",
    ) -> bool:
        record = await self._request_history.async_get_record(
            musicbrainz_id, request_kind=request_kind
        )
        if not record:
            return False
        # Check ownership before clearability so a non-owner gets 403 rather than a
        # misleading success/false for another listener's row.
        if user_role != "admin" and not await self._request_history.async_is_requester(
            user_id, musicbrainz_id, request_kind=request_kind
        ):
            raise PermissionDeniedError("Cannot clear another user's request")
        if record.status not in _CLEARABLE_STATUSES:
            return False
        if user_role == "admin":
            return await self._request_history.async_delete_record(
                musicbrainz_id, request_kind=request_kind
            )
        return await self._request_history.async_dismiss_record(
            user_id, musicbrainz_id, request_kind=request_kind
        )

    async def get_active_count(
        self, user_id: str | None = None, request_kind: str | None = None
    ) -> int:
        if user_id is not None:
            return await self._request_history.async_get_active_count_for_user(
                user_id, request_kind=request_kind
            )
        return await self._request_history.async_get_active_count(
            request_kind=request_kind
        )

    # download_task.status -> request_history.status
    _TASK_TO_REQUEST_STATUS = {
        "downloading": "downloading",
        "processing": "downloading",
        "completed": "imported",
        "partial": "incomplete",
        "failed": "failed",
        "cancelled": "cancelled",
    }

    async def sync_request_statuses(self) -> None:
        """Reconcile active request rows with their native download tasks."""
        active_records = await self._request_history.async_get_active_requests()
        if not active_records:
            return
        library_mbids = await self._fetch_library_mbids()
        for record in active_records:
            try:
                await self._reconcile_request(record, library_mbids)
            except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
                logger.warning("Failed to reconcile request %s", record.musicbrainz_id)

    async def _reconcile_request(
        self, record: RequestHistoryRecord, library_mbids: set[str]
    ) -> None:
        request_kind = getattr(record, "request_kind", "album")
        task = await self._find_download_task(record)
        if task is not None:
            mapped = self._TASK_TO_REQUEST_STATUS.get(task.status)
            if mapped and mapped != record.status:
                completed_at = (
                    datetime.now(timezone.utc).isoformat()
                    if mapped in ("imported", "failed", "cancelled")
                    else None
                )
                kwargs: dict[str, object] = {
                    "completed_at": completed_at,
                    "request_kind": request_kind,
                }
                generation = _generation_of(record)
                if generation is not None:
                    kwargs["expected_generation"] = generation
                changed = await self._request_history.async_update_status(
                    record.musicbrainz_id,
                    mapped,
                    **kwargs,
                )
                if _mutation_won(changed) and mapped == "imported":
                    await self._notify_import(record)
            return
        # Only album rows can be reconciled from album-level library presence.
        # Exact tracks require their linked task ID; a recording MBID is not an
        # album/library key.
        await self._check_if_completed(record, library_mbids)

    async def _find_download_task(self, record: RequestHistoryRecord):
        request_kind = getattr(record, "request_kind", "album")
        if self._download_store is None:
            return None
        # Task IDs are globally unique across album and track requests. Do not
        # constrain this lookup by request kind.
        if record.download_task_id:
            task = await self._download_store.get_task(record.download_task_id)
            if task is not None:
                return task
        if request_kind == "track":
            return None
        return await self._download_store.get_active_task_for_album_any_user(
            record.musicbrainz_id
        )

    async def _fetch_library_mbids(self) -> set[str]:
        now = _time.monotonic()
        if (
            self._library_mbids_cache is not None
            and (now - self._library_mbids_cache_time) < _LIBRARY_MBIDS_CACHE_TTL
        ):
            return self._library_mbids_cache
        try:
            result = await self._library_mbids_fn()
            self._library_mbids_cache = result
            self._library_mbids_cache_time = now
            return result
        except Exception:  # noqa: BLE001
            if self._library_mbids_cache is not None:
                return self._library_mbids_cache
            return set()

    async def _cancel_orphan_task(self, task_id: str | None, user_id: str) -> None:
        if not task_id:
            return
        try:
            if self._get_download_service is not None:
                await self._get_download_service().cancel_task(task_id, user_id, "user")
        except Exception:  # noqa: BLE001 - stale request rows remain untouched
            logger.warning("Failed to cancel orphan download task %s", task_id)

    async def _restore_request_status(
        self,
        musicbrainz_id: str,
        status: str,
        *,
        request_kind: str,
        completed_at: str | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        try:
            kwargs: dict[str, object] = {
                "completed_at": completed_at,
                "request_kind": request_kind,
            }
            if expected_generation is not None:
                kwargs["expected_generation"] = expected_generation
            result = await self._request_history.async_update_status(
                musicbrainz_id, status, **kwargs
            )
            return _mutation_won(result)
        except Exception:  # noqa: BLE001 - restoration is best effort
            logger.exception("Failed to restore request %s status", musicbrainz_id)
            return False

    async def _dispatch_record(
        self,
        record: RequestHistoryRecord,
        *,
        origin: str,
        fallback_user_id: str = "",
    ) -> str:
        """Dispatch one stored request without widening exact-track identity."""
        request_kind = getattr(record, "request_kind", "album")
        # Shared retries retain the immutable primary owner. Only ownerless
        # legacy rows use the acting user as a fallback.
        user_id = record.user_id or fallback_user_id
        if request_kind == "track":
            if not record.track_title:
                raise ValidationError("Exact-track request is missing its track title")
            return await self._acquisition.request_track(
                user_id=user_id,
                recording_mbid=record.musicbrainz_id,
                artist_name=record.artist_name or "Unknown",
                track_title=record.track_title,
                album_title=record.album_title,
                duration_seconds=record.duration_seconds,
                release_group_mbid=record.track_release_group_mbid,
                artist_mbid=record.artist_mbid,
                origin=origin,
                release_mbid=record.release_mbid,
            )
        return await self._acquisition.request_album(
            user_id=user_id,
            release_group_mbid=record.musicbrainz_id,
            artist_name=record.artist_name or "Unknown",
            album_title=record.album_title or "Unknown",
            year=record.year,
            artist_mbid=record.artist_mbid,
            origin=origin,
            release_mbid=record.release_mbid,
            track_count_priority=RequestPriority.USER_INITIATED,
        )

    @staticmethod
    def _record_title(record: RequestHistoryRecord) -> str:
        request_kind = getattr(record, "request_kind", "album")
        if request_kind == "track" and record.track_title:
            return record.track_title
        return record.album_title

    @staticmethod
    def _build_pending_item(record: RequestHistoryRecord) -> ActiveRequestItem:
        request_kind = getattr(record, "request_kind", "album")
        cover_mbid = (
            record.track_release_group_mbid
            if request_kind == "track" and record.track_release_group_mbid
            else record.musicbrainz_id
        )
        return ActiveRequestItem(
            musicbrainz_id=record.musicbrainz_id,
            artist_name=record.artist_name,
            album_title=record.album_title,
            artist_mbid=record.artist_mbid,
            year=record.year,
            cover_url=prefer_release_group_cover_url(
                cover_mbid,
                record.cover_url,
                size=500,
            ),
            requested_at=datetime.fromisoformat(record.requested_at),
            status=record.status,
            progress=None,
            eta=None,
            size=None,
            size_remaining=None,
            download_status=None,
            download_state=None,
            status_messages=None,
            library_queue_id=None,
            user_id=record.user_id,
            requested_by_name=record.requested_by_name,
            request_kind=request_kind,
            track_title=record.track_title,
            duration_seconds=record.duration_seconds,
            track_release_group_mbid=record.track_release_group_mbid,
        )

    async def _check_if_completed(
        self,
        record: RequestHistoryRecord,
        library_mbids: set[str],
    ) -> bool:
        request_kind = getattr(record, "request_kind", "album")
        if request_kind == "track":
            task = await self._find_download_task(record)
            if task is None:
                return False
            mapped = self._TASK_TO_REQUEST_STATUS.get(task.status)
            if mapped and mapped != record.status:
                completed_at = (
                    datetime.now(timezone.utc).isoformat()
                    if mapped in ("imported", "failed", "cancelled")
                    else None
                )
                kwargs: dict[str, object] = {
                    "completed_at": completed_at,
                    "request_kind": request_kind,
                }
                generation = _generation_of(record)
                if generation is not None:
                    kwargs["expected_generation"] = generation
                changed = await self._request_history.async_update_status(
                    record.musicbrainz_id,
                    mapped,
                    **kwargs,
                )
                if _mutation_won(changed) and mapped == "imported":
                    await self._notify_import(record)
            return mapped in ("imported", "incomplete", "failed", "cancelled")

        now_iso = datetime.now(timezone.utc).isoformat()
        if record.musicbrainz_id.lower() in library_mbids:
            kwargs = {
                "completed_at": now_iso,
                "request_kind": request_kind,
            }
            generation = _generation_of(record)
            if generation is not None:
                kwargs["expected_generation"] = generation
            changed = await self._request_history.async_update_status(
                record.musicbrainz_id,
                "imported",
                **kwargs,
            )
            if _mutation_won(changed):
                await self._notify_import(record)
            return True
        return False

    async def _notify_import(self, record: RequestHistoryRecord) -> None:
        self._library_mbids_cache = None
        self._library_mbids_cache_time = 0
        if self._on_import_callback:
            try:
                await self._on_import_callback(record)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Import callback failed for %s: %s", record.musicbrainz_id, e
                )
