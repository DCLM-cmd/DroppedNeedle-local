"""GH-293 owner-approved calibration record (2026-08-20).

Every numeric value in this module is the corpus-pinned calibration authorized by
the owner on 2026-08-20 (recorded in ``context/10-decisions-and-conflicts.md`` and
the GH-293 dossier). Tests and the production implementation MUST load these
exact values; changing a value here without a new owner decision is a contract
violation, and tests assert the record is present and unchanged.

Calibration contract (owner-approved):

- setup-status probe: connect 2 s, total 10 s (runbook/evidence probes only)
- health probe: connect 2 s, total 5 s
- setup-status SLO: p95 <= 1 s, max <= 5 s, zero timeouts and zero HTTP 503 under
  sustained background identity work
- active-WAL high water: 64 MiB; low water: 16 MiB
- PASSIVE checkpoint cadence: 30 s; reader-blocked bound: 60 s
- worklist materialization: at most 500 subjects per transaction
- background timeslice: 250 ms per worker admission before yielding the lease
- public bootstrap demand maximum hold: 5 s (a single background wait)
- forced-fairness background-progress floor: at least one subject per 120 s
- no live ``PRAGMA wal_checkpoint(TRUNCATE)`` anywhere in the request, startup,
  healthcheck, or worker path
"""

# Materialization page cap: at most 500 subjects per transaction (E15).
MATERIALIZATION_PAGE_CAP = 500

# Background worker timeslice: after this wall-clock budget a worker releases the
# operation lease back to 'queued' so the event loop and other tasks proceed.
BACKGROUND_TIMESLICE_SECONDS = 0.25

# Cooldown persisted as ``next_attempt_at`` on every lease yield (timeslice,
# WAL backpressure, public demand). It prevents immediate re-claim hot loops:
# the worker waits on a real timed wakeup (``notify_after``) instead of waking
# itself, while staying well under the forced-fairness progress floor.
BACKGROUND_YIELD_COOLDOWN_SECONDS = 0.5

# Public bootstrap demand: one process-wide coalesced signal; a single background
# wait for public demand may hold at most this long before forced progress.
PUBLIC_DEMAND_MAX_HOLD_SECONDS = 5.0

# Forced-fairness progress floor: at least one subject per 120 s under sustained
# public demand (matches the workload-gate 120 s forced pass).
FORCED_FAIRNESS_PROGRESS_FLOOR_SECONDS = 120.0

# Active-WAL (uncheckpointed frames) backpressure watermarks.
ACTIVE_WAL_HIGH_WATER_BYTES = 64 * 1024 * 1024
ACTIVE_WAL_LOW_WATER_BYTES = 16 * 1024 * 1024

# Safe PASSIVE checkpoint cadence and reader-blocked bound.
CHECKPOINT_CADENCE_SECONDS = 30.0
CHECKPOINT_READER_BLOCKED_MAX_SECONDS = 60.0

# Setup-status SLO (qualified against the representative fixture).
SETUP_STATUS_SLO_P95_SECONDS = 1.0
SETUP_STATUS_SLO_MAX_SECONDS = 5.0

# Read-only probe budgets (evidence runbook; not runtime behavior).
SETUP_PROBE_CONNECT_TIMEOUT_SECONDS = 2.0
SETUP_PROBE_TOTAL_TIMEOUT_SECONDS = 10.0
HEALTH_PROBE_CONNECT_TIMEOUT_SECONDS = 2.0
HEALTH_PROBE_TOTAL_TIMEOUT_SECONDS = 5.0

# Existing corpus-pinned defaults (unchanged by this ticket; recorded here so one
# module owns every calibration number).
SQLITE_BUSY_TIMEOUT_MS = 5000
SQLITE_SYNCHRONOUS = "NORMAL"
SETUP_BOOTSTRAP_TIMEOUT_MS = 10_000
FAIRNESS_PASS_SECONDS = 120.0
