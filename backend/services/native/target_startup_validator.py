"""Fail-closed validation for the separately started target-only application."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Literal

from core.exceptions import TargetStartupInvariantError
from infrastructure.persistence.native_library_store import NativeLibraryStore

logger = logging.getLogger(__name__)

TargetStartupValidationPhase = Literal["cutover", "admission", "steady_state"]


class TargetStartupValidator:
    def __init__(
        self,
        store: NativeLibraryStore,
        configured_root_ids: Callable[[], set[str]] | None = None,
        emit_progress: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._configured_root_ids = configured_root_ids
        self._emit_progress = emit_progress or (lambda _message: None)

    async def validate(self, phase: TargetStartupValidationPhase) -> dict[str, Any]:
        started = time.perf_counter()
        self._emit_progress("Validating the migration marker and catalog revision.")
        clause_started = time.perf_counter()
        state = await self._store.get_target_startup_state()
        marker = state["marker"]
        migration = state["migration"]
        logger.info(
            "target_startup.catalog_validation_clause phase=%s "
            "clause=startup_state_read elapsed_ms=%.3f checked_count=1 "
            "result_count=0",
            phase,
            (time.perf_counter() - clause_started) * 1000,
        )
        marker_missing = int(marker is None or migration is None)
        logger.info(
            "target_startup.catalog_validation_clause phase=%s "
            "clause=migration_marker_present elapsed_ms=0.000 checked_count=1 "
            "result_count=%d",
            phase,
            marker_missing,
        )
        if marker_missing:
            raise TargetStartupInvariantError(
                "The completed legacy-catalog migration marker is missing."
            )
        migration_mismatch = int(
            migration["state"] != "completed"
            or migration["source_revision"] != marker["source_revision"]
            or migration["completed_at"] is None
        )
        logger.info(
            "target_startup.catalog_validation_clause phase=%s "
            "clause=migration_marker_revision elapsed_ms=0.000 checked_count=1 "
            "result_count=%d",
            phase,
            migration_mismatch,
        )
        if migration_mismatch:
            raise TargetStartupInvariantError(
                "The migration run does not match the completed target marker."
            )
        target_revision_stale = int(
            int(marker["target_catalog_revision"]) > int(state["catalog_revision"])
        )
        logger.info(
            "target_startup.catalog_validation_clause phase=%s "
            "clause=target_catalog_revision elapsed_ms=0.000 checked_count=1 "
            "result_count=%d",
            phase,
            target_revision_stale,
        )
        if target_revision_stale:
            raise TargetStartupInvariantError(
                "The target catalog revision predates its migration marker."
            )
        self._emit_progress("Checking catalog integrity.")
        clause_started = time.perf_counter()
        invariants = await self._store.validate_catalog_integrity()
        logger.info(
            "target_startup.catalog_validation_clause phase=%s clause=catalog_integrity "
            "elapsed_ms=%.3f checked_count=%d result_count=%d",
            phase,
            (time.perf_counter() - clause_started) * 1000,
            len(invariants),
            sum(invariants.values()),
        )
        if phase in {"cutover", "admission"}:
            self._emit_progress("Checking all migrated saved references.")
            clause_started = time.perf_counter()
            unresolved_references = await self._store.validate_migration_references()
            logger.info(
                "target_startup.catalog_validation_clause phase=%s "
                "clause=migration_references elapsed_ms=%.3f checked_count=1 "
                "result_count=%d",
                phase,
                (time.perf_counter() - clause_started) * 1000,
                unresolved_references,
            )
            invariants = {
                **invariants,
                "unresolved_references": unresolved_references,
            }
        elif phase != "steady_state":
            raise ValueError(f"Unsupported target startup validation phase: {phase}")
        failures = {name: count for name, count in invariants.items() if count != 0}
        if failures:
            logger.error(
                "target_startup.catalog_integrity_failed phase=%s counters=%s",
                phase,
                ",".join(f"{name}={count}" for name, count in sorted(failures.items())),
            )
            raise TargetStartupInvariantError(
                "The target catalog failed startup integrity validation."
            )
        if phase in {"cutover", "admission"} and self._configured_root_ids is not None:
            self._emit_progress("Checking configured library roots.")
            clause_started = time.perf_counter()
            configured = self._configured_root_ids()
            migrated = await self._store.get_migrated_root_ids()
            root_mismatch = int(configured != migrated)
            logger.info(
                "target_startup.catalog_validation_clause phase=%s clause=library_roots "
                "elapsed_ms=%.3f checked_count=%d result_count=%d",
                phase,
                (time.perf_counter() - clause_started) * 1000,
                max(len(configured), len(migrated)),
                root_mismatch,
            )
            if configured != migrated:
                raise TargetStartupInvariantError(
                    "The configured library roots do not match the migrated catalog."
                )
        elapsed_seconds = time.perf_counter() - started
        logger.info(
            "target_startup.catalog_integrity_completed phase=%s elapsed_seconds=%.3f",
            phase,
            elapsed_seconds,
        )
        return {**state, "invariants": invariants}
