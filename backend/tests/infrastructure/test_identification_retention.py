"""F-PERF-04: bounded identification history retention.

Signed decision (LibraryAudit DECISIONS-LIVE): terminal automatic jobs older
than 30 days prune in bounded transactions behind an additive
``(state, terminal_at)`` index; active/deferred/needs-attention/review work and
any attempt/evidence chain referenced by an identity, review, other job, or a
repair finding survives. The activity aggregate stays request-driven; the
two-second stream poll remains a cheap revision signal."""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_work import IdentificationJob

DAY = 86400.0
NOW = 1_800_000_000.0


def _seed_auth(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")


class RetentionStore(NativeLibraryStore):
    """Records every statement issued after construction."""

    def __init__(self, *args, **kwargs):
        self.statements: list[str] = []
        super().__init__(*args, **kwargs)

    def _connect(self):
        conn = super()._connect()
        conn.set_trace_callback(self.statements.append)
        return conn


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "library.db"
    _seed_auth(path)
    return path


@pytest.fixture
def store(db_path: Path) -> RetentionStore:
    return RetentionStore(db_path, threading.Lock())


def _job(job_id: str, album_id: str) -> IdentificationJob:
    return IdentificationJob(
        id=job_id,
        dedupe_key=f"automatic:{album_id}:policy:{job_id}",
        local_album_id=album_id,
    )


def _seed_job(
    db_path: Path,
    job_id: str,
    *,
    kind: str = "automatic",
    state: str = "succeeded",
    terminal_at: float | None = NOW - 40 * DAY,
    last_failure_code: str | None = None,
    terminal_result_id: str | None = None,
) -> None:
    with sqlite3.connect(db_path) as connection:
        if terminal_at is None or state in ("queued", "running", "paused", "needs_review"):
            terminal_sql = "NULL"
        else:
            terminal_sql = str(terminal_at)
        connection.execute(
            "INSERT INTO library_identification_jobs "
            "(id, local_album_id, local_track_id, kind, state, priority, "
            "enqueue_sequence, input_revision, dedupe_key, created_at, "
            "updated_at, terminal_at, last_failure_code, terminal_result_id) "
            "VALUES (?, ?, NULL, ?, ?, 100, 0, 'rev', ?, 1, 1, ?, ?, ?)",
            # exactly one subject per the table CHECK constraint
            (
                job_id,
                f"album-{job_id}",
                kind,
                state,
                f"automatic:{job_id}:policy:{job_id}",
                terminal_sql,
                last_failure_code,
                terminal_result_id,
            ),
        )


def _seed_attempt_chain(
    db_path: Path,
    attempt_id: str,
    *,
    evidence_id: str = "ev-1",
    with_evidence: bool = True,
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO library_identification_attempts "
            "(id, local_album_id, trigger, input_tag_revision, "
            "input_policy_revision, input_file_revision, matcher_version, "
            "state, terminal_reason_code, candidate_count, started_at, completed_at) "
            "VALUES (?, 'album-x', 'automatic', 't', 'p', 'f', 'm-1', "
            "'needs_review', 'no_candidate', 1, 1, 2)",
            (attempt_id,),
        )
        if with_evidence:
            connection.execute(
                "INSERT INTO library_identification_evidence "
                "(id, attempt_id, candidate_key, evidence_json, evidence_size_bytes, "
                "compacted, created_at) VALUES (?, ?, 'cand-1', '{}', 2, 0, 2)",
                (evidence_id, attempt_id),
            )


def _scalar(db_path: Path, sql: str) -> object:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(sql).fetchone()
        return row[0] if row is not None else None


def test_store_init_twice_creates_terminal_index_once(store: RetentionStore, db_path: Path) -> None:
    _seed_job(db_path, "job-old")
    before = _scalar(db_path, "SELECT COUNT(*) FROM library_identification_jobs")
    again = NativeLibraryStore(db_path, threading.Lock())  # second construction
    assert isinstance(again, NativeLibraryStore)
    names = {
        row[0]
        for row in sqlite3.connect(db_path).execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='library_identification_jobs'"
        )
    }
    assert "idx_identification_jobs_terminal" in names
    # exactly one definition, rows untouched by re-initialization
    with sqlite3.connect(db_path) as connection:
        definitions = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND "
            "name='idx_identification_jobs_terminal'"
        ).fetchone()[0]
    assert definitions == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_jobs") == before


