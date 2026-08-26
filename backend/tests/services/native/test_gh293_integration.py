"""GH-293 bounded real shared-file integration.

Exercises the ACTUAL production components together on one real SQLite file:
AuthStore, NativeLibraryStore, the coalesced bootstrap demand signal, the WAL
checkpoint service, the operation supervisor and one identity job of 500
subjects, while unauthenticated setup-status requests (through the real route
handler over ASGI) run concurrently. Owner-calibrated budgets are enforced:

- setup-status p95 <= 1 s, max <= 5 s, zero HTTP errors/timeouts
- active WAL stays within the 64 MiB high-water budget
- the job seals and completes with every materialized subject terminal
- no immediate reclaim hot loop: yields persist a cooldown, the driver waits on
  the timed wakeup, and a no-progress guard fails the test
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api.v1.routes.auth import router
from core.dependencies import service_providers
from core.dependencies.auth_providers import get_auth_service
from infrastructure.persistence._database import PriorityWriteLock, _fold_text
from infrastructure.persistence.auth_store import AuthStore
from infrastructure.persistence.gh293_calibration import (
    ACTIVE_WAL_HIGH_WATER_BYTES,
    SETUP_STATUS_SLO_MAX_SECONDS,
    SETUP_STATUS_SLO_P95_SECONDS,
)
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.auth_service import AuthService
from services.native.background_workload_gate import BackgroundWorkloadGate
from services.native.bootstrap_demand_signal import BootstrapDemandSignal
from services.native.catalog_identity_hygiene_service import (
    CatalogIdentityHygieneService,
)
from services.native.identity_repair_service import IdentityRepairService
from services.native.library_operation_service import LibraryOperationService
from services.native.library_operation_supervisor import LibraryOperationSupervisor
from services.native.wal_checkpoint_service import WalCheckpointService


class _CountingConnection(sqlite3.Connection):
    """Commit counter on the real shared SQLite connection."""

    def __init__(self, *args, counter: list[int] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._commit_counter = counter

    def commit(self):
        if self._commit_counter is not None:
            self._commit_counter[0] += 1
        return super().commit()


class _CountingStore(NativeLibraryStore):
    def __init__(self, *args, **kwargs):
        self.commit_counter: list[int] = [0]
        self.claim_counter: list[int] = [0]
        self.yield_counter: list[int] = [0]
        super().__init__(*args, **kwargs)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path, check_same_thread=False, factory=_CountingConnection
        )
        connection._commit_counter = self.commit_counter
        connection.row_factory = sqlite3.Row
        connection.create_function("fold", 1, _fold_text, deterministic=True)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    async def claim_operation_job(self, *args, **kwargs):
        result = await super().claim_operation_job(*args, **kwargs)
        if result is not None:
            self.claim_counter[0] += 1
        return result

    async def yield_operation_job(self, *args, **kwargs):
        self.yield_counter[0] += 1
        return await super().yield_operation_job(*args, **kwargs)


def _seed_catalog(path: Path, albums: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO local_artists (id, display_name, folded_name, kind, "
            "created_at, updated_at) VALUES ('artist-1', 'Artist', 'artist', 'group', 1, 1)"
        )
        connection.executemany(
            "INSERT INTO local_albums (id, root_id, grouping_key, title, title_folded, "
            "album_artist_id, grouping_source, created_at, updated_at) "
            "VALUES (?, 'root', ?, ?, ?, 'artist-1', 'automatic', 1, 1)",
            [
                (f"album-{i:06d}", f"fk-{i:06d}", f"Album {i:06d}", f"album {i:06d}")
                for i in range(albums)
            ],
        )


def _build_app(service: AuthService) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_auth_service] = lambda: service
    return app


@pytest.mark.asyncio
async def test_operation_worker_with_setup_status_flood_stays_within_budgets(
    tmp_path: Path,
) -> None:
    db = tmp_path / "library.db"
    subjects = 500
    # Real shared file: AuthStore creates the auth schema, the native store the
    # library schema; the catalog seed then inserts into tables both created.
    auth_store = AuthStore(db, threading.Lock())
    auth_service = AuthService(auth_store)
    lock = PriorityWriteLock()
    store = _CountingStore(db_path=db, write_lock=lock)
    _seed_catalog(db, subjects)

    signal = BootstrapDemandSignal()
    checkpoint = WalCheckpointService(db)
    gate = BackgroundWorkloadGate()
    on_changed = AsyncMock()
    hygiene = CatalogIdentityHygieneService(
        store, gate, on_changed, bootstrap_demand=signal,
        wal_checkpoint=checkpoint, clock=time.time,
    )
    operations = LibraryOperationService(store)
    repairs = IdentityRepairService(store)
    supervisor = LibraryOperationSupervisor(
        store,
        operations,
        repairs,
        AsyncMock(),
        workload_gate=gate,
        catalog_identity_hygiene=hygiene,
    )

    from models.library_work import OperationJob
    import uuid as _uuid

    job = OperationJob(
        id=str(_uuid.uuid4()),
        kind="repair",
        requested_by_user_id=None,
        input_catalog_revision=await store.get_catalog_revision(),
        idempotency_key=f"catalog-identity-hygiene:v1:backfill:{_uuid.uuid4().hex[:8]}",
        created_at=time.time(),
    )
    created = await store.create_repair_operation(
        job,
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    assert created["expected_work_count"] == subjects

    app = _build_app(auth_service)
    import core.dependencies.service_providers as _sp

    _sp.get_bootstrap_demand_signal = lambda: signal

    latencies: list[float] = []
    setup_errors = [0]
    wal_peaks = [0]

    async def flood() -> None:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://int") as client:
            while True:
                for _ in range(5):
                    started = time.monotonic()
                    try:
                        response = await client.get("/auth/setup/status")
                    except Exception:  # noqa: BLE001
                        setup_errors[0] += 1
                        break
                    latencies.append(time.monotonic() - started)
                    if response.status_code != 200:
                        setup_errors[0] += 1
                    if response.status_code >= 500:
                        raise AssertionError(
                            f"setup-status returned {response.status_code}"
                        )
                outcome = checkpoint.run_once()
                if outcome is not None:
                    wal_peaks[0] = max(
                        wal_peaks[0], int(outcome.get("active_bytes", 0) or 0)
                    )
                if done.is_set():
                    return
                await asyncio.sleep(0.02)

    done = asyncio.Event()
    work_wakeups = store.work_wakeups
    revision = work_wakeups.revision("operation")
    revision_moves = [0]
    started = time.monotonic()
    no_progress = 0
    last_completed = -1
    flooder = asyncio.create_task(flood())
    try:
        while True:
            await supervisor.recover()
            result = await supervisor.run_once("int-worker")
            if result is not None and result.state == "succeeded":
                break
            if result is not None:
                # A yield/requeue response: REFRESH the wakeup revision and wait
                # on the real timed wakeup (cooldown notify_after) so each yield
                # is proven to be a genuine timed wakeup, never an immediate
                # re-claim through a stale revision.
                current = work_wakeups.revision("operation")
                if current != revision:
                    revision_moves[0] += 1
                revision = current
                await work_wakeups.wait(
                    "operation", after_revision=revision, timeout_seconds=2.0
                )
                current = work_wakeups.revision("operation")
                if current != revision:
                    revision_moves[0] += 1
                revision = current
                continue
            # No claimable work: check the durable job state.
            job_row = await store.get_operation_job(created["id"])
            if job_row is not None and job_row["state"] in (
                "succeeded", "failed", "cancelled", "stopped",
            ):
                break
            completed = int(job_row["completed_count"]) if job_row else 0
            if completed == last_completed:
                no_progress += 1
            else:
                last_completed = completed
                no_progress = 0
            if no_progress > 50:
                raise AssertionError("no-progress guard exceeded (hot loop?)")
            if time.monotonic() - started > 120:
                raise AssertionError("integration wall budget exceeded")
            await asyncio.sleep(0.05)
    finally:
        done.set()
        await flooder

    elapsed = time.monotonic() - started
    final = await store.get_operation_job(created["id"])
    assert final is not None
    assert final["state"] == "succeeded", final

    with sqlite3.connect(db) as connection:
        terminal = connection.execute(
            "SELECT COUNT(*) FROM library_operation_work WHERE job_id = ? "
            "AND state IN ('succeeded','skipped','failed')",
            (created["id"],),
        ).fetchone()[0]
        pending = connection.execute(
            "SELECT COUNT(*) FROM library_operation_work WHERE job_id = ? "
            "AND state IN ('pending','running')",
            (created["id"],),
        ).fetchone()[0]
    assert terminal == subjects
    assert pending == 0

    assert setup_errors[0] == 0
    ordered = sorted(latencies)
    p95 = ordered[int(0.95 * len(ordered))] if ordered else 0.0
    maximum = max(latencies) if latencies else 0.0
    assert len(latencies) >= 20, "setup-status flood never ran"
    assert p95 <= SETUP_STATUS_SLO_P95_SECONDS, (p95, SETUP_STATUS_SLO_P95_SECONDS)
    assert maximum <= SETUP_STATUS_SLO_MAX_SECONDS, (maximum, SETUP_STATUS_SLO_MAX_SECONDS)
    assert wal_peaks[0] <= ACTIVE_WAL_HIGH_WATER_BYTES, wal_peaks[0]
    assert signal.active is False
    # Multiple revision increments prove the timed wakeups actually fired across
    # the yield cycles (a busy-wait driver would show zero moves).
    assert revision_moves[0] >= 3, revision_moves

    # Evidence metrics for the handoff.
    print(
        "integration_metrics "
        f"subjects={subjects} wall={elapsed:.2f}s "
        f"commits={store.commit_counter[0]} claims={store.claim_counter[0]} "
        f"yields={store.yield_counter[0]} "
        f"setup_samples={len(latencies)} p95={p95 * 1000:.2f}ms max={maximum * 1000:.2f}ms "
        f"setup_errors={setup_errors[0]} wal_active_peak={wal_peaks[0]} "
        f"revision_moves={revision_moves[0]}"
    )
    assert store.yield_counter[0] > 0, "timeslice/cooldown yields never exercised"
    assert store.claim_counter[0] > subjects / 100, "claims should be bounded and real"
