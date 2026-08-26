import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from api.v1.schemas.library_policies import LibraryRootSettings, TypedLibrarySettings
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.bounded_legacy_catalog_migrator import (
    BoundedLegacyCatalogMigrator,
)
from services.native.legacy_pending_migration_service import (
    LegacyPendingMigrationService,
)
from services.native.library_policy_resolver import LibraryPolicyResolver
from tests.infrastructure.test_legacy_catalog_importer import (
    TRACK_1,
    TRACK_2,
    _create_source,
)
from tests.infrastructure.test_bounded_legacy_catalog_migrator import (
    _insert_legacy_library_file,
)


def _store(database: Path) -> NativeLibraryStore:
    return NativeLibraryStore(database, threading.Lock())


def _resolver(*roots: tuple[str, Path]) -> LibraryPolicyResolver:
    return LibraryPolicyResolver(
        TypedLibrarySettings(
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
    )


def _write_catalog_files(root: Path) -> None:
    compilation = root / "Compilation"
    compilation.mkdir(parents=True)
    (compilation / "01.flac").write_bytes(b"a" * 100)
    (compilation / "02.flac").write_bytes(b"b" * 200)


@pytest.mark.asyncio
async def test_lenient_migration_skips_unmappable_paths_and_completes(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = _store(database)
    resolver = _resolver(("root", tmp_path / "Missing" / "Music"))

    outcome = await BoundedLegacyCatalogMigrator(
        store,
        resolver,
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate("lenient-migration", now=100)

    assert outcome.blocker_count == 0
    assert outcome.skipped_counts["library_file"] == 2
    assert outcome.skipped_counts["review_row"] == 4
    assert outcome.report.state == "applied"
    review_counts = next(
        count for count in outcome.report.reference_counts if count.kind == "review_row"
    )
    assert review_counts.unresolved == 4
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_tracks").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM library_migration_markers "
            "WHERE marker = 'legacy_catalog_import_complete'"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_pending_migration_imports_newly_resolvable_rows(tmp_path: Path) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = _store(database)

    first = await BoundedLegacyCatalogMigrator(
        store,
        _resolver(("root", tmp_path / "Missing" / "Music")),
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate("lenient-migration", now=100)
    assert first.skipped_counts["library_file"] == 2

    resolver = _resolver(("root", historical_root))
    pending = await BoundedLegacyCatalogMigrator(
        store,
        resolver,
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate_pending(f"legacy-pending-{resolver.policy_revision}", now=200)

    assert pending.blocker_count == 0
    assert "library_file" not in pending.skipped_counts
    assert "review_row" not in pending.skipped_counts
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_tracks").fetchone() == (
            4,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM library_migration_provenance "
            "WHERE source_kind = 'library_file'"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM library_migration_provenance "
            "WHERE source_kind = 'review_row'"
        ).fetchone() == (4,)

    repeat = await BoundedLegacyCatalogMigrator(
        store,
        resolver,
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate_pending(f"legacy-pending-{resolver.policy_revision}", now=300)

    assert repeat.blocker_count == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_tracks").fetchone() == (
            4,
        )


@pytest.mark.asyncio
async def test_pending_migration_skips_rows_already_owned_by_tracks(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = _store(database)

    outcome = await BoundedLegacyCatalogMigrator(
        store,
        _resolver(("root", historical_root)),
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate("initial-migration", now=100)
    assert outcome.blocker_count == 0

    with sqlite3.connect(database) as connection:
        _insert_legacy_library_file(
            connection,
            file_id="99999999-9999-4999-8999-000000000001",
            path=historical_root / "Compilation" / "01.flac",
            title="Duplicate",
            track_number=1,
            release_group_mbid=None,
        )

    resolver = _resolver(("root", historical_root))
    pending = await BoundedLegacyCatalogMigrator(
        store,
        resolver,
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate_pending(f"legacy-pending-{resolver.policy_revision}", now=200)

    assert pending.blocker_count == 0
    assert pending.skipped_counts["scan_owned_library_file"] == 1
    with sqlite3.connect(database) as connection:
        paths = [
            str(row[0])
            for row in connection.execute(
                "SELECT file_path FROM local_tracks ORDER BY file_path"
            ).fetchall()
        ]
    assert paths.count(str(historical_root / "Compilation" / "01.flac")) == 1


@pytest.mark.asyncio
async def test_pending_service_gates_scheduling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = _store(database)

    service = LegacyPendingMigrationService(
        store, lambda: _resolver(("root", tmp_path / "Missing" / "Music"))
    )
    runs: list[str] = []

    async def fake_run(run_id: str) -> None:
        runs.append(run_id)
        service._running = False

    monkeypatch.setattr(service, "_run", fake_run)

    assert await service.schedule() is False

    await BoundedLegacyCatalogMigrator(
        store,
        _resolver(("root", tmp_path / "Missing" / "Music")),
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate("lenient-migration", now=100)

    assert await service.schedule() is True
    await asyncio.sleep(0)
    assert len(runs) == 1
    assert runs[0].startswith("legacy-pending-")

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO library_migration_runs "
            "(id, source_revision, root_revision, state, report_json, "
            "started_at, updated_at) VALUES (?, '', '', 'completed', '', 100, 100)",
            (runs[0],),
        )

    assert await service.schedule() is False

    service = LegacyPendingMigrationService(
        store, lambda: _resolver(("root", historical_root))
    )
    monkeypatch.setattr(service, "_run", fake_run)
    assert await service.schedule() is True
    await asyncio.sleep(0)
    assert runs[-1] != runs[0]


@pytest.mark.asyncio
async def test_lenient_migration_skips_unmappable_local_only_rows(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    with sqlite3.connect(database) as connection:
        _insert_legacy_library_file(
            connection,
            file_id="99999999-9999-4999-8999-000000000002",
            path=historical_root / "Compilation" / "03.flac",
            title="Local Only",
            track_number=3,
            release_group_mbid=None,
        )
    store = _store(database)

    outcome = await BoundedLegacyCatalogMigrator(
        store,
        _resolver(("root", tmp_path / "Missing" / "Music")),
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate("lenient-migration", now=100)

    assert outcome.blocker_count == 0
    assert outcome.skipped_counts["library_file"] == 3
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_tracks").fetchone() == (
            0,
        )


@pytest.mark.asyncio
async def test_pending_migration_ignores_already_migrated_local_only_rows(
    tmp_path: Path,
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    with sqlite3.connect(database) as connection:
        _insert_legacy_library_file(
            connection,
            file_id="99999999-9999-4999-8999-000000000003",
            path=historical_root / "Compilation" / "03.flac",
            title="Local Only",
            track_number=3,
            release_group_mbid=None,
        )
    store = _store(database)
    resolver = _resolver(("root", historical_root))

    first = await BoundedLegacyCatalogMigrator(
        store,
        resolver,
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate("initial-migration", now=100)
    assert first.blocker_count == 0
    with sqlite3.connect(database) as connection:
        migrated_tracks = connection.execute(
            "SELECT COUNT(*) FROM local_tracks"
        ).fetchone()[0]

    pending = await BoundedLegacyCatalogMigrator(
        store,
        resolver,
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate_pending(f"legacy-pending-{resolver.policy_revision}", now=200)

    assert pending.blocker_count == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_tracks").fetchone() == (
            migrated_tracks,
        )


@pytest.mark.asyncio
async def test_pending_service_rejects_concurrent_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = _store(database)
    await BoundedLegacyCatalogMigrator(
        store,
        _resolver(("root", tmp_path / "Missing" / "Music")),
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate("lenient-migration", now=100)

    service = LegacyPendingMigrationService(
        store, lambda: _resolver(("root", tmp_path / "Missing" / "Music"))
    )

    async def fake_run(run_id: str) -> None:
        service._running = False

    monkeypatch.setattr(service, "_run", fake_run)

    first, second = await asyncio.gather(service.schedule(), service.schedule())

    assert sorted([first, second]) == [False, True]