@pytest.mark.asyncio
async def test_prune_respects_strict_30_day_cutoff(store: RetentionStore, db_path: Path) -> None:
    at_cutoff = NOW - 30 * DAY          # exactly at the boundary: keep
    older = NOW - 30 * DAY - 1          # strictly older: eligible
    _seed_job(db_path, "job-at-cutoff", terminal_at=at_cutoff)
    _seed_job(db_path, "job-older", terminal_at=older)

    removed, has_more = await store.prune_old_terminal_identification_jobs(now=NOW)

    assert removed == 1 and has_more is False
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_jobs WHERE id='job-at-cutoff'") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_jobs WHERE id='job-older'") == 0


@pytest.mark.asyncio
async def test_prune_preserves_active_deferred_attention_review_and_other_kinds(
    store: RetentionStore, db_path: Path
) -> None:
    preserved = {
        "job-queued": dict(state="queued", terminal_at=None),
        "job-running": dict(state="running", terminal_at=None),
        "job-paused": dict(state="paused", terminal_at=None),
        "job-deferred": dict(
            state="paused", terminal_at=None, last_failure_code="PROVIDER_TEMPORARILY_UNAVAILABLE"
        ),
        "job-needs-review": dict(state="needs_review"),
        "job-attention-cap": dict(
            state="failed", last_failure_code="MAX_DEFERRALS_EXCEEDED"
        ),
        "job-attention-subject": dict(
            state="failed", last_failure_code="SUBJECT_NOT_AVAILABLE"
        ),
        "job-review-retry": dict(kind="review_retry"),
        "job-post-processing": dict(kind="post_processing"),
    }
    for job_id, overrides in preserved.items():
        _seed_job(db_path, job_id, **overrides)

    removed, has_more = await store.prune_old_terminal_identification_jobs(now=NOW)

    assert removed == 0 and has_more is False
    remaining = _scalar(db_path, "SELECT COUNT(*) FROM library_identification_jobs")
    assert remaining == len(preserved)


@pytest.mark.asyncio
async def test_prune_removes_unreferenced_chain_and_keeps_referenced_one(
    store: RetentionStore, db_path: Path
) -> None:
    # Old succeeded job with a fully unreferenced terminal chain: all removed.
    _seed_job(
        db_path,
        "job-free",
        state="succeeded",
        terminal_at=NOW - 40 * DAY,
        terminal_result_id="attempt-free",
    )
    _seed_attempt_chain(db_path, "attempt-free", evidence_id="ev-free")

    # Old failed job whose terminal attempt an IDENTITY references: the job
    # goes, but the attempt and its evidence survive for audit.
    await store.create_catalog_membership(
        __import__(
            "tests.infrastructure.test_native_library_store", fromlist=["_membership"]
        )._membership("7")
    )
    _seed_job(
        db_path,
        "job-referenced",
        state="failed",
        terminal_at=NOW - 41 * DAY,
        last_failure_code="UNRELATED_CODE",
        terminal_result_id="attempt-kept",
    )
    _seed_attempt_chain(db_path, "attempt-kept", evidence_id="ev-kept")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO local_album_external_identities "
            "(local_album_id, provider, release_group_mbid, decision_source, "
            "matcher_version, attempt_id, selected_at) VALUES "
            "('album-7', 'musicbrainz', 'rg-kept', 'automatic', 'm-1', "
            "'attempt-kept', 3)"
        )

    removed, has_more = await store.prune_old_terminal_identification_jobs(now=NOW)

    assert removed == 2 and has_more is False
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_jobs WHERE id='job-free'") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_attempts WHERE id='attempt-free'") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_evidence WHERE id='ev-free'") == 0
    # referenced chain intact
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_attempts WHERE id='attempt-kept'") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_evidence WHERE id='ev-kept'") == 1


@pytest.mark.asyncio
async def test_prune_keeps_attempt_when_a_review_references_it(
    store: RetentionStore, db_path: Path
) -> None:
    _seed_job(
        db_path,
        "job-reviewed",
        state="failed",
        terminal_at=NOW - 40 * DAY,
        terminal_result_id="attempt-reviewed",
    )
    _seed_attempt_chain(db_path, "attempt-reviewed", evidence_id="ev-reviewed")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO library_identification_reviews "
            "(id, local_album_id, state, reason_code, attempt_id, input_revision, "
            "created_at, updated_at) VALUES ('review-1', 'album-x', 'resolved', "
            "'r', 'attempt-reviewed', 'rev', 3, 3)"
        )

    await store.prune_old_terminal_identification_jobs(now=NOW)

    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_jobs WHERE id='job-reviewed'") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_attempts WHERE id='attempt-reviewed'") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_evidence WHERE id='ev-reviewed'") == 1


