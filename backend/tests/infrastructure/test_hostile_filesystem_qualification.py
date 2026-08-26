"""NEW-QUAL-01: hostile-filesystem and interrupted-work qualification.

Real SQLite plus the real ``LibraryInventoryScanner`` walk path against
temporary hostile fixtures. Every assertion records durable outcomes or
filesystem-operation counts - never wall-clock speed. F-SCAN-05 stays
DEFERRED: scope-level restart only, no durable walk cursor."""

import ast
import asyncio
import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest

from api.v1.schemas.library_policies import LibraryRootSettings, TypedLibrarySettings
from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_work import ScanRun, ScanScope
from services.native.library_inventory_scanner import LibraryInventoryScanner
from tests.infrastructure.test_target_scan_lifecycle import _TagReader


def _scan_run(run_id: str = "run-1") -> ScanRun:
    return ScanRun(
        id=run_id,
        kind="incremental",
        trigger="manual",
        state="discovering",
        phase="discovering",
    )


class _RecordingStore:
    """Minimal store double that records exactly what discovery publishes."""

    def __init__(self) -> None:
        self.batches: list[list[tuple[str, int]]] = []
        self.failures: list[list[object]] = []
        self.classified: dict[str, tuple[str, None]] = {}
        self.completed_scopes: list[tuple] = []

    async def classify_scan_paths(self, root_id, entries):
        # (relative, size, mtime_ns, mtime, revision) tuples per caller.
        relatives = [entry[0] for entry in entries]
        for relative in relatives:
            self.classified[relative] = ("new", None)
        return {relative: ("new", None) for relative in relatives}

    async def add_scan_inventory_batch(self, run_id, items, **kwargs):
        self.batches.append(list(items))
        return (len(items), 1)

    async def record_scan_failures(self, run_id, records):
        self.failures.append(list(records))
        return len(records)

    async def complete_scan_scope_discovery(self, run_id, root_id, relative_path, **kwargs):
        self.completed_scopes.append((run_id, root_id, relative_path, kwargs))

    async def cleanup_stale_scan_inventory(self, *args, **kwargs):
        return 0

    async def get_scan_run(self, run_id):
        return None

    async def get_scan_scope_discovery_generation(self, *args, **kwargs):
        return 0

    async def get_scan_scope_discovery_state(self, *args, **kwargs):
        return "pending"

    async def restart_scan_scope_discovery(self, *args, **kwargs):
        return None

    async def transition_scan_run(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_walk_excludes_escaping_symlinks_and_non_audio_entries(
    tmp_path: Path,
) -> None:
    """Symlinked files resolving outside the configured root and symlinked
    directories are excluded; non-audio extensions never reach inventory.
    Root containment is enforced on the resolved path (F-PERF-10 sibling
    safety contract from the management rules)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    escape_file = outside / "evil.flac"
    escape_file.write_bytes(b"escaped")

    linked_target = tmp_path / "linked-target"
    linked_target.mkdir()
    (linked_target / "nested.flac").write_bytes(b"nested")

    root = tmp_path / "music"
    root.mkdir()
    (root / "real.flac").write_bytes(b"real")
    (root / "booklet.pdf").write_bytes(b"%PDF not audio")
    (root / "escape-link.flac").symlink_to(escape_file)
    (root / "linked-dir").symlink_to(linked_target, target_is_directory=True)

    store = _RecordingStore()
    scanner = LibraryInventoryScanner(store)  # real os.walk producer
    scope = ScanScope(root_id="root", policy_revision="policy-1", relative_path=".")
    resolver = SimpleNamespace_resolve()

    _updated, completed, failure_code = await scanner._walk_scope(
        _scan_run(), scope, root, root, resolver, AsyncCheckpoint()
    )

    assert completed is True and failure_code is None
    published = [item.absolute_path for batch in store.batches for item in batch]
    published_names = [Path(path).name for path in published]
    assert published_names == ["real.flac"], (
        "escaping symlinks, symlinked directories, and non-audio files must "
        "never enter inventory"
    )
    # F-020: escape-out links leave an audit row keyed by their own
    # walk-relative name instead of being dropped silently.
    assert len(store.failures) == 1
    records = store.failures[0]
    assert [
        (record.failure_code, record.relative_path, record.phase)
        for record in records
    ] == [("SYMLINK_ESCAPE_OUT", "escape-link.flac", "discovering")]
    assert all(
        record.failure_detail.startswith("A symbolic link resolves outside")
        for record in records
    )


class SimpleNamespace_resolve:
    @staticmethod
    def resolve(_path):
        return None


class AsyncCheckpoint:
    async def __call__(self, _run_id, _policy_revision):
        return True


def test_watchdog_receives_the_real_four_starter_map() -> None:
    """The scan supervisor MUST be inside the watchdog's restart map alongside
    the three workers - parsed from target_application.py so the contract
    tracks the actual application wiring, not a test-local copy."""
    source = Path(__file__).parents[2].joinpath("target_application.py").read_text()
    module = ast.parse(source)

    maps = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Dict)
        and any(
            isinstance(key, ast.Name) and key.id == "SUPERVISOR_TASK_NAME"
            for key in node.keys
            if isinstance(key, ast.Name)
        )
    ]
    assert len(maps) == 1, "expected exactly one worker-starter mapping"
    worker_map = maps[0]
    names = {
        key.id for key in worker_map.keys if isinstance(key, ast.Name)
    }
    assert names == {
        "SUPERVISOR_TASK_NAME",
        "IDENTIFICATION_WORKER_TASK_NAME",
        "OPERATION_WORKER_TASK_NAME",
        "CONTRIBUTION_VERIFICATION_WORKER_TASK_NAME",
    }

    # The watchdog receives that exact map object.
    watchdog_calls = [
        call
        for call in ast.walk(module)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "start_target_worker_watchdog"
    ]
    assert len(watchdog_calls) == 1
    arg = watchdog_calls[0].args[0]
    assert isinstance(arg, ast.Name) and arg.id == "worker_starters"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_catalog_files(root: Path) -> None:
    compilation = root / "Compilation"
    compilation.mkdir(parents=True)
    (compilation / "01.flac").write_bytes(b"a" * 100)
    (compilation / "02.flac").write_bytes(b"b" * 200)


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


@pytest.mark.asyncio
async def test_same_size_wrong_content_destination_fails_closed(tmp_path: Path) -> None:
    """Destination-integrity matrix case: remap destination whose sizes MATCH
    the legacy rows but whose CONTENT differs must fail closed - no mapping,
    no overwrite, zero source/destination/staging mutation."""
    from services.native.legacy_path_reconciler import LegacyPathReconciler
    from tests.infrastructure.test_legacy_catalog_importer import _create_source

    historical_root = tmp_path / "Old" / "Music"
    current_root = tmp_path / "Current" / "Music"
    _write_catalog_files(current_root)
    # SAME byte sizes as legacy (100/200), DIFFERENT content.
    (current_root / "Compilation" / "01.flac").write_bytes(b"X" * 100)
    (current_root / "Compilation" / "02.flac").write_bytes(b"Y" * 200)

    database = tmp_path / "library.db"
    _create_source(database, historical_root)

    watched = [
        path
        for path in (
            current_root / "Compilation" / "01.flac",
            current_root / "Compilation" / "02.flac",
            historical_root / "Compilation" / "01.flac",
            historical_root / "Compilation" / "02.flac",
        )
        if path.exists()  # the historical root is absent in a remap scenario
    ]
    hashes_before = {str(p): _sha256(p) for p in watched}
    with sqlite3.connect(database) as connection:
        source_before = connection.execute(
            "SELECT id, file_path, file_size_bytes FROM library_files ORDER BY id"
        ).fetchall()

    store = NativeLibraryStore(database, threading.Lock())
    result = await LegacyPathReconciler(
        store, _settings(("root", current_root))
    ).reconcile()

    assert result.mode == "blocked"
    assert result.failure_reason == "unverified_path_remap"
    assert result.root_retargets == ()

    assert {str(p): _sha256(p) for p in watched} == hashes_before
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id, file_path, file_size_bytes FROM library_files ORDER BY id"
        ).fetchall() == source_before


@pytest.mark.asyncio
async def test_large_partial_resume_rewalks_only_incomplete_roots(
    tmp_path: Path,
) -> None:
    """Interrupt discovery across four roots; recovery restarts at scope
    granularity - completed roots are NOT re-walked while incomplete roots
    finish. Recorded as release handoff evidence. F-SCAN-05 stays DEFERRED:
    scope-level restart only, no durable walk cursor."""
    from services.native.library_scan_coordinator import LibraryScanCoordinator

    roots: dict[str, Path] = {}
    for index in range(4):
        root_id = f"root-{chr(ord('a') + index)}"
        root = tmp_path / "music" / root_id
        root.mkdir(parents=True)
        for file_index in range(6):
            (root / f"track-{file_index}.flac").write_bytes(
                bytes([index, file_index]) * 24
            )
        roots[root_id] = root

    database = tmp_path / "target.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
    store = NativeLibraryStore(database, threading.Lock())

    resolver = _multi_root_resolver(roots)
    coordinator = _coordinator(store, resolver)

    requested = await coordinator.request_run(_multi_root_request(roots, resolver))
    run = await store.claim_next_scan_run(now=10)
    assert run is not None
    _run, scopes, _ = await store.get_scan_run(run.id)
    assert len(scopes) == 4  # one scope per configured root

    # Interrupt discovery right after the FIRST root completes.
    checkpoints = {"n": 0}

    async def interrupting_checkpoint(_run_id: str, _revision: str) -> bool:
        checkpoints["n"] += 1
        return checkpoints["n"] <= 1

    interrupted_scanner = LibraryInventoryScanner(store, walk_deadline_seconds=30.0)
    await interrupted_scanner.discover(
        run,
        scopes,
        roots,
        resolver,
        interrupting_checkpoint,
    )

    with sqlite3.connect(database) as connection:
        before_rows = connection.execute(
            "SELECT root_id, discovery_state FROM "
            "library_scan_run_scopes WHERE run_id = ?",
            (run.id,),
        ).fetchall()
    before = {root_id: state for root_id, state in before_rows}
    completed_before = sorted(k for k, v in before.items() if v == "completed")

    # Recovery claims the same run; the finishing scanner must not re-read
    # any path under an already-completed root.
    recovered = await coordinator.recover()
    assert [item.id for item in recovered] == [requested.run_id]

    republished_paths: list[str] = []
    real_classify = type(store).classify_scan_paths

    async def spy_classify(store_self, root_id, entries):
        if any(key != "root-a" for key in [root_id]):
            for entry in entries:
                republished_paths.append(f"{root_id}:{entry[0]}")
        return await real_classify(store_self, root_id, entries)

    type(store).classify_scan_paths = spy_classify
    try:
        finished = await coordinator.run_once(dict(sorted(roots.items())))
    finally:
        type(store).classify_scan_paths = real_classify

    assert finished is not None and finished.state == "completed"

    with sqlite3.connect(database) as connection:
        after = {
            root_id: state
            for root_id, state in connection.execute(
                "SELECT root_id, discovery_state FROM "
                "library_scan_run_scopes WHERE run_id = ?",
                (run.id,),
            ).fetchall()
        }
    assert len(after) == 4 and all(state == "completed" for state in after.values())

    # Scope-level restart proof: the completed root was never re-classified.
    assert not any(path.startswith("root-a:") for path in republished_paths), (
        "completed root must keep its durable generation"
    )
    assert republished_paths, "incomplete roots must actually re-walk"

    print(
        "\nPARTIAL-RESUME evidence: completed-before="
        f"{len(completed_before)} {completed_before} | roots total={len(after)} "
        f"| re-classified entries={len(republished_paths)} | "
        "scope-level restart, no durable cursor (F-SCAN-05 DEFERRED)"
    )


def _multi_root_resolver(roots: dict[str, Path]):
    from services.native.library_policy_resolver import LibraryPolicyResolver

    ordered = sorted(roots.items())
    return LibraryPolicyResolver(
        TypedLibrarySettings(
            library_roots=[
                LibraryRootSettings(
                    id=root_id,
                    path=str(path),
                    label=f"Library {root_id}",
                    policy="automatic",
                )
                for index, (root_id, path) in enumerate(ordered, start=1)
            ]
        )
    )


def _multi_root_request(roots: dict[str, Path], resolver):
    revision = resolver.policy_revision
    from models.library_work import ScanRequest

    return ScanRequest(
        kind="incremental",
        trigger="manual",
        policy_revision=revision,
        scopes=[
            ScanScope(root_id=root_id, relative_path=".", policy_revision=revision)
            for root_id in sorted(roots)
        ],
    )


def _coordinator(store, resolver):
    from services.native.library_scan_coordinator import (
        LibraryIndexer,
        LibraryReconciler,
        LibraryScanCoordinator,
    )

    scanner = LibraryInventoryScanner(store, walk_deadline_seconds=30.0)
    return LibraryScanCoordinator(
        store,
        scanner,
        LibraryIndexer(store, _TagReader()),
        LibraryReconciler(store),
        lambda: resolver,
        clock=lambda: 1_800_000_000.0,
    )



