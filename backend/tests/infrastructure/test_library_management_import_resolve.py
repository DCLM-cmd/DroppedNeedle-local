"""Store slice of the admin resolve action for stuck import bundles."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from core.exceptions import ConflictError, ResourceNotFoundError
from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_management import (
    LibraryManagementImportBundleRecord,
    LibraryManagementImportJournal,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users(id) VALUES ('admin')")
    return path


def _bundle(
    bundle_id: str = "bundle-1",
    key: str = "key-1",
    state: str = "preparing",  # type: ignore[arg-type]
) -> LibraryManagementImportBundleRecord:
    return LibraryManagementImportBundleRecord(
        id=bundle_id,
        idempotency_key=key,
        origin="acquisition",
        policy_revision="policy-1",
        request_json="{}",
        request_hash="0" * 64,
        state=state,
        created_at=1.0,
        updated_at=1.0,
    )


def _journal(
    bundle_id: str,
    ordinal: int,
    state: str = "planned",  # type: ignore[assignment]
) -> LibraryManagementImportJournal:
    return LibraryManagementImportJournal(
        bundle_id=bundle_id,
        ordinal=ordinal,
        state=state,  # type: ignore[arg-type]
        source_fingerprint="b" * 64,
        source_size=10,
        source_mtime_ns=5,
        temporary_relative_path=f"tmp-{ordinal}.flac",
        destination_root_id="root-1",
        destination_relative_path=f"Artist/Album/{ordinal:02d}.flac",
        staged_fingerprint="a" * 64,
        created_at=1.0,
        updated_at=1.0,
    )


async def _stick_bundle(
    store: NativeLibraryStore, bundle_id: str = "bundle-1"
) -> None:
    await store.ensure_library_management_import_bundle(_bundle(bundle_id))
    await store.ensure_library_management_import_journal(
        _journal(bundle_id, 0)
    )
    await store.ensure_library_management_import_journal(
        _journal(bundle_id, 1)
    )
    await store.ensure_library_management_import_journal(
        _journal(bundle_id, 2, state="completed")
    )
    await store.mark_library_management_import_needs_attention(
        bundle_id, failure_code="SEED", updated_at=2.0
    )


@pytest.mark.asyncio
async def test_resolve_flips_bundle_and_needs_attention_journals(
    db_path: Path,
) -> None:
    store = NativeLibraryStore(db_path, threading.Lock())
    await _stick_bundle(store)

    resolved = await store.resolve_library_management_import_bundle(
        "bundle-1", updated_at=3.0
    )

    assert resolved.id == "bundle-1"
    assert resolved.state == "resolved"
    assert resolved.row_revision == 3
    journals = await store.list_library_management_import_journals("bundle-1")
    by_ordinal = {journal.ordinal: journal for journal in journals}
    assert by_ordinal[0].state == "resolved"
    assert by_ordinal[1].state == "resolved"
    assert by_ordinal[0].row_revision == 3
    # Terminal journals are out of scope for the flip.
    assert by_ordinal[2].state == "completed"
    assert by_ordinal[2].row_revision == 1


@pytest.mark.asyncio
async def test_resolve_missing_bundle_raises_not_found(db_path: Path) -> None:
    store = NativeLibraryStore(db_path, threading.Lock())

    with pytest.raises(ResourceNotFoundError):
        await store.resolve_library_management_import_bundle(
            "bundle-missing", updated_at=3.0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["preparing", "publishing", "completed", "rolled_back", "resolved"]
)
async def test_resolve_wrong_state_raises_conflict(
    db_path: Path, state: str
) -> None:
    store = NativeLibraryStore(db_path, threading.Lock())
    await store.ensure_library_management_import_bundle(
        _bundle("bundle-1", state=state)  # type: ignore[arg-type]
    )

    with pytest.raises(ConflictError):
        await store.resolve_library_management_import_bundle(
            "bundle-1", updated_at=3.0
        )


@pytest.mark.asyncio
async def test_resolved_is_terminal_for_diagnostics_and_recovery(
    db_path: Path,
) -> None:
    store = NativeLibraryStore(db_path, threading.Lock())
    await _stick_bundle(store)

    stuck = await store.library_management_recovery_diagnostics()
    assert stuck["needs_attention_count"] == 1
    assert stuck["needs_attention_bundles"] == [{"bundle_id": "bundle-1"}]

    await store.resolve_library_management_import_bundle(
        "bundle-1", updated_at=3.0
    )

    diagnostics = await store.library_management_recovery_diagnostics()
    assert diagnostics["needs_attention_count"] == 0
    assert diagnostics["needs_attention_bundles"] == []
    assert diagnostics["recoverable_bundle_count"] == 0
    assert "import_resolved" not in diagnostics["state_counts"]
    recoverable = await store.list_recoverable_library_management_import_bundles(
        limit=10
    )
    assert recoverable == []


@pytest.mark.asyncio
async def test_resolved_bundle_cannot_regress_to_needs_attention(
    db_path: Path,
) -> None:
    store = NativeLibraryStore(db_path, threading.Lock())
    await _stick_bundle(store)
    await store.resolve_library_management_import_bundle(
        "bundle-1", updated_at=3.0
    )

    record = await store.mark_library_management_import_needs_attention(
        "bundle-1", failure_code="LATE", updated_at=4.0
    )

    assert record.state == "resolved"
    journals = await store.list_library_management_import_journals("bundle-1")
    assert {journal.state for journal in journals} == {"resolved", "completed"}


def test_schema_construction_is_idempotent(db_path: Path) -> None:
    NativeLibraryStore(db_path, threading.Lock())
    NativeLibraryStore(db_path, threading.Lock())

    with sqlite3.connect(db_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('library_management_import_bundles',"
                "'library_management_import_journal')"
            )
        }
    assert len(tables) == 2
    assert all("'resolved'" in sql for sql in tables)


_LEGACY_BUNDLE_DDL = """
CREATE TABLE library_management_import_bundles (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(trim(idempotency_key)) > 0),
    origin TEXT NOT NULL CHECK(origin IN ('acquisition','drop_import')),
    policy_revision TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_hash TEXT NOT NULL
        CHECK(length(request_hash) = 64 AND request_hash = lower(request_hash)
              AND request_hash NOT GLOB '*[^0-9a-f]*'),
    state TEXT NOT NULL CHECK(state IN (
        'preparing','publishing','catalog_committed','cleanup_pending','completed',
        'rolled_back','needs_attention'
    )),
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    row_revision INTEGER NOT NULL DEFAULT 1
        CHECK(row_revision BETWEEN 1 AND 9223372036854775807)
);
"""

_LEGACY_JOURNAL_DDL = """
CREATE TABLE library_management_import_journal (
    bundle_id TEXT NOT NULL
        REFERENCES library_management_import_bundles(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    state TEXT NOT NULL CHECK(state IN (
        'planned','staged','validated','replacement_backed_up','published',
        'catalog_committed','cleanup_pending','completed','rollback_pending',
        'rolled_back','needs_attention'
    )),
    source_fingerprint TEXT NOT NULL
        CHECK(length(source_fingerprint) = 64
              AND source_fingerprint = lower(source_fingerprint)
              AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
    source_size INTEGER NOT NULL CHECK(source_size >= 0),
    source_mtime_ns INTEGER NOT NULL,
    temporary_relative_path TEXT NOT NULL,
    destination_root_id TEXT NOT NULL,
    destination_relative_path TEXT NOT NULL,
    staged_fingerprint TEXT,
    replacement_fingerprint TEXT,
    replacement_backup_relative_path TEXT,
    baseline_blob_sha256 TEXT REFERENCES library_management_blobs(sha256) ON DELETE RESTRICT,
    baseline_format TEXT,
    baseline_adapter_version TEXT,
    baseline_stat_revision TEXT,
    baseline_tag_revision TEXT,
    baseline_image_snapshot_json TEXT NOT NULL DEFAULT '[]',
    baseline_ancillary_snapshot_json TEXT NOT NULL DEFAULT '[]',
    baseline_file_mtime_ns INTEGER,
    baseline_file_mode INTEGER,
    failure_code TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    row_revision INTEGER NOT NULL DEFAULT 1
        CHECK(row_revision BETWEEN 1 AND 9223372036854775807),
    PRIMARY KEY(bundle_id, ordinal)
);
"""


@pytest.mark.asyncio
async def test_legacy_check_migration_preserves_rows_and_admits_resolved(
    db_path: Path,
) -> None:
    NativeLibraryStore(db_path, threading.Lock())
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TABLE library_management_import_journal")
        connection.execute("DROP TABLE library_management_import_bundles")
        connection.executescript(_LEGACY_BUNDLE_DDL + _LEGACY_JOURNAL_DDL)
        connection.execute(
            "INSERT INTO library_management_import_bundles "
            "(id,idempotency_key,origin,policy_revision,request_json,request_hash,"
            "state,result_json,created_at,updated_at,row_revision) "
            "VALUES ('bundle-1','key-1','acquisition','policy-1','{}','"
            + "0" * 64
            + "','needs_attention','{}',1,2,2)"
        )
        connection.execute(
            "INSERT INTO library_management_import_journal "
            "(bundle_id,ordinal,state,source_fingerprint,source_size,"
            "source_mtime_ns,temporary_relative_path,destination_root_id,"
            "destination_relative_path,staged_fingerprint,failure_code,"
            "created_at,updated_at,row_revision) "
            "VALUES ('bundle-1',0,'needs_attention','"
            + "b" * 64
            + "',10,5,'tmp-0.flac','root-1','Artist/Album/00.flac','"
            + "a" * 64
            + "','SEED',1,2,2)"
        )
        connection.commit()

    # Constructing twice over the legacy CHECK must rebuild once, idempotently.
    NativeLibraryStore(db_path, threading.Lock())
    store = NativeLibraryStore(db_path, threading.Lock())

    record = await store.get_library_management_import_bundle("bundle-1")
    assert record is not None
    assert record.state == "needs_attention"
    assert record.row_revision == 2
    journals = await store.list_library_management_import_journals("bundle-1")
    assert [journal.state for journal in journals] == ["needs_attention"]
    resolved = await store.resolve_library_management_import_bundle(
        "bundle-1", updated_at=3.0
    )
    assert resolved.state == "resolved"
