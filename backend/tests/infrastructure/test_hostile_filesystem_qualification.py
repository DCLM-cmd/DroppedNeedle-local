"""NEW-QUAL-01: hostile-filesystem and interrupted-work qualification.

Real SQLite plus the real ``LibraryInventoryScanner`` walk path against
temporary hostile fixtures. Every assertion records durable outcomes or
filesystem-operation counts - never wall-clock speed. F-SCAN-05 stays
DEFERRED: scope-level restart only, no durable walk cursor."""

import ast
import asyncio
import hashlib
import os
import sqlite3
import stat
import threading
import time
import unicodedata
from pathlib import Path

import pytest

from api.v1.schemas.library_policies import LibraryRootSettings, TypedLibrarySettings
from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_work import ScanFailureRecord, ScanRun, ScanScope
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
        self.classify_run_ids: list = []

    async def classify_scan_paths(self, root_id, entries, *, run_id=None):
        # (relative, size, mtime_ns, mtime, revision) tuples per caller.
        self.classify_run_ids.append(run_id)
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


def test_watchdog_receives_the_real_five_starter_map() -> None:
    """The scan supervisor MUST be inside the watchdog's restart map alongside
    the three workers and the filesystem watcher (S-01 Hook C, step 2.8) -
    parsed from target_application.py so the contract tracks the actual
    application wiring, not a test-local copy."""
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
        "WATCHER_TASK_NAME",
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

    async def spy_classify(store_self, root_id, entries, *, run_id=None):
        if any(key != "root-a" for key in [root_id]):
            for entry in entries:
                republished_paths.append(f"{root_id}:{entry[0]}")
        return await real_classify(store_self, root_id, entries, run_id=run_id)

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


# --- F-12/F-13 (step 3.7, T38): wedged-read reapers + non-regular gate ---
#
# Fake-stat/tag-reader injection ONLY throughout: never a real FIFO (`open()`
# on a writerless FIFO blocks forever and would wedge test threads past the
# detach).


def _native_store(db_path: Path) -> NativeLibraryStore:
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    return NativeLibraryStore(db_path=db_path, write_lock=threading.Lock())


class _GatedTagReader(_TagReader):
    """_TagReader that blocks on a per-file gate (None = read immediately)."""

    def __init__(self, gates: dict[str, threading.Event]) -> None:
        super().__init__()
        self.gates = gates

    def read_tags(self, path: Path):
        gate = self.gates.get(path.name)
        if gate is not None:
            gate.wait(10)
        return super().read_tags(path)


def test_detached_reap_multipliers_pin_three() -> None:
    """N=3 for both reapers: the horizon is N x the 30s base timeout."""
    from services.native.library_indexer import DETACHED_TAG_READ_REAP_MULTIPLIER
    from services.native.library_inventory_scanner import (
        DETACHED_WALKER_REAP_MULTIPLIER,
    )

    assert DETACHED_TAG_READ_REAP_MULTIPLIER == 3.0
    assert DETACHED_WALKER_REAP_MULTIPLIER == 3.0


@pytest.mark.asyncio
async def test_four_wedged_tag_reads_drain_past_horizon(tmp_path: Path) -> None:
    """4 wedged reads fill the detach set; past N x timeout the next read
    reaps them on refusal and proceeds instead of deferring."""
    from services.native.library_indexer import (
        DETACHED_TAG_READ_REAP_MULTIPLIER,
        LibraryIndexer,
        _TagReadTimedOut,
    )

    root = tmp_path / "music"
    root.mkdir()
    paths = [root / f"track-{number}.flac" for number in range(1, 6)]
    for path in paths:
        path.write_bytes(b"audio")
    gate = threading.Event()
    reader = _GatedTagReader({path.name: gate for path in paths[:4]})
    now = [1000.0]
    indexer = LibraryIndexer(
        _native_store(tmp_path / "target.db"),
        reader,
        tag_read_timeout_seconds=0.05,
        max_detached_tag_reads=4,
        monotonic_clock=lambda: now[0],
    )
    wedged: list = []
    try:
        for path in paths[:4]:
            with pytest.raises(_TagReadTimedOut):
                await indexer._read_tags_and_stat(path, "root-a")
        assert len(indexer._detached_tag_reads) == 4
        wedged = list(indexer._detached_tag_reads)
        horizon = 0.05 * DETACHED_TAG_READ_REAP_MULTIPLIER
        now[0] += horizon + 1.0
        tag, _info, _stat_result = await indexer._read_tags_and_stat(
            paths[4], "root-a"
        )
        assert tag.title == "Track 5"
        assert len(indexer._detached_tag_reads) == 0
    finally:
        gate.set()
        if wedged:
            await asyncio.gather(*wedged, return_exceptions=True)
        for _ in range(50):
            if not indexer._detached_tag_reads:
                break
            await asyncio.sleep(0.01)
        assert indexer._detached_tag_reads == set()


