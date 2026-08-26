import sqlite3
import threading
from pathlib import Path

import pytest

from api.v1.schemas.library_policies import LibraryRootSettings, TypedLibrarySettings
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.bounded_legacy_catalog_migrator import (
    BoundedLegacyCatalogMigrator,
)
from services.native.legacy_catalog_importer import _hash
from services.native.legacy_path_reconciler import LegacyPathReconciler
from services.native.library_policy_resolver import LibraryPolicyResolver
from tests.infrastructure.test_legacy_catalog_importer import (
    TRACK_1,
    TRACK_2,
    _create_source,
)


@pytest.fixture(autouse=True)
def allow_test_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "services.native.legacy_path_reconciler._BLOCKED_ROOTS", (Path("/"),)
    )


def _settings(*roots: tuple[str, Path]) -> TypedLibrarySettings:
    return TypedLibrarySettings(
        library_roots=[
            LibraryRootSettings(
                id=root_id,
                path=str(path),
                label=f"Library {index}",
                policy="automatic",
            )
            for index, (root_id, path) in enumerate(roots, start=1)
        ]
    )


def _write_catalog_files(root: Path) -> None:
    compilation = root / "Compilation"
    compilation.mkdir(parents=True)
    (compilation / "01.flac").write_bytes(b"a" * 100)
    (compilation / "02.flac").write_bytes(b"b" * 200)


