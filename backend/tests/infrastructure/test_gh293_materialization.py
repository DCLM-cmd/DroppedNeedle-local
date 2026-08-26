"""GH-293 paged repair-worklist materialization store tests.

Cover the pinned keyset contract: at most 500 subjects per transaction, a
durable cursor/ordinal/count with a sealed marker, crash-before-and-after-page
resume without omission or duplication, insertion boundary semantics, terminal
completion only after sealing, lease yield/requeue, and schema idempotency.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

from core.exceptions import StaleRevisionError
from infrastructure.persistence.gh293_calibration import MATERIALIZATION_PAGE_CAP
from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_work import OperationJob

ARTIST_ID = "artist-000001"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users VALUES ('admin')")
    return path


@pytest.fixture
def store(db_path: Path) -> NativeLibraryStore:
    return NativeLibraryStore(db_path, threading.Lock())


def _seed_albums(store: NativeLibraryStore, count: int, *, prefix: str = "al-") -> None:
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO local_artists (id, display_name, folded_name, kind, "
            "created_at, updated_at) VALUES (?, 'Artist', 'artist', 'group', 1, 1)",
            (ARTIST_ID,),
        )
        connection.executemany(
            "INSERT INTO local_albums (id, root_id, grouping_key, title, title_folded, "
            "album_artist_id, grouping_source, created_at, updated_at) "
            "VALUES (?, 'root', ?, ?, ?, ?, 'automatic', 1, 1)",
            [
                (
                    f"{prefix}{i:06d}",
                    f"fk-{prefix}{i:06d}",
                    f"Album {i:06d}",
                    f"album {i:06d}",
                    ARTIST_ID,
                )
                for i in range(count)
            ],
        )


def _hygiene_job(*, key: str) -> OperationJob:
    return OperationJob(
        id=str(uuid.uuid4()),
        kind="repair",
        requested_by_user_id=None,
        input_catalog_revision=0,
        idempotency_key=key,
        created_at=1,
    )


async def _claim(store: NativeLibraryStore, job_id: str, worker: str, *, now: float) -> dict:
    claimed = await store.claim_operation_job(
        worker, now=now, lease_seconds=60, kind="repair"
    )
    assert claimed is not None and claimed["id"] == job_id
    return claimed


def _work_rows(path: Path, job_id: str) -> list[tuple[int, str, str]]:
    with sqlite3.connect(path) as connection:
        return [
            (int(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT ordinal, local_album_id, idempotency_key "
                "FROM library_operation_work WHERE job_id = ? ORDER BY ordinal",
                (job_id,),
            ).fetchall()
        ]


@pytest.mark.asyncio
async def test_create_repair_operation_idempotent_by_key(db_path: Path) -> None:
    store = NativeLibraryStore(db_path, threading.Lock())
    _seed_albums(store, 3)
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:backfill"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    repeated = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:backfill"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    assert repeated["id"] == created["id"]
    with sqlite3.connect(db_path) as connection:
        jobs = connection.execute(
            "SELECT COUNT(*) FROM library_operation_jobs WHERE idempotency_key = ?",
            ("hygiene:v1:backfill",),
        ).fetchone()[0]
        materializations = connection.execute(
            "SELECT COUNT(*) FROM library_repair_materialization WHERE job_id = ?",
            (created["id"],),
        ).fetchone()[0]
        work = connection.execute(
            "SELECT COUNT(*) FROM library_operation_work WHERE job_id = ?",
            (created["id"],),
        ).fetchone()[0]
    assert jobs == 1
    assert materializations == 1
    assert work == 0  # materialization is paged; nothing is materialized at create


@pytest.mark.asyncio
async def test_materialization_pages_at_most_500_subjects_per_transaction(
    db_path: Path,
) -> None:
    total = 1200
    store = NativeLibraryStore(db_path, threading.Lock())
    _seed_albums(store, total)
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:backfill"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    assert created["expected_work_count"] == total
    await _claim(store, created["id"], "worker", now=2)

    page_sizes: list[int] = []
    counts: list[int] = []
    sealed_at: list[bool] = []
    while True:
        staged = await store.materialize_repair_operation_batch(
            created["id"], "worker", now=3
        )
        page_sizes.append(staged["page_size"])
        counts.append(staged["materialized_count"])
        sealed_at.append(staged["complete"])
        if staged["complete"]:
            break
    assert page_sizes == [500, 500, 200]
    assert all(size <= MATERIALIZATION_PAGE_CAP for size in page_sizes)
    assert counts == [500, 1000, 1200]
    assert sealed_at == [False, False, True]

    status = await store.get_repair_materialization_status(created["id"])
    assert status is not None
    assert status["sealed"] == 1
    assert status["staged_count"] == total
    assert status["staged_ordinal"] == total - 1
    assert status["staging_cursor"] == "al-001199"

    rows = _work_rows(db_path, created["id"])
    assert [row[0] for row in rows] == list(range(total))
    assert len({row[2] for row in rows}) == total
    # The sealed count is authoritative and corrects the pinned estimate.
    with sqlite3.connect(db_path) as connection:
        expected = connection.execute(
            "SELECT expected_work_count FROM library_operation_jobs WHERE id = ?",
            (created["id"],),
        ).fetchone()[0]
    assert expected == total

    # After sealing, materialization is a no-op.
    staged_again = await store.materialize_repair_operation_batch(
        created["id"], "worker", now=4
    )
    assert staged_again["complete"] is True
    assert staged_again["materialized_count"] == total
    assert staged_again["page_size"] == 0
    assert len(_work_rows(db_path, created["id"])) == total


@pytest.mark.asyncio
async def test_crash_before_and_after_page_commits_resumes_without_duplication(
    db_path: Path,
) -> None:
    total = 1200
    store = NativeLibraryStore(db_path, threading.Lock())
    _seed_albums(store, total)
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:backfill"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    await _claim(store, created["id"], "worker-1", now=2)
    # Page 1 commits; the process "crashes" here.
    staged = await store.materialize_repair_operation_batch(
        created["id"], "worker-1", now=3
    )
    assert staged["page_size"] == 500 and staged["complete"] is False

    # "Restart": a fresh store instance on the same file (schema idempotency),
    # the dead worker's lease expires, and the same static-key job resumes.
    restarted = NativeLibraryStore(db_path, threading.Lock())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE library_operation_jobs SET lease_expires_at = 0 "
            "WHERE id = ?",
            (created["id"],),
        )
    assert await restarted.recover_expired_operation_leases(now=4) == 1
    resumed = await restarted.claim_operation_job(
        "worker-2", now=4, lease_seconds=60, kind="repair"
    )
    assert resumed is not None and resumed["id"] == created["id"]
    assert resumed["state"] == "running"

    counts: list[int] = []
    while True:
        staged = await restarted.materialize_repair_operation_batch(
            created["id"], "worker-2", now=5
        )
        counts.append(staged["materialized_count"])
        if staged["complete"]:
            break
    assert counts == [1000, 1200]
    rows = _work_rows(db_path, created["id"])
    assert len(rows) == total
    assert len({row[2] for row in rows}) == total
    assert [row[0] for row in rows] == list(range(total))


@pytest.mark.asyncio
async def test_strict_revision_pin_rejects_catalog_changes_between_pages(
    store: NativeLibraryStore, db_path: Path,
) -> None:
    """The pinned catalog revision (captured inside the creation tx) is enforced
    on EVERY materialization page: any recorded catalog change between pages
    fails/stales the job BEFORE a live changed subject is added; the sealed set
    never changes mid-flight. Empty pinned sets seal as empty."""
    _seed_albums(store, 600)
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:backfill"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    status = await store.get_repair_materialization_status(created["id"])
    assert status is not None and status["sealed"] == 0
    with sqlite3.connect(db_path) as connection:
        job_revision = connection.execute(
            "SELECT input_catalog_revision FROM library_operation_jobs WHERE id = ?",
            (created["id"],),
        ).fetchone()[0]
        pinned_revision = connection.execute(
            "SELECT pinned_catalog_revision FROM library_repair_materialization "
            "WHERE job_id = ?",
            (created["id"],),
        ).fetchone()[0]
    # Strict: the job header and the materialization pin share the in-tx revision.
    assert job_revision == pinned_revision
    await _claim(store, created["id"], "worker", now=2)
    first = await store.materialize_repair_operation_batch(
        created["id"], "worker", now=3
    )
    assert first["materialized_count"] == 500 and first["complete"] is False

    # A catalog change that bumps the revision (any store catalog write would)
    # must stale the job before the next page inserts anything.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO local_albums (id, root_id, grouping_key, title, title_folded, "
            "album_artist_id, grouping_source, created_at, updated_at) "
            "VALUES ('al-000700', 'root', 'fk-after', 'After', 'after', ?, "
            "'automatic', 1, 1)",
            (ARTIST_ID,),
        )
        connection.execute(
            "UPDATE library_catalog_revision SET value = value + 1 WHERE singleton = 1"
        )
    with pytest.raises(StaleRevisionError):
        await store.materialize_repair_operation_batch(
            created["id"], "worker", now=4
        )
    rows = _work_rows(db_path, created["id"])
    assert len(rows) == 500  # no live changed subject was added
    assert "al-000700" not in {row[1] for row in rows}


@pytest.mark.asyncio
async def test_empty_creation_set_seals_as_empty(store: NativeLibraryStore, db_path: Path) -> None:
    """A job whose pinned predicate matches nothing seals with zero subjects and
    completes immediately (no subjects, no work rows, truthful counts)."""
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:empty"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    assert created["expected_work_count"] == 0
    await _claim(store, created["id"], "worker", now=2)
    staged = await store.materialize_repair_operation_batch(
        created["id"], "worker", now=3
    )
    assert staged["complete"] is True
    assert staged["materialized_count"] == 0
    status = await store.get_repair_materialization_status(created["id"])
    assert status is not None and status["sealed"] == 1 and status["staged_count"] == 0
    with sqlite3.connect(db_path) as connection:
        work_count = connection.execute(
            "SELECT COUNT(*) FROM library_operation_work WHERE job_id = ?",
            (created["id"],),
        ).fetchone()[0]
        expected = connection.execute(
            "SELECT expected_work_count FROM library_operation_jobs WHERE id = ?",
            (created["id"],),
        ).fetchone()[0]
    assert work_count == 0 and expected == 0
    terminal = await store.finish_sealed_repair_operation_job(
        created["id"], "worker", state="succeeded",
        terminal_code="CATALOG_IDENTITY_HYGIENE_COMPLETED", now=4,
    )
    assert terminal["state"] == "succeeded"


@pytest.mark.asyncio
async def test_non_recorded_raw_change_re_evaluates_live_set(
    store: NativeLibraryStore, db_path: Path,
) -> None:
    """Only STORE-RECORDED (revision-bumping) catalog changes invalidate the pin.
    A raw out-of-band deletion that does not bump the revision re-evaluates the
    live set on the next page: the vanished subject is simply not materialized
    and the sealed count corrects truthfully (valid progress, no stale churn)."""
    _seed_albums(store, 600)
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:deletion"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    await _claim(store, created["id"], "worker", now=2)
    first = await store.materialize_repair_operation_batch(
        created["id"], "worker", now=3
    )
    assert first["materialized_count"] == 500
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM local_albums WHERE id = 'al-000550'")
    second = await store.materialize_repair_operation_batch(
        created["id"], "worker", now=4
    )
    assert second["complete"] is True
    assert second["materialized_count"] == 599
    rows = _work_rows(db_path, created["id"])
    assert len(rows) == 599
    assert "al-000550" not in {row[1] for row in rows}
    with sqlite3.connect(db_path) as connection:
        expected = connection.execute(
            "SELECT expected_work_count FROM library_operation_jobs WHERE id = ?",
            (created["id"],),
        ).fetchone()[0]
    assert expected == 599


def _seed_albums_with_legacy_identities(
    path: Path, count: int, *, identity_source: str = "legacy_import"
) -> None:
    """Albums each with one indexed track and one identity row (existing_matches)."""
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO local_artists (id, display_name, folded_name, kind, "
            "created_at, updated_at) VALUES (?, 'Artist', 'artist', 'group', 1, 1)",
            (ARTIST_ID,),
        )
        for i in range(count):
            album_id = f"ea-{i:06d}"
            connection.execute(
                "INSERT INTO local_albums (id, root_id, grouping_key, title, title_folded, "
                "album_artist_id, grouping_source, created_at, updated_at) "
                "VALUES (?, 'root', ?, ?, ?, ?, 'automatic', 1, 1)",
                (album_id, f"fk-{album_id}", f"Album {i:06d}", f"album {i:06d}", ARTIST_ID),
            )
            connection.execute(
                "INSERT INTO local_tracks (id, local_album_id, root_id, file_path, "
                "relative_path, path_hash, file_size_bytes, file_mtime_ns, stat_revision, "
                "title, title_folded, album_title, album_title_folded, availability, "
                "ingest_source, imported_at, membership_source, applied_policy, file_format) "
                "VALUES (?, ?, 'root', ?, ?, ?, 100, 200, 'stat', 'Track', 'track', "
                "'Album', 'album', 'indexed', 'legacy_import', 1, 'legacy_import', "
                "'automatic', 'flac')",
                (f"track-{album_id}", album_id, f"/music/{album_id}.flac",
                 f"{album_id}.flac", f"hash-{album_id}"),
            )
            connection.execute(
                "INSERT INTO local_album_external_identities "
                "(local_album_id, provider, release_group_mbid, release_mbid, "
                "decision_source, matcher_version, selected_at) "
                "VALUES (?, 'musicbrainz', ?, ?, ?, '1', 1)",
                (album_id, f"rg-{i:06d}", f"rel-{i:06d}", identity_source),
            )


async def _run_existing_matches_with_mutation(
    db_path: Path, *, flip_from: int, flip_to: int
) -> dict:
    store = NativeLibraryStore(db_path, threading.Lock())
    created = await store.create_repair_operation(
        _hygiene_job(key=f"existing-matches:{flip_from}"),
        scope={"purpose": "existing_matches", "root_ids": [], "legacy_only": True},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    await _claim(store, created["id"], "worker", now=2)
    first = await store.materialize_repair_operation_batch(
        created["id"], "worker", now=3
    )
    assert first["materialized_count"] == 500
    with sqlite3.connect(db_path) as connection:
        for index in range(flip_from, flip_to):
            connection.execute(
                "UPDATE local_album_external_identities SET decision_source = 'manual' "
                "WHERE local_album_id = ?",
                (f"ea-{index:06d}",),
            )
        # A store-recorded eligibility change bumps the catalog revision.
        connection.execute(
            "UPDATE library_catalog_revision SET value = value + 1 WHERE singleton = 1"
        )
    return created


@pytest.mark.asyncio
async def test_eligibility_change_between_pages_stales_the_job(
    store: NativeLibraryStore, db_path: Path,
) -> None:
    """An eligibility change between pages (identity source flip that bumps the
    catalog revision) stales the pinned job BEFORE the changed subjects load:
    no changing set is ever materialized."""
    _seed_albums_with_legacy_identities(db_path, 520)
    created = await _run_existing_matches_with_mutation(
        db_path, flip_from=500, flip_to=510
    )
    with pytest.raises(StaleRevisionError):
        await store.materialize_repair_operation_batch(
            created["id"], "worker", now=4
        )
    # The sealed set is still exactly page 1: no flipped subject leaked in.
    rows = _work_rows(db_path, created["id"])
    assert len(rows) == 500
    assert "ea-000500" not in {row[1] for row in rows}


@pytest.mark.asyncio
async def test_scoped_multi_page_job_binds_parameters_in_fixed_order(
    store: NativeLibraryStore, db_path: Path,
) -> None:
    """Scoped multi-page materialization (root_ids + album_ids + keyset + high
    water + limit) binds placeholders in the fixed order across pages."""
    _seed_albums(store, 600)
    album_ids = [f"al-{i:06d}" for i in range(600)]
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:scoped"),
        scope={
            "purpose": "catalog_identity_hygiene",
            "root_ids": ["root"],
            "album_ids": album_ids,
        },
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    await _claim(store, created["id"], "worker", now=2)
    page_sizes: list[int] = []
    while True:
        staged = await store.materialize_repair_operation_batch(
            created["id"], "worker", now=3
        )
        page_sizes.append(staged["page_size"])
        if staged["complete"]:
            break
    assert page_sizes == [500, 100]
    rows = _work_rows(db_path, created["id"])
    assert len(rows) == 600
    assert [row[0] for row in rows] == list(range(600))


@pytest.mark.asyncio
async def test_pre_gh293_job_retro_seals_without_duplication(store: NativeLibraryStore, db_path: Path) -> None:
    """Compatibility: a repair job created before GH-293 (work rows already
    inline, no materialization row) is retro-sealed on first sight and keeps its
    queued/running/ready semantics - idempotent and restart-safe."""
    _seed_albums(store, 3)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO library_operation_jobs (id, kind, state, input_catalog_revision, "
            "expected_work_count, idempotency_key, created_at, updated_at) "
            "VALUES ('old-job', 'repair', 'queued', 0, 2, 'legacy:key', 1, 1)"
        )
        connection.execute(
            "INSERT INTO library_repair_snapshots (job_id, scope_json, "
            "source_matcher_version, target_matcher_version, created_at) "
            "VALUES ('old-job', '{\"purpose\":\"catalog_identity_hygiene\",\"album_ids\":[]}', "
            "NULL, 'v1', 1)"
        )
        connection.executemany(
            "INSERT INTO library_operation_work (job_id, ordinal, local_album_id, "
            "expected_subject_revision, expected_input_revision, action, idempotency_key, "
            "updated_at) VALUES ('old-job', ?, ?, 1, '1:::', 'catalog_identity_hygiene', ?, 1)",
            [(0, "al-000000", "old-job:al-000000:audit"),
             (1, "al-000001", "old-job:al-000001:audit")],
        )
    store = NativeLibraryStore(db_path, threading.Lock())
    claimed = await store.claim_operation_job(
        "old-worker", now=2, lease_seconds=60, kind="repair"
    )
    assert claimed is not None and claimed["id"] == "old-job"
    staged = await store.materialize_repair_operation_batch(
        "old-job", "old-worker", now=2
    )
    assert staged["complete"] is True
    assert staged["materialized_count"] == 2
    rows = _work_rows(db_path, "old-job")
    assert len(rows) == 2  # no duplication, no omission
    staged_again = await store.materialize_repair_operation_batch(
        "old-job", "old-worker", now=3
    )
    assert staged_again["complete"] is True
    assert staged_again["materialized_count"] == 2
    for ordinal in (0, 1):
        claimed_work = await store.claim_operation_work("old-job", "old-worker", now=4)
        assert claimed_work is not None
        await store.complete_operation_work(
            "old-job", ordinal, worker_id="old-worker",
            expected_work_revision=int(claimed_work["row_revision"]),
            state="succeeded", result_json=None, failure_code=None, completed_at=4,
        )
    ready = await store.mark_repair_ready("old-job", "old-worker", now=5)
    assert ready["state"] == "ready"


@pytest.mark.asyncio
async def test_finish_sealed_requires_seal_and_terminal_work(db_path: Path) -> None:
    store = NativeLibraryStore(db_path, threading.Lock())
    _seed_albums(store, 2)
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:backfill"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    await _claim(store, created["id"], "worker", now=2)

    with pytest.raises(StaleRevisionError):
        await store.finish_sealed_repair_operation_job(
            created["id"], "worker", state="succeeded",
            terminal_code="X", now=3,
        )
    staged = await store.materialize_repair_operation_batch(
        created["id"], "worker", now=3
    )
    assert staged["complete"] is True
    with pytest.raises(StaleRevisionError):
        await store.finish_sealed_repair_operation_job(
            created["id"], "worker", state="succeeded",
            terminal_code="X", now=3,
        )
    for ordinal in (0, 1):
        claimed_work = await store.claim_operation_work(
            created["id"], "worker", now=4
        )
        assert claimed_work is not None
        await store.complete_operation_work(
            created["id"],
            claimed_work["ordinal"],
            worker_id="worker",
            expected_work_revision=int(claimed_work["row_revision"]),
            state="succeeded",
            result_json=None,
            failure_code=None,
            completed_at=4,
        )
    done = await store.finish_sealed_repair_operation_job(
        created["id"], "worker", state="succeeded",
        terminal_code="CATALOG_IDENTITY_HYGIENE_COMPLETED", now=5,
    )
    assert done["state"] == "succeeded"
    with pytest.raises(StaleRevisionError):
        await store.finish_sealed_repair_operation_job(
            created["id"], "worker", state="succeeded",
            terminal_code="X", now=6,
        )


@pytest.mark.asyncio
async def test_mark_repair_ready_requires_sealed_materialization(
    db_path: Path,
) -> None:
    store = NativeLibraryStore(db_path, threading.Lock())
    _seed_albums(store, 2)
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:backfill"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    await _claim(store, created["id"], "worker", now=2)
    with pytest.raises(StaleRevisionError):
        await store.mark_repair_ready(created["id"], "worker", now=3)
    staged = await store.materialize_repair_operation_batch(
        created["id"], "worker", now=3
    )
    assert staged["complete"] is True
    with pytest.raises(StaleRevisionError):  # unfinished subjects still pending
        await store.mark_repair_ready(created["id"], "worker", now=3)
    for _ in range(2):
        claimed_work = await store.claim_operation_work(
            created["id"], "worker", now=4
        )
        assert claimed_work is not None
        await store.complete_operation_work(
            created["id"],
            claimed_work["ordinal"],
            worker_id="worker",
            expected_work_revision=int(claimed_work["row_revision"]),
            state="succeeded",
            result_json=None,
            failure_code=None,
            completed_at=4,
        )
    ready = await store.mark_repair_ready(created["id"], "worker", now=5)
    assert ready["state"] == "ready"


@pytest.mark.asyncio
async def test_yield_operation_job_requeues_and_resets_running_work(
    db_path: Path,
) -> None:
    store = NativeLibraryStore(db_path, threading.Lock())
    _seed_albums(store, 2)
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:backfill"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    await _claim(store, created["id"], "worker-1", now=2)
    await store.materialize_repair_operation_batch(created["id"], "worker-1", now=3)
    running = await store.claim_operation_work(created["id"], "worker-1", now=3)
    assert running is not None and running["state"] == "running"

    yielded = await store.yield_operation_job(
        created["id"], "worker-1", now=4, reason_code="WAL_BACKPRESSURE"
    )
    assert yielded["state"] == "queued"
    assert yielded["lease_owner"] is None
    with sqlite3.connect(db_path) as connection:
        work_state, failure_code = connection.execute(
            "SELECT state, failure_code FROM library_operation_work "
            "WHERE job_id = ? AND ordinal = ?",
            (created["id"], running["ordinal"]),
        ).fetchone()
    assert work_state == "pending"
    assert failure_code == "WAL_BACKPRESSURE"

    resumed = await store.claim_operation_job(
        "worker-2", now=5, lease_seconds=60, kind="repair"
    )
    assert resumed is not None and resumed["id"] == created["id"]
    claimed_again = await store.claim_operation_work(
        created["id"], "worker-2", now=5
    )
    assert claimed_again is not None and claimed_again["ordinal"] == running["ordinal"]


@pytest.mark.asyncio
async def test_construct_twice_schema_is_idempotent(db_path: Path) -> None:
    first = NativeLibraryStore(db_path, threading.Lock())
    _seed_albums(first, 1)
    second = NativeLibraryStore(db_path, threading.Lock())
    created = await second.create_repair_operation(
        _hygiene_job(key="hygiene:v1:backfill"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    await _claim(second, created["id"], "worker", now=2)
    staged = await second.materialize_repair_operation_batch(
        created["id"], "worker", now=3
    )
    assert staged["complete"] is True and staged["materialized_count"] == 1
    assert first is not None


def _seed_legacy_job(
    db_path: Path, state: str, *, lease_owner: str | None = None,
    terminal_work: int = 2, total_work: int = 3,
) -> str:
    """Seed a pre-GH-293 repair job (work inline, no materialization row)."""
    job_id = f"legacy-{state}"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO library_operation_jobs (id, kind, state, input_catalog_revision, "
            "expected_work_count, idempotency_key, created_at, updated_at, "
            "lease_owner, lease_expires_at) VALUES (?, 'repair', ?, 7, ?, ?, 1, 1, ?, ?)",
            (job_id, state, total_work, f"legacy:key:{state}", lease_owner,
             200 if lease_owner else None),
        )
        connection.execute(
            "INSERT INTO library_repair_snapshots (job_id, scope_json, "
            "source_matcher_version, target_matcher_version, created_at) "
            "VALUES (?, '{\"purpose\":\"catalog_identity_hygiene\",\"album_ids\":[]}', "
            "NULL, 'v1', 1)",
            (job_id,),
        )
        rows = []
        for ordinal in range(total_work):
            state_work = (
                "succeeded"
                if ordinal < terminal_work
                else "pending"
                if state != "succeeded"
                else "succeeded"
            )
            rows.append(
                (job_id, ordinal, f"al-{ordinal:06d}", 1, "1:::", "catalog_identity_hygiene",
                 f"{job_id}:al-{ordinal:06d}:audit", 1, state_work)
            )
        connection.executemany(
            "INSERT INTO library_operation_work (job_id, ordinal, local_album_id, "
            "expected_subject_revision, expected_input_revision, action, idempotency_key, "
            "updated_at, state) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return job_id


@pytest.mark.parametrize(
    "state,lease_owner,claimable",
    [
        ("queued", None, True),
        ("running", "legacy-worker", True),
        ("paused", None, False),
        ("ready", None, False),
        ("succeeded", None, False),
    ],
)
@pytest.mark.asyncio
async def test_pre_gh293_job_state_matrix(
    store: NativeLibraryStore, db_path: Path, state: str,
    lease_owner: str | None, claimable: bool,
) -> None:
    """Compatibility matrix for jobs created before GH-293: every state stays
    coherent, work stays inline and non-duplicated, and restart/crash recovery
    resumes the SAME job id."""
    _seed_albums(store, 3)
    job_id = _seed_legacy_job(
        db_path, state, lease_owner=lease_owner,
        terminal_work=2 if state != "succeeded" else 3,
    )
    if state == "running":
        # Already running under the legacy worker's live lease: materialize
        # directly (the retry path is covered by the queued case and the crash
        # recovery test).
        worker = "legacy-worker"
    else:
        worker = "worker"
    if not claimable:
        claimed = await store.claim_operation_job(worker, now=2, lease_seconds=60, kind="repair")
        assert claimed is None or claimed["id"] != job_id
        # Paused/ready/succeeded jobs need no materialization involvement and
        # stay coherent with their inline work intact.
        with sqlite3.connect(db_path) as connection:
            state_row = connection.execute(
                "SELECT state FROM library_operation_jobs WHERE id = ?", (job_id,),
            ).fetchone()[0]
            work_state = connection.execute(
                "SELECT COUNT(*) FROM library_operation_work WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
        assert state_row == state and work_state == 3
        return
    if state == "queued":
        claimed = await store.claim_operation_job(worker, now=2, lease_seconds=60, kind="repair")
        assert claimed is not None and claimed["id"] == job_id

    staged = await store.materialize_repair_operation_batch(job_id, worker, now=2)
    assert staged["complete"] is True
    assert staged["materialized_count"] == 3  # inline rows are the sealed set
    rows = _work_rows(db_path, job_id)
    assert len(rows) == 3  # no duplication
    # Finish the remaining pending unit and complete the same job id.
    work = await store.claim_operation_work(job_id, worker, now=3)
    assert work is not None and work["ordinal"] == 2
    await store.complete_operation_work(
        job_id, 2, worker_id=worker,
        expected_work_revision=int(work["row_revision"]),
        state="succeeded", result_json=None, failure_code=None, completed_at=3,
    )
    done = await store.finish_sealed_repair_operation_job(
        job_id, worker, state="succeeded",
        terminal_code="CATALOG_IDENTITY_HYGIENE_COMPLETED", now=4,
    )
    assert done["state"] == "succeeded"
    with sqlite3.connect(db_path) as connection:
        terminal = connection.execute(
            "SELECT COUNT(*) FROM library_operation_work WHERE job_id = ? "
            "AND state = 'succeeded'",
            (job_id,),
        ).fetchone()[0]
    assert terminal == 3


@pytest.mark.asyncio
async def test_pre_gh293_crash_recovery_resumes_same_job(
    store: NativeLibraryStore, db_path: Path,
) -> None:
    """Restart/crash semantics for a pre-GH-293 running job: expired lease
    recovery requeues the SAME job; retro-seal keeps the inline work; the job
    completes with no duplicate work and no fresh job id."""
    _seed_albums(store, 3)
    job_id = _seed_legacy_job(db_path, "running", lease_owner="dead-worker")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE library_operation_jobs SET lease_expires_at = 0 WHERE id = ?",
            (job_id,),
        )
    assert await store.recover_expired_operation_leases(now=3) == 1
    claimed = await store.claim_operation_job("worker-2", now=3, lease_seconds=60, kind="repair")
    assert claimed is not None and claimed["id"] == job_id
    staged = await store.materialize_repair_operation_batch(job_id, "worker-2", now=3)
    assert staged["complete"] is True and staged["materialized_count"] == 3
    rows = _work_rows(db_path, job_id)
    assert len(rows) == 3  # no duplication after restart
    with sqlite3.connect(db_path) as connection:
        jobs = connection.execute(
            "SELECT COUNT(*) FROM library_operation_jobs WHERE idempotency_key = ?",
            ("legacy:key:running",),
        ).fetchone()[0]
    assert jobs == 1  # no fresh job


def _bump_catalog_revision(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE library_catalog_revision SET value = value + 1 WHERE singleton = 1"
        )


@pytest.mark.asyncio
async def test_stale_pin_rebases_same_static_job_and_completes(
    store: NativeLibraryStore, db_path: Path,
) -> None:
    """Two-startup/catalog-change scenario: the static-key job resurfaced by a
    second startup enqueue is the SAME job id; when its pin goes stale mid-
    materialization the worker atomically rebases it to the current revision
    (no fresh job, no stranded backfill) and it completes."""
    static_key = "catalog-identity-hygiene:v1:backfill:rebase"
    _seed_albums(store, 600)
    created = await store.create_repair_operation(
        _hygiene_job(key=static_key),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    await _claim(store, created["id"], "worker", now=2)
    first = await store.materialize_repair_operation_batch(
        created["id"], "worker", now=3
    )
    assert first["materialized_count"] == 500 and first["complete"] is False

    # Catalog change: a new album and a revision bump (any store write).
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO local_albums (id, root_id, grouping_key, title, title_folded, "
            "album_artist_id, grouping_source, created_at, updated_at) "
            "VALUES ('al-000700', 'root', 'fk-after', 'After', 'after', ?, "
            "'automatic', 1, 1)",
            (ARTIST_ID,),
        )
    _bump_catalog_revision(db_path)

    # A second startup enqueue with the same static key returns the SAME job.
    repeated = await store.create_repair_operation(
        _hygiene_job(key=static_key),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    assert repeated["id"] == created["id"]

    with pytest.raises(StaleRevisionError):
        await store.materialize_repair_operation_batch(created["id"], "worker", now=4)
    rebased = await store.rebase_repair_operation(created["id"], "worker", now=4)
    assert rebased["rebased"] is True
    with sqlite3.connect(db_path) as connection:
        current = connection.execute(
            "SELECT value FROM library_catalog_revision WHERE singleton = 1"
        ).fetchone()[0]
        job_row = connection.execute(
            "SELECT input_catalog_revision, expected_work_count, state, completed_count "
            "FROM library_operation_jobs WHERE id = ?",
            (created["id"],),
        ).fetchone()
        mat = connection.execute(
            "SELECT pinned_catalog_revision, staged_ordinal, staged_count, sealed, "
            "staging_cursor FROM library_repair_materialization WHERE job_id = ?",
            (created["id"],),
        ).fetchone()
        work_left = connection.execute(
            "SELECT COUNT(*) FROM library_operation_work WHERE job_id = ?",
            (created["id"],),
        ).fetchone()[0]
    assert job_row[0] == current and mat[0] == current
    assert job_row[1] == 601  # re-estimated at the new revision
    assert mat[1] == -1 and mat[2] == 0 and mat[3] == 0 and mat[4] is None
    assert work_left == 0  # only staged rows were cleared
    assert job_row[2] == "running" and job_row[3] == 0

    # The worker resumes on a later pass: materialize cleanly at the new pin.
    page_sizes = []
    while True:
        staged = await store.materialize_repair_operation_batch(created["id"], "worker", now=5)
        page_sizes.append(staged["page_size"])
        if staged["complete"]:
            break
    assert page_sizes == [500, 101]
    assert staged["materialized_count"] == 601
    rows = _work_rows(db_path, created["id"])
    assert len(rows) == 601
    assert "al-000700" in {row[1] for row in rows}
    for ordinal in range(601):
        claimed_work = await store.claim_operation_work(created["id"], "worker", now=6)
        assert claimed_work is not None and claimed_work["ordinal"] == ordinal
        await store.complete_operation_work(
            created["id"], ordinal, worker_id="worker",
            expected_work_revision=int(claimed_work["row_revision"]),
            state="succeeded", result_json=None, failure_code=None, completed_at=6,
        )
    done = await store.finish_sealed_repair_operation_job(
        created["id"], "worker", state="succeeded",
        terminal_code="CATALOG_IDENTITY_HYGIENE_COMPLETED", now=7,
    )
    assert done["state"] == "succeeded"
    # The static key still resolves to the one completed job (no fresh job).
    again = await store.create_repair_operation(
        _hygiene_job(key=static_key),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    assert again["id"] == created["id"]
    with sqlite3.connect(db_path) as connection:
        job_count = connection.execute(
            "SELECT COUNT(*) FROM library_operation_jobs WHERE idempotency_key = ?",
            (static_key,),
        ).fetchone()[0]
    assert job_count == 1


@pytest.mark.asyncio
async def test_rebase_fails_closed_when_progress_exists(
    store: NativeLibraryStore, db_path: Path,
) -> None:
    """With any completed work, a stale pin fails closed to a durable failed
    state (PIN_STALE_WITH_PROGRESS) instead of silently discarding evidence."""
    _seed_albums(store, 3)
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:progress"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    await _claim(store, created["id"], "worker", now=2)
    staged = await store.materialize_repair_operation_batch(created["id"], "worker", now=3)
    assert staged["complete"] is True and staged["materialized_count"] == 3
    # Simulate completed work on an unsealed job (unusual but must fail closed).
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE library_repair_materialization SET sealed = 0, staged_count = 1 "
            "WHERE job_id = ?",
            (created["id"],),
        )
        connection.execute(
            "UPDATE library_operation_work SET state = 'succeeded' "
            "WHERE job_id = ? AND ordinal = 0",
            (created["id"],),
        )
        connection.execute(
            "UPDATE library_operation_jobs SET completed_count = 1 WHERE id = ?",
            (created["id"],),
        )
    _bump_catalog_revision(db_path)
    rebased = await store.rebase_repair_operation(created["id"], "worker", now=4)
    assert rebased["rebased"] is False
    assert rebased["job"]["state"] == "failed"
    assert rebased["job"]["terminal_code"] == "PIN_STALE_WITH_PROGRESS"
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT COUNT(*) FROM library_operation_work WHERE job_id = ?",
            (created["id"],),
        ).fetchone()[0]
        sealed = connection.execute(
            "SELECT sealed FROM library_repair_materialization WHERE job_id = ?",
            (created["id"],),
        ).fetchone()[0]
    assert rows == 3  # evidence retained, nothing deleted
    assert sealed == 0


@pytest.mark.asyncio
async def test_rebase_requires_running_lease_and_sealed_fails_closed(
    store: NativeLibraryStore, db_path: Path,
) -> None:
    """Rebase preconditions: a job not running under the worker raises; a sealed
    materialization fails closed instead of silently rebasing."""
    _seed_albums(store, 2)
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:rebase-guards"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    # Not claimed: no running lease -> rebase raises.
    with pytest.raises(StaleRevisionError):
        await store.rebase_repair_operation(created["id"], "worker", now=3)
    await _claim(store, created["id"], "worker", now=2)
    staged = await store.materialize_repair_operation_batch(created["id"], "worker", now=3)
    assert staged["complete"] is True  # sealed
    _bump_catalog_revision(db_path)
    rebased = await store.rebase_repair_operation(created["id"], "worker", now=4)
    assert rebased["rebased"] is False
    assert rebased["job"]["terminal_code"] == "PIN_STALE_WITH_PROGRESS"
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT COUNT(*) FROM library_operation_work WHERE job_id = ?",
            (created["id"],),
        ).fetchone()[0]
    assert rows == 2  # sealed set retained


@pytest.mark.asyncio
async def test_rebase_is_atomic_across_crash_before_rebase(
    store: NativeLibraryStore, db_path: Path,
) -> None:
    """Crash between the stale page failure and the rebase: lease expiry and a
    later claim resurface the SAME job, materialize still reports the stale pin
    (no partial rows), and the subsequent rebase clears staged rows exactly."""
    _seed_albums(store, 600)
    created = await store.create_repair_operation(
        _hygiene_job(key="hygiene:v1:rebase-crash"),
        scope={"purpose": "catalog_identity_hygiene", "album_ids": []},
        source_matcher_version=None,
        target_matcher_version="v1",
    )
    await _claim(store, created["id"], "worker", now=2)
    first = await store.materialize_repair_operation_batch(created["id"], "worker", now=3)
    assert first["materialized_count"] == 500
    _bump_catalog_revision(db_path)
    # The page failed upstream (StaleRevisionError) and the worker then crashed
    # BEFORE calling rebase: the staged 500 rows are still there.
    rows_before = _work_rows(db_path, created["id"])
    assert len(rows_before) == 500
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE library_operation_jobs SET lease_expires_at = 0 WHERE id = ?",
            (created["id"],),
        )
    assert await store.recover_expired_operation_leases(now=4) == 1
    resumed = await store.claim_operation_job("worker-2", now=4, lease_seconds=60, kind="repair")
    assert resumed is not None and resumed["id"] == created["id"]
    with pytest.raises(StaleRevisionError):
        await store.materialize_repair_operation_batch(created["id"], "worker-2", now=4)
    # No partial rows were added by the failed page.
    rows_mid = _work_rows(db_path, created["id"])
    assert len(rows_mid) == 500
    rebased = await store.rebase_repair_operation(created["id"], "worker-2", now=4)
    assert rebased["rebased"] is True
    rows_after = _work_rows(db_path, created["id"])
    assert rows_after == []  # staged rows cleared atomically by the rebase
