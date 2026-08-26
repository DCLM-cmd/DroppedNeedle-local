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
    pending_run_id,
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

    # NEW-MIG-01: mark the launched run completed for ITS input revision.
    # Repeated schedules with unchanged pending input stay idempotently skipped.
    source_revision = await store.get_bounded_legacy_source_revision()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO library_migration_runs "
            "(id, source_revision, root_revision, state, report_json, "
            "started_at, updated_at) VALUES (?, ?, '', 'completed', '', 100, 100)",
            (runs[0], source_revision),
        )

    assert await service.schedule() is False

    # A new legacy row arrives under the SAME policy revision: the pending
    # input revision changes, so the completed policy-only gate must not
    # suppress the next schedule.
    with sqlite3.connect(database) as connection:
        _insert_legacy_library_file(
            connection,
            file_id="99999999-9999-4999-8999-000000000004",
            path=historical_root / "Compilation" / "04.flac",
            title="Late Arrival",
            track_number=4,
            release_group_mbid=None,
        )

    service = LegacyPendingMigrationService(
        store, lambda: _resolver(("root", historical_root))
    )
    monkeypatch.setattr(service, "_run", fake_run)
    assert await service.schedule() is True
    await asyncio.sleep(0)
    assert runs[-1] != runs[0]
    assert runs[-1] == pending_run_id(
        _resolver(("root", historical_root)).policy_revision,
        await store.get_bounded_legacy_source_revision(),
    )


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


@pytest.mark.asyncio
async def test_pending_run_id_binds_policy_and_input_revision() -> None:
    """NEW-MIG-01: the run identity changes when either the policy revision or
    the pending input revision changes, and is stable when both are unchanged."""
    assert (
        pending_run_id("policy-a", "rev-1") == "legacy-pending-policy-a-rev-1"
    )
    assert pending_run_id("policy-a", "rev-1") != pending_run_id("policy-b", "rev-1")
    assert pending_run_id("policy-a", "rev-1") != pending_run_id("policy-a", "rev-2")


