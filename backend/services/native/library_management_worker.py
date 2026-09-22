"""Dispatch claimed Library Management modes through the durable supervisor."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

import msgspec

from core.exceptions import (
    ConfigurationError,
    ConflictError,
    LibraryManagementDestinationConflictError,
    ResourceNotFoundError,
    StaleRevisionError,
    ValidationError,
)
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.library_filesystem_coordinator import (
    LibraryFilesystemCoordinator,
)
from models.library_management import LibraryManagementJobSnapshot
from models.library_management_planning import (
    LibraryManagementSelection,
    NormalizedLibraryManagementSelection,
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

if TYPE_CHECKING:
    from services.native.automatic_scan_management_service import (
        AutomaticScanManagementService,
    )

logger = logging.getLogger(__name__)

# Slice C (stale-storm fix): cap on stale requeues per scan_discovered lineage.
# Replacements spawn only after settle, so a handful of generations covers a
# flapping catalog; beyond that the lineage terminal-fails honestly.
MAX_SCAN_PREVIEW_STALE_REQUEUES = 3

# Terminal code for a scan_discovered lineage that kept going stale past the
# cap. Distinct from STALE_INPUT so history shows the attempts ran out.
STALE_INPUT_RETRIES_EXHAUSTED = "STALE_INPUT_RETRIES_EXHAUSTED"

# Generation suffix chained onto the stale job's idempotency key. The cap
# counter rides the job row's existing idempotency_key column, so replacement
# creation stays idempotent per generation with no schema change.
_STALE_RETRY_KEY_SUFFIX = ":stale-retry:"


def _stale_retry_attempt(idempotency_key: str) -> int:
    """Generation of a stale-retry lineage encoded on the job row's key."""
    _, separator, tail = idempotency_key.rpartition(_STALE_RETRY_KEY_SUFFIX)
    if not separator or not tail.isdigit():
        return 0
    return int(tail)


