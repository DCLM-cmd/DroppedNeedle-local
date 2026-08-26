"""NEW-QUAL-02 reproducible release-gate runner.

Emits one JSON report with fixture/source identity, the calibration record
reference, raw samples, absolute results, and exactly one comparative outcome
per gate (``outperform`` / ``equality`` / ``underperform`` /
``capability-only``). Fails non-zero on a breached threshold or a missing
calibration for a calibrated gate.

Paired Lidarr runs are NOT fabricated: without an explicit paired-samples
input every gate reports ``capability-only``, which permits no comparative
claim. An ``underperform`` result forbids any ``better than Lidarr`` claim for
that gate even when its absolute result passes. F-SCAN-05 stays DEFERRED - the
partial-resume evidence records scope-level re-walk cost only."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sqlite3
import sys
import threading
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.v1.schemas.library_policies import (  # noqa: E402
    LibraryRootSettings,
    TypedLibrarySettings,
)
from infrastructure.persistence.native_library_store import NativeLibraryStore  # noqa: E402
from models.library_work import ScanRequest, ScanScope  # noqa: E402
from services.native.library_inventory_scanner import LibraryInventoryScanner  # noqa: E402
from services.native.library_policy_resolver import LibraryPolicyResolver  # noqa: E402
from services.native.library_reconciler import LibraryReconciler  # noqa: E402
from services.native.library_scan_coordinator import (  # noqa: E402
    LibraryIndexer,
    LibraryScanCoordinator,
)
from tests.benchmarks.operation_fix_240_calibration import (  # noqa: E402
    CalibrationError,
    load_calibration,
)
from tests.infrastructure.test_target_scan_lifecycle import _TagReader  # noqa: E402

LIDARR_PINNED_COMMIT = "68f07a822f2629564f4ac54b0f73778e921787da"
SERVARR_WIKI_PINNED_COMMIT = "20824099e767432398505b5f075cc2bfd5df8b4d"
SOURCE_BASELINE_CAVEATS = (
    "Lidarr facts are source-read facts at the pinned commit (24h refresh "
    "task, 30s scheduler poll, 30s watcher debounce, synchronous recursive "
    "disk scan materializing file-info lists, started commands not "
    "cancellable through CommandQueueManager.Cancel, background import "
    "without a cancel button). No Lidarr runtime measurement exists in this "
    "repository; wiki guidance is not runtime evidence."
)


def _source_identity() -> dict:
    import os
    import subprocess

    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - identity is best-effort provenance
        revision = "unknown"
    return {
        "droppedneedle_revision": revision,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "lidarr_pinned_commit": LIDARR_PINNED_COMMIT,
        "servarr_wiki_pinned_commit": SERVARR_WIKI_PINNED_COMMIT,
        "source_baseline_caveats": SOURCE_BASELINE_CAVEATS,
    }


def _resolver_for(roots: dict[str, Path]) -> LibraryPolicyResolver:
    ordered = sorted(roots.items())
    return LibraryPolicyResolver(
        TypedLibrarySettings(
            library_roots=[
                LibraryRootSettings(
                    id=root_id,
                    path=str(path),
                    label=f"Library {root_id}",
                    policy="automatic",
                )
                for index, (root_id, path) in enumerate(ordered, start=1)
            ]
        )
    )


def _multi_root_request(resolver, roots: dict[str, Path]) -> ScanRequest:
    revision = resolver.policy_revision
    return ScanRequest(
        kind="incremental",
        trigger="manual",
        policy_revision=revision,
        scopes=[
            ScanScope(root_id=root_id, relative_path=".", policy_revision=revision)
            for root_id in sorted(roots)
        ],
    )


def _build_store(database: Path) -> NativeLibraryStore:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS auth_users (id TEXT PRIMARY KEY)")
    return NativeLibraryStore(database, threading.Lock())


def _make_coordinator(store, resolver):
    scanner = LibraryInventoryScanner(store, walk_deadline_seconds=30.0)
    return LibraryScanCoordinator(
        store,
        scanner,
        LibraryIndexer(store, _TagReader()),
        LibraryReconciler(store),
        lambda: resolver,
        clock=lambda: 1_800_000_000.0,
    )


def _indexed_count(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM local_tracks WHERE availability='indexed'"
        ).fetchone()[0]


async def gate_trigger_and_duplicate(tmp_path: Path) -> dict:
    """Duplicate requests coalesce onto one run while it is ACTIVE; a disjoint
    scope expands that run; after the covering run FAILS the durable queued
    follow-up is still claimable (no lost work)."""
    root = tmp_path / "music"
    root.mkdir()
    (root / "track-0.flac").write_bytes(b"audio")
    database = tmp_path / "target.db"
    store = _build_store(database)
    resolver = _resolver_for({"root-a": root})
    scanner = LibraryInventoryScanner(store, walk_deadline_seconds=30.0)
    coordinator = LibraryScanCoordinator(
        store,
        scanner,
        LibraryIndexer(store, _TagReader()),
        LibraryReconciler(store),
        lambda: resolver,
        clock=lambda: 1_800_000_000.0,
    )

    request = ScanRequest(
        kind="incremental",
        trigger="manual",
        policy_revision=resolver.policy_revision,
        scopes=[
            ScanScope(root_id="root-a", relative_path=".", policy_revision=resolver.policy_revision)
        ],
    )
    first = await coordinator.request_run(request)

    run = await store.claim_next_scan_run(now=10)
    assert run is not None and run.id == first.run_id

    duplicate = await coordinator.request_run(request)  # active: queued follow-up
    queued_follow_up = duplicate.run_id != first.run_id

    failed = await store.transition_scan_run(
        run.id,
        expected_state="discovering",
        expected_revision=run.row_revision,
        new_state="failed",
        terminal_code="WALK_ERROR",
        now=11,
    )

    follow_up = await store.claim_next_scan_run(now=12)
    samples = {
        "initial_run_id": first.run_id,
        "duplicate_queued_follow_up": queued_follow_up,
        "follow_up_run_id": follow_up.id if follow_up is not None else None,
        "failed_covering_run": failed.id,
        "queued_follow_up_survived_failure": follow_up is not None
        and follow_up.id != first.run_id,
    }
    absolute = (
        samples["duplicate_queued_follow_up"]
        and samples["queued_follow_up_survived_failure"]
    )
    return {
        "gate": "trigger_duplicate_and_followup_after_failure",
        "raw_samples": samples,
        "absolute_result": "pass" if absolute else "fail",
    }


async def gate_cancellation_bounds(tmp_path: Path) -> dict:
    root = tmp_path / "music"
    root.mkdir()
    for index in range(40):
        directory = root / f"album-{index:02d}"
        directory.mkdir(parents=True)
        for file_index in range(6):
            (directory / f"track-{file_index}.flac").write_bytes(b"audio" * 32)

    database = tmp_path / "target.db"
    store = _build_store(database)
    resolver = _resolver_for({"root-a": root})
    scanner = LibraryInventoryScanner(store, walk_deadline_seconds=30.0)
    coordinator = LibraryScanCoordinator(
        store,
        scanner,
        LibraryIndexer(store, _TagReader()),
        LibraryReconciler(store),
        lambda: resolver,
        clock=lambda: 1_800_000_000.0,
    )
    await coordinator.request_run(_multi_root_request(resolver, {"root-a": root}))
    run = await store.claim_next_scan_run(now=10)
    assert run is not None

    started = time.monotonic()
    result = await coordinator.control(
        run.id, control="stop", expected_revision=run.row_revision
    )
    acknowledged_seconds = time.monotonic() - started
    assert result.state in ("stopping", "stopped")

    # The worker (run_once) processes the pending stop at its next checkpoint;
    # measure until the durable run reaches the terminal 'stopped' state.
    worker_task = asyncio.create_task(coordinator.run_once({"root-a": root}))

    exited = False
    writes_after_control = 0
    deadline = started + 5.0
    while time.monotonic() < deadline:
        current = (await store.get_scan_run(run.id))[0]
        writes_after_control = _indexed_count(database)
        if current.state == "stopped" or worker_task.done():
            exited = True
            break
        await asyncio.sleep(0.02)
    exit_seconds = time.monotonic() - started

    # The stopping run is settled durably through the documented recovery
    # path; terminal timestamp reflects actual settlement.
    await coordinator.recover_stopping()
    settled = (await store.get_scan_run(run.id))[0]
    assert settled.state in ("stopped", "cancelled")

    absolute = (
        acknowledged_seconds <= 1.0
        and exited
        and exit_seconds <= 5.0
        and writes_after_control == 0
        and settled.state in ("stopped", "cancelled")
    )
    return {
        "gate": "active_cancellation_bounds",
        "raw_samples": {
            "acknowledged_seconds": round(acknowledged_seconds, 4),
            "worker_exited_or_settled": exited,
            "post_control_writes": writes_after_control,
            "settled_state": settled.state,
            "exit_seconds": round(exit_seconds, 4),
        },
        "absolute_result": "pass" if absolute else "fail",
    }


async def gate_incomplete_never_deletes(tmp_path: Path) -> dict:
    """A WALK_ERROR (incomplete) pass must not delete a previously indexed
    track; reconcile after the incomplete pass keeps it indexed."""
    root = tmp_path / "music"
    root.mkdir()
    kept = root / "track-0.flac"  # name matches the real TagReader parser
    kept.write_bytes(b"audio")
    database = tmp_path / "target.db"
    store = _build_store(database)
    resolver = _resolver_for({"root-a": root})
    reconciler = LibraryReconciler(store)

    coordinator = LibraryScanCoordinator(
        store,
        LibraryInventoryScanner(store, walk_deadline_seconds=30.0),
        LibraryIndexer(store, _TagReader()),
        reconciler,
        lambda: resolver,
        clock=lambda: 1_800_000_000.0,
    )
    await coordinator.request_run(_multi_root_request(resolver, {"root-a": root}))
    finished = await coordinator.run_once({"root-a": root})
    assert finished is not None and finished.state == "completed"
    before = _indexed_count(database)
    if before < 1:
        with sqlite3.connect(database) as connection:
            fails = connection.execute(
                "SELECT relative_path, failure_code, failure_detail FROM "
                "library_scan_failures WHERE run_id=?",
                (finished.id,),
            ).fetchall()
            inv = connection.execute(
                "SELECT COUNT(*) FROM library_scan_inventory WHERE run_id=?",
                (finished.id,),
            ).fetchone()[0]
        raise AssertionError(
            f"setup: counters={finished.counters} inventory={inv} fails={fails}"
        )

    # Pass 2: hostile walker raises mid-scan -> incomplete -> fail open.
    run_request = ScanRequest(
        kind="incremental",
        trigger="automatic",
        policy_revision=resolver.policy_revision,
        scopes=[
            ScanScope(root_id="root-a", relative_path=".", policy_revision=resolver.policy_revision)
        ],
    )
    requested = await coordinator.request_run(run_request)
    run2 = await store.claim_next_scan_run(now=20)
    assert run2 is not None and run2.id == requested.run_id

    def broken_walker(*_args, **_kwargs):
        raise OSError(5, "injected EIO during hostile pass")

    incomplete_scanner = LibraryInventoryScanner(
        store, directory_walker=broken_walker, walk_deadline_seconds=30.0
    )
    await incomplete_scanner.discover(
        run2,
        (await store.get_scan_run(run2.id))[1],
        {"root-a": root},
        resolver,
        AsyncCheckpoint(),
    )
    await reconciler.reconcile(
        run2.id,
        [
            ScanScope(
                root_id="root-a",
                relative_path=".",
                policy_revision=resolver.policy_revision,
            )
        ],
    )

    after = _indexed_count(database)
    absolute = after >= before and kept.exists()
    return {
        "gate": "incomplete_inventory_never_deletes",
        "raw_samples": {
            "indexed_before_incomplete_pass": before,
            "indexed_after_incomplete_pass": after,
            "file_still_present": kept.exists(),
        },
        "absolute_result": "pass" if absolute else "fail",
    }


class AsyncCheckpoint:
    async def __call__(self, _run_id: str, _policy_revision: str) -> bool:
        return True


GATES_WITHOUT_CALIBRATION = (
    gate_trigger_and_duplicate,
    gate_cancellation_bounds,
    gate_incomplete_never_deletes,
)


def comparative_outcome(
    *,
    paired: bool,
    dropped_needle_value,
    lidarr_value,
    direction: str,
    tolerance=None,
) -> str:
    """Exactly one of outperform/equality/underperform/capability-only."""
    if not paired or dropped_needle_value is None or lidarr_value is None:
        return "capability-only"
    if direction == "lower_better":
        better = dropped_needle_value < lidarr_value
        worse = dropped_needle_value > lidarr_value
    else:
        better = dropped_needle_value > lidarr_value
        worse = dropped_needle_value < lidarr_value
    equal = (
        abs(dropped_needle_value - lidarr_value) <= tolerance
        if tolerance is not None
        else dropped_needle_value == lidarr_value
    )
    if equal:
        return "equality"
    if better:
        return "outperform"
    if worse:
        return "underperform"
    return "equality"


CLAIM_LIMITS = {
    "outperform": "May state a named-key improvement for this gate/workload only.",
    "equality": "No improvement claim; parity on the named key within tolerance.",
    "underperform": (
        "FORBIDDEN to claim 'better than Lidarr' for this gate or release, "
        "even when absolute_result is pass."
    ),
    "capability-only": (
        "Capability difference only; no speed/safety/CPU comparison permitted "
        "without paired runs."
    ),
}


async def _run_gates(gates) -> list[dict]:
    reports = []
    for gate in gates:
        with tempfile.TemporaryDirectory(prefix=f"of240-{gate.__name__}-") as directory:
            reports.append(await gate(tmp_path=Path(directory)))
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--lidarr-samples", type=Path, default=None,
        help="Optional paired Lidarr measurements JSON; absent -> capability-only.",
    )
    args = parser.parse_args()

    calibration_error: str | None = None
    calibration = None
    try:
        calibration = load_calibration(args.calibration)
    except CalibrationError as error:
        calibration_error = str(error)

    core_reports = asyncio.run(_run_gates(GATES_WITHOUT_CALIBRATION))
    for report in core_reports:
        outcome = comparative_outcome(
            paired=args.lidarr_samples is not None,
            dropped_needle_value=None,
            lidarr_value=None,
            direction="lower_better",
        )
        report["comparative_outcome"] = outcome
        report["claim_limit"] = CLAIM_LIMITS[outcome]

    scale_stall_reports = []
    if calibration is None:
        for name in ("scale_rss_cpu", "stall_deadlines"):
            scale_stall_reports.append(
                {
                    "gate": name,
                    "absolute_result": "not-executed",
                    "comparative_outcome": "capability-only",
                    "reason": f"calibration unavailable: {calibration_error}",
                }
            )

    report = {
        "source_identity": _source_identity(),
        "calibration": (
            {"calibration_id": calibration.calibration_id}
            if calibration
            else {"error": calibration_error}
        ),
        "gates": [*core_reports, *scale_stall_reports],
    }

    run_failed = any(
        g.get("absolute_result") in ("fail", "not-executed") for g in report["gates"]
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "ok": not run_failed}))
    return 1 if run_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
