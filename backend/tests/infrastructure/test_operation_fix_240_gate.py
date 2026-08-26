"""NEW-QUAL-02 gate-runner contract tests.

The runner must fail closed on missing/rejected calibration, emit raw
samples plus exactly one comparative outcome per gate, treat
``underperform`` as forbidding any ``better than Lidarr`` claim, and use the
owner's frozen thresholds unchanged. No real network is touched."""

import json
from pathlib import Path

import pytest

from tests.benchmarks.operation_fix_240_calibration import (
    CalibrationError,
    load_calibration,
)
from tests.benchmarks.operation_fix_240_gate import (
    CLAIM_LIMITS,
    comparative_outcome,
)

REQUIRED = (
    "calibration_id", "threshold_owner", "decision_date",
    "fixture_manifest_hash", "host_profile", "storage_profile",
    "tool_versions", "sample_count", "rss_base_bytes",
    "rss_slope_bytes_per_file", "idle_cpu_limit_core_seconds_per_minute",
    "quiescent_window_seconds", "queue_limit", "batch_limit",
    "walk_deadline_ms", "scheduling_allowance_ms",
    "management_write_deadline_ms", "detached_worker_cap",
    "detached_drain_deadline_ms",
)


def _valid_record(tmp_path: Path) -> Path:
    record = {field: 1 for field in REQUIRED}
    record.update(
        {
            "calibration_id": "cal-test-001",
            "decision": "approved",
            "threshold_owner": "owner",
            "host_profile": "linux/amd64 test host",
            "storage_profile": "tmpfs",
            "tool_versions": {"python": "3.13"},
        }
    )
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(record))
    return path


def test_comparative_outcomes_are_exhaustive_and_mutually_exclusive():
    directions = ("lower_better", "higher_better")
    for direction in directions:
        seen = set()
        for dn, lidarr in ((5, 10), (10, 5), (7, 7)):
            seen.add(
                comparative_outcome(
                    paired=True,
                    dropped_needle_value=dn,
                    lidarr_value=lidarr,
                    direction=direction,
                )
            )
        assert seen <= {"outperform", "equality", "underperform"}
    # unpaired: capability-only, never an implicit win
    assert (
        comparative_outcome(
            paired=False, dropped_needle_value=1, lidarr_value=99,
            direction="lower_better",
        )
        == "capability-only"
    )


def test_underperform_forbids_better_than_lidarr_claim():
    outcome = comparative_outcome(
        paired=True,
        dropped_needle_value=120.0,
        lidarr_value=30.0,
        direction="lower_better",  # e.g. p95 latency ms: DN far worse
    )
    assert outcome == "underperform"
    limit = CLAIM_LIMITS[outcome]
    assert "FORBIDDEN" in limit and "better than Lidarr" in limit


def test_tolerance_enables_equality_for_continuous_metrics():
    outcome = comparative_outcome(
        paired=True,
        dropped_needle_value=1.04,
        lidarr_value=1.00,
        direction="lower_better",
        tolerance=0.05,
    )
    assert outcome == "equality"


def test_missing_calibration_file_fails_closed(tmp_path: Path):
    with pytest.raises(CalibrationError, match="missing"):
        load_calibration(tmp_path / "absent.json")


def test_rejected_or_incomplete_calibration_fails_closed(tmp_path: Path):
    record = _valid_record(tmp_path)
    payload = json.loads(record.read_text())
    payload["decision"] = "rejected"
    record.write_text(json.dumps(payload))
    with pytest.raises(CalibrationError, match="not 'approved'"):
        load_calibration(record)

    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"calibration_id": "x"}))
    with pytest.raises(CalibrationError, match="missing fields"):
        load_calibration(incomplete)


def test_approved_calibration_loads_with_frozen_thresholds(tmp_path: Path):
    path = _valid_record(tmp_path)
    calibration = load_calibration(path)
    assert calibration.approved is True
    assert calibration.limit("walk_deadline_ms") == 1.0


def test_fixture_manifest_mismatch_fails_closed(tmp_path: Path):
    fixture_root = tmp_path / "fixture"
    (fixture_root / "sub").mkdir(parents=True)
    (fixture_root / "sub" / "a.flac").write_bytes(b"x")

    from tests.benchmarks.operation_fix_240_calibration import (
        fixture_manifest_hash,
    )

    record_path = _valid_record(tmp_path)
    payload = json.loads(record_path.read_text())
    payload["fixture_manifest_hash"] = fixture_manifest_hash(fixture_root)
    record_path.write_text(json.dumps(payload))
    load_calibration(record_path, fixture_root=fixture_root)  # ok

    (fixture_root / "sub" / "b.flac").write_bytes(b"changed")
    with pytest.raises(CalibrationError, match="manifest hash mismatch"):
        load_calibration(record_path, fixture_root=fixture_root)


def _run_runner(tmp_path: Path, calibration_path: Path | None, monkeypatch) -> tuple[int, dict]:
    import subprocess
    import sys

    output = tmp_path / "report.json"
    command = [
        sys.executable,
        str(Path(__file__).parents[1] / "benchmarks" / "operation_fix_240_gate.py"),
        "--output", str(output),
    ]
    if calibration_path is not None:
        command += ["--calibration", str(calibration_path)]
    else:
        # the runner requires the flag; point it at a guaranteed-missing file
        command += ["--calibration", str(tmp_path / "absent.json")]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=180)
    report = json.loads(output.read_text()) if output.exists() else {}
    return proc.returncode, report


def test_runner_fails_closed_without_calibration(tmp_path: Path, monkeypatch) -> None:
    code, report = _run_runner(tmp_path, None, monkeypatch)
    assert code != 0
    by_name = {gate["gate"]: gate for gate in report.get("gates", [])}
    assert by_name["scale_rss_cpu"]["absolute_result"] == "not-executed"
    assert by_name["stall_deadlines"]["absolute_result"] == "not-executed"
    assert "calibration unavailable" in by_name["scale_rss_cpu"]["reason"]


def test_runner_reports_absolute_results_and_capability_only(
    tmp_path: Path, monkeypatch
) -> None:
    calibration = _valid_record(tmp_path)
    # The measured core gates do not consume calibration values; the approved
    # record authorizes the calibrated scale/stall gates in the runner.
    code, report = _run_runner(tmp_path, calibration, monkeypatch)

    gates = {gate["gate"]: gate for gate in report["gates"]}
    for name in (
        "trigger_duplicate_and_followup_after_failure",
        "active_cancellation_bounds",
        "incomplete_inventory_never_deletes",
    ):
        assert gates[name]["absolute_result"] == "pass"
        assert gates[name]["comparative_outcome"] == "capability-only"
        assert "raw_samples" in gates[name]
        assert gates[name]["claim_limit"].startswith("Capability difference only")


def test_report_carries_pinned_source_identity(tmp_path: Path):
    from tests.benchmarks.operation_fix_240_gate import (
        LIDARR_PINNED_COMMIT,
        SERVARR_WIKI_PINNED_COMMIT,
        _source_identity,
    )

    identity = _source_identity()
    assert identity["lidarr_pinned_commit"] == LIDARR_PINNED_COMMIT
    assert identity["servarr_wiki_pinned_commit"] == SERVARR_WIKI_PINNED_COMMIT
    assert identity["lidarr_pinned_commit"] == (
        "68f07a822f2629564f4ac54b0f73778e921787da"
    )
    assert "not runtime evidence" in identity["source_baseline_caveats"]
