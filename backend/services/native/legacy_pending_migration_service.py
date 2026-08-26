"""Re-migrate legacy catalog rows left pending after the automatic upgrade.

F6/H6 trigger model. There is NO periodic scheduler BY DESIGN (owner decision
in the lenient-upgrade posture; NEW-MIG-01 explicitly declined a second
supervisor). Pending rows are retried only when one of these triggers fires:

1. Target startup - ``backend/target_application.py`` schedules once during
   the operational-runtime lifespan stage, gated on ``library_enabled()`` and
   exception-isolated so a scheduling failure never blocks startup.
2. ``PUT /api/v1/settings/library`` (``library_policies_target.py``) - a
   saved roots change schedules when the saved settings are enabled
   (``response.enabled``).
3. ``POST /api/v1/settings/library/restore-roots`` (same module) - a root
   restore schedules under the same ``response.enabled`` gate.

The fourth site named by the migration audit (the legacy-app schedule route)
is dead code since the legacy composition removal (audit DR-3); only the
three target-app triggers above exist. Consequence: a legacy row arriving
after a run's final revision check waits for the NEXT trigger - latency is
accepted. Gate correctness lives in ``_due_run_id``: durable completed marker,
nonzero pending counts, and composite run id
``legacy-pending-<policy_revision>-<source_revision>`` not already completed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from core.task_registry import TaskRegistry
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.bounded_legacy_catalog_migrator import (
    BoundedLegacyCatalogMigrator,
)
from services.native.library_policy_resolver import LibraryPolicyResolver

logger = logging.getLogger(__name__)

PENDING_RUN_PREFIX = "legacy-pending"


def pending_run_id(policy_revision: str, source_revision: str) -> str:
    """Durable pending-run identity: policy revision plus pending INPUT revision.

    NEW-MIG-01: gating on the policy revision alone let a completed run suppress
    every later schedule whenever new legacy rows arrived under an unchanged
    policy. Keying the ID on the bounded legacy source revision makes an
    unchanged input idempotently skipped while any new pending input yields a
    fresh run."""
    return f"{PENDING_RUN_PREFIX}-{policy_revision}-{source_revision}"



class LegacyPendingMigrationService:
    """Migrate legacy rows that became resolvable after the upgrade cutover."""

    def __init__(
        self,
        store: NativeLibraryStore,
        resolver_getter: Callable[[], LibraryPolicyResolver],
    ) -> None:
        self._store = store
        self._resolver_getter = resolver_getter
        self._running = False

    async def schedule(self) -> bool:
        """Start a pending migration task when one is due; False when skipped."""
        if self._running:
            return False
        self._running = True
        try:
            run_id = await self._due_run_id()
        except Exception:
            self._running = False
            raise
        if run_id is None:
            self._running = False
            return False
        task = asyncio.create_task(self._run(run_id))
        task.add_done_callback(_log_task_error)
        TaskRegistry.get_instance().register("legacy-pending-migration", task)
        return True

    async def _due_run_id(self) -> str | None:
        if not await self._store.has_completed_legacy_migration_marker():
            return None
        counts = await self._store.get_pending_legacy_counts()
        if not any(value > 0 for value in counts.values()):
            return None
        # NEW-MIG-01: include the bounded pending-input revision in the identity.
        # The migrator's own source/root revision checks remain the final
        # authority; this gate only decides whether a task may start.
        source_revision = await self._store.get_bounded_legacy_source_revision()
        run_id = pending_run_id(
            self._resolver_getter().policy_revision, source_revision
        )
        if await self._store.get_migration_run_state(run_id) == "completed":
            return None
        return run_id

    async def _pending_path_projector(self) -> Callable[[str], str] | None:
        """NEW-MIG-03: re-prove moved legacy paths against the CURRENT typed
        settings right before a pending migration, using the same all-row proof
        rules as the cutover reconciler. Returns a projector only for a verified
        ``remapped`` result; every other mode (including reconciler failure)
        stays unprojected so unverifiable rows remain pending."""
        try:
            from services.native.legacy_path_reconciler import LegacyPathReconciler

            reconciler = LegacyPathReconciler(
                self._store, self._resolver_getter().settings
            )
            try:
                reconciliation = await reconciler.reconcile()
            finally:
                await reconciler.aclose()
        except Exception:  # noqa: BLE001 - a failed proof must not block lenient retry
            logger.exception("legacy_pending_migration path reconciliation failed")
            return None
        if reconciliation.mode == "remapped":
            logger.info(
                "legacy_pending_migration path_reconciled mode=remapped "
                "library_files=%d review_rows=%d",
                reconciliation.library_file_count,
                reconciliation.review_row_count,
            )
            return reconciliation.project
        if reconciliation.mode != "unchanged":
            logger.info(
                "legacy_pending_migration path_reconciled mode=%s "
                "reason=%s",
                reconciliation.mode,
                reconciliation.failure_reason,
            )
        return None

    async def _run(self, run_id: str) -> None:
        try:
            resolver = self._resolver_getter()
            path_projector = await self._pending_path_projector()
            outcome = await BoundedLegacyCatalogMigrator(
                self._store,
                resolver,
                emit_progress=lambda message: logger.info(
                    "legacy_pending_migration %s", message
                ),
                path_projector=path_projector,
                skip_unmappable_paths=True,
            ).migrate_pending(run_id)
            skipped = (
                ", ".join(
                    f"{kind}={count}"
                    for kind, count in sorted(outcome.skipped_counts.items())
                )
                or "none"
            )
            logger.info(
                "legacy_pending_migration completed run=%s blockers=%d skipped=%s",
                run_id,
                outcome.blocker_count,
                skipped,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("legacy_pending_migration failed run=%s", run_id)
        finally:
            self._running = False


def _log_task_error(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("Legacy pending migration task failed: %s", error, exc_info=error)
