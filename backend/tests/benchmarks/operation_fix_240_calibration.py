"""NEW-QUAL-02 calibration record loading - fail closed by design.

The owner must approve one immutable calibration record before any scale or
stall qualification run executes. A missing, rejected, changed, or
fixture-mismatched value makes the affected result ``not-executed`` - never a
pass. This module is the single loader the gate runner and tests use."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

REQUIRED_FIELDS = (
    "calibration_id",
    "decision",  # "approved" | "rejected"
    "threshold_owner",
    "decision_date",
    "fixture_manifest_hash",
    "host_profile",
    "storage_profile",
    "tool_versions",
    "sample_count",
    "rss_base_bytes",
    "rss_slope_bytes_per_file",
    "idle_cpu_limit_core_seconds_per_minute",
    "quiescent_window_seconds",
    "queue_limit",
    "batch_limit",
    "walk_deadline_ms",
    "scheduling_allowance_ms",
    "management_write_deadline_ms",
    "detached_worker_cap",
    "detached_drain_deadline_ms",
)


class CalibrationError(RuntimeError):
    """Raised when a calibration record cannot authorize a measured gate."""


@dataclass(frozen=True)
class Calibration:
    raw: dict

    @property
    def calibration_id(self) -> str:
        return str(self.raw["calibration_id"])

    @property
    def approved(self) -> bool:
        return self.raw.get("decision") == "approved"

    def limit(self, name: str) -> float:
        return float(self.raw[name])

    def manifest_hash(self) -> str:
        return str(self.raw["fixture_manifest_hash"])


def fixture_manifest_hash(root: Path) -> str:
    """Stable hash over the relative path/size manifest of a fixture tree."""
    entries: list[str] = []
    for current, _dirs, files in os.walk(root):
        for name in sorted(files):
            path = Path(current) / name
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            entries.append(
                f"{path.relative_to(root).as_posix()}:{digest.hexdigest()}"
            )
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


def load_calibration(path: Path, *, fixture_root: Path | None = None) -> Calibration:
    """Load + validate one calibration record, failing closed.

    Raises :class:`CalibrationError` when the file is missing/unreadable,
    when any required field is absent, when the decision is not ``approved``,
    or - when a fixture root is supplied - when the recorded manifest hash no
    longer matches the fixture tree."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CalibrationError(
            f"calibration record missing: {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise CalibrationError(f"calibration record is not valid JSON: {path}") from error
    if not isinstance(raw, dict):
        raise CalibrationError("calibration record must be a JSON object")
    missing = [field for field in REQUIRED_FIELDS if field not in raw]
    if missing:
        raise CalibrationError(f"calibration record missing fields: {missing}")
    if raw.get("decision") != "approved":
        raise CalibrationError(
            "calibration decision is not 'approved' "
            f"(got {raw.get('decision')!r}); affected gates are not-executed"
        )
    if fixture_root is not None:
        current_hash = fixture_manifest_hash(fixture_root)
        if current_hash != raw["fixture_manifest_hash"]:
            raise CalibrationError(
                "fixture manifest hash mismatch: calibration was recorded for "
                "a different fixture tree"
            )
    return Calibration(raw=raw)
