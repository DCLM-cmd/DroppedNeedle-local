"""Dispatch claimed Library Management modes through the durable supervisor."""

from __future__ import annotations

import json
import logging
import time

from core.exceptions import (
    ConflictError,
    LibraryManagementDestinationConflictError,
    StaleRevisionError,
    ValidationError,
)
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.library_filesystem_coordinator import (
    LibraryFilesystemCoordinator,
)
from services.native.library_management_planner import LibraryManagementPlanner
from services.native.library_operation_service import LEASE_SECONDS
from services.native.library_management_publisher import LibraryManagementPublisher
from services.native.library_management_undo_service import LibraryManagementUndoService
from services.native.library_management_baseline_service import (
    LibraryManagementBaselineService,
)
from services.native.library_management_duplicate_service import (
    LibraryManagementDuplicateService,
)
from services.native.library_image_hash_service import LibraryImageHashService

logger = logging.getLogger(__name__)


class LibraryManagementWorker:
    def __init__(
        self,
        store: NativeLibraryStore,
        planner: LibraryManagementPlanner,
        publisher: LibraryManagementPublisher,
        undo: LibraryManagementUndoService,
        baseline: LibraryManagementBaselineService,
        duplicates: LibraryManagementDuplicateService,
        filesystem: "LibraryFilesystemCoordinator | None" = None,
        image_hashes: "LibraryImageHashService | None" = None,
    ) -> None:
        self._store = store
        self._planner = planner
        self._publisher = publisher
        self._undo = undo
        self._baseline = baseline
        self._duplicates = duplicates
        # Used to hold the library still while a settings dry run plans against it.
        self._filesystem = filesystem
        # Artwork blurhashes are computed here, once, the way Jellyfin computes them
        # during a scan - never while a client is waiting for a listing.
        self._image_hashes = image_hashes

    async def run_claimed(self, job: dict, worker_id: str) -> dict:
        job_id = str(job["id"])
        snapshot = await self._store.get_library_management_job_snapshot(job_id)
        if snapshot is None:
            return await self._store.finish_operation_job(
                job_id,
                worker_id,
                state="failed",
                terminal_code="MISSING_SNAPSHOT",
                now=time.time(),
            )
        if snapshot.mode == "undo" and snapshot.phase == "planning":
            try:
                await self._undo.run_claimed_preview(job, worker_id)
            except StaleRevisionError:
                return await self._store.finish_operation_job(
                    job_id,
                    worker_id,
                    state="failed",
                    terminal_code="STALE_INPUT",
                    now=time.time(),
                )
            except (ValidationError, ConflictError):
                return await self._store.finish_operation_job(
                    job_id,
                    worker_id,
                    state="failed",
                    terminal_code="PLANNING_FAILED",
                    now=time.time(),
                )
            current = await self._store.get_operation_job(job_id)
            if current is None:
                raise ValidationError("Undo job disappeared after planning.")
            return current
        if snapshot.mode == "baseline_restore" and snapshot.phase == "planning":
            try:
                await self._baseline.run_claimed_preview(job, worker_id)
            except StaleRevisionError:
                return await self._store.finish_operation_job(
                    job_id,
                    worker_id,
                    state="failed",
                    terminal_code="STALE_INPUT",
                    now=time.time(),
                )
            except (ValidationError, ConflictError):
                return await self._store.finish_operation_job(
                    job_id,
                    worker_id,
                    state="failed",
                    terminal_code="PLANNING_FAILED",
                    now=time.time(),
                )
            current = await self._store.get_operation_job(job_id)
            if current is None:
                raise ValidationError(
                    "Baseline restore job disappeared after planning."
                )
            return current
        if snapshot.mode == "duplicate_resolution" and snapshot.phase == "planning":
            try:
                await self._duplicates.run_claimed_preview(job, worker_id)
            except StaleRevisionError:
                return await self._store.finish_operation_job(
                    job_id,
                    worker_id,
                    state="failed",
                    terminal_code="STALE_INPUT",
                    now=time.time(),
                )
            except (ValidationError, ConflictError):
                return await self._store.finish_operation_job(
                    job_id,
                    worker_id,
                    state="failed",
                    terminal_code="PLANNING_FAILED",
                    now=time.time(),
                )
            current = await self._store.get_operation_job(job_id)
            if current is None:
                raise ValidationError(
                    "Duplicate-resolution job disappeared after planning."
                )
            return current
        if snapshot.mode in {
            "apply",
            "automatic_apply",
            "undo",
            "baseline_restore",
            "duplicate_resolution",
        } and snapshot.phase in {
            "applying",
            "undoing",
            "restoring",
        }:
            return await self._run_apply(job_id, worker_id)
        if snapshot.mode != "preview":
            return await self._store.finish_operation_job(
                job_id,
                worker_id,
                state="failed",
                terminal_code="MODE_NOT_AVAILABLE",
                now=time.time(),
            )
        try:
            # A dry run for the automation settings (it proposes a new settings
            # revision) must see a library that is not moving underneath it: an
            # import or Organizer apply landing mid-plan produces a preview that
            # describes files that have since moved. Ordinary previews do not pay
            # this cost - only the one whose whole purpose is to predict what the
            # new settings would do.
            if snapshot.proposed_settings_revision is not None and (
                self._filesystem is not None
            ):
                async with self._filesystem.quiesce():
                    planned = await self._planner.run_claimed_preview(job, worker_id)
            else:
                planned = await self._planner.run_claimed_preview(job, worker_id)
        except StaleRevisionError:
            return await self._store.finish_operation_job(
                job_id,
                worker_id,
                state="failed",
                terminal_code="STALE_INPUT",
                now=time.time(),
            )
        except (ValidationError, ConflictError):
            return await self._store.finish_operation_job(
                job_id,
                worker_id,
                state="failed",
                terminal_code="PLANNING_FAILED",
                now=time.time(),
            )
        current = await self._store.get_operation_job(job_id)
        if current is None:
            raise ValidationError("Library management job disappeared after planning.")
        if planned.origin == "scan_discovered" and planned.phase == "ready":
            try:
                summary = json.loads(planned.summary_json)
            except (json.JSONDecodeError, TypeError) as error:
                # F-107: a corrupt stored summary must classify as a
                # deterministic validation failure, not escape as unknown.
                raise ValidationError(
                    "The stored Library Management snapshot is invalid."
                ) from error
            if (
                int(summary.get("blocked_count", 0)) == 0
                and int(summary.get("stale_count", 0)) == 0
            ):
                if planned.preview_token_hash is None:
                    return current
                try:
                    return await self._store.begin_library_management_apply(
                        job_id,
                        preview_token_hash=planned.preview_token_hash,
                        expected_job_revision=int(current["row_revision"]),
                        idempotency_key=f"automatic-scan-apply:{job_id}",
                        now=time.time(),
                    )
                except (StaleRevisionError, ValidationError):
                    return current
        return current

    async def _hash_artwork(self, job_id: str) -> None:
        """Give the covers this run placed their blurhashes, before anyone asks.

        Jellyfin does the same at the end of a scan (LibraryManager.UpdateImagesAsync
        via ImageNeedsRefresh): hash what has none yet, store it on the image record,
        and let serving be a pure lookup. Never fatal - a run that organized files
        correctly must not be reported as failed because a cover would not decode.
        """
        if self._image_hashes is None:
            return
        try:
            await self._image_hashes.backfill()
        except Exception:  # noqa: BLE001 - decorative work, never fails a run
            logger.warning(
                "Could not hash artwork after management job %s", job_id, exc_info=True
            )

    async def _compact_records(self, job_id: str) -> None:
        """Drop the artwork bytes this run no longer needs.

        A finished run's documents keep a full copy of every cover, twice per plan item
        and once per file in the import bundle. That was 2.67 GB of a 2.90 GB database.
        The bytes are only read while a run is executing, so the moment it reaches a
        terminal state they can go - see the store method for the full cross-check.
        """
        try:
            counts = await self._store.compact_finished_organization_records()
            if any(counts.values()):
                logger.info(
                    "Compacted organization records after %s: %s", job_id, counts
                )
        except Exception:  # noqa: BLE001 - housekeeping never fails a run
            logger.warning(
                "Could not compact records after management job %s",
                job_id,
                exc_info=True,
            )

    async def _run_apply(self, job_id: str, worker_id: str) -> dict:
        snapshot = await self._store.get_library_management_job_snapshot(job_id)
        if snapshot is None:
            raise ValidationError("The management apply snapshot is missing.")
        while True:
            controlled = await self._store.checkpoint_operation_control(
                job_id, worker_id, now=time.time()
            )
            if controlled is not None and controlled["state"] != "running":
                return controlled
            # F-105: renew the 60 s operation lease once per bundle so a
            # concurrent lease reaper can never yank a long apply mid-flight.
            await self._renew_lease(job_id, worker_id)
            work = await self._store.claim_operation_work(
                job_id, worker_id, now=time.time()
            )
            if work is None:
                finished = await self._store.finish_library_management_apply(
                    job_id, worker_id, now=time.time()
                )
                await self._hash_artwork(job_id)
                await self._compact_records(job_id)
                return finished
            ordinal = int(work["ordinal"])
            try:
                items = (
                    await self._store.get_library_management_bundle_plan_items(
                        job_id, ordinal
                    )
                    if snapshot.mode == "duplicate_resolution"
                    else []
                )
                if (
                    snapshot.mode == "duplicate_resolution"
                    and items
                    and all(
                        json.loads(item.diff_json)
                        .get("duplicate_resolution", {})
                        .get("action")
                        == "keep_existing"
                        for item in items
                    )
                ):
                    await self._store.complete_operation_work(
                        job_id,
                        ordinal,
                        worker_id=worker_id,
                        expected_work_revision=int(work["row_revision"]),
                        state="succeeded",
                        result_json=json.dumps(
                            {"resolution": "kept_existing", "filesystem_writes": 0},
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        failure_code=None,
                        completed_at=time.time(),
                    )
                    continue
                await self._renew_lease(job_id, worker_id)
                await self._publisher.publish_bundle(job_id, ordinal, worker_id)
            except (
                StaleRevisionError,
                LibraryManagementDestinationConflictError,
                ConflictError,
            ) as error:
                current = await self._store.get_operation_work_item(job_id, ordinal)
                if current is not None and current["state"] == "succeeded":
                    continue
                if isinstance(error, StaleRevisionError):
                    failure_code = "STALE_INPUT"
                    result_json = json.dumps(
                        {"reason": str(error)}, separators=(",", ":"), sort_keys=True
                    )
                elif isinstance(error, LibraryManagementDestinationConflictError):
                    failure_code = "STALE_DESTINATION"
                    result_json = json.dumps(
                        {"reason": str(error)}, separators=(",", ":"), sort_keys=True
                    )
                else:
                    failure_code = "PUBLICATION_CONFLICT"
                    result_json = json.dumps(
                        {
                            "conflict_type": type(error).__name__,
                            "reason": str(error),
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                logger.warning(
                    "Library management publication rejected "
                    "job_id=%s bundle_ordinal=%s conflict_type=%s reason=%s",
                    job_id,
                    ordinal,
                    type(error).__name__,
                    str(error),
                )
                await self._store.complete_operation_work(
                    job_id,
                    ordinal,
                    worker_id=worker_id,
                    expected_work_revision=int(work["row_revision"]),
                    state="skipped",
                    result_json=result_json,
                    failure_code=failure_code,
                    completed_at=time.time(),
                )
            except (ValidationError, OSError) as error:
                current = await self._store.get_operation_work_item(job_id, ordinal)
                if current is not None and current["state"] == "succeeded":
                    continue
                logger.error(
                    "Library management publication failed "
                    "job_id=%s bundle_ordinal=%s failure_type=%s reason=%s",
                    job_id,
                    ordinal,
                    type(error).__name__,
                    str(error),
                    exc_info=True,
                )
                await self._store.complete_operation_work(
                    job_id,
                    ordinal,
                    worker_id=worker_id,
                    expected_work_revision=int(work["row_revision"]),
                    state="failed",
                    result_json=None,
                    failure_code="PUBLICATION_FAILED",
                    completed_at=time.time(),
                )
            except Exception as error:
                # F-107: an unclassified failure must still terminate the work
                # row durably; otherwise a deterministic poison bundle requeues
                # forever with no administrator-visible outcome.
                current = await self._store.get_operation_work_item(job_id, ordinal)
                if current is not None and current["state"] == "succeeded":
                    continue
                logger.error(
                    "Library management publication failed unexpectedly "
                    "job_id=%s bundle_ordinal=%s failure_type=%s reason=%s",
                    job_id,
                    ordinal,
                    type(error).__name__,
                    str(error),
                    exc_info=True,
                )
                try:
                    await self._store.complete_operation_work(
                        job_id,
                        ordinal,
                        worker_id=worker_id,
                        expected_work_revision=int(work["row_revision"]),
                        state="failed",
                        result_json=json.dumps(
                            {
                                "failure_type": type(error).__name__,
                                "reason": str(error),
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        failure_code="PUBLICATION_FAILED",
                        completed_at=time.time(),
                    )
                except Exception:  # noqa: BLE001 - marking must not mask the cause
                    logger.exception(
                        "Library management failed-work marking itself failed "
                        "for %s/%s",
                        job_id,
                        ordinal,
                    )
                    raise

    async def _renew_lease(self, job_id: str, worker_id: str) -> None:
        """F-105: treat a failed heartbeat as loss-of-lease for this applier."""

        renewed = await self._store.heartbeat_operation_job(
            job_id,
            worker_id,
            now=time.time(),
            lease_seconds=LEASE_SECONDS,
        )
        if not renewed:
            raise StaleRevisionError(
                "The Library Management operation lease changed before completion."
            )