@pytest.mark.asyncio
async def test_prune_keeps_evidence_held_by_repair_findings(
    store: RetentionStore, db_path: Path
) -> None:
    await store.create_catalog_membership(
        __import__(
            "tests.infrastructure.test_native_library_store", fromlist=["_membership"]
        )._membership("8")
    )
    _seed_job(
        db_path,
        "job-findings",
        state="succeeded",
        terminal_at=NOW - 40 * DAY,
        terminal_result_id="attempt-findings",
    )
    _seed_attempt_chain(db_path, "attempt-findings", evidence_id="ev-findings")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE library_identification_jobs SET local_album_id = 'album-8' "
            "WHERE id = 'job-findings'"
        )
        connection.execute(
            "INSERT INTO library_operation_jobs (id, kind, state, created_at, "
            "updated_at) VALUES ('op-1', 'repair', 'succeeded', 1, 1)"
        )
        connection.execute(
            "INSERT INTO library_identity_repair_findings (id, job_id, "
            "local_album_id, evidence_id, expected_album_revision, finding_code, "
            "confidence, created_at, updated_at) VALUES ('finding-1', 'op-1', "
            "'album-8', 'ev-findings', 1, 'DUPLICATE_IDENTITY', 'high', 2, 2)"
        )
        # detach the job's album link so operation FK graph stays consistent
        connection.execute(
            "UPDATE library_operation_jobs SET state='cancelled' WHERE id='op-1'"
        )

    await store.prune_old_terminal_identification_jobs(now=NOW)

    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_jobs WHERE id='job-findings'") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_evidence WHERE id='ev-findings'") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_attempts WHERE id='attempt-findings'") == 1


@pytest.mark.asyncio
async def test_prune_batch_limit_continues_in_stable_order(
    store: RetentionStore, db_path: Path
) -> None:
    # five eligible jobs with distinct terminal timestamps
    for i in range(5):
        _seed_job(db_path, f"job-{i}", terminal_at=NOW - (50 - i) * DAY)

    removed_1, more_1 = await store.prune_old_terminal_identification_jobs(now=NOW, limit=2)
    assert removed_1 == 2 and more_1 is True
    left = [
        row[0]
        for row in sqlite3.connect(db_path).execute(
            "SELECT id FROM library_identification_jobs"
        )
    ]
    # oldest first were removed (stable terminal_at ASC order)
    assert left == ["job-2", "job-3", "job-4"]

    removed_2, more_2 = await store.prune_old_terminal_identification_jobs(now=NOW, limit=2)
    assert removed_2 == 2 and more_2 is True

    removed_3, more_3 = await store.prune_old_terminal_identification_jobs(now=NOW, limit=2)
    assert removed_3 == 1 and more_3 is False
    assert _scalar(db_path, "SELECT COUNT(*) FROM library_identification_jobs") == 0


@pytest.mark.asyncio
async def test_activity_snapshot_statement_shapes_and_index_plan(
    store: RetentionStore, db_path: Path
) -> None:
    await store.create_catalog_membership(
        __import__(
            "tests.infrastructure.test_native_library_store", fromlist=["_membership"]
        )._membership("9")
    )
    _seed_job(db_path, "job-live", state="queued", terminal_at=None)
    _seed_job(
        db_path,
        "job-failed-latest",
        state="failed",
        terminal_at=NOW - DAY,
        last_failure_code="UNRELATED_CODE",
    )
    _seed_job(db_path, "job-done", state="succeeded", terminal_at=NOW - 2 * DAY)

    store.statements.clear()
    snapshot = await store.get_identification_activity_snapshot(now=NOW)
    selects = [
        s for s in store.statements if s.lstrip().upper().startswith("SELECT")
    ]
    # one bounded fixed set of aggregates plus the joined deferred-job
    # summary query, NOT one statement per job row
    assert len(selects) <= 11
    assert snapshot["counts"] == {"queued": 1, "failed": 1, "succeeded": 1}
    assert snapshot["failure_event_id"] == "job-failed-latest"

    # retention runs, then the same aggregate stays truthful
    _seed_job(db_path, "job-ancient", state="succeeded", terminal_at=NOW - 60 * DAY)
    await store.prune_old_terminal_identification_jobs(now=NOW)
    after = await store.get_identification_activity_snapshot(now=NOW)
    assert after["counts"] == {"queued": 1, "failed": 1, "succeeded": 1}
    assert after["failure_event_id"] == "job-failed-latest"

    with sqlite3.connect(db_path) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT id, terminal_at FROM library_identification_jobs "
            "WHERE state = 'failed' ORDER BY terminal_at DESC, id DESC LIMIT 1"
        ).fetchall()
    plan_text = " ".join(str(row[-1]) for row in plan)
    assert "INDEX idx_identification_jobs_terminal" in plan_text
    assert "TEMP B-TREE FOR ORDER BY" not in plan_text