@pytest.mark.asyncio
async def test_evicted_live_tag_read_finish_is_noop(tmp_path: Path) -> None:
    """Evicting a live thread's entry is safe: its later done-callback
    discard is a no-op and the exception is still consumed."""
    from services.native.library_indexer import LibraryIndexer

    root = tmp_path / "music"
    root.mkdir()
    first = root / "track-1.flac"
    second = root / "track-2.flac"
    first.write_bytes(b"audio")
    second.write_bytes(b"audio")
    gate = threading.Event()
    reader = _GatedTagReader({first.name: gate, second.name: gate})
    now = [2000.0]
    indexer = LibraryIndexer(
        _native_store(tmp_path / "target.db"),
        reader,
        tag_read_timeout_seconds=0.05,
        max_detached_tag_reads=1,
        monotonic_clock=lambda: now[0],
    )
    from services.native.library_indexer import _TagReadTimedOut

    live: list = []
    try:
        with pytest.raises(_TagReadTimedOut):
            await indexer._read_tags_and_stat(first, "root-a")
        assert len(indexer._detached_tag_reads) == 1
        live = list(indexer._detached_tag_reads)
        now[0] += 3 * 0.05 + 1.0
        # Refusal reaps the still-running first entry, then detaches this one.
        with pytest.raises(_TagReadTimedOut):
            await indexer._read_tags_and_stat(second, "root-a")
        assert live[0] not in indexer._detached_tag_reads
        assert len(indexer._detached_tag_reads) == 1
        assert not live[0].done()
    finally:
        gate.set()
        if live:
            await asyncio.gather(*live, *indexer._detached_tag_reads,
                                 return_exceptions=True)
        for _ in range(50):
            if not indexer._detached_tag_reads:
                break
            await asyncio.sleep(0.01)
        assert indexer._detached_tag_reads == set()
        assert indexer._detached_tag_read_started == {}
        assert live and live[0].done() and live[0].exception() is None


@pytest.mark.asyncio
async def test_completed_tag_read_reaps_other_wedged_reads(tmp_path: Path) -> None:
    """Opportunistic reap on completion: one detached read finishing drains
    the other still-wedged entry without any refusal."""
    from services.native.library_indexer import LibraryIndexer

    root = tmp_path / "music"
    root.mkdir()
    first = root / "track-1.flac"
    second = root / "track-2.flac"
    first.write_bytes(b"audio")
    second.write_bytes(b"audio")
    gate_first = threading.Event()
    gate_second = threading.Event()
    reader = _GatedTagReader({first.name: gate_first, second.name: gate_second})
    now = [3000.0]
    indexer = LibraryIndexer(
        _native_store(tmp_path / "target.db"),
        reader,
        tag_read_timeout_seconds=0.05,
        max_detached_tag_reads=4,
        monotonic_clock=lambda: now[0],
    )
    from services.native.library_indexer import _TagReadTimedOut

    try:
        with pytest.raises(_TagReadTimedOut):
            await indexer._read_tags_and_stat(first, "root-a")
        with pytest.raises(_TagReadTimedOut):
            await indexer._read_tags_and_stat(second, "root-a")
        assert len(indexer._detached_tag_reads) == 2
        now[0] += 3 * 0.05 + 1.0
        gate_first.set()
        for _ in range(100):
            if len(indexer._detached_tag_reads) == 0:
                break
            await asyncio.sleep(0.01)
        # The second entry drained while its thread is still wedged.
        assert indexer._detached_tag_reads == set()
        assert not gate_second.is_set()
    finally:
        gate_first.set()
        gate_second.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_deferred_file_reindexes_next_run(tmp_path: Path) -> None:
    """A TAG_READ_DEFERRED file with unchanged stat re-offers as changed on
    the next run (classify), and the clean read clears the marker."""
    from services.native.library_scan_coordinator import (
        LibraryIndexer,
        LibraryReconciler,
        LibraryScanCoordinator,
    )

    root = tmp_path / "music"
    root.mkdir()
    (root / "track-1.flac").write_bytes(b"audio")
    store = _native_store(tmp_path / "target.db")
    roots = {"root-a": root}
    resolver = _multi_root_resolver(roots)

    def _coordinator_with(reader):
        return LibraryScanCoordinator(
            store,
            LibraryInventoryScanner(store, walk_deadline_seconds=30.0),
            LibraryIndexer(store, reader),
            LibraryReconciler(store),
            lambda: resolver,
            clock=lambda: 1_800_000_000.0,
        )

    await _coordinator_with(_TagReader()).request_run(
        _multi_root_request(roots, resolver)
    )
    first = await _coordinator_with(_TagReader()).run_once(dict(roots))
    assert first is not None and first.state == "completed"
    _, _, first_counters = await store.get_scan_run(first.id)
    assert first_counters["indexed_count"] == 1

    await store.record_scan_failures(
        first.id,
        [
            ScanFailureRecord(
                root_id="root-a",
                relative_path="track-1.flac",
                failure_code="TAG_READ_DEFERRED",
                recorded_at=1_800_000_001.0,
                failure_detail="deferred for qualification",
                phase="indexing",
            )
        ],
    )

    second_reader = _TagReader()
    await _coordinator_with(second_reader).request_run(
        _multi_root_request(roots, resolver)
    )
    second = await _coordinator_with(second_reader).run_once(dict(roots))
    assert second is not None and second.state == "completed"
    assert "track-1.flac" in [path.name for path in second_reader.calls]
    _, _, second_counters = await store.get_scan_run(second.id)
    assert second_counters["indexed_count"] == 1
    with sqlite3.connect(store.db_path) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM library_scan_failures "
            "WHERE failure_code = 'TAG_READ_DEFERRED'"
        ).fetchone()[0]
    assert remaining == 0