@pytest.mark.asyncio
async def test_reconciler_preserves_existing_paths_and_retargets_root(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = NativeLibraryStore(database, threading.Lock())
    settings = _settings(("root", tmp_path / "Missing" / "Music"))

    result = await LegacyPathReconciler(store, settings, batch_size=1).reconcile()

    assert result.mode == "exact"
    assert result.root_retargets == (("root", str(historical_root)),)
    assert result.project(str(historical_root / "Compilation" / "01.flac")) == str(
        historical_root / "Compilation" / "01.flac"
    )
    assert result.library_file_count == 2
    assert result.review_row_count == 4


@pytest.mark.asyncio
async def test_exact_path_recovery_ignores_stale_legacy_file_size(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE library_files SET file_size_bytes = 999999")
    store = NativeLibraryStore(database, threading.Lock())

    result = await LegacyPathReconciler(
        store, _settings(("root", tmp_path / "Missing" / "Music"))
    ).reconcile()

    assert result.mode == "exact"


@pytest.mark.asyncio
async def test_reconciler_projects_absent_paths_to_verified_current_root(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Old" / "Music"
    current_root = tmp_path / "Current" / "Music"
    _write_catalog_files(current_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = NativeLibraryStore(database, threading.Lock())
    settings = _settings(("root", current_root))

    result = await LegacyPathReconciler(store, settings, batch_size=1).reconcile()

    assert result.mode == "remapped"
    assert result.root_retargets == ()
    assert result.project(str(historical_root / "Compilation" / "01.flac")) == str(
        current_root / "Compilation" / "01.flac"
    )


@pytest.mark.asyncio
async def test_reconciler_rejects_ambiguous_exact_root_assignment(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = NativeLibraryStore(database, threading.Lock())
    settings = _settings(
        ("root-a", tmp_path / "Missing A" / "Music"),
        ("root-b", tmp_path / "Missing B" / "Music"),
    )

    result = await LegacyPathReconciler(store, settings).reconcile()

    assert result.mode == "blocked"
    assert result.failure_reason == "ambiguous_root_assignment"


@pytest.mark.asyncio
async def test_reconciler_rejects_mixed_present_and_absent_legacy_paths(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    first = historical_root / "Compilation" / "01.flac"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"a" * 100)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = NativeLibraryStore(database, threading.Lock())

    result = await LegacyPathReconciler(
        store, _settings(("root", tmp_path / "Current" / "Music"))
    ).reconcile()

    assert result.mode == "blocked"
    assert result.failure_reason == "mixed_legacy_path_state"


@pytest.mark.asyncio
async def test_reconciler_rejects_remap_with_wrong_destination_size(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Old" / "Music"
    current_root = tmp_path / "Current" / "Music"
    _write_catalog_files(current_root)
    (current_root / "Compilation" / "01.flac").write_bytes(b"wrong")
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = NativeLibraryStore(database, threading.Lock())

    result = await LegacyPathReconciler(
        store, _settings(("root", current_root))
    ).reconcile()

    assert result.mode == "blocked"
    assert result.failure_reason == "unverified_path_remap"


@pytest.mark.asyncio
async def test_reconciler_blocks_relative_legacy_paths(tmp_path: Path) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE library_files SET file_path = 'relative/path.flac' WHERE id = ?",
            (TRACK_1,),
        )
    store = NativeLibraryStore(database, threading.Lock())

    result = await LegacyPathReconciler(
        store, _settings(("root", tmp_path / "Missing" / "Music"))
    ).reconcile()

    assert result.mode == "blocked"
    assert result.failure_reason == "legacy_path_inaccessible"


@pytest.mark.asyncio
async def test_exact_recovery_reports_rows_outside_matched_historical_roots(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    other_root = tmp_path / "Historical" / "Other"
    _write_catalog_files(historical_root)
    _write_catalog_files(other_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE library_files SET file_path = ? WHERE id = ?",
            (str(other_root / "Compilation" / "01.flac"), TRACK_1),
        )
    store = NativeLibraryStore(database, threading.Lock())

    result = await LegacyPathReconciler(
        store, _settings(("root", tmp_path / "Missing" / "Music"))
    ).reconcile()

    assert result.mode == "blocked"
    assert result.failure_reason == "no_historical_root_match"


@pytest.mark.asyncio
async def test_reconciler_rejects_remap_covering_a_configured_root(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Old" / "Music"
    used_root = historical_root / "Current"
    current_root = tmp_path / "Current" / "Music"
    _write_catalog_files(current_root)
    used_root.mkdir(parents=True)
    (used_root / "Compilation").mkdir()
    (used_root / "Compilation" / "01.flac").write_bytes(b"a" * 100)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE library_files SET file_path = ? WHERE id = ?",
            (str(used_root / "Compilation" / "01.flac"), TRACK_1),
        )
    store = NativeLibraryStore(database, threading.Lock())

    result = await LegacyPathReconciler(
        store, _settings(("current", current_root), ("used", used_root))
    ).reconcile()

    assert result.mode == "blocked"
    assert result.failure_reason == "candidate_overlaps_configured_root"


@pytest.mark.asyncio
async def test_review_rows_cannot_establish_a_mapping_without_catalog_files(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM library_files")
    store = NativeLibraryStore(database, threading.Lock())

    result = await LegacyPathReconciler(
        store, _settings(("root", tmp_path / "Missing" / "Music"))
    ).reconcile()

    assert result.mode == "blocked"
    assert result.failure_reason == "review_paths_without_catalog_proof"


@pytest.mark.asyncio
async def test_exact_recovery_rejects_a_root_inside_staging(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Staging" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = NativeLibraryStore(database, threading.Lock())
    settings = _settings(("root", tmp_path / "Missing" / "Music"))
    settings.staging_path = str(tmp_path / "Staging")

    result = await LegacyPathReconciler(store, settings).reconcile()

    assert result.mode == "blocked"
    assert result.failure_reason == "root_validation_failed"


@pytest.mark.asyncio
async def test_projected_migration_preserves_source_rows_and_provenance(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Old" / "Music"
    current_root = tmp_path / "Current" / "Music"
    _write_catalog_files(current_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = NativeLibraryStore(database, threading.Lock())
    settings = _settings(("root", current_root))
    reconciliation = await LegacyPathReconciler(store, settings).reconcile()
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        source_rows = {
            str(row["id"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM library_files WHERE deleted_at IS NULL"
            )
        }

    outcome = await BoundedLegacyCatalogMigrator(
        store,
        LibraryPolicyResolver(settings),
        path_projector=reconciliation.project,
    ).migrate("projected-path-migration", now=100)

    assert outcome.blocker_count == 0
    with sqlite3.connect(database) as connection:
        migrated = dict(
            connection.execute(
                "SELECT id, file_path FROM local_tracks WHERE id IN (?, ?)",
                (TRACK_1, TRACK_2),
            ).fetchall()
        )
        legacy = dict(
            connection.execute(
                "SELECT id, file_path FROM library_files WHERE id IN (?, ?)",
                (TRACK_1, TRACK_2),
            ).fetchall()
        )
        provenance = dict(
            connection.execute(
                "SELECT source_key, source_revision FROM library_migration_provenance "
                "WHERE source_kind = 'library_file'"
            ).fetchall()
        )
        linked_reviews = connection.execute(
            "SELECT COUNT(*) FROM library_identification_reviews "
            "WHERE local_track_id IN (?, ?)",
            (TRACK_1, TRACK_2),
        ).fetchone()[0]
    assert legacy[TRACK_1].startswith(str(historical_root))
    assert migrated[TRACK_1].startswith(str(current_root))
    assert provenance == {
        track_id: _hash(source_rows[track_id]) for track_id in (TRACK_1, TRACK_2)
    }
    assert linked_reviews == 2