@pytest.mark.asyncio
async def test_completed_input_a_does_not_suppress_new_pending_input_b(
    tmp_path: Path,
) -> None:
    """NEW-MIG-01 core regression, real SQLite: complete pending input A, then
    a new legacy row (input B) arrives under the SAME policy revision. The
    service must schedule a fresh run keyed on B's input revision instead of
    being suppressed by A's completed policy-only-style ID."""
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = _store(database)
    missing_resolver = _resolver(("root", tmp_path / "Missing" / "Music"))
    resolvable_resolver = _resolver(("root", historical_root))
    service = LegacyPendingMigrationService(store, lambda: missing_resolver)
    runs: list[str] = []

    async def fake_run(run_id: str) -> None:
        runs.append(run_id)
        service._running = False

    monkeypatch_runs = True

    # Input A: two skippable-under-missing-root rows make pending counts
    # nonzero; schedule and complete the launched run for input A.
    await BoundedLegacyCatalogMigrator(
        store,
        missing_resolver,
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate("lenient-migration", now=100)

    import asyncio as _asyncio

    original_run = service._run

    async def real_then_record(run_id: str) -> None:
        runs.append(run_id)
        try:
            await original_run(run_id)
        finally:
            # the real _run resets ITS OWN instance's flag; this wrapper's
            # owner needs the same guarantee or scheduling deadlocks
            service._running = False

    service._run = real_then_record
    assert await service.schedule() is True
    while service._running:
        await _asyncio.sleep(0.01)
    revision_a = await store.get_bounded_legacy_source_revision()
    assert len(runs) == 1
    revision_a = await store.get_bounded_legacy_source_revision()
    assert runs[0] == pending_run_id(missing_resolver.policy_revision, revision_a)
    with sqlite3.connect(database) as connection:
        state = connection.execute(
            "SELECT state FROM library_migration_runs WHERE id = ?", (runs[0],)
        ).fetchone()[0]
    assert state == "completed"

    # Same input again: idempotently skipped.
    service._run = real_then_record
    skipped = await service.schedule()
    assert skipped is False

    # Input B: one new legacy row arrives; policy revision unchanged.
    with sqlite3.connect(database) as connection:
        _insert_legacy_library_file(
            connection,
            file_id="99999999-9999-4999-8999-000000000005",
            path=historical_root / "Compilation" / "05.flac",
            title="Late Arrival",
            track_number=5,
            release_group_mbid=None,
        )

    counts = await store.get_pending_legacy_counts()
    assert any(value > 0 for value in counts.values())
    revision_b = await store.get_bounded_legacy_source_revision()
    assert revision_b != revision_a
    candidate = pending_run_id(resolvable_resolver.policy_revision, revision_b)
    assert await store.get_migration_run_state(candidate) != "completed"

    service = LegacyPendingMigrationService(store, lambda: resolvable_resolver)
    service._run = real_then_record
    launched = await service.schedule()
    while service._running:
        await _asyncio.sleep(0.01)
    assert runs[-1] == candidate


@pytest.mark.asyncio
async def test_schedule_without_marker_or_pending_launches_nothing(
    tmp_path: Path,
) -> None:
    """Fresh install (no marker) and completed-marker-with-zero-pending are
    no-op paths that launch no migration task."""
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = _store(database)
    resolver = _resolver(("root", tmp_path / "Missing" / "Music"))
    service = LegacyPendingMigrationService(store, lambda: resolver)

    # Fresh install: no completion marker.
    assert await service.schedule() is False

    # Marker present but zero pending rows: an empty legacy source migrates
    # to a completed marker with nothing left pending, so the gate skips.
    empty_database = tmp_path / "empty.db"
    _create_source(empty_database, historical_root)
    with sqlite3.connect(empty_database) as connection:
        for table in (
            "library_files",
            "manual_review_queue",
            "library_albums",
            "library_artists",
            "library_album_meta",
            "user_favorites",
            "play_history",
            "playlists",
            "playlist_tracks",
            "album_release_pins",
            "compat_bookmarks",
            "compat_play_queues",
            "compat_play_queue_items",
            "compat_id_map",
        ):
            connection.execute(f"DELETE FROM {table}")
    empty_store = _store(empty_database)
    await BoundedLegacyCatalogMigrator(
        empty_store,
        resolver,
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate("empty-migration", now=300)
    empty_service = LegacyPendingMigrationService(empty_store, lambda: resolver)
    counts = await empty_store.get_pending_legacy_counts()
    assert all(value == 0 for value in counts.values())
    assert await empty_service.schedule() is False


@pytest.mark.asyncio
async def test_runtime_smoke_real_migration_through_schedule_for_input_b(
    tmp_path: Path,
) -> None:
    """NEW-MIG-01 disposable SQLite runtime smoke: complete pending input A
    through the service, then a NEW legacy row (input B) arrives under the same
    policy revision. The gate must launch a fresh run for B, the real migrator
    must give B provenance, and scheduling B again must be a no-op."""
    historical_root = tmp_path / "Historical" / "Music"
    historical_root.mkdir(parents=True)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = _store(database)
    resolver = _resolver(("root", tmp_path / "Missing" / "Music"))
    service = LegacyPendingMigrationService(store, lambda: resolver)
    original_run = service._run

    async def run_and_reset(run_id: str) -> None:
        try:
            await original_run(run_id)
        finally:
            service._running = False

    async def run_and_reset(run_id: str) -> None:
        try:
            await original_run(run_id)
        finally:
            service._running = False

    service._run = run_and_reset

    async def wait_done() -> None:
        while service._running:
            await asyncio.sleep(0.01)

    def insert_row(file_id: str, name: str, title: str, track: int) -> None:
        with sqlite3.connect(database) as connection:
            _insert_legacy_library_file(
                connection,
                file_id=file_id,
                path=historical_root / name,
                title=title,
                track_number=track,
                release_group_mbid=None,
            )

    # The lenient upgrade completes first and creates the durable marker,
    # skipping every row because its root is gone.
    await BoundedLegacyCatalogMigrator(
        store,
        resolver,
        emit_progress=lambda _message: None,
        batch_size=1,
        skip_unmappable_paths=True,
    ).migrate("lenient-migration", now=100)

    # Input A arrives after the upgrade: the gate launches a pending run whose
    # real migration resolves the row against the restored root.
    insert_row(
        "99999999-9999-4999-8999-000000000008",
        "Late A/08.flac",
        "Late A",
        8,
    )
    Path(historical_root / "Late A").mkdir(parents=True)
    (historical_root / "Late A" / "08.flac").write_bytes(b"a" * 100)
    assert await service.schedule() is True
    await wait_done()
    with sqlite3.connect(database) as connection:
        provenance_a = connection.execute(
            "SELECT COUNT(*) FROM library_migration_provenance"
        ).fetchone()[0]
        states_a = dict(
            connection.execute(
                "SELECT id, state FROM library_migration_runs "
                "WHERE id LIKE 'legacy-pending-%'"
            ).fetchall()
        )
    revision_a = await store.get_bounded_legacy_source_revision()
    assert states_a
    assert all(state == "completed" for state in states_a.values())
    assert list(states_a) == [
        pending_run_id(resolver.policy_revision, revision_a)
    ]
    with sqlite3.connect(database) as connection:
        rows_a = connection.execute(
            "SELECT source_kind, source_key FROM library_migration_provenance"
        ).fetchall()
    print("SMOKE provenance A:", rows_a, flush=True)

    # Same input again: idempotently skipped.
    assert await service.schedule() is False

    # Input B arrives under the SAME policy revision.
    insert_row(
        "99999999-9999-4999-8999-000000000009",
        "Late B/09.flac",
        "Late B",
        9,
    )
    Path(historical_root / "Late B").mkdir(parents=True)
    (historical_root / "Late B" / "09.flac").write_bytes(b"b" * 100)

    revision_b = await store.get_bounded_legacy_source_revision()
    assert revision_b != revision_a
    candidate = pending_run_id(resolver.policy_revision, revision_b)
    assert candidate not in states_a
    assert await store.get_migration_run_state(candidate) != "completed"

    assert await service.schedule() is True
    await wait_done()

    # The durable run row is keyed to B's exact input revision.
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT source_revision, state FROM library_migration_runs "
            "WHERE id = ?",
            (candidate,),
        ).fetchone()
    assert stored is not None
    assert stored[0] == revision_b
    assert stored[1] == "completed"
    with sqlite3.connect(database) as connection:
        provenance_b = connection.execute(
            "SELECT COUNT(*) FROM library_migration_provenance"
        ).fetchone()[0]
        states_b = dict(
            connection.execute(
                "SELECT id, state FROM library_migration_runs "
                "WHERE id LIKE 'legacy-pending-%'"
            ).fetchall()
        )
    assert candidate in states_b
    assert states_b[candidate] == "completed"
    assert len(states_b) == len(states_a) + 1
    # Idempotency: scheduling the same input again adds nothing.
    assert await service.schedule() is False