@pytest.mark.asyncio
async def test_fifo_audio_suffix_skips_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FIFO named *.flac skips in the inventory phase with
    NON_REGULAR_FILE (fake stat only, never a real FIFO) without touching
    any tag-reader or walker slot."""
    root = tmp_path / "music"
    root.mkdir()
    (root / "real.flac").write_bytes(b"real")
    real_stat = Path.stat

    def _fake_stat(self: Path, *, follow_symlinks: bool = True):
        if self.name == "pipe.flac":
            return os.stat_result(
                (stat.S_IFIFO | 0o644, 0, 0, 1, 0, 0, 0, 0, 0, 0)
            )
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", _fake_stat)

    def _fake_walker(selected, followlinks=False, onerror=None):
        yield (str(root), [], ["pipe.flac", "real.flac"])

    store = _RecordingStore()
    scanner = LibraryInventoryScanner(store, directory_walker=_fake_walker)
    scope = ScanScope(root_id="root", policy_revision="policy-1", relative_path=".")
    started = time.monotonic()
    _updated, completed, failure_code = await scanner._walk_scope(
        _scan_run(), scope, root, root, SimpleNamespace_resolve(), AsyncCheckpoint()
    )
    elapsed = time.monotonic() - started
    assert completed is True and failure_code is None
    published = [Path(item.absolute_path).name for batch in store.batches for item in batch]
    assert published == ["real.flac"]
    skips = [
        (record.failure_code, record.relative_path, record.phase)
        for batch in store.failures
        for record in batch
    ]
    assert skips == [("NON_REGULAR_FILE", "pipe.flac", "discovering")]
    assert scanner._detached_walkers == set()
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_stale_walkers_drain_on_refusal() -> None:
    """A full walker-detach set past N x the walk deadline drains on refusal
    instead of counting a leak."""
    from services.native.library_inventory_scanner import (
        DETACHED_WALKER_REAP_MULTIPLIER,
        LibraryInventoryScanner,
    )

    assert DETACHED_WALKER_REAP_MULTIPLIER == 3.0
    now = [4000.0]
    scanner = LibraryInventoryScanner(
        _RecordingStore(),
        walk_deadline_seconds=30.0,
        max_detached_walkers=1,
        monotonic_clock=lambda: now[0],
    )
    gate_first = asyncio.Event()
    gate_second = asyncio.Event()

    async def _wedged_first() -> None:
        await gate_first.wait()

    async def _wedged_second() -> None:
        await gate_second.wait()

    task_first = asyncio.create_task(_wedged_first())
    try:
        assert scanner._detach_walker(task_first) is True
        now[0] += 3 * 30.0 + 1.0
        task_second = asyncio.create_task(_wedged_second())
        # Reaps the still-running first walker, then tracks the second.
        assert scanner._detach_walker(task_second) is True
        assert task_first not in scanner._detached_walkers
        assert scanner.leaked_walker_count == 0
        gate_first.set()
        gate_second.set()
        await asyncio.gather(task_first, task_second)
        for _ in range(50):
            if not scanner._detached_walkers:
                break
            await asyncio.sleep(0.01)
        assert scanner._detached_walkers == set()
    finally:
        gate_first.set()
        gate_second.set()


# --- Slice D / step 4.13: NFC-normalized inventory keys + twin rule ---
#
# Fake-stat/tag-reader injection ONLY throughout: neither test creates a
# real NFC/NFD twin on disk (the walker is faked and Path.stat is patched
# to report a canned regular file for both names).


def _nfc_nfd_pair() -> tuple[str, str]:
    nfc = unicodedata.normalize("NFC", "café.flac")
    nfd = unicodedata.normalize("NFD", "café.flac")
    assert nfc != nfd, "expected distinct NFC/NFD byte forms on this platform"
    assert unicodedata.normalize("NFC", nfd) == nfc
    return nfc, nfd


def _canned_regular_stat(
    probe: Path, names: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report the probe file's real stat for the given names (fake stat
    only: the named files never exist on disk)."""
    canned = probe.stat()
    real_stat = Path.stat

    def _fake_stat(self: Path, *, follow_symlinks: bool = True):
        if self.name in names:
            return canned
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", _fake_stat)


