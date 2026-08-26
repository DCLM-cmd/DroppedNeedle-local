from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException

from api.v1.routes.library_operations_target import router
from api.v1.schemas.library_operations import (
    IdentityPreparationEstimateResponse,
    OperationListResponse,
    OperationResponse,
    RepairFindingListResponse,
    RepairEstimateResponse,
    RepairFindingResponse,
    ReviewActionResponse,
    ReviewDetailResponse,
    ReviewListItem,
    ReviewListResponse,
    SuggestedEditionSummary,
)
from api.v1.schemas.artist_reconciliation import (
    ArtistDuplicateGroupDetail,
    ArtistDuplicateGroupDismissResponse,
    ArtistDuplicateGroupListResponse,
    ArtistReconciliationProgress,
)
from core.dependencies import (
    get_artist_identity_reconciliation_service,
    get_target_catalog_correction_service,
    get_target_explicit_reidentification_worker,
    get_target_identity_repair_service,
    get_target_library_diagnostics_service,
    get_target_library_operation_service,
    get_target_library_review_service,
    get_target_reidentification_service,
)
from core.exceptions import ResourceNotFoundError, ValidationError
from middleware import _get_current_admin
from tests.helpers import build_test_client, override_admin_auth


@pytest.fixture
def services() -> dict[str, AsyncMock]:
    review = AsyncMock()
    review.list_reviews.return_value = ReviewListResponse(items=[])
    review.detail.return_value = ReviewDetailResponse(
        review=ReviewListItem(
            id="review-1", state="needs_review", reason_code="NO_SAFE_MATCH"
        ),
        tracks=[],
    )
    review.act.return_value = ReviewActionResponse(
        review_id="review-1",
        state="resolved",
        row_revision=2,
        catalog_revision=1,
        action_id="action-dismiss",
        remaining_exclusion_source=None,
    )
    operation = AsyncMock()
    operation.get.return_value = OperationResponse(
        id="job-1", kind="repair", state="queued"
    )
    operation.control.return_value = operation.get.return_value
    repair = AsyncMock()
    repair.history.return_value = OperationListResponse(items=[])
    repair.get_for_purpose.return_value = operation.get.return_value
    repair.create_management_preparation.return_value = operation.get.return_value
    repair.begin_management_preparation_apply.return_value = operation.get.return_value
    repair.discard_management_preparation.return_value = operation.get.return_value
    repair.findings.return_value = RepairFindingListResponse(items=[])
    repair.estimate.return_value = RepairEstimateResponse(
        identity_count=12, selected_root_count=1, queued_repair_count=2
    )
    repair.estimate_management_preparation.return_value = (
        IdentityPreparationEstimateResponse(
            album_count=20,
            ready_album_count=4,
            mapping_required_count=12,
            exact_release_required_count=4,
            selected_root_count=1,
            queued_preparation_count=0,
        )
    )
    diagnostics = AsyncMock()
    diagnostics.export.return_value = ("droppedneedle-library-run-safe.json", b"{}")
    reidentification = AsyncMock()
    reidentification.create_or_coalesce.return_value = {
        "id": "job-1",
        "kind": "explicit_reidentification",
        "state": "queued",
    }
    artist_reconciliation = AsyncMock()
    artist_reconciliation.progress.return_value = ArtistReconciliationProgress(
        state="idle"
    )
    artist_reconciliation.list_groups.return_value = ArtistDuplicateGroupListResponse(
        items=[]
    )
    artist_reconciliation.group_detail.return_value = ArtistDuplicateGroupDetail(
        id="group-1",
        display_name="Artist",
        state="same_name_only",
        member_count=0,
        members=[],
    )
    artist_reconciliation.dismiss_group.return_value = (
        ArtistDuplicateGroupDismissResponse(group_id="group-1", dismissed_pairs=1)
    )
    return {
        "review": review,
        "operation": operation,
        "correction": AsyncMock(),
        "repair": repair,
        "diagnostics": diagnostics,
        "reidentification": reidentification,
        "explicit_worker": AsyncMock(),
        "artist_reconciliation": artist_reconciliation,
    }


@pytest.fixture
def app(services: dict[str, AsyncMock]) -> FastAPI:
    application = FastAPI()
    application.include_router(router)

    def provide(service: AsyncMock) -> Callable[[], AsyncMock]:
        def dependency_override() -> AsyncMock:
            return service

        return dependency_override

    overrides = {
        get_target_library_review_service: services["review"],
        get_target_library_operation_service: services["operation"],
        get_target_catalog_correction_service: services["correction"],
        get_target_identity_repair_service: services["repair"],
        get_target_library_diagnostics_service: services["diagnostics"],
        get_target_reidentification_service: services["reidentification"],
        get_target_explicit_reidentification_worker: services["explicit_worker"],
        get_artist_identity_reconciliation_service: services["artist_reconciliation"],
    }
    for provider, service in overrides.items():
        application.dependency_overrides[provider] = provide(service)
    return application


