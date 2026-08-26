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
@pytest.mark.asyncio
async def test_source_stat_blocks_past_deadline_returns_timeout_and_no_retarget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = NativeLibraryStore(database, threading.Lock())
    settings = _settings(("root", tmp_path / "Missing" / "Music"))
    blocked = threading.Event()
    orig_stat = Path.stat

    def blocking_stat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(self).endswith("01.flac") or str(self).endswith("02.flac"):
            blocked.wait(timeout=5.0)
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", blocking_stat)
    reconciler = LegacyPathReconciler(store, settings, probe_timeout=0.05, probe_max_concurrent=4)
    try:
        result = await asyncio.wait_for(reconciler.reconcile(), timeout=1.0)
        assert result.mode == "blocked"
        assert result.failure_reason == "legacy_path_probe_timeout"
        assert result.library_file_count == 2
        assert reconciler.probe_pending_count == 2
        assert result.root_retargets == ()
        assert result.mappings == ()
    finally:
        blocked.set()
        deadline = asyncio.get_event_loop().time() + 2.0
        while reconciler.probe_pending_count and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert reconciler.probe_pending_count == 0
        await reconciler.aclose()


@pytest.mark.asyncio
async def test_destination_stat_blocks_returns_no_remap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    historical_root = tmp_path / "Old" / "Music"
    old_files = historical_root
    old_files.mkdir(parents=True)
    (old_files / "track.flac").write_bytes(b"a" * 100)
    current_root = tmp_path / "Current" / "Music"
    current_root.mkdir(parents=True)
    (current_root / "track.flac").write_bytes(b"a" * 100)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = NativeLibraryStore(database, threading.Lock())
    settings = _settings(("root", current_root))
    blocked = threading.Event()
    orig_stat = Path.stat

    def blocking_stat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(self).startswith(str(current_root)):
            blocked.wait(timeout=5.0)
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", blocking_stat)
    reconciler = LegacyPathReconciler(store, settings, probe_timeout=0.05, probe_max_concurrent=4)
    try:
        result = await asyncio.wait_for(reconciler.reconcile(), timeout=1.0)
        assert result.mode == "blocked"
        assert result.failure_reason in {"legacy_path_probe_timeout", "legacy_path_inaccessible"}
        assert result.mappings == ()
    finally:
        blocked.set()
        deadline = asyncio.get_event_loop().time() + 2.0
        while reconciler.probe_pending_count and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert reconciler.probe_pending_count == 0
        await reconciler.aclose()


@pytest.mark.asyncio
async def test_probe_timeout_does_not_create_unbounded_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    historical_root.mkdir(parents=True)
    for i in range(10):
        (historical_root / f"track-{i}.flac").write_bytes(b"a" * 100)
    database = tmp_path / "library.db"
    from tests.infrastructure.test_legacy_catalog_importer import TRACK_1, _create_source as _cs
    _create_source(database, historical_root)
    # _create_source seeds 2 rows; add 8 more for 10 total
    conn = sqlite3.connect(database)
    for i in range(2, 10):
        conn.execute(
            "INSERT INTO library_files (id, file_path, file_size_bytes, file_mtime, duration_seconds, file_format) VALUES (?, ?, ?, ?, ?, ?)",
            (f"extra-{i}", str(historical_root / f"track-{i}.flac"), 100, 0.0, 180.0, "flac"),
        )
    conn.commit()
    conn.close()
    store = NativeLibraryStore(database, threading.Lock())
    settings = _settings(("root", tmp_path / "Missing" / "Music"))
    blocked = threading.Event()
    orig_stat = Path.stat

    def blocking_stat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        blocked.wait(timeout=5.0)
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", blocking_stat)
    reconciler = LegacyPathReconciler(store, settings, probe_timeout=0.05, probe_max_concurrent=4, batch_size=5)
    try:
        result = await asyncio.wait_for(reconciler.reconcile(), timeout=2.0)
        assert result.mode == "blocked"
        assert result.failure_reason == "legacy_path_probe_timeout"
        assert reconciler.probe_pending_count <= 4
        assert reconciler.probe_pending_count >= 1
    finally:
        blocked.set()
        deadline = asyncio.get_event_loop().time() + 2.0
        while reconciler.probe_pending_count and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert reconciler.probe_pending_count == 0
        await reconciler.aclose()
        await asyncio.wait_for(asyncio.to_thread(lambda: 42), timeout=0.5)