def _listing_walker(root: Path, listings: list[list[str]]):
    def _fake_walker(selected, followlinks=False, onerror=None):
        for names in listings:
            yield (str(root), [], list(names))

    return _fake_walker


@pytest.mark.asyncio
async def test_nfc_to_nfd_rename_keeps_inventory_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 4.13 rename churn: an NFC->NFD rename of an unchanged file
    publishes one stable NFC inventory key across both walks (a move, not
    delete+new). Fails pre-fix: the second walk publishes the raw NFD key."""
    nfc, nfd = _nfc_nfd_pair()
    root = tmp_path / "music"
    root.mkdir()
    probe = root / "probe.flac"
    probe.write_bytes(b"audio")
    _canned_regular_stat(probe, {nfc, nfd}, monkeypatch)

    keys: list[list[str]] = []
    for name in (nfc, nfd):
        store = _RecordingStore()
        scanner = LibraryInventoryScanner(
            store, directory_walker=_listing_walker(root, [[name]])
        )
        scope = ScanScope(
            root_id="root", policy_revision="policy-1", relative_path="."
        )
        _updated, completed, failure_code = await scanner._walk_scope(
            _scan_run(), scope, root, root, SimpleNamespace_resolve(),
            AsyncCheckpoint(),
        )
        assert completed is True and failure_code is None
        keys.append(
            [item.relative_path for batch in store.batches for item in batch]
        )
        # Skew wiring: the run id reaches classify_scan_paths so prod
        # MTIME_SKEW rows are actually recorded (store defaults to None).
        assert store.classify_run_ids == ["run-1"]
    assert keys == [[nfc], [nfc]], (
        "both walks must publish the single NFC inventory key"
    )


@pytest.mark.asyncio
async def test_nfc_nfd_twins_first_wins_with_loser_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twin collision: coexisting NFC/NFD names map to one normalized key -
    first wins in inventory, the loser gets an inventory-phase
    (discovering) failure row, and the run stays green. Fails pre-fix: two
    inventory rows land and no loser row is recorded."""
    nfc, nfd = _nfc_nfd_pair()
    root = tmp_path / "music"
    root.mkdir()
    probe = root / "probe.flac"
    probe.write_bytes(b"audio")
    _canned_regular_stat(probe, {nfc, nfd}, monkeypatch)

    store = _RecordingStore()
    scanner = LibraryInventoryScanner(
        store, directory_walker=_listing_walker(root, [[nfc, nfd]])
    )
    scope = ScanScope(root_id="root", policy_revision="policy-1", relative_path=".")
    _updated, completed, failure_code = await scanner._walk_scope(
        _scan_run(), scope, root, root, SimpleNamespace_resolve(),
        AsyncCheckpoint(),
    )
    assert completed is True and failure_code is None
    published = [item.relative_path for batch in store.batches for item in batch]
    assert published == [nfc], "first twin wins the single NFC inventory key"
    rows = [record for batch in store.failures for record in batch]
    assert [
        (record.failure_code, record.relative_path, record.phase)
        for record in rows
    ] == [("NFC_TWIN_COLLISION", nfd, "discovering")]
    assert rows[0].failure_detail.startswith("Two on-disk names normalize")



