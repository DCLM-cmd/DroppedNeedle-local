"""Target policy settings routes mounted only at the offline replacement boundary."""

from fastapi import APIRouter, Depends

from api.v1.schemas.library_policies import (
    LibraryPolicyApplyPreviewResponse,
    LibraryPolicyApplyRequest,
    LibraryPolicyImpactRequest,
    LibraryPolicyImpactResponse,
    LibraryPolicyTreeResponse,
    LibraryRestorableRootsResponse,
    LibraryRestoreRootsRequest,
    LibrarySettingsResponse,
    LibrarySettingsUpdateRequest,
)
from core.dependencies import (
    LegacyPendingMigrationServiceDep,
    TargetLibraryPolicyServiceDep,
)
from infrastructure.msgspec_fastapi import MsgSpecBody, MsgSpecRoute
from middleware import CurrentAdminDep


async def _admin_guard(_: CurrentAdminDep) -> None: ...


router = APIRouter(
    route_class=MsgSpecRoute,
    prefix="/settings/library",
    tags=["library-policies-target"],
    dependencies=[Depends(_admin_guard)],
)


@router.get("", response_model=LibrarySettingsResponse)
async def get_library_settings(
    service: TargetLibraryPolicyServiceDep,
) -> LibrarySettingsResponse:
    return await service.get_settings()


@router.put("", response_model=LibrarySettingsResponse)
async def update_library_settings(
    service: TargetLibraryPolicyServiceDep,
    pending_migration: LegacyPendingMigrationServiceDep,
    request: LibrarySettingsUpdateRequest = MsgSpecBody(LibrarySettingsUpdateRequest),
) -> LibrarySettingsResponse:
    response = await service.save_settings(
        request.settings,
        expected_policy_revision=request.expected_policy_revision,
    )
    if response.enabled:
        await pending_migration.schedule()
    return response


@router.get("/policy-tree", response_model=LibraryPolicyTreeResponse)
async def get_library_policy_tree(
    service: TargetLibraryPolicyServiceDep,
) -> LibraryPolicyTreeResponse:
    return await service.policy_tree()


@router.post("/policy-impact", response_model=LibraryPolicyImpactResponse)
async def preview_library_policy_impact(
    service: TargetLibraryPolicyServiceDep,
    request: LibraryPolicyImpactRequest = MsgSpecBody(LibraryPolicyImpactRequest),
) -> LibraryPolicyImpactResponse:
    return await service.preview_impact(request)


@router.post("/policy-apply-preview", response_model=LibraryPolicyApplyPreviewResponse)
async def preview_saved_policy_apply(
    service: TargetLibraryPolicyServiceDep,
    request: LibraryPolicyApplyRequest = MsgSpecBody(LibraryPolicyApplyRequest),
) -> LibraryPolicyApplyPreviewResponse:
    return await service.preview_apply(request)


@router.get("/restorable-roots", response_model=LibraryRestorableRootsResponse)
async def get_restorable_library_roots(
    service: TargetLibraryPolicyServiceDep,
) -> LibraryRestorableRootsResponse:
    return await service.restorable_roots()


@router.post("/restore-roots", response_model=LibrarySettingsResponse)
async def restore_library_roots(
    service: TargetLibraryPolicyServiceDep,
    pending_migration: LegacyPendingMigrationServiceDep,
    request: LibraryRestoreRootsRequest = MsgSpecBody(LibraryRestoreRootsRequest),
) -> LibrarySettingsResponse:
    response = await service.restore_roots(request)
    if response.enabled:
        await pending_migration.schedule()
    return response
