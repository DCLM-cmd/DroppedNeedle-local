"""task-049: approve dispatches the native pipeline via DownloadService.request_album
and links download_task_id, replacing the retired request_queue hop."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.exceptions import PermissionDeniedError
from infrastructure.persistence.request_history import (
    RequestBeginResult,
    RequesterCancelDecision,
    RequestHistoryRecord,
)
from services.native.acquisition.status import DownloadStatus
from services.native.download_orchestrator import DownloadOrchestrator
from infrastructure.queue.priority_queue import RequestPriority
from services.requests_page_service import RequestsPageService
from tests.helpers import make_builtin_dispatcher


def _make(
    record_status="awaiting_approval",
    *,
    request_album_result="task-9",
    request_track_result="track-task-9",
    download_task_id=None,
    request_kind="album",
    dispatch_authorized=True,
    record_user_id="u1",
    record_generation=1,
):
    request_history = MagicMock()
    request_history.async_get_record = AsyncMock(
        return_value=SimpleNamespace(
            status=record_status,
            album_title="OK Computer",
            artist_name="Radiohead",
            artist_mbid="artist-mbid-1",
            year=1997,
            user_id=record_user_id,
            download_task_id=download_task_id,
            release_mbid="release-edition",
            musicbrainz_id="mbid-1",
            request_kind=request_kind,
            track_title="Airbag" if request_kind == "track" else None,
            duration_seconds=287 if request_kind == "track" else None,
            track_release_group_mbid=(
                "release-group-1" if request_kind == "track" else None
            ),
            dispatch_authorized=dispatch_authorized,
            generation=record_generation,
        )
    )
    request_history.async_record_review = AsyncMock()
    request_history.async_claim_approval = AsyncMock(
        return_value=RequestBeginResult(
            musicbrainz_id="mbid-1",
            request_kind=request_kind,
            generation=record_generation,
        )
    )
    request_history.async_claim_retry = AsyncMock(
        return_value=RequestBeginResult(
            musicbrainz_id="mbid-1",
            request_kind=request_kind,
            generation=record_generation,
        )
    )
    request_history.async_update_dispatch_authorized = AsyncMock(return_value=True)
    request_history.async_update_download_task_id = AsyncMock(return_value=True)
    request_history.async_update_status = AsyncMock(return_value=True)
    request_history.async_restore_request_status = AsyncMock(return_value=True)
    request_history.async_is_requester = AsyncMock(return_value=True)
    request_history.async_requester_count = AsyncMock(return_value=1)
    request_history.async_remove_requester = AsyncMock(return_value=True)
    request_history.async_prepare_requester_cancel = AsyncMock(
        return_value=RequesterCancelDecision(
            "cancel_task", record_status, record_generation
        )
    )

    download_service = MagicMock()
    download_service.request_album = AsyncMock(return_value=request_album_result)
    download_service.request_track = AsyncMock(return_value=request_track_result)
    download_service.cancel_task = AsyncMock()

    async def _mbids() -> set[str]:
        return set()

    service = RequestsPageService(
        library_repo=MagicMock(),
        request_history=request_history,
        library_mbids_fn=_mbids,
        get_download_service=lambda: download_service,
        acquisition=make_builtin_dispatcher(lambda: download_service),
    )
    return service, request_history, download_service


@pytest.mark.asyncio
async def test_approve_dispatches_download_and_links_task():
    service, history, download_service = _make()
    dispatch = service._acquisition.request_album
    service._acquisition.request_album = AsyncMock(wraps=dispatch)

    resp = await service.approve_request("mbid-1", "admin-id", "Admin")

    assert resp.success is True
    download_service.request_album.assert_awaited_once()
    assert (
        download_service.request_album.await_args.kwargs["release_mbid"]
        == "release-edition"
    )
    assert (
        service._acquisition.request_album.await_args.kwargs["track_count_priority"]
        is RequestPriority.USER_INITIATED
    )
    history.async_update_download_task_id.assert_awaited_once_with(
        "mbid-1", "task-9", request_kind="album", expected_generation=1
    )


@pytest.mark.asyncio
async def test_approve_already_in_library_not_linked():
    service, history, download_service = _make(
        request_album_result="already_in_library"
    )

    resp = await service.approve_request("mbid-1", "admin-id", "Admin")

    assert resp.success is True
    download_service.request_album.assert_awaited_once()
    history.async_update_download_task_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_rejects_non_awaiting_record():
    service, _history, download_service = _make(record_status="pending")

    resp = await service.approve_request("mbid-1", "admin-id", "Admin")

    assert resp.success is False
    download_service.request_album.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_request_cancels_linked_native_task():
    service, history, download_service = _make(
        record_status="downloading", download_task_id="task-9"
    )

    resp = await service.cancel_request("mbid-1", user_id="u1", user_role="user")

    assert resp.success is True
    history.async_update_status.assert_awaited_once()
    assert history.async_update_status.await_args.kwargs["expected_generation"] == 1


@pytest.mark.asyncio
async def test_retry_request_redispatches_native_and_links():
    service, history, download_service = _make(
        record_status="failed", download_task_id="old-task"
    )
    dispatch = service._acquisition.request_album
    service._acquisition.request_album = AsyncMock(wraps=dispatch)

    resp = await service.retry_request("mbid-1", user_id="u1", user_role="user")

    assert resp.success is True
    download_service.request_album.assert_awaited_once()
    assert (
        download_service.request_album.await_args.kwargs["release_mbid"]
        == "release-edition"
    )
    assert (
        service._acquisition.request_album.await_args.kwargs["track_count_priority"]
        is RequestPriority.USER_INITIATED
    )
    history.async_update_download_task_id.assert_awaited_once_with(
        "mbid-1", "task-9", request_kind="album", expected_generation=1
    )


@pytest.mark.asyncio
async def test_sync_reconciles_request_from_native_download_task():
    """The rewritten reconciler reads the native download task (not the dead Lidarr
    queue): a failed task flips its still-active request to 'failed'."""
    record = SimpleNamespace(
        musicbrainz_id="mbid-x", status="downloading", download_task_id="task-x"
    )
    history = MagicMock()
    history.async_get_active_requests = AsyncMock(return_value=[record])
    history.async_update_status = AsyncMock()

    download_store = MagicMock()
    download_store.get_task = AsyncMock(return_value=SimpleNamespace(status="failed"))

    async def _mbids() -> set[str]:
        return set()

    service = RequestsPageService(
        library_repo=MagicMock(),
        request_history=history,
        library_mbids_fn=_mbids,
        download_store=download_store,
    )

    await service.sync_request_statuses()

    history.async_update_status.assert_awaited_once()
    assert history.async_update_status.await_args.args[:2] == ("mbid-x", "failed")


@pytest.mark.asyncio
async def test_approve_over_cap_returns_to_approval_queue_with_reason():
    """Feature C: a cap/quota rejection at approve time must NOT swallow the request
    into 'failed' (it silently vanished from every view) - it goes back to
    awaiting_approval and the admin sees the actual reason."""
    from core.exceptions import ValidationError

    service, history, download_service = _make()
    download_service.request_album = AsyncMock(
        side_effect=ValidationError("Library storage limit reached (12.0 / 10 GB)")
    )

    resp = await service.approve_request("mbid-1", "admin-id", "Admin")

    assert resp.success is False
    assert "Library storage limit reached" in resp.message
    history.async_update_status.assert_awaited_once()
    assert history.async_update_status.await_args.args[:2] == (
        "mbid-1",
        "awaiting_approval",
    )
    assert history.async_update_status.await_args.kwargs == {
        "completed_at": None,
        "request_kind": "album",
        "expected_generation": 1,
    }


@pytest.mark.asyncio
async def test_retry_over_cap_restores_prior_status_with_reason():
    from core.exceptions import ValidationError

    service, history, download_service = _make(record_status="failed")
    download_service.request_album = AsyncMock(
        side_effect=ValidationError("Your storage budget is full (5.0 / 5 GB)")
    )

    resp = await service.retry_request("mbid-1", user_id="u1", user_role="admin")

    assert resp.success is False
    assert "storage budget" in resp.message
    # The retry claim moves the row to pending atomically; the failed dispatch
    # restores the original terminal status for this generation.
    assert history.async_update_status.await_args_list[-1].args == ("mbid-1", "failed")
    assert history.async_update_status.await_args_list[-1].kwargs == {
        "completed_at": None,
        "request_kind": "album",
        "expected_generation": 1,
    }


@pytest.mark.asyncio
async def test_approve_exact_track_preserves_kind_and_metadata():
    service, history, download_service = _make(request_kind="track")

    response = await service.approve_request(
        "mbid-1", "admin-id", "Admin", request_kind="track"
    )

    assert response.success is True
    download_service.request_album.assert_not_awaited()
    download_service.request_track.assert_awaited_once()
    kwargs = download_service.request_track.await_args.kwargs
    assert kwargs["recording_mbid"] == "mbid-1"
    assert kwargs["track_title"] == "Airbag"
    assert kwargs["duration_seconds"] == 287
    assert kwargs["release_group_mbid"] == "release-group-1"
    assert kwargs["release_mbid"] == "release-edition"
    history.async_update_download_task_id.assert_awaited_once_with(
        "mbid-1", "track-task-9", request_kind="track", expected_generation=1
    )

@pytest.mark.asyncio
async def test_retry_authorized_failed_exact_track_does_not_widen_to_album():
    service, history, download_service = _make(
        record_status="failed",
        request_kind="track",
        dispatch_authorized=True,
        download_task_id="old-track-task",
    )

    response = await service.retry_request(
        "mbid-1", user_id="u1", user_role="user", request_kind="track"
    )

    assert response.success is True
    download_service.request_album.assert_not_awaited()
    download_service.request_track.assert_awaited_once()
    kwargs = download_service.request_track.await_args.kwargs
    assert kwargs["recording_mbid"] == "mbid-1"
    assert kwargs["track_title"] == "Airbag"
    assert kwargs["duration_seconds"] == 287
    assert kwargs["release_group_mbid"] == "release-group-1"
    history.async_update_download_task_id.assert_awaited_once_with(
        "mbid-1", "track-task-9", request_kind="track", expected_generation=1
    )


@pytest.mark.asyncio
async def test_approval_authorizes_before_dispatch_and_keeps_authorization_on_failure():
    service, history, _download_service = _make()
    events: list[str] = []

    async def authorize(*_args, **_kwargs):
        events.append("authorize")
        return RequestBeginResult(
            musicbrainz_id="mbid-1",
            request_kind="album",
            generation=1,
        )

    async def dispatch(*_args, **_kwargs):
        events.append("dispatch")
        raise RuntimeError("provider credential must not leak")

    history.async_claim_approval.side_effect = authorize
    service._acquisition.request_album = AsyncMock(side_effect=dispatch)

    response = await service.approve_request("mbid-1", "admin-id", "Admin")

    assert response.success is False
    assert events == ["authorize", "dispatch"]
    history.async_claim_approval.assert_awaited_once_with(
        "mbid-1",
        reviewer_id="admin-id",
        reviewer_name="Admin",
        request_kind="album",
        expected_generation=1,
    )
    history.async_update_dispatch_authorized.assert_not_awaited()
    status_calls = history.async_update_status.await_args_list
    assert status_calls[-1].args[:2] == ("mbid-1", "failed")
    assert status_calls[-1].kwargs["expected_generation"] == 1
    assert "provider credential" not in response.message


@pytest.mark.asyncio
async def test_cancel_denied_by_atomic_requester_decision_raises_permission_denied():
    service, history, download_service = _make(
        record_status="downloading", download_task_id="task-9"
    )
    history.async_prepare_requester_cancel.return_value = RequesterCancelDecision(
        "denied", "downloading"
    )

    with pytest.raises(PermissionDeniedError):
        await service.cancel_request("mbid-1", user_id="other", user_role="user")

    download_service.cancel_task.assert_not_awaited()
    history.async_update_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_detaches_shared_listener_without_stopping_underlying_task():
    service, history, download_service = _make(
        record_status="downloading", download_task_id="task-9"
    )
    history.async_prepare_requester_cancel.return_value = RequesterCancelDecision(
        "detached", "downloading"
    )

    response = await service.cancel_request("mbid-1", user_id="u1", user_role="user")

    assert response.success is True
    assert "another listener" in response.message
    download_service.cancel_task.assert_not_awaited()
    history.async_update_status.assert_not_awaited()
    history.async_remove_requester.assert_not_awaited()


@pytest.mark.asyncio
async def test_last_listener_cancellation_uses_primary_task_owner_and_ordinary_role():
    service, history, download_service = _make(
        record_status="downloading",
        download_task_id="task-9",
        record_user_id="primary-owner",
    )
    history.async_prepare_requester_cancel.return_value = RequesterCancelDecision(
        "cancel_task", "downloading", 1
    )

    response = await service.cancel_request(
        "mbid-1", user_id="second-listener", user_role="user"
    )

    assert response.success is True
    download_service.cancel_task.assert_awaited_once_with(
        "task-9", "primary-owner", "user"
    )
    status_calls = history.async_update_status.await_args_list
    assert status_calls[-1].args[:2] == ("mbid-1", "cancelled")
    assert status_calls[-1].kwargs["expected_generation"] == 1


@pytest.mark.asyncio
async def test_cancel_failure_restores_prior_status_and_redacts_transport_error():
    service, history, download_service = _make(
        record_status="downloading", download_task_id="task-9"
    )
    history.async_prepare_requester_cancel.return_value = RequesterCancelDecision(
        "cancel_task", "downloading", 1
    )
    download_service.cancel_task.side_effect = RuntimeError("private transport detail")

    response = await service.cancel_request("mbid-1", user_id="u1", user_role="user")

    assert response.success is False
    assert "private transport detail" not in response.message
    history.async_restore_request_status.assert_awaited_once_with(
        "mbid-1",
        "downloading",
        request_kind="album",
        expected_status="cancelling",
        expected_generation=1,
    )


@pytest.mark.asyncio
async def test_cancel_admin_is_authoritative_and_does_not_require_requester_membership():
    service, history, download_service = _make(
        record_status="downloading", download_task_id="task-9"
    )
    history.async_is_requester.return_value = False

    response = await service.cancel_request(
        "mbid-1", user_id="admin-id", user_role="admin"
    )
    assert response.success is True

    history.async_prepare_requester_cancel.assert_not_awaited()
    download_service.cancel_task.assert_awaited_once_with("task-9", "admin-id", "admin")
    status_call = history.async_update_status.await_args
    assert status_call.kwargs["expected_generation"] == 1


@pytest.mark.asyncio
async def test_cancel_before_approval_then_retry_returns_to_approval_without_dispatch():
    service, history, download_service = _make(
        record_status="awaiting_approval",
        request_kind="track",
        dispatch_authorized=False,
    )
    history.async_prepare_requester_cancel.return_value = RequesterCancelDecision(
        "cancelled", "awaiting_approval"
    )

    cancelled = await service.cancel_request(
        "mbid-1", user_id="u1", user_role="user", request_kind="track"
    )
    assert cancelled.success is True
    download_service.cancel_task.assert_not_awaited()

    history.async_get_record.return_value.status = "cancelled"
    retried = await service.retry_request(
        "mbid-1", user_id="u1", user_role="user", request_kind="track"
    )

    assert retried.success is True
    assert "approval" in retried.message.lower()
    download_service.request_track.assert_not_awaited()
    history.async_update_status.assert_not_awaited()
    history.async_claim_retry.assert_awaited_once()
    claim_kwargs = history.async_claim_retry.await_args.kwargs
    assert claim_kwargs["request_kind"] == "track"
    assert claim_kwargs["target_status"] == "awaiting_approval"
    assert claim_kwargs["dispatch_authorized"] is False
    assert claim_kwargs["expected_generation"] == 1


@pytest.mark.asyncio
async def test_ordinary_retry_of_unauthorized_failed_generation_holds_for_approval():
    service, history, download_service = _make(
        record_status="failed",
        request_kind="track",
        dispatch_authorized=False,
    )

    response = await service.retry_request(
        "mbid-1", user_id="u1", user_role="user", request_kind="track"
    )

    assert response.success is True
    assert "approval" in response.message.lower()
    download_service.request_track.assert_not_awaited()
    history.async_update_status.assert_not_awaited()
    history.async_claim_retry.assert_awaited_once()
    claim_kwargs = history.async_claim_retry.await_args.kwargs
    assert claim_kwargs["request_kind"] == "track"
    assert claim_kwargs["target_status"] == "awaiting_approval"
    assert claim_kwargs["dispatch_authorized"] is False
    assert claim_kwargs["expected_generation"] == 1


@pytest.mark.asyncio
async def test_retry_transport_errors_have_a_stable_generic_response():
    first, first_history, first_download = _make(record_status="failed")
    first_download.request_album.side_effect = RuntimeError("first secret")
    first_response = await first.retry_request(
        "mbid-1", user_id="u1", user_role="user"
    )

    second, _second_history, second_download = _make(record_status="failed")
    second_download.request_album.side_effect = RuntimeError("different secret")
    second_response = await second.retry_request(
        "mbid-1", user_id="u1", user_role="user"
    )

    assert first_response.success is False
    assert second_response.success is False
    assert first_response.message == second_response.message
    assert "first secret" not in first_response.message
    assert "different secret" not in second_response.message
    assert first_history.async_update_status.await_args_list[-1].args[:2] == (
        "mbid-1",
        "failed",
    )
    assert (
        first_history.async_update_status.await_args_list[-1].kwargs[
            "expected_generation"
        ]
        == 1
    )


def test_pending_exact_track_item_exposes_kind_metadata_and_release_group_cover():
    item = RequestsPageService._build_pending_item(
        RequestHistoryRecord(
            musicbrainz_id="recording-1",
            artist_name="Radiohead",
            album_title="OK Computer",
            requested_at="2026-08-24T12:00:00+00:00",
            status="awaiting_approval",
            request_kind="track",
            track_title="Airbag",
            duration_seconds=287,
            track_release_group_mbid="7b0032d0-09b3-4f21-a207-9eb26b746c4f",
        )
    )

    assert item.request_kind == "track"
    assert item.track_title == "Airbag"
    assert item.duration_seconds == 287
    assert item.track_release_group_mbid == "7b0032d0-09b3-4f21-a207-9eb26b746c4f"
    assert "7b0032d0-09b3-4f21-a207-9eb26b746c4f" in (item.cover_url or "")


@pytest.mark.asyncio
async def test_concurrent_approvals_dispatch_only_the_claim_winner():
    service, history, download_service = _make()
    history.async_claim_approval = AsyncMock(
        side_effect=[
            RequestBeginResult(
                musicbrainz_id="mbid-1",
                request_kind="album",
                generation=1,
            ),
            None,
        ]
    )

    responses = await asyncio.gather(
        service.approve_request("mbid-1", "admin-a", "Admin A"),
        service.approve_request("mbid-1", "admin-b", "Admin B"),
    )

    assert sorted(response.success for response in responses) == [False, True]
    assert download_service.request_album.await_count == 1
    history.async_record_review.assert_not_awaited()
    assert history.async_claim_approval.await_count == 2
    assert all(
        call.kwargs["request_kind"] == "album"
        and call.kwargs["expected_generation"] == 1
        for call in history.async_claim_approval.await_args_list
    )


@pytest.mark.asyncio
async def test_approval_after_cancel_before_claim_does_not_dispatch():
    service, history, download_service = _make(record_status="awaiting_approval")
    history.async_claim_approval = AsyncMock(return_value=None)

    response = await service.approve_request("mbid-1", "admin-id", "Admin")

    assert response.success is False
    download_service.request_album.assert_not_awaited()
    history.async_record_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_exact_track_retries_dispatch_only_the_claim_winner():
    service, history, download_service = _make(
        record_status="failed",
        request_kind="track",
        download_task_id="old-track-task",
    )
    history.async_claim_retry = AsyncMock(
        side_effect=[
            RequestBeginResult(
                musicbrainz_id="mbid-1",
                request_kind="track",
                generation=1,
            ),
            None,
        ]
    )

    responses = await asyncio.gather(
        service.retry_request(
            "mbid-1", user_id="u1", user_role="user", request_kind="track"
        ),
        service.retry_request(
            "mbid-1", user_id="u1", user_role="user", request_kind="track"
        ),
    )

    assert sorted(response.success for response in responses) == [False, True]
    assert download_service.request_track.await_count == 1
    download_service.request_album.assert_not_awaited()
    assert history.async_claim_retry.await_count == 2
    assert all(
        call.kwargs["request_kind"] == "track"
        and call.kwargs["expected_generation"] == 1
        for call in history.async_claim_retry.await_args_list
    )


@pytest.mark.asyncio
async def test_queued_request_cancellation_cancels_native_task():
    service, history, download_service = _make(
        record_status="queued", download_task_id="queued-task"
    )

    response = await service.cancel_request("mbid-1", user_id="u1", user_role="user")

    assert response.success is True
    download_service.cancel_task.assert_awaited_once_with("queued-task", "u1", "user")
    history.async_update_status.assert_awaited_once_with(
        "mbid-1",
        "cancelled",
        completed_at=history.async_update_status.await_args.kwargs["completed_at"],
        request_kind="album",
        expected_generation=1,
    )


@pytest.mark.asyncio
async def test_retry_dispatch_keeps_immutable_primary_owner_for_co_requester():
    service, history, download_service = _make(
        record_status="failed",
        record_user_id="primary-owner",
        download_task_id="failed-task",
    )

    response = await service.retry_request(
        "mbid-1", user_id="co-requester", user_role="user"
    )

    assert response.success is True
    assert download_service.request_album.await_args.kwargs["user_id"] == "primary-owner"
    history.async_claim_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_failure_restores_decision_prior_status_conditionally():
    service, history, download_service = _make(
        record_status="pending", download_task_id="task-9"
    )
    history.async_prepare_requester_cancel.return_value = RequesterCancelDecision(
        "cancel_task", "downloading", 1
    )
    download_service.cancel_task.side_effect = RuntimeError("private transport detail")

    response = await service.cancel_request("mbid-1", user_id="u1", user_role="user")

    assert response.success is False
    history.async_restore_request_status.assert_awaited_once_with(
        "mbid-1",
        "downloading",
        request_kind="album",
        expected_status="cancelling",
        expected_generation=1,
    )
    assert "private transport detail" not in response.message


@pytest.mark.asyncio
async def test_terminal_track_task_sync_uses_task_lookup_and_kind():
    history = MagicMock()
    record = SimpleNamespace(
        musicbrainz_id="recording-1",
        request_kind="track",
        generation=4,
        download_task_id="track-task-1",
    )
    history.async_get_record_by_download_task_id = AsyncMock(return_value=record)
    history.async_get_record = AsyncMock()
    history.async_update_status = AsyncMock(return_value=True)

    orchestrator = object.__new__(DownloadOrchestrator)
    orchestrator._request_history = history
    orchestrator._wanted_store = None
    orchestrator._on_import = None
    task = SimpleNamespace(
        id="track-task-1",
        release_group_mbid="release-group-1",
        download_type="track",
    )

    await orchestrator._sync_request_on_terminal(task, DownloadStatus.FAILED)

    history.async_get_record_by_download_task_id.assert_awaited_once_with(
        "track-task-1"
    )
    history.async_get_record.assert_not_awaited()
    history.async_update_status.assert_awaited_once_with(
        "recording-1",
        "failed",
        completed_at=history.async_update_status.await_args.kwargs["completed_at"],
        request_kind="track",
        expected_generation=4,
    )


@pytest.mark.asyncio
async def test_clear_history_album_bound_method_deletes_for_admin():
    service, history, _download_service = _make(record_status="imported")
    history.async_delete_record = AsyncMock(return_value=True)
    history.async_dismiss_record = AsyncMock(return_value=True)

    cleared = await service.clear_history_item(
        "mbid-1", user_id="admin-id", user_role="admin", request_kind="album"
    )

    assert cleared is True
    history.async_is_requester.assert_not_awaited()
    history.async_delete_record.assert_awaited_once_with(
        "mbid-1", request_kind="album"
    )
    history.async_dismiss_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_history_track_bound_method_dismisses_for_owner():
    service, history, _download_service = _make(
        record_status="imported", request_kind="track"
    )
    history.async_delete_record = AsyncMock(return_value=True)
    history.async_dismiss_record = AsyncMock(return_value=True)

    cleared = await service.clear_history_item(
        "mbid-1", user_id="u1", user_role="user", request_kind="track"
    )

    assert cleared is True
    history.async_is_requester.assert_awaited_once_with(
        "u1", "mbid-1", request_kind="track"
    )
    history.async_dismiss_record.assert_awaited_once_with(
        "u1", "mbid-1", request_kind="track"
    )
    history.async_delete_record.assert_not_awaited()
