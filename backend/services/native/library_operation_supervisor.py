"""Claim and dispatch one durable target library operation at a time."""

from __future__ import annotations

import json
import time

from api.v1.schemas.library_operations import OperationResponse
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.background_workload_gate import BackgroundWorkloadGate
from services.native.explicit_reidentification_worker import (
    ExplicitReidentificationWorker,
)
from services.native.identity_repair_service import IdentityRepairService
from services.native.library_operation_service import (
    LEASE_SECONDS,
    LibraryOperationService,
)
from services.native.library_management_worker import LibraryManagementWorker
from services.native.library_management_notification_service import (
    LibraryManagementNotificationService,
)
from services.native.artist_identity_reconciliation_service import (
    ARTIST_RECONCILIATION_PURPOSE,
    ArtistIdentityReconciliationService,
)
from services.native.catalog_identity_hygiene_service import (
    CATALOG_IDENTITY_HYGIENE_PURPOSE,
    CatalogIdentityHygieneService,
)


class LibraryOperationSupervisor:
    def __init__(
        self,
        store: NativeLibraryStore,
        operations: LibraryOperationService,
        repairs: IdentityRepairService,
        reidentification: ExplicitReidentificationWorker,
        workload_gate: BackgroundWorkloadGate | None = None,
        management: LibraryManagementWorker | None = None,
        notifications: LibraryManagementNotificationService | None = None,
        artist_reconciliation: ArtistIdentityReconciliationService | None = None,
        catalog_identity_hygiene: CatalogIdentityHygieneService | None = None,
    ) -> None:
        self._store = store
        self._operations = operations
        self._repairs = repairs
        self._reidentification = reidentification
        self._workload_gate = workload_gate
        self._management = management
        self._notifications = notifications
        self._artist_reconciliation = artist_reconciliation
        self._catalog_identity_hygiene = catalog_identity_hygiene

    async def recover(self, *, now: float | None = None) -> int:
        recovered = await self._operations.recover(now=now)
        if self._notifications is not None:
            recovered += await self._notifications.recover(now=now)
        return recovered

    async def run_once(
        self, worker_id: str, *, now: float | None = None
    ) -> OperationResponse | None:
        timestamp = time.time() if now is None else now
        job = None
        kinds = ["bulk_review_apply"]
        if self._workload_gate is None or not self._workload_gate.scan_active:
            kinds.insert(0, "explicit_reidentification")
            if self._management is not None:
                kinds.insert(0, "library_management")
            kinds.append("repair")
        for kind in kinds:
            job = await self._store.claim_operation_job(
                worker_id,
                now=timestamp,
                lease_seconds=LEASE_SECONDS,
                kind=kind,
            )
            if job is not None:
                break
        if job is None:
            if self._notifications is not None:
                operation_id = await self._notifications.run_once(
                    worker_id, now=timestamp
                )
                if operation_id is not None:
                    return await self._operations.get(operation_id)
            return None
        if job["kind"] == "explicit_reidentification":
            row = await self._reidentification.run_claimed(
                job, worker_id, now=timestamp
            )
            return self._operations.response_for(row)
        if job["kind"] == "library_management":
            row = await self._management.run_claimed(job, worker_id)
            return self._operations.response_for(row)
        if job["kind"] == "bulk_review_apply":
            return await self._operations.run_bulk_claimed(
                job,
                worker_id,
                str(job["requested_by_user_id"]),
                now=timestamp,
            )
        snapshot = await self._store.get_operation_snapshot(str(job["id"]))
        if snapshot is None:
            return await self._operations.get(str(job["id"]))
        repair_scope = json.loads(str(snapshot["snapshot"]["scope_json"]))
        if repair_scope.get("purpose") == CATALOG_IDENTITY_HYGIENE_PURPOSE:
            if self._catalog_identity_hygiene is None:
                row = await self._store.finish_operation_job(
                    str(job["id"]),
                    worker_id,
                    state="failed",
                    terminal_code="CATALOG_IDENTITY_HYGIENE_WORKER_UNAVAILABLE",
                    now=timestamp,
                )
                return self._operations.response_for(row)
            row = await self._catalog_identity_hygiene.run_claimed(job, worker_id)
            return self._operations.response_for(row)
        if repair_scope.get("purpose") == ARTIST_RECONCILIATION_PURPOSE:
            if self._artist_reconciliation is None:
                row = await self._store.finish_operation_job(
                    str(job["id"]),
                    worker_id,
                    state="failed",
                    terminal_code="RECONCILIATION_WORKER_UNAVAILABLE",
                    now=timestamp,
                )
                return self._operations.response_for(row)
            row = await self._artist_reconciliation.run_claimed(job, worker_id)
            return self._operations.response_for(row)
        if snapshot["snapshot"]["phase"] == "apply":
            return await self._repairs.run_claimed_apply(
                job,
                worker_id,
                str(job["requested_by_user_id"]),
                now=now,
            )
        return await self._repairs.run_claimed_audit(job, worker_id, now=now)
