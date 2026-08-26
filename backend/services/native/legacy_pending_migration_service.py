"""Re-migrate legacy catalog rows left pending after the automatic upgrade."""

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
        run_id = f"{PENDING_RUN_PREFIX}-{self._resolver_getter().policy_revision}"
        if await self._store.get_migration_run_state(run_id) == "completed":
            return None
        return run_id

    async def _run(self, run_id: str) -> None:
        try:
            resolver = self._resolver_getter()
            outcome = await BoundedLegacyCatalogMigrator(
                self._store,
                resolver,
                emit_progress=lambda message: logger.info(
                    "legacy_pending_migration %s", message
                ),
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