def _stale_scan_preview_rebuild(
    snapshot: LibraryManagementJobSnapshot,
    *,
    idempotency_key: str,
    attempt: int,
    actor_user_id: str | None,
) -> dict | None:
    """Rebuild kwargs for a faithful scan_discovered replacement, or None.

    Faithful means origin=scan_discovered with the same selection. Anything
    that cannot carry over exactly (tag-edit intent, a non-album scope, an
    undecodable selection or pinned profile) returns None so the caller
    terminal-fails as before instead of requeueing a mangled preview.
    """
    if (snapshot.intent_json or "").strip() not in ("{}", "", "null"):
        return None
    if not snapshot.selection_json or not snapshot.profile_snapshot_json:
        return None
    try:
        normalized = msgspec.json.decode(
            snapshot.selection_json.encode("utf-8"),
            type=NormalizedLibraryManagementSelection,
        )
    except (msgspec.DecodeError, msgspec.ValidationError):
        return None
    if normalized.kind != "albums" or not normalized.ids or normalized.root_scopes:
        return None
    try:
        profile_doc = json.loads(snapshot.profile_snapshot_json)
        profile_id = profile_doc.get("profile", {}).get("id")
    except (ValueError, AttributeError):
        return None
    if not isinstance(profile_id, str) or not profile_id:
        return None
    base, separator, tail = idempotency_key.rpartition(_STALE_RETRY_KEY_SUFFIX)
    base_key = base if separator and tail.isdigit() else idempotency_key
    return {
        "selection": LibraryManagementSelection(
            kind="albums",
            ids=tuple(normalized.ids),
            catalog_filter=normalized.catalog_filter,
        ),
        "profile_id": profile_id,
        "expected_settings_revision": snapshot.settings_revision,
        "expected_policy_revision": snapshot.policy_revision,
        "actor_user_id": actor_user_id,
        "idempotency_key": f"{base_key}{_STALE_RETRY_KEY_SUFFIX}{attempt + 1}",
        "origin": "scan_discovered",
        "target_root_id": snapshot.target_root_id,
    }


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
        *,
        scan_settle_gate: AutomaticScanManagementService | None = None,
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
        self._scan_settle_gate = scan_settle_gate

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
            return await self._finish_stale_preview(job_id, worker_id, snapshot)
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

    async def _finish_stale_preview(
        self,
        job_id: str,
        worker_id: str,
        snapshot: LibraryManagementJobSnapshot,
    ) -> dict:
        """Stale-input terminal path for preview planning.

        A scan_discovered preview whose inputs moved is rebuilt with a fresh
        snapshot after the catalog settles instead of terminal-failing, up to
        MAX_SCAN_PREVIEW_STALE_REQUEUES generations. Manual previews never
        auto-reissue: explicit user intent terminal-fails exactly as before.
        """
        if snapshot.origin == "scan_discovered":
            requeued = await self._maybe_requeue_stale_scan_preview(
                job_id, worker_id, snapshot
            )
            if requeued is not None:
                return requeued
        return await self._store.finish_operation_job(
            job_id,
            worker_id,
            state="failed",
            terminal_code="STALE_INPUT",
            now=time.time(),
        )

    async def _maybe_requeue_stale_scan_preview(
        self,
        job_id: str,
        worker_id: str,
        snapshot: LibraryManagementJobSnapshot,
    ) -> dict | None:
        """Queue a fresh replacement for a stale scan preview, or None.

        Returns the stale attempt's honest STALE_INPUT record after queueing
        its replacement, the EXHAUSTED record past the cap, or None when no
        requeue issues (caller terminal-fails as before). The existing
        reissue_preview_token mechanism does not fit: it re-derives the sealed
        token for a ready, non-stale preview and raises on stale input, while
        a stale preview needs fresh pinned revisions via create_preview.
        """
        operation = await self._store.get_operation_job(job_id)
        if operation is None:
            return None
        idempotency_key = operation.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            return None
        attempt = _stale_retry_attempt(idempotency_key)
        if attempt >= MAX_SCAN_PREVIEW_STALE_REQUEUES:
            logger.warning(
                "Library management stale scan preview exhausted job_id=%s attempts=%s",
                job_id,
                attempt,
            )
            return await self._store.finish_operation_job(
                job_id,
                worker_id,
                state="failed",
                terminal_code=STALE_INPUT_RETRIES_EXHAUSTED,
                now=time.time(),
            )
        rebuild = _stale_scan_preview_rebuild(
            snapshot,
            idempotency_key=idempotency_key,
            attempt=attempt,
            actor_user_id=operation.get("requested_by_user_id"),
        )
        if rebuild is None:
            return None
        if not await self._settle_gate_open():
            logger.info(
                "Library management stale scan preview waiting for settle "
                "job_id=%s attempt=%s",
                job_id,
                attempt,
            )
            return None
        try:
            handle = await self._planner.create_preview(**rebuild)
        except (
            StaleRevisionError,
            ValidationError,
            ConfigurationError,
            ResourceNotFoundError,
        ) as error:
            # The profile is gone, or settings/policy/catalog raced the
            # rebuild: terminal, no requeue. The spawn flow recreates once
            # inputs are identifiable again.
            logger.info(
                "Library management stale scan preview rebuild refused "
                "job_id=%s attempt=%s reason=%s",
                job_id,
                attempt,
                type(error).__name__,
            )
            return None
        logger.info(
            "Library management stale scan preview requeued "
            "job_id=%s attempt=%s replacement_job_id=%s",
            job_id,
            attempt + 1,
            handle.job_id,
        )
        return await self._store.finish_operation_job(
            job_id,
            worker_id,
            state="failed",
            terminal_code="STALE_INPUT",
            now=time.time(),
        )

    async def _settle_gate_open(self) -> bool:
        """Debounce stale requeues on the spawn gate's settle notion.

        Reuses AutomaticScanManagementService.scan_preview_settled exactly;
        the worker defines no second notion of settle. Unwired workers share
        the process singleton (warm memo); tests inject a fake gate.
        """
        gate = self._scan_settle_gate
        if gate is None:
            from core.dependencies.service_providers import (
                get_automatic_scan_management_service,
            )

            gate = get_automatic_scan_management_service()
        return await gate.scan_preview_settled()

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
            except Exception as error:  # noqa: BLE001 - F-107 poison bundle must terminate durably, not requeue
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
