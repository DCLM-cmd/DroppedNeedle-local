"""Short per-root filesystem leases shared by scans and publishers."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
import os
import uuid
from pathlib import Path
from pathlib import PurePosixPath
import stat

from core.exceptions import LibraryManagementDestinationConflictError

_RENAME_NOREPLACE = 1 << 0
# Kernels/filesystems without renameat2 support report these errnos; the
# publication then falls back to the previous recheck-then-replace behavior.
_NOREPLACE_UNSUPPORTED_ERRNOS = frozenset(
    {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTTY}
)
_LIBC = ctypes.CDLL(None, use_errno=True)
# renameat2 is Linux-only. On a platform that simply does not export it (macOS, and
# any libc without the syscall) resolving the symbol raises AttributeError rather
# than returning an errno, which escaped the caller's fallback and turned every
# publication into a crash instead of the documented recheck-then-replace. Resolved
# once here so the missing symbol reports as ENOSYS - the same condition the errno
# path already handles - and the fallback below covers both.
try:
    _RENAMEAT2 = _LIBC.renameat2
except AttributeError:
    _RENAMEAT2 = None


class _RootLeaseState:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.readers = 0
        self.writer_active = False
        self.waiting_writers = 0
        self.revision = 0

    def acquire_read(self) -> None:
        with self.condition:
            while self.writer_active or self.waiting_writers:
                self.condition.wait()
            self.readers += 1

    def release_read(self) -> None:
        with self.condition:
            self.readers -= 1
            if self.readers == 0:
                self.condition.notify_all()

    def register_write_waiter(self) -> None:
        with self.condition:
            self.waiting_writers += 1

    def unregister_write_waiter(self) -> None:
        with self.condition:
            self.waiting_writers -= 1
            # a departing pending writer may unblock parked readers
            self.condition.notify_all()

    def acquire_registered_write(self) -> None:
        with self.condition:
            try:
                while self.writer_active or self.readers:
                    self.condition.wait()
                self.writer_active = True
            finally:
                self.waiting_writers -= 1

    def acquire_write(self) -> None:
        self.register_write_waiter()
        self.acquire_registered_write()

    def release_write(self) -> None:
        with self.condition:
            self.writer_active = False
            self.revision += 1
            self.condition.notify_all()

    def current_revision(self) -> int:
        with self.condition:
            return self.revision


class LibraryFilesystemCoordinator:
    """Writer-preferring read/write leases, isolated by stable library-root ID.

    The coordinator is deliberately in-process: production uses one worker. Durable
    publication and recovery state belongs in SQLite and the filesystem journal, not
    in this object.
    """

    def __init__(self) -> None:
        self._states: dict[str, _RootLeaseState] = {}
        self._states_lock = threading.Lock()
        self._scan_revisions: dict[tuple[str, str], int] = {}
        # Quiesce support: a settings dry run must plan against a library that is
        # standing still. See ``quiesced``.
        self._quiesce_depth = 0
        self._not_quiesced = asyncio.Event()
        self._not_quiesced.set()
        self._active_writes = 0
        self._writes_drained = asyncio.Event()
        self._writes_drained.set()

    @property
    def quiesced(self) -> bool:
        """Whether filesystem mutation is currently suspended."""
        return self._quiesce_depth > 0

    @asynccontextmanager
    async def quiesce(self) -> AsyncIterator[None]:
        """Suspend every library write for the duration of the block.

        A dry run for the automation settings plans against what is on disk. If a
        scan import, an Organizer apply or a recovery pass moves files while it is
        planning, the plan it produces describes a library that no longer exists -
        which is how "a destination was created after planning" happens. Holding
        this blocks NEW writes and waits for in-flight ones to finish, so the
        preview sees a still library.

        Reads stay open: the dry run itself has to inspect the files.
        """
        self._quiesce_depth += 1
        self._not_quiesced.clear()
        try:
            # Let whatever was already mid-write finish before planning starts.
            await self._writes_drained.wait()
            yield
        finally:
            self._quiesce_depth -= 1
            if self._quiesce_depth <= 0:
                self._quiesce_depth = 0
                self._not_quiesced.set()

    def _enter_write(self) -> None:
        self._active_writes += 1
        self._writes_drained.clear()

    def _exit_write(self) -> None:
        self._active_writes -= 1
        if self._active_writes <= 0:
            self._active_writes = 0
            self._writes_drained.set()

    def _state(self, root_id: str) -> _RootLeaseState:
        if not root_id:
            raise ValueError("A filesystem lease requires a library root ID.")
        with self._states_lock:
            return self._states.setdefault(root_id, _RootLeaseState())

    def _ordered_states(
        self, root_ids: Iterable[str]
    ) -> list[tuple[str, _RootLeaseState]]:
        ordered = sorted(set(root_ids))
        if not ordered:
            raise ValueError("A filesystem lease requires at least one library root.")
        return [(root_id, self._state(root_id)) for root_id in ordered]

    @staticmethod
    async def _acquire_without_leaking_on_cancel(
        acquire: Callable[[], None], release: Callable[[], None]
    ) -> None:
        pending = asyncio.create_task(asyncio.to_thread(acquire))
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    continue
            pending.result()
            release()
            raise

    @asynccontextmanager
    async def read(self, root_id: str) -> AsyncIterator[None]:
        async with self.read_many([root_id]):
            yield

    @asynccontextmanager
    async def read_many(self, root_ids: Iterable[str]) -> AsyncIterator[None]:
        states = self._ordered_states(root_ids)
        acquired: list[_RootLeaseState] = []
        try:
            for _root_id, state in states:
                await self._acquire_without_leaking_on_cancel(
                    state.acquire_read, state.release_read
                )
                acquired.append(state)
            yield
        finally:
            for state in reversed(acquired):
                state.release_read()

    @asynccontextmanager
    async def write(self, root_id: str) -> AsyncIterator[None]:
        async with self.write_many([root_id]):
            yield

    @asynccontextmanager
    async def write_many(self, root_ids: Iterable[str]) -> AsyncIterator[None]:
        states = self._ordered_states(root_ids)
        acquired: list[_RootLeaseState] = []
        # Wait BEFORE taking any root lease: holding one while blocked on a quiesce
        # would stall the dry run's own reads behind this writer.
        await self._not_quiesced.wait()
        self._enter_write()
        try:
            # F-150: register every requested root as writer-pending BEFORE the
            # acquisition loop, so a reader for a later root cannot overtake a
            # writer still queued on an earlier one.
            for _root_id, state in states:
                state.register_write_waiter()
            for _root_id, state in states:
                await self._acquire_without_leaking_on_cancel(
                    state.acquire_registered_write, state.release_write
                )
                acquired.append(state)
            yield
        finally:
            # Both are required, and neither substitutes for the other: the leases
            # gate other writers on this root, while the counter is what tells a
            # waiting quiesce that the library has stopped moving. Releasing only
            # the leases leaves _active_writes climbing forever, so the first write
            # of the process makes every later dry run wait for a drain that can
            # never happen.
            self._exit_write()
            for state in reversed(acquired):
                state.release_write()
            # acquire_registered_write consumes its own registration, so only
            # the registered-but-not-acquired remainder needs unwinding.
            for _root_id, lease in states[len(acquired) :]:
                lease.unregister_write_waiter()

    @contextmanager
    def read_sync(self, root_id: str) -> Iterator[None]:
        state = self._state(root_id)
        state.acquire_read()
        try:
            yield
        finally:
            state.release_read()

    def revision(self, root_id: str) -> int:
        return self._state(root_id).current_revision()

    def record_scan_revision(self, run_id: str, root_id: str) -> None:
        revision = self.revision(root_id)
        with self._states_lock:
            self._scan_revisions[(run_id, root_id)] = revision

    def scan_revision(self, run_id: str, root_id: str) -> int:
        with self._states_lock:
            recorded = self._scan_revisions.get((run_id, root_id))
        return self.revision(root_id) if recorded is None else recorded

    def forget_scan(self, run_id: str) -> None:
        with self._states_lock:
            keys = [key for key in self._scan_revisions if key[0] == run_id]
            for key in keys:
                self._scan_revisions.pop(key, None)


MANAGEMENT_ARTIFACT_PREFIX = ".droppedneedle-management-"


def is_management_artifact(path: Path) -> bool:
    """Return whether a path uses the reserved hidden management namespace."""

    return any(part.startswith(MANAGEMENT_ARTIFACT_PREFIX) for part in path.parts)


@contextmanager
def _rooted_parent(root: Path, relative_path: str) -> Iterator[tuple[int, str]]:
    relative = PurePosixPath(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("A rooted filesystem path must be a safe relative path.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, flags | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, relative.parts[-1]
    finally:
        os.close(descriptor)


def _renameat2_noreplace(
    old_dir_fd: int, old_name: str, new_dir_fd: int, new_name: str
) -> None:
    """renameat2(RENAME_NOREPLACE): fail with EEXIST instead of overwriting."""

    if _RENAMEAT2 is None:
        raise OSError(
            errno.ENOSYS, os.strerror(errno.ENOSYS), os.fspath(new_name)
        )
    result = _RENAMEAT2(
        ctypes.c_int(old_dir_fd),
        ctypes.c_char_p(os.fsencode(old_name)),
        ctypes.c_int(new_dir_fd),
        ctypes.c_char_p(os.fsencode(new_name)),
        ctypes.c_uint(_RENAME_NOREPLACE),
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), os.fspath(new_name))


def replace_rooted_publication(
    roots: dict[str, Path],
    source_root_id: str,
    source_relative_path: str,
    destination_root_id: str,
    destination_relative_path: str,
) -> None:
    """Publish one staged temp onto its destination with a NOREPLACE backstop.

    F-112: the recheck-then-replace window must not silently overwrite an
    out-of-model external writer's file. Unsupported platforms/filesystems
    fall back to plain os.replace (previous behavior); an existing destination
    becomes LibraryManagementDestinationConflictError.
    """

    try:
        source_root = roots[source_root_id]
        destination_root = roots[destination_root_id]
    except KeyError as error:
        raise ValueError("A rooted replacement references an unknown root.") from error
    with _rooted_parent(source_root, source_relative_path) as source:
        with _rooted_parent(
            destination_root, destination_relative_path
        ) as destination:
            try:
                _renameat2_noreplace(
                    source[0], source[1], destination[0], destination[1]
                )
            except OSError as error:
                if error.errno == errno.EEXIST:
                    raise LibraryManagementDestinationConflictError(
                        "A management destination was created after preview."
                    ) from error
                if error.errno not in _NOREPLACE_UNSUPPORTED_ERRNOS:
                    raise
                # renameat2 unsupported here: previous recheck-then-replace
                # behavior is the only option on this filesystem.
                os.replace(
                    source[1],
                    destination[1],
                    src_dir_fd=source[0],
                    dst_dir_fd=destination[0],
                )


def replace_rooted(
    roots: dict[str, Path],
    source_root_id: str,
    source_relative_path: str,
    destination_root_id: str,
    destination_relative_path: str,
) -> None:
    """Replace one rooted path without following a swapped parent symlink."""

    try:
        source_root = roots[source_root_id]
        destination_root = roots[destination_root_id]
    except KeyError as error:
        raise ValueError("A rooted replacement references an unknown root.") from error
    with _rooted_parent(source_root, source_relative_path) as source:
        with _rooted_parent(destination_root, destination_relative_path) as destination:
            _replace_at(source, destination)


def _replace_at(source: tuple[int, str], destination: tuple[int, str]) -> None:
    """``os.replace`` that also lands a case-only rename.

    On a case-insensitive filesystem - an SMB or exFAT share, or a macOS volume,
    all ordinary places to keep a music library - replacing "01 - ARIA.flac" with
    "01 - Aria.flac" swaps the file's contents but KEEPS the existing directory
    entry, so a rename that only changes capitalisation silently does nothing.
    Vacating the old entry first is what makes the new spelling take.

    Case-sensitive filesystems never enter this path, and the displaced entry is
    put back if the replacement fails, so a failure is not destructive.
    """
    source_fd, source_name = source
    destination_fd, destination_name = destination
    occupant = _case_variant(destination_fd, destination_name)
    if occupant is None:
        os.replace(
            source_name,
            destination_name,
            src_dir_fd=source_fd,
            dst_dir_fd=destination_fd,
        )
        return
    displaced = f"{destination_name}.{uuid.uuid4().hex}.case"
    os.replace(
        occupant, displaced, src_dir_fd=destination_fd, dst_dir_fd=destination_fd
    )
    try:
        os.replace(
            source_name,
            destination_name,
            src_dir_fd=source_fd,
            dst_dir_fd=destination_fd,
        )
    except OSError:
        os.replace(
            displaced, occupant, src_dir_fd=destination_fd, dst_dir_fd=destination_fd
        )
        raise
    # os.replace would have removed the old destination anyway; this is that removal.
    try:
        os.unlink(displaced, dir_fd=destination_fd)
    except OSError:
        pass


def _case_variant(directory_fd: int, name: str) -> str | None:
    """The existing entry that differs from ``name`` only by case, if any."""
    try:
        entries = os.listdir(directory_fd)
    except OSError:
        return None
    if name in entries:
        return None
    folded = name.casefold()
    return next((entry for entry in entries if entry.casefold() == folded), None)


def unlink_rooted(
    roots: dict[str, Path],
    root_id: str,
    relative_path: str,
    *,
    missing_ok: bool = False,
) -> None:
    """Unlink one rooted path without following a swapped parent symlink."""

    try:
        root = roots[root_id]
    except KeyError as error:
        raise ValueError("A rooted unlink references an unknown root.") from error
    with _rooted_parent(root, relative_path) as target:
        try:
            os.unlink(target[1], dir_fd=target[0])
        except FileNotFoundError:
            if not missing_ok:
                raise


def copy_rooted(
    roots: dict[str, Path],
    source_root_id: str,
    source_relative_path: str,
    destination_root_id: str,
    destination_relative_path: str,
) -> None:
    """Copy one regular file through stable rooted directory descriptors."""

    try:
        source_root = roots[source_root_id]
        destination_root = roots[destination_root_id]
    except KeyError as error:
        raise ValueError("A rooted copy references an unknown root.") from error
    with _rooted_parent(source_root, source_relative_path) as source:
        with _rooted_parent(destination_root, destination_relative_path) as destination:
            source_fd = os.open(
                source[1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=source[0]
            )
            try:
                source_stat = os.fstat(source_fd)
                if not stat.S_ISREG(source_stat.st_mode):
                    raise OSError("A rooted copy source is not a regular file.")
                destination_fd = os.open(
                    destination[1],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    source_stat.st_mode & 0o777,
                    dir_fd=destination[0],
                )
                try:
                    while block := os.read(source_fd, 1024 * 1024):
                        view = memoryview(block)
                        while view:
                            written = os.write(destination_fd, view)
                            view = view[written:]
                    os.fchmod(destination_fd, source_stat.st_mode & 0o777)
                    os.utime(
                        destination_fd,
                        ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
                    )
                    os.fsync(destination_fd)
                except BaseException:
                    os.close(destination_fd)
                    destination_fd = -1
                    os.unlink(destination[1], dir_fd=destination[0])
                    raise
                finally:
                    if destination_fd >= 0:
                        os.close(destination_fd)
            finally:
                os.close(source_fd)
