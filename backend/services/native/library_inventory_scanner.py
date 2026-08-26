"""One-walk, bounded-queue discovery for the inactive target catalog."""

from __future__ import annotations

import asyncio
import errno
import os
import threading
import time
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import msgspec

from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_work import (
    ScanFailureRecord,
    ScanInventoryItem,
    ScanRun,
    ScanScope,
)
from services.local_files_service import AUDIO_EXTENSIONS
from services.native.library_filesystem_coordinator import (
    LibraryFilesystemCoordinator,
    is_management_artifact,
)
from services.native.library_policy_resolver import LibraryPolicyResolver
from services.native.file_revision import revision_from_stat

INVENTORY_QUEUE_SIZE = 256
INVENTORY_BATCH_SIZE = 256

Checkpoint = Callable[[str, str], Awaitable[bool]]
DirectoryWalker = Callable[..., Iterator[tuple[str, list[str], list[str]]]]
DirectoryProbe = Callable[[Path], bool]
logger = logging.getLogger(__name__)


@contextmanager
def _uncoordinated_read() -> Iterator[None]:
    yield


class _WalkHeartbeat:
    """Thread-safe liveness signal written by the walk producer thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._touched_at = time.monotonic()
        self.last_directory = ""

    def touch(self, directory: str = "") -> None:
        with self._lock:
            self._touched_at = time.monotonic()
            if directory:
                self.last_directory = directory

    def age(self) -> float:
        with self._lock:
            return time.monotonic() - self._touched_at


class LibraryInventoryScanner:
    def __init__(
        self,
        store: NativeLibraryStore,
        *,
        directory_walker: DirectoryWalker = os.walk,
        filesystem_coordinator: LibraryFilesystemCoordinator | None = None,
        walk_deadline_seconds: float = 30.0,
        directory_probe: DirectoryProbe = Path.is_dir,
        max_detached_walkers: int = 4,
    ) -> None:
        self._store = store
        self._directory_walker = directory_walker
        self._filesystem = filesystem_coordinator
        self._walk_deadline_seconds = walk_deadline_seconds
        self._directory_probe = directory_probe
        self._max_detached_walkers = max_detached_walkers
        self._detached_walkers: set[asyncio.Task[None]] = set()

    def _finish_detached_walker(self, task: asyncio.Task[None]) -> None:
        self._detached_walkers.discard(task)
        if not task.cancelled():
            task.exception()

    def _detach_walker(self, task: asyncio.Task[None]) -> None:
        if task.done():
            return
        if len(self._detached_walkers) < self._max_detached_walkers:
            self._detached_walkers.add(task)
        task.add_done_callback(self._finish_detached_walker)

    async def _record_failure(
        self,
        run_id: str,
        scope: ScanScope,
        *,
        relative_path: str,
        failure_code: str,
        failure_detail: str,
    ) -> None:
        await self._store.record_scan_failures(
            run_id,
            [
                ScanFailureRecord(
                    root_id=scope.root_id,
                    relative_path=relative_path,
                    failure_code=failure_code,
                    recorded_at=time.time(),
                    failure_detail=failure_detail,
                    phase="discovering",
                )
            ],
        )

    @staticmethod
    def _relativize(path: Path, root: Path) -> str:
        try:
            return PurePosixPath(*path.relative_to(root).parts).as_posix()
        except ValueError:
            return path.as_posix()

    @staticmethod
    def _failure_relative_path(
        exc: BaseException, root: Path, heartbeat: _WalkHeartbeat
    ) -> str:
        filename = getattr(exc, "filename", None)
        candidate = (
            Path(str(filename))
            if filename
            else (Path(heartbeat.last_directory) if heartbeat.last_directory else None)
        )
        if candidate is None:
            return "."
        return LibraryInventoryScanner._relativize(candidate, root)

    async def discover(
        self,
        run: ScanRun,
        scopes: list[ScanScope],
        root_paths: dict[str, Path],
        resolver: LibraryPolicyResolver,
        checkpoint: Checkpoint,
    ) -> ScanRun:
        current = run
        for scope in scopes:
            if (
                await self._store.get_scan_scope_discovery_state(
                    run.id, scope.root_id, scope.relative_path
                )
                == "completed"
            ):
                continue
            if not await checkpoint(run.id, scope.policy_revision):
                return (await self._store.get_scan_run(run.id))[0]
            root = root_paths.get(scope.root_id)
            if root is None and scope.root_path is not None:
                root = Path(scope.root_path)
            if root is None:
                await self._record_failure(
                    run.id,
                    scope,
                    relative_path=scope.relative_path,
                    failure_code="ROOT_UNAVAILABLE",
                    failure_detail="The library root has no configured path.",
                )
                await self._store.complete_scan_scope_discovery(
                    run.id,
                    scope.root_id,
                    scope.relative_path,
                    state="unavailable",
                    error_code="ROOT_UNAVAILABLE",
                )
                return await self._store.transition_scan_run(
                    run.id,
                    expected_state=current.state,
                    expected_revision=current.row_revision,
                    new_state="failed",
                    now=current.updated_at,
                    terminal_code="ROOT_UNAVAILABLE",
                )
            selected = (
                root if scope.relative_path == "." else root / scope.relative_path
            )
            try:
                exists = await asyncio.wait_for(
                    asyncio.to_thread(self._directory_probe, selected),
                    timeout=self._walk_deadline_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "library_scan event=walk_timeout run_id=%s root_id=%s path=%s",
                    run.id,
                    scope.root_id,
                    scope.relative_path,
                )
                await self._record_failure(
                    run.id,
                    scope,
                    relative_path=scope.relative_path,
                    failure_code="WALK_TIMEOUT",
                    failure_detail=(
                        "The library root probe exceeded "
                        f"{self._walk_deadline_seconds:.1f}s."
                    ),
                )
                await self._store.complete_scan_scope_discovery(
                    run.id,
                    scope.root_id,
                    scope.relative_path,
                    state="unavailable",
                    error_code="WALK_TIMEOUT",
                )
                return await self._store.transition_scan_run(
                    run.id,
                    expected_state=current.state,
                    expected_revision=current.row_revision,
                    new_state="failed",
                    now=current.updated_at,
                    terminal_code="WALK_TIMEOUT",
                )
            if not exists:
                await self._record_failure(
                    run.id,
                    scope,
                    relative_path=scope.relative_path,
                    failure_code="ROOT_UNAVAILABLE",
                    failure_detail=f"The library root path is missing: {selected}",
                )
                await self._store.complete_scan_scope_discovery(
                    run.id,
                    scope.root_id,
                    scope.relative_path,
                    state="unavailable",
                    error_code="ROOT_UNAVAILABLE",
                )
                return await self._store.transition_scan_run(
                    run.id,
                    expected_state=current.state,
                    expected_revision=current.row_revision,
                    new_state="failed",
                    now=current.updated_at,
                    terminal_code="ROOT_UNAVAILABLE",
                )
            while True:
                discovery_generation = (
                    await self._store.get_scan_scope_discovery_generation(
                        run.id, scope.root_id, scope.relative_path
                    )
                )
                filesystem_revision = (
                    self._filesystem.revision(scope.root_id)
                    if self._filesystem is not None
                    else None
                )
                current, completed, walk_failure_code = await self._walk_scope(
                    current,
                    scope,
                    root,
                    selected,
                    resolver,
                    checkpoint,
                    discovery_generation,
                )
                if not completed or self._filesystem is None:
                    break
                async with self._filesystem.read(scope.root_id):
                    if self._filesystem.revision(scope.root_id) == filesystem_revision:
                        self._filesystem.record_scan_revision(run.id, scope.root_id)
                        break
                    await self._store.restart_scan_scope_discovery(
                        run.id, scope.root_id, scope.relative_path
                    )
                    current = (await self._store.get_scan_run(run.id))[0]
                await self._store.cleanup_stale_scan_inventory(run.id)
            if not completed:
                current = (await self._store.get_scan_run(run.id))[0]
                if current.state == "discovering":
                    current = await self._store.transition_scan_run(
                        run.id,
                        expected_state="discovering",
                        expected_revision=current.row_revision,
                        new_state="failed",
                        now=current.updated_at,
                        terminal_code=walk_failure_code or "ROOT_PERMISSION_DENIED",
                    )
                return current
            await self._store.complete_scan_scope_discovery(
                run.id,
                scope.root_id,
                scope.relative_path,
                state="completed",
                error_code=None,
            )
        return current

    async def _walk_scope(
        self,
        run: ScanRun,
        scope: ScanScope,
        root: Path,
        selected: Path,
        resolver: LibraryPolicyResolver,
        checkpoint: Checkpoint,
        discovery_generation: int = 1,
    ) -> tuple[ScanRun, bool, str | None]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[Path, os.stat_result] | BaseException | None] = (
            asyncio.Queue(maxsize=INVENTORY_QUEUE_SIZE)
        )
        stopped = threading.Event()
        heartbeat = _WalkHeartbeat()

        def producer() -> None:
            try:

                def onerror(error: OSError) -> None:
                    raise error

                walker = iter(
                    self._directory_walker(selected, followlinks=False, onerror=onerror)
                )
                while True:
                    if stopped.is_set():
                        break
                    lease = (
                        self._filesystem.read_sync(scope.root_id)
                        if self._filesystem is not None
                        else _uncoordinated_read()
                    )
                    with lease:
                        try:
                            directory, subdirectories, filenames = next(walker)
                        except StopIteration:
                            break
                        heartbeat.touch(directory)
                        subdirectories[:] = [
                            name
                            for name in subdirectories
                            if not is_management_artifact(Path(name))
                        ]
                        inspected: list[
                            tuple[Path, os.stat_result] | BaseException
                        ] = []
                        for filename in filenames:
                            heartbeat.touch(directory)
                            path = Path(directory) / filename
                            if (
                                path.suffix.casefold() not in AUDIO_EXTENSIONS
                                or is_management_artifact(path)
                            ):
                                continue
                            resolved = path.resolve(strict=False)
                            if not resolved.is_relative_to(root):
                                continue
                            try:
                                inspected.append((resolved, resolved.stat()))
                            except FileNotFoundError:
                                continue
                            except OSError as exc:
                                inspected.append(exc)
                    for item in inspected:
                        asyncio.run_coroutine_threadsafe(queue.put(item), loop).result()
            except (OSError, RuntimeError) as exc:
                asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

        producer_task = asyncio.create_task(asyncio.to_thread(producer))
        batch: list[tuple[Path, os.stat_result]] = []
        current = run
        completed = True
        discard_remaining = False
        detached = False
        walk_failure_code: str | None = None
        discovered = 0
        stale_cleanup_pending = True
        last_checkpoint = time.monotonic()
        last_log = last_checkpoint
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.25)
                except TimeoutError:
                    if heartbeat.age() > self._walk_deadline_seconds:
                        completed = False
                        stopped.set()
                        walk_failure_code = "WALK_TIMEOUT"
                        logger.warning(
                            "library_scan event=walk_timeout run_id=%s root_id=%s "
                            "path=%s",
                            run.id,
                            scope.root_id,
                            heartbeat.last_directory or scope.relative_path,
                        )
                        await self._record_failure(
                            run.id,
                            scope,
                            relative_path=(
                                self._relativize(Path(heartbeat.last_directory), root)
                                if heartbeat.last_directory
                                else scope.relative_path
                            ),
                            failure_code="WALK_TIMEOUT",
                            failure_detail=(
                                "The directory walk made no progress for "
                                f"{self._walk_deadline_seconds:.1f}s."
                            ),
                        )
                        # The producer thread is wedged in a syscall; awaiting it
                        # would wedge the scan worker, so it is detached instead.
                        self._detach_walker(producer_task)
                        detached = True
                        break
                    if not await checkpoint(run.id, scope.policy_revision):
                        completed = False
                        stopped.set()
                        discard_remaining = True
                    last_checkpoint = time.monotonic()
                    continue
                if item is None:
                    break
                if discard_remaining:
                    continue
                if isinstance(item, BaseException):
                    completed = False
                    stopped.set()
                    discard_remaining = True
                    walk_failure_code = "ROOT_PERMISSION_DENIED"
                    relative_path = self._failure_relative_path(item, root, heartbeat)
                    failure_code = (
                        "WALK_" + errno.errorcode.get(item.errno, "EUNKNOWN")
                        if isinstance(item, OSError) and item.errno is not None
                        else "WALK_ERROR"
                    )
                    logger.warning(
                        "library_scan event=walk_error run_id=%s root_id=%s path=%s "
                        "error=%s",
                        run.id,
                        scope.root_id,
                        relative_path,
                        item,
                    )
                    await self._record_failure(
                        run.id,
                        scope,
                        relative_path=relative_path,
                        failure_code=failure_code,
                        failure_detail=str(item),
                    )
                    continue
                batch.append(item)
                if len(batch) >= INVENTORY_BATCH_SIZE:
                    current = await self._persist_batch(
                        current,
                        scope,
                        root,
                        batch,
                        resolver,
                        discovery_generation,
                    )
                    discovered += len(batch)
                    batch = []
                    if stale_cleanup_pending:
                        stale_cleanup_pending = bool(
                            await self._store.cleanup_stale_scan_inventory(run.id)
                        )
                    if not await checkpoint(run.id, scope.policy_revision):
                        completed = False
                        stopped.set()
                        discard_remaining = True
                    last_checkpoint = time.monotonic()
                elif time.monotonic() - last_checkpoint >= 0.25:
                    if not await checkpoint(run.id, scope.policy_revision):
                        completed = False
                        stopped.set()
                        discard_remaining = True
                    last_checkpoint = time.monotonic()
                if time.monotonic() - last_log >= 30.0:
                    logger.info(
                        "library_scan event=discovery_progress discovered=%d",
                        discovered + len(batch),
                    )
                    last_log = time.monotonic()
            if batch and completed:
                current = await self._persist_batch(
                    current,
                    scope,
                    root,
                    batch,
                    resolver,
                    discovery_generation,
                )
                discovered += len(batch)
                if stale_cleanup_pending:
                    await self._store.cleanup_stale_scan_inventory(run.id)
        except asyncio.CancelledError:
            stopped.set()
            while not producer_task.done():
                try:
                    await asyncio.wait_for(queue.get(), timeout=0.1)
                except TimeoutError:
                    if heartbeat.age() > self._walk_deadline_seconds:
                        self._detach_walker(producer_task)
                        detached = True
                        break
                    continue
            if not detached:
                await asyncio.shield(producer_task)
            raise
        finally:
            stopped.set()
            if not detached and not producer_task.done():
                await producer_task
        if not completed:
            await self._store.complete_scan_scope_discovery(
                run.id,
                scope.root_id,
                scope.relative_path,
                state="partially_read",
                error_code=walk_failure_code or "ROOT_PERMISSION_DENIED",
            )
        return current, completed, walk_failure_code

    async def _persist_batch(
        self,
        run: ScanRun,
        scope: ScanScope,
        root: Path,
        batch: list[tuple[Path, os.stat_result]],
        resolver: LibraryPolicyResolver,
        discovery_generation: int,
    ) -> ScanRun:
        raw: list[tuple[Path, str, os.stat_result, str]] = []
        for path, stat in batch:
            relative = PurePosixPath(*path.relative_to(root).parts).as_posix()
            raw.append((path, relative, stat, revision_from_stat(stat)))
        comparisons = await self._store.classify_scan_paths(
            scope.root_id,
            [
                (relative, stat.st_size, stat.st_mtime_ns, stat.st_mtime, revision)
                for _, relative, stat, revision in raw
            ],
        )
        items: list[ScanInventoryItem] = []
        for path, relative, stat, revision in raw:
            policy = resolver.resolve(path)
            effective_policy = (
                policy.policy if policy is not None else scope.effective_policy
            )
            comparison, track_id = comparisons[relative]
            if effective_policy == "excluded":
                comparison = "excluded"
            items.append(
                ScanInventoryItem(
                    root_id=scope.root_id,
                    relative_path=relative,
                    absolute_path=str(path),
                    file_size_bytes=stat.st_size,
                    file_mtime_ns=stat.st_mtime_ns,
                    stat_revision=revision,
                    policy_revision=scope.policy_revision,
                    effective_policy=effective_policy,
                    comparison_result=comparison,
                    local_track_id=track_id,
                    scope_relative_path=scope.relative_path,
                )
            )
        updated_at = time.time()
        revision, _ = await self._store.add_scan_inventory_batch(
            run.id,
            items,
            expected_run_revision=run.row_revision,
            updated_at=updated_at,
            discovery_generation=discovery_generation,
        )
        return msgspec.structs.replace(
            run, row_revision=revision, updated_at=updated_at
        )
