"""GH-293 owner-approved calibration binding.

Every value here is the corpus-pinned calibration recorded by the owner on
2026-08-20 (GH-293 dossier + context/10). Changing a value without a new owner
decision is a contract violation: this test fails when the record is absent or a
different value is used. Implementation and qualification code MUST load these
exact values from infrastructure.persistence.gh293_calibration.
"""

from __future__ import annotations

from infrastructure.persistence import gh293_calibration as calibration


def test_calibration_record_is_present_and_pinned() -> None:
    assert calibration.MATERIALIZATION_PAGE_CAP == 500
    assert calibration.BACKGROUND_TIMESLICE_SECONDS == 0.25
    assert calibration.BACKGROUND_YIELD_COOLDOWN_SECONDS == 0.5
    assert calibration.PUBLIC_DEMAND_MAX_HOLD_SECONDS == 5.0
    assert calibration.FORCED_FAIRNESS_PROGRESS_FLOOR_SECONDS == 120.0
    assert calibration.ACTIVE_WAL_HIGH_WATER_BYTES == 64 * 1024 * 1024
    assert calibration.ACTIVE_WAL_LOW_WATER_BYTES == 16 * 1024 * 1024
    assert calibration.CHECKPOINT_CADENCE_SECONDS == 30.0
    assert calibration.CHECKPOINT_READER_BLOCKED_MAX_SECONDS == 60.0
    assert calibration.SETUP_STATUS_SLO_P95_SECONDS == 1.0
    assert calibration.SETUP_STATUS_SLO_MAX_SECONDS == 5.0
    assert calibration.SETUP_PROBE_CONNECT_TIMEOUT_SECONDS == 2.0
    assert calibration.SETUP_PROBE_TOTAL_TIMEOUT_SECONDS == 10.0
    assert calibration.HEALTH_PROBE_CONNECT_TIMEOUT_SECONDS == 2.0
    assert calibration.HEALTH_PROBE_TOTAL_TIMEOUT_SECONDS == 5.0
    assert calibration.SQLITE_BUSY_TIMEOUT_MS == 5000
    assert calibration.SQLITE_SYNCHRONOUS == "NORMAL"