@pytest.mark.asyncio
async def test_close_with_blocked_probe_does_not_hang(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = NativeLibraryStore(database, threading.Lock())
    settings = _settings(("root", tmp_path / "Missing" / "Music"))
    blocked = threading.Event()
    orig_stat = Path.stat

    def blocking_stat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "Historical" in str(self):
            blocked.wait(timeout=10.0)
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", blocking_stat)
    reconciler = LegacyPathReconciler(store, settings, probe_timeout=0.05, probe_max_concurrent=4)
    task = asyncio.create_task(reconciler.reconcile())
    await asyncio.sleep(0.02)
    assert reconciler.probe_pending_count == 2
    start = asyncio.get_event_loop().time()
    reconciler.close()
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.2
    assert reconciler.probe_pending_count == 0
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result.mode == "blocked"
    assert result.failure_reason == "legacy_path_probe_timeout"
    await reconciler.aclose()
    blocked.set()
def test_legacy_path_reconciler_import_surface() -> None:
    import services.native.legacy_path_reconciler as m
    import typing

    hints = typing.get_type_hints(m.LegacyPathReconciler.__init__)
    assert isinstance(hints, dict)
    assert hasattr(m.LegacyPathReconciler, "reconcile")
    assert hasattr(m.LegacyPathReconciler, "close")


@pytest.mark.asyncio
async def test_legacy_probe_timeout_completes_leniently_when_no_non_path_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = NativeLibraryStore(database, threading.Lock())
    settings = _settings(("root", tmp_path / "Missing" / "Music"))
    blocked = threading.Event()
    orig_stat = Path.stat

    def blocking_stat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "Historical" in str(self):
            blocked.wait(timeout=5.0)
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", blocking_stat)
    reconciler = LegacyPathReconciler(store, settings, probe_timeout=0.05, probe_max_concurrent=4)
    result = await reconciler.reconcile()
    assert result.mode == "blocked"
    assert result.failure_reason == "legacy_path_probe_timeout"
    assert result.mappings == ()
    assert result.root_retargets == ()
    assert result.project("some/path") == "some/path"
    assert reconciler.probe_pending_count >= 1
    await reconciler.aclose()
    assert reconciler.probe_pending_count == 0
    import json

    evidence = result.evidence()
    assert evidence is not None
    assert evidence["failure_reason"] == "legacy_path_probe_timeout"
    assert str(historical_root) not in json.dumps(evidence)
    blocked.set()
    # same db after the timeout: drive the migrator directly with no projector;
    # skip_unmappable turns the timeout rows into skips instead of blockers
    from services.native.bounded_legacy_catalog_migrator import BoundedLegacyCatalogMigrator
    from services.native.library_policy_resolver import LibraryPolicyResolver

    store2 = NativeLibraryStore(database, threading.Lock())
    resolver = LibraryPolicyResolver(settings)
    migrator = BoundedLegacyCatalogMigrator(
        store2, resolver, emit_progress=lambda m: None, path_projector=None, skip_unmappable_paths=True
    )
    outcome = await migrator.migrate("test-timeout-lenient", now=100)
    assert outcome.report.state == "applied"
    assert outcome.blocker_count == 0
    assert outcome.skipped_counts.get("library_file", 0) >= 2
    assert outcome.skipped_counts.get("review_row", 0) >= 4
    # No guessed local tracks
    with sqlite3.connect(database) as conn:
        local_tracks = conn.execute("SELECT COUNT(*) FROM local_tracks").fetchone()[0]
        # With skip_unmappable and no projector, no tracks should be created for timeout rows
        assert local_tracks == 0
        # Legacy rows retained
        remaining = conn.execute("SELECT COUNT(*) FROM library_files").fetchone()[0]
        assert remaining >= 2
        pending = conn.execute("SELECT COUNT(*) FROM library_files WHERE file_path LIKE ?", (f"%{historical_root.name}%",)).fetchone()[0]
        assert pending >= 2


@pytest.mark.asyncio
async def test_legacy_probe_timeout_still_aborts_on_non_path_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    # Seed a REAL non-path blocker using the existing bounded-migrator pattern
    malformed_release_group = "shared-legacy-release"
    with sqlite3.connect(database) as conn:
        # same release_group, different album_title -> ambiguous favorite_unresolved
        # once user_favorites references it
        for suffix in ("1", "2"):
            conn.execute(
                "INSERT INTO library_files (id, release_group_mbid, release_mbid, recording_mbid, disc_number, track_number, track_title, artist_name, album_artist_name, album_title, file_path, file_size_bytes, file_mtime, duration_seconds, file_format, source, is_compilation, tagged_at, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"99999999-9999-4999-8999-99999999999{suffix}",
                    malformed_release_group,
                    None,
                    None,
                    1,
                    1,
                    f"Track {suffix}",
                    "Local Artist",
                    "Local Artist",
                    f"Album {suffix}",
                    str(historical_root / f"Ambiguous {suffix}" / "01.flac"),
                    1000,
                    20.0,
                    180.0,
                    "flac",
                    "manual_review",
                    0,
                    21.0,
                    20.0,
                ),
            )
        conn.execute("INSERT INTO user_favorites VALUES ('alice', 'album', ?, 4)", (malformed_release_group,))
        conn.commit()
    store = NativeLibraryStore(database, threading.Lock())
    settings = _settings(("root", tmp_path / "Missing" / "Music"))
    blocked = threading.Event()
    orig_stat = Path.stat

    def blocking_stat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "Historical" in str(self):
            blocked.wait(timeout=5.0)
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", blocking_stat)
    reconciler = LegacyPathReconciler(store, settings, probe_timeout=0.05, probe_max_concurrent=4)
    result = await reconciler.reconcile()
    assert result.mode == "blocked"
    assert result.failure_reason == "legacy_path_probe_timeout"
    await reconciler.aclose()
    assert reconciler.probe_pending_count == 0
    # non-path blocker still fails the migrator even though the path leg only timed out
    from services.native.bounded_legacy_catalog_migrator import BoundedLegacyCatalogMigrator
    from services.native.library_policy_resolver import LibraryPolicyResolver

    store2 = NativeLibraryStore(database, threading.Lock())
    resolver = LibraryPolicyResolver(settings)
    migrator = BoundedLegacyCatalogMigrator(
        store2, resolver, emit_progress=lambda m: None, path_projector=None, skip_unmappable_paths=False
    )
    outcome = await migrator.migrate("test-timeout-with-blocker", now=100)
    assert outcome.report.state == "blocked"
    assert outcome.blocker_count > 0
    assert outcome.blocker_reason_counts
    # Timeout evidence should still be present in reconciler's evidence
    import json

    evidence = result.evidence()
    assert evidence is not None
    assert str(historical_root) not in json.dumps(evidence)
    blocked.set()