def test_review_and_diagnostic_contracts(
    app: FastAPI, services: dict[str, AsyncMock]
) -> None:
    override_admin_auth(app)
    client = build_test_client(app)
    assert client.get("/library/reviews").status_code == 200
    assert client.get("/library/reviews/review-1").status_code == 200
    response = client.get("/library/scan-runs/run-1/diagnostics")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"] == (
        'attachment; filename="droppedneedle-library-run-safe.json"'
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == b"{}"


def test_review_dismiss_forwards_action_and_returns_resolved(
    app: FastAPI, services: dict[str, AsyncMock]
) -> None:
    override_admin_auth(app)
    client = build_test_client(app)
    response = client.post(
        "/library/reviews/review-1/dismiss",
        json={
            "expected_review_revision": 1,
            "expected_catalog_revision": 1,
            "idempotency_key": "dismiss-1",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "review_id": "review-1",
        "state": "resolved",
        "row_revision": 2,
        "catalog_revision": 1,
        "action_id": "action-dismiss",
        "operation_job_id": None,
        "remaining_exclusion_source": None,
    }
    call = services["review"].act.await_args
    assert call is not None
    assert call.args[0] == "review-1"
    assert call.args[1] == "dismiss"
    assert call.args[2].expected_review_revision == 1
    assert call.args[2].expected_catalog_revision == 1
    assert call.args[2].expected_identity_revision is None
    assert call.args[2].idempotency_key == "dismiss-1"
    assert call.args[2].confirmation is False
    assert call.args[3] == "test-admin-id"


def test_target_operation_routes_are_admin_only(app: FastAPI) -> None:
    def reject_admin() -> None:
        raise HTTPException(status_code=403, detail="Admin access required")

    app.dependency_overrides[_get_current_admin] = reject_admin
    client = build_test_client(app)
    assert client.get("/library/reviews").status_code == 403
    assert client.get("/library/operations/job-1").status_code == 403
    assert client.get("/library/identity-repairs/job-1/findings").status_code == 403
    assert client.get("/library/scan-runs/run-1/diagnostics").status_code == 403
    assert client.get("/library/artists/reconciliation").status_code == 403
    assert client.get("/library/artists/duplicate-groups").status_code == 403

    unauthenticated = FastAPI()
    unauthenticated.include_router(router)
    client = build_test_client(unauthenticated)
    assert client.get("/library/reviews").status_code == 401
    assert client.get("/library/operations/job-1").status_code == 401
    assert client.get("/library/artists/reconciliation").status_code == 401


def test_artist_reconciliation_routes_forward_group_workflow(
    app: FastAPI, services: dict[str, AsyncMock]
) -> None:
    override_admin_auth(app)
    client = build_test_client(app)

    assert client.get("/library/artists/reconciliation").status_code == 200
    listing = client.get(
        "/library/artists/duplicate-groups",
        params={
            "limit": 25,
            "cursor": "group-0",
            "state": "same_name_only",
            "search": "art",
        },
    )
    assert listing.status_code == 200
    services["artist_reconciliation"].list_groups.assert_awaited_once_with(
        limit=25,
        cursor="group-0",
        state="same_name_only",
        search="art",
    )
    assert client.get("/library/artists/duplicate-groups/group-1").status_code == 200
    dismissed = client.post(
        "/library/artists/duplicate-groups/group-1/dismiss",
        json={"expected_member_revisions": {"artist-1": 2, "artist-2": 3}},
    )
    assert dismissed.status_code == 200
    services["artist_reconciliation"].dismiss_group.assert_awaited_once_with(
        "group-1", {"artist-1": 2, "artist-2": 3}, "test-admin-id"
    )


def test_repair_findings_forwards_category_and_pagination(
    app: FastAPI, services: dict[str, AsyncMock]
) -> None:
    override_admin_auth(app)
    response = build_test_client(app).get(
        "/library/identity-repairs/job-1/findings",
        params={
            "limit": 37,
            "cursor": "12.5:finding-2",
            "finding_category": "unverifiable",
        },
    )
    assert response.status_code == 200
    services["repair"].findings.assert_awaited_once_with(
        "job-1",
        limit=37,
        cursor="12.5:finding-2",
        finding_category="unverifiable",
    )


def test_repair_estimate_forwards_selected_roots(
    app: FastAPI, services: dict[str, AsyncMock]
) -> None:
    override_admin_auth(app)
    response = build_test_client(app).get(
        "/library/identity-repairs/estimate",
        params=[("root_id", "root-2"), ("root_id", "root-1")],
    )
    assert response.status_code == 200
    assert response.json()["identity_count"] == 12
    services["repair"].estimate.assert_awaited_once_with(["root-2", "root-1"])


def test_management_identity_preparation_contracts(
    app: FastAPI, services: dict[str, AsyncMock]
) -> None:
    override_admin_auth(app)
    client = build_test_client(app)
    estimate = client.get(
        "/library/management/identity-preparations/estimate",
        params=[("root_id", "root-2"), ("root_id", "root-1")],
    )
    assert estimate.status_code == 200
    assert estimate.json()["mapping_required_count"] == 12
    services["repair"].estimate_management_preparation.assert_awaited_once_with(
        ["root-2", "root-1"]
    )

    started = client.post(
        "/library/management/identity-preparations",
        json={"idempotency_key": "identity-preparation-1", "root_ids": ["root-1"]},
    )
    assert started.status_code == 200
    services["repair"].create_management_preparation.assert_awaited_once()

    assert client.get("/library/management/identity-preparations").status_code == 200
    assert (
        client.get("/library/management/identity-preparations/job-1").status_code == 200
    )
    findings = client.get(
        "/library/management/identity-preparations/job-1/findings",
        params={"finding_category": "mapping_ready"},
    )
    assert findings.status_code == 200
    services["repair"].findings.assert_awaited_with(
        "job-1",
        limit=100,
        cursor=None,
        finding_category="mapping_ready",
    )
    applied = client.post(
        "/library/management/identity-preparations/job-1/apply",
        json={"expected_row_revision": 2, "confirmation": True},
    )
    assert applied.status_code == 200
    services["repair"].begin_management_preparation_apply.assert_awaited_once_with(
        "job-1", expected_row_revision=2, confirmation=True
    )
    discarded = client.post(
        "/library/management/identity-preparations/job-1/discard",
        json={"expected_row_revision": 3},
    )
    assert discarded.status_code == 200
    services["repair"].discard_management_preparation.assert_awaited_once_with(
        "job-1", expected_row_revision=3
    )


def test_management_identity_preparation_findings_serialize_suggested_edition(
    app: FastAPI, services: dict[str, AsyncMock]
) -> None:
    override_admin_auth(app)
    suggested = RepairFindingResponse(
        id="finding-suggested",
        local_album_id="album-1",
        album_title="Album 1",
        album_artist_name="Artist 1",
        album_year=2020,
        cover_available=False,
        evidence_id="evidence-1",
        review_id=None,
        finding_code="exact_release_suggested",
        reason_code="EXACT_EDITION_SUGGESTED",
        confidence="complete",
        apply_eligible=True,
        state="open",
        suggested_edition=SuggestedEditionSummary(
            release_mbid="release-1",
            release_group_mbid="rg-1",
            title="Album 1",
            track_count=11,
            competing_count=3,
            date="2019-03-01",
            country="DE",
            status="Official",
        ),
    )
    bare = RepairFindingResponse(
        id="finding-bare",
        local_album_id="album-2",
        album_title="Album 2",
        album_artist_name="Artist 2",
        album_year=None,
        cover_available=False,
        evidence_id=None,
        review_id=None,
        finding_code="exact_release_required",
        reason_code="EXACT_EDITION_NOT_ACCEPTED",
        confidence="bounded",
        apply_eligible=False,
        state="open",
    )
    services["repair"].findings.return_value = RepairFindingListResponse(
        items=[suggested, bare]
    )
    response = build_test_client(app).get(
        "/library/management/identity-preparations/job-1/findings",
        params={"finding_category": "exact_release_required"},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["suggested_edition"] == {
        "release_mbid": "release-1",
        "release_group_mbid": "rg-1",
        "title": "Album 1",
        "track_count": 11,
        "competing_count": 3,
        "date": "2019-03-01",
        "country": "DE",
        "status": "Official",
    }
    assert items[1]["suggested_edition"] is None


def test_route_errors_use_typed_envelopes(
    app: FastAPI, services: dict[str, AsyncMock]
) -> None:
    override_admin_auth(app)
    services["operation"].get.side_effect = ResourceNotFoundError(
        "Library operation not found."
    )
    response = build_test_client(app).get("/library/operations/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"

    services["diagnostics"].export.side_effect = ValidationError(
        "The scan run ID is invalid."
    )
    response = build_test_client(app).get("/library/scan-runs/bad/diagnostics")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_reidentification_forwards_a_normalized_exact_release(
    app: FastAPI, services: dict[str, AsyncMock]
) -> None:
    override_admin_auth(app)
    response = build_test_client(app).post(
        "/library/albums/album-1/reidentify",
        json={
            "expected_album_revision": 4,
            "expected_input_revision": "input-1",
            "idempotency_key": "request-1",
            "release_mbid": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        },
    )

    assert response.status_code == 200
    services["reidentification"].create_or_coalesce.assert_awaited_once_with(
        "album-1",
        "test-admin-id",
        expected_album_revision=4,
        expected_input_revision="input-1",
        one_off_local_metadata=False,
        release_mbid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        idempotency_key="request-1",
    )


def test_reidentification_rejects_a_malformed_exact_release(
    app: FastAPI, services: dict[str, AsyncMock]
) -> None:
    override_admin_auth(app)
    response = build_test_client(app).post(
        "/library/albums/album-1/reidentify",
        json={
            "expected_album_revision": 4,
            "expected_input_revision": "input-1",
            "idempotency_key": "request-1",
            "release_mbid": "not-a-release-id",
        },
    )

    assert response.status_code == 422
    assert "MusicBrainz UUID" in response.text
    services["reidentification"].create_or_coalesce.assert_not_awaited()


def test_diagnostic_route_uses_fixed_5xx_copy(
    app: FastAPI, services: dict[str, AsyncMock]
) -> None:
    override_admin_auth(app)
    services["diagnostics"].export.side_effect = RuntimeError(
        "provider failed at /secret/music/private.flac"
    )
    response = build_test_client(app).get("/library/scan-runs/run-1/diagnostics")
    assert response.status_code == 500
    assert response.json()["error"]["message"] == "Internal server error"
    assert "/secret/music" not in response.text
    assert "provider failed" not in response.text


def test_target_operation_route_inventory_is_complete() -> None:
    inventory = {
        (method, route.path)
        for route in router.routes
        for method in getattr(route, "methods", set())
        if method in {"GET", "POST"}
    }
    assert inventory == {
        ("GET", "/library/reviews"),
        ("GET", "/library/reviews/{review_id}"),
        ("POST", "/library/reviews/{review_id}/keep-tagged"),
        ("POST", "/library/reviews/{review_id}/detach-and-keep-tagged"),
        ("POST", "/library/reviews/{review_id}/exclude"),
        ("POST", "/library/reviews/{review_id}/restore"),
        ("POST", "/library/reviews/{review_id}/dismiss"),
        ("POST", "/library/reviews/{review_id}/candidate"),
        ("POST", "/library/reviews/bulk-preview"),
        ("POST", "/library/reviews/bulk-apply"),
        ("POST", "/library/reviews/{review_id}/retry"),
        ("GET", "/library/operations/{job_id}"),
        ("POST", "/library/operations/{job_id}/pause"),
        ("POST", "/library/operations/{job_id}/resume"),
        ("POST", "/library/operations/{job_id}/stop"),
        ("POST", "/library/albums/{album_id}/reidentify"),
        ("POST", "/library/operations/{job_id}/candidate"),
        ("POST", "/library/albums/{album_id}/split-preview"),
        ("POST", "/library/albums/{album_id}/split"),
        ("POST", "/library/albums/merge-preview"),
        ("POST", "/library/albums/merge"),
        ("POST", "/library/tracks/move-preview"),
        ("POST", "/library/tracks/move"),
        ("POST", "/library/albums/{album_id}/reset-grouping-preview"),
        ("POST", "/library/albums/{album_id}/reset-grouping"),
        ("POST", "/library/artists/merge-preview"),
        ("POST", "/library/artists/merge"),
        ("GET", "/library/artists/reconciliation"),
        ("GET", "/library/artists/duplicate-groups"),
        ("GET", "/library/artists/duplicate-groups/{group_id}"),
        ("POST", "/library/artists/duplicate-groups/{group_id}/dismiss"),
        ("POST", "/library/identity-repairs"),
        ("GET", "/library/identity-repairs"),
        ("GET", "/library/identity-repairs/estimate"),
        ("GET", "/library/identity-repairs/{job_id}"),
        ("GET", "/library/identity-repairs/{job_id}/findings"),
        ("POST", "/library/identity-repairs/{job_id}/apply"),
        ("POST", "/library/identity-repairs/{job_id}/pause"),
        ("POST", "/library/identity-repairs/{job_id}/resume"),
        ("POST", "/library/identity-repairs/{job_id}/stop"),
        ("POST", "/library/management/identity-preparations"),
        ("GET", "/library/management/identity-preparations"),
        ("GET", "/library/management/identity-preparations/estimate"),
        ("GET", "/library/management/identity-preparations/{job_id}"),
        ("GET", "/library/management/identity-preparations/{job_id}/findings"),
        ("POST", "/library/management/identity-preparations/{job_id}/apply"),
        ("POST", "/library/management/identity-preparations/{job_id}/discard"),
        ("GET", "/library/scan-runs/{run_id}/diagnostics"),
    }