@pytest.mark.asyncio
async def test_reconcile_emits_sanitized_progress_heartbeats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F5/H5: reconcile() with an emitter reports cumulative counts and
    outcome classes only - zero paths, zero user identifiers - and the
    default (no emitter) stays completely silent."""
    import time as time_module

    historical_root = tmp_path / "Historical" / "Music"
    _write_catalog_files(historical_root)
    database = tmp_path / "library.db"
    _create_source(database, historical_root)
    store = NativeLibraryStore(database, threading.Lock())
    settings = _settings(("root", tmp_path / "Missing" / "Music"))

    stat_calls = {"count": 0}
    orig_stat = Path.stat

    def counting_stat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        stat_calls["count"] += 1
        time_module.sleep(0.001)
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counting_stat)

    messages: list[str] = []
    result = await LegacyPathReconciler(store, settings, batch_size=1).reconcile(
        emit_progress=messages.append
    )

    assert result.mode == "exact"
    assert stat_calls["count"] > 0
    assert messages
    assert any("scanned=" in message for message in messages)
    joined = "\n".join(messages)
    # Zero absolute paths or user identifiers in any heartbeat.
    assert str(historical_root) not in joined
    assert str(tmp_path) not in joined
    assert ".flac" not in joined
    assert "alice" not in joined and "admin" not in joined
    # Cumulative scanned counts are monotonic.
    scanned_values = [
        int(part.split("=", 1)[1])
        for message in messages
        for part in message.split()
        if part.startswith("scanned=")
    ]
    assert scanned_values == sorted(scanned_values)
    # Final summary names the outcome class and totals.
    assert messages[-1].startswith("legacy_path_reconciled mode=")
    assert "library_files=2" in messages[-1]
    assert "review_rows=4" in messages[-1]

    # Default path: no emitter, nothing printed at all.
    silent_result = await LegacyPathReconciler(
        store, settings, batch_size=1
    ).reconcile()
    assert silent_result.mode == "exact"
    captured = capsys.readouterr()
    assert captured.out == ""
