from __future__ import annotations

import asyncio
import errno
import logging
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from api.v1.schemas.library_policies import (
    LibraryRootSettings,
    TypedLibrarySettings,
)
from core.exceptions import ConfigurationError
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.library_policy_resolver import LibraryPolicyResolver

BATCH_SIZE = 500
MAX_CANDIDATES = 256
MIN_SQLITE_ROWID = -9_223_372_036_854_775_808
PROBE_TIMEOUT_SECONDS = 5.0
PROBE_MAX_CONCURRENT = 4

logger = logging.getLogger(__name__)
_BLOCKED_ROOTS = tuple(
    Path(value)
    for value in (
        "/",
        "/app",
        "/proc",
        "/sys",
        "/dev",
        "/run",
        "/var",
        "/usr",
        "/etc",
        "/boot",
        "/bin",
        "/sbin",
        "/lib",
        "/root",
        "/tmp",
    )
)


@dataclass(frozen=True)
class LegacyPathMapping:
    source_prefix: str
    root_id: str
    target_prefix: str

    def project(self, path: str) -> str | None:
        candidate = Path(path).resolve(strict=False)
        source = Path(self.source_prefix)
        if not candidate.is_relative_to(source):
            return None
        return str(Path(self.target_prefix) / candidate.relative_to(source))


@dataclass(frozen=True)
class LegacyPathReconciliationResult:
    mode: Literal["unchanged", "exact", "remapped", "blocked"]
    mappings: tuple[LegacyPathMapping, ...] = ()
    root_retargets: tuple[tuple[str, str], ...] = ()
    library_file_count: int = 0
    review_row_count: int = 0
    failure_reason: str | None = None

    def project(self, path: str) -> str:
        projected = [
            value
            for mapping in self.mappings
            if (value := mapping.project(path)) is not None
        ]
        return projected[0] if len(projected) == 1 else path

    def evidence(self) -> dict[str, object] | None:
        if self.mode == "unchanged":
            return None
        payload: dict[str, object] = {
            "mode": self.mode,
            "library_file_count": self.library_file_count,
            "review_row_count": self.review_row_count,
        }
        if self.failure_reason is not None:
            payload["failure_reason"] = self.failure_reason
        if self.mode != "blocked":
            payload["root_ids"] = sorted(mapping.root_id for mapping in self.mappings)
        return payload


@dataclass(frozen=True)
class _Candidate:
    source_prefix: str
    root_id: str
    target_prefix: str

    def mapping(self) -> LegacyPathMapping:
        return LegacyPathMapping(
            source_prefix=self.source_prefix,
            root_id=self.root_id,
            target_prefix=self.target_prefix,
        )


class _CandidateLimitError(RuntimeError):
    pass


class _ProbeTimeoutError(RuntimeError):
    pass


class _PathInaccessibleError(RuntimeError):
    pass


def _emit_progress(emit: Callable[[str], None] | None, message: str) -> None:
    if emit is not None:
        emit(message)




class LegacyPathReconciler:
    def __init__(
        self,
        store: NativeLibraryStore,
        settings: TypedLibrarySettings,
        *,
        batch_size: int = BATCH_SIZE,
        probe_timeout: float = PROBE_TIMEOUT_SECONDS,
        probe_max_concurrent: int = PROBE_MAX_CONCURRENT,
    ) -> None:
        self._store = store
        self._settings = settings
        self._resolver = LibraryPolicyResolver(settings)
        self._batch_size = max(1, batch_size)
        self._probe_timeout = probe_timeout
        self._probe_max_concurrent = probe_max_concurrent
        self._probe_lock = threading.Lock()
        self._pending_probes: set[asyncio.Future[Any]] = set()
        self._closed = False
    def _remove_pending_probe(self, fut: asyncio.Future[Any]) -> None:
        with self._probe_lock:
            self._pending_probes.discard(fut)

    @property
    def probe_pending_count(self) -> int:
        with self._probe_lock:
            return len(self._pending_probes)

    def close(self) -> None:
        with self._probe_lock:
            if self._closed:
                return
            self._closed = True
            pending = list(self._pending_probes)
            self._pending_probes.clear()
        for fut in pending:
            if fut.done():
                continue
            try:
                loop = fut.get_loop()  # type: ignore[attr-defined]
                if loop.is_running():
                    loop.call_soon_threadsafe(fut.cancel)
                else:
                    fut.cancel()
            except Exception:  # noqa: BLE001
                try:
                    fut.cancel()
                except Exception:  # noqa: BLE001
                    pass

    async def aclose(self) -> None:
        self.close()

    async def _bounded_probe(
        self, func: Callable[..., Any], *args: Any, timeout: float | None = None
    ) -> Any:
        if timeout is None:
            timeout = self._probe_timeout
        loop = asyncio.get_running_loop()
        # Atomic reserve
        probe_future: asyncio.Future[Any] | None = None
        should_fail_closed = False
        should_fail_capacity = False
        with self._probe_lock:
            if self._closed:
                should_fail_closed = True
            elif len(self._pending_probes) >= self._probe_max_concurrent:
                should_fail_capacity = True
            else:
                probe_future = loop.create_future()
                self._pending_probes.add(probe_future)

                def _on_done(f: asyncio.Future[Any]) -> None:
                    with self._probe_lock:
                        self._pending_probes.discard(f)

                probe_future.add_done_callback(_on_done)
        if should_fail_closed:
            raise _ProbeTimeoutError("probe executor closed")
        if should_fail_capacity:
            raise _ProbeTimeoutError("probe capacity exceeded")
        assert probe_future is not None

        def _runner() -> None:
            try:
                result = func(*args)
                exc: BaseException | None = None
            except BaseException as e:  # noqa: BLE001
                result = None
                exc = e

            def _complete() -> None:
                if probe_future.done():
                    return
                if exc is not None:
                    if not probe_future.done():
                        probe_future.set_exception(exc)
                else:
                    if not probe_future.done():
                        probe_future.set_result(result)

            try:
                loop.call_soon_threadsafe(_complete)
            except RuntimeError:
                logger.debug(
                    "legacy_path_reconciler probe_loop_closed",
                )
                return

        thread = threading.Thread(target=_runner, daemon=True, name="legacy-probe")
        thread.start()
        try:
            return await asyncio.wait_for(asyncio.shield(probe_future), timeout=timeout)
        except TimeoutError:
            # Leave pending until real thread completes (daemon, not cancelled)
            raise _ProbeTimeoutError("probe timed out")
        except asyncio.CancelledError:
            if probe_future.cancelled():
                raise _ProbeTimeoutError("probe cancelled due to close")
            raise


    async def reconcile(
        self, *, emit_progress: Callable[[str], None] | None = None
    ) -> LegacyPathReconciliationResult:
        """F5/H5: ``emit_progress`` receives sanitized counts and outcome
        classes only - never paths or user identifiers."""
        result = await self._reconcile(emit_progress)
        _emit_progress(
            emit_progress,
            f"legacy_path_reconciled mode={result.mode} "
            f"library_files={result.library_file_count} "
            f"review_rows={result.review_row_count} "
            f"reason={result.failure_reason or '-'}",
        )
        return result

    async def _reconcile(
        self, emit: Callable[[str], None] | None
    ) -> LegacyPathReconciliationResult:
        inventory = await self._inventory(emit)
        outside_files = inventory["outside_files"]
        outside_reviews = inventory["outside_reviews"]
        if outside_files == 0 and outside_reviews == 0:
            return LegacyPathReconciliationResult(mode="unchanged")
        if outside_files == 0:
            return self._blocked(
                "review_paths_without_catalog_proof", outside_files, outside_reviews
            )
        if inventory["timeout_files"]:
            return self._blocked(
                "legacy_path_probe_timeout", outside_files, outside_reviews
            )
        if inventory["inaccessible_files"]:
            return self._blocked(
                "legacy_path_inaccessible", outside_files, outside_reviews
            )
        present = inventory["present_files"]
        absent = inventory["absent_files"]
        if present and absent:
            return self._blocked(
                "mixed_legacy_path_state", outside_files, outside_reviews
            )

        unused_roots = [
            root
            for root in self._settings.library_roots
            if root.id not in inventory["used_root_ids"]
        ]
        if not unused_roots:
            return self._blocked(
                "no_unused_configured_root", outside_files, outside_reviews
            )
        used_root_ids = inventory["used_root_ids"]
        if present == outside_files:
            return await self._reconcile_exact(
                unused_roots, outside_files, outside_reviews, used_root_ids, emit
            )
        if absent == outside_files:
            return await self._reconcile_moved(
                unused_roots, outside_files, outside_reviews, used_root_ids, emit
            )
        return self._blocked("legacy_path_inaccessible", outside_files, outside_reviews)

    async def _inventory(self, emit: Callable[[str], None] | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "outside_files": 0,
            "outside_reviews": 0,
            "present_files": 0,
            "absent_files": 0,
            "inaccessible_files": 0,
            "timeout_files": 0,
            "used_root_ids": set(),
        }
        scanned = 0
        async for rows in self._library_batches():
            outside: list[dict[str, Any]] = []
            for row in rows:
                resolved = self._resolver.resolve(str(row.get("file_path") or ""))
                if resolved is not None:
                    result["used_root_ids"].add(resolved.root_id)
                    continue
                outside.append(row)
            # Bounded per-probe source states with concurrency capped by probe executor (cap 4, no queue)
            async def _classify(row: dict[str, Any]) -> str:
                raw = str(row.get("file_path") or "")
                if not Path(raw).is_absolute():
                    return "inaccessible"
                try:
                    st = await self._bounded_probe(lambda p=raw: Path(p).stat())
                except _ProbeTimeoutError:
                    return "timeout"
                except asyncio.CancelledError:
                    return "timeout"
                except OSError as error:
                    return "absent" if error.errno in {errno.ENOENT, errno.ENOTDIR} else "inaccessible"
                return "present" if stat.S_ISREG(st.st_mode) else "inaccessible"

            states = await asyncio.gather(*[_classify(row) for row in outside])
            result["outside_files"] += len(outside)
            for value in states:
                if value == "timeout":
                    result["timeout_files"] += 1
                    result["inaccessible_files"] += 1
                else:
                    result[f"{value}_files"] += 1
            scanned += len(rows)
            _emit_progress(
                emit,
                "legacy_path_inventory "
                f"scanned={scanned} outside={result['outside_files']} "
                f"present={result['present_files']} absent={result['absent_files']} "
                f"inaccessible={result['inaccessible_files']} "
                f"timeout={result['timeout_files']}",
            )

        async for rows in self._review_batches():
            for row in rows:
                resolved = self._resolver.resolve(str(row.get("file_path") or ""))
                if resolved is not None:
                    result["used_root_ids"].add(resolved.root_id)
                else:
                    result["outside_reviews"] += 1
        _emit_progress(
            emit,
            f"legacy_path_inventory review_rows_outside={result['outside_reviews']}",
        )
        return result

    async def _reconcile_exact(
        self,
        roots: list[LibraryRootSettings],
        outside_files: int,
        outside_reviews: int,
        used_root_ids: set[str],
        emit: Callable[[str], None] | None = None,
    ) -> LegacyPathReconciliationResult:
        basenames = {
            root.id: Path(root.path).resolve(strict=False).name.casefold()
            for root in roots
        }
        candidates: set[_Candidate] = set()
        async for rows in self._library_batches():
            try:
                batch_candidates = await self._exact_candidates(rows, basenames)
            except _ProbeTimeoutError:
                return self._blocked(
                    "legacy_path_probe_timeout", outside_files, outside_reviews
                )
            except _PathInaccessibleError:
                return self._blocked(
                    "legacy_path_inaccessible", outside_files, outside_reviews
                )
            except _CandidateLimitError:
                return self._blocked(
                    "ambiguous_root_assignment", outside_files, outside_reviews
                )
            candidates.update(batch_candidates)
            _emit_progress(emit, f"legacy_path_candidates count={len(candidates)}")
        selected = await self._select_candidates(
            candidates,
            moved=False,
            outside_files=outside_files,
            outside_reviews=outside_reviews,
            used_root_ids=used_root_ids,
            emit=emit,
        )
        if isinstance(selected, str):
            return self._blocked(selected, outside_files, outside_reviews)
        retargets = tuple((item.root_id, item.source_prefix) for item in selected)
        try:
            self._validate_retargeted_settings(dict(retargets))
        except ConfigurationError:
            return self._blocked(
                "root_validation_failed", outside_files, outside_reviews
            )
        return LegacyPathReconciliationResult(
            mode="exact",
            mappings=tuple(item.mapping() for item in selected),
            root_retargets=retargets,
            library_file_count=outside_files,
            review_row_count=outside_reviews,
        )

    async def _reconcile_moved(
        self,
        roots: list[LibraryRootSettings],
        outside_files: int,
        outside_reviews: int,
        used_root_ids: set[str],
        emit: Callable[[str], None] | None = None,
    ) -> LegacyPathReconciliationResult:
        candidates: set[_Candidate] = set()
        async for rows in self._library_batches():
            try:
                discoveries = await self._moved_candidates(rows, roots)
            except _ProbeTimeoutError:
                return self._blocked(
                    "legacy_path_probe_timeout", outside_files, outside_reviews
                )
            except _CandidateLimitError:
                return self._blocked(
                    "ambiguous_root_assignment", outside_files, outside_reviews
                )
            except _PathInaccessibleError:
                return self._blocked(
                    "legacy_path_inaccessible", outside_files, outside_reviews
                )
            candidates.update(discoveries)
            if len(candidates) > MAX_CANDIDATES:
                return self._blocked(
                    "ambiguous_root_assignment", outside_files, outside_reviews
                )
        selected = await self._select_candidates(
            candidates,
            moved=True,
            outside_files=outside_files,
            outside_reviews=outside_reviews,
            used_root_ids=used_root_ids,
            emit=emit,
        )
        if isinstance(selected, str):
            return self._blocked(selected, outside_files, outside_reviews)
        return LegacyPathReconciliationResult(
            mode="remapped",
            mappings=tuple(item.mapping() for item in selected),
            library_file_count=outside_files,
            review_row_count=outside_reviews,
        )

    async def _select_candidates(
        self,
        candidates: set[_Candidate],
        *,
        moved: bool,
        outside_files: int,
        outside_reviews: int,
        used_root_ids: set[str],
        emit: Callable[[str], None] | None = None,
    ) -> list[_Candidate] | str:
        if not candidates:
            return "unverified_path_remap" if moved else "no_historical_root_match"
        used: set[_Candidate] = set()
        verified = 0
        async for rows in self._library_batches():
            try:
                matches, inaccessible = await self._matching_candidates(rows, candidates, moved)
            except _ProbeTimeoutError:
                return "legacy_path_probe_timeout"
            if inaccessible:
                return "legacy_path_inaccessible"
            for row, row_matches in zip(rows, matches, strict=True):
                path = str(row.get("file_path") or "")
                if self._resolver.resolve(path) is not None:
                    continue
                if len(row_matches) != 1:
                    return (
                        "ambiguous_root_assignment"
                        if row_matches
                        else (
                            "unverified_path_remap"
                            if moved
                            else "no_historical_root_match"
                        )
                    )
                used.add(row_matches[0])
                verified += 1
            _emit_progress(emit, f"legacy_path_verify verified={verified}")

        roots = [candidate.root_id for candidate in used]
        if len(roots) != len(set(roots)):
            return "ambiguous_root_assignment"
        if moved and self._covers_configured_root(used, used_root_ids):
            return "candidate_overlaps_configured_root"

        async for rows in self._review_batches():
            for row in rows:
                path = str(row.get("file_path") or "")
                if self._resolver.resolve(path) is not None:
                    continue
                matches = [
                    candidate
                    for candidate in used
                    if candidate.mapping().project(path) is not None
                ]
                if len(matches) != 1:
                    return "review_path_outside_proven_mapping"
        if not used and (outside_files or outside_reviews):
            return "unverified_path_remap"
        return sorted(used, key=lambda item: (item.root_id, item.source_prefix))

    async def _exact_candidates(
        self, rows: list[dict[str, Any]], basenames: dict[str, str]
    ) -> set[_Candidate]:
        candidates: set[_Candidate] = set()
        for row in rows:
            path = str(row.get("file_path") or "")
            if self._resolver.resolve(path) is not None:
                continue
            if not Path(path).is_absolute():
                raise _PathInaccessibleError
            try:
                canonical = await self._bounded_probe(lambda p=path: Path(p).resolve(strict=False))
            except _ProbeTimeoutError:
                raise
            except (OSError, RuntimeError):
                raise _PathInaccessibleError
            for root_id, basename in basenames.items():
                if not basename:
                    continue
                for ancestor in (canonical.parent, *canonical.parents):
                    if ancestor.name.casefold() != basename:
                        continue
                    if not self._allowed_prefix(ancestor):
                        continue
                    candidates.add(_Candidate(str(ancestor), root_id, str(ancestor)))
                    if len(candidates) > MAX_CANDIDATES:
                        raise _CandidateLimitError
        return candidates

    async def _moved_candidates(
        self,
        rows: list[dict[str, Any]],
        roots: list[LibraryRootSettings],
    ) -> set[_Candidate]:
        candidates: set[_Candidate] = set()
        for row in rows:
            path = str(row.get("file_path") or "")
            if self._resolver.resolve(path) is not None:
                continue
            if not Path(path).is_absolute():
                continue
            try:
                source = await self._bounded_probe(lambda p=path: Path(p).resolve(strict=False))
            except _ProbeTimeoutError:
                raise
            except (OSError, RuntimeError):
                continue
            expected_size = int(row.get("file_size_bytes") or 0)
            for ancestor in (source.parent, *source.parents):
                if not self._allowed_prefix(ancestor):
                    continue
                relative = source.relative_to(ancestor)
                for root in roots:
                    try:
                        target_root = await self._bounded_probe(lambda p=root.path: Path(p).resolve(strict=False))
                    except _ProbeTimeoutError:
                        raise
                    except (OSError, RuntimeError):
                        continue
                    target = target_root / relative
                    try:
                        destination_state = await self._bounded_destination_state(target, expected_size)
                    except _ProbeTimeoutError:
                        raise
                    if destination_state == "inaccessible":
                        raise _PathInaccessibleError
                    if destination_state == "match":
                        candidates.add(
                            _Candidate(str(ancestor), root.id, str(target_root))
                        )
                        if len(candidates) > MAX_CANDIDATES:
                            raise _CandidateLimitError
        return candidates

    async def _matching_candidates(
        self,
        rows: list[dict[str, Any]],
        candidates: set[_Candidate],
        moved: bool,
    ) -> tuple[list[list[_Candidate]], bool]:
        matches: list[list[_Candidate]] = []
        inaccessible = False
        for row in rows:
            path = str(row.get("file_path") or "")
            if self._resolver.resolve(path) is not None:
                matches.append([])
                continue
            expected_size = int(row.get("file_size_bytes") or 0)
            row_matches = []
            for candidate in candidates:
                target = candidate.mapping().project(path)
                if target is None:
                    continue
                if moved:
                    try:
                        destination_state = await self._bounded_destination_state(Path(target), expected_size)
                    except _ProbeTimeoutError:
                        raise
                    inaccessible = inaccessible or destination_state == "inaccessible"
                    if destination_state == "match":
                        row_matches.append(candidate)
                else:
                    row_matches.append(candidate)
            matches.append(row_matches)
        return matches, inaccessible

    def _covers_configured_root(
        self, candidates: set[_Candidate], used_root_ids: set[str]
    ) -> bool:
        if not used_root_ids:
            return False
        sources = [Path(candidate.source_prefix) for candidate in candidates]
        return any(
            Path(root.path).resolve(strict=False).is_relative_to(source)
            for root in self._settings.library_roots
            if root.id in used_root_ids
            for source in sources
        )

    def _validate_retargeted_settings(self, replacements: dict[str, str]) -> None:
        roots = [
            LibraryRootSettings(
                id=root.id,
                path=replacements.get(root.id, root.path),
                label=root.label,
                policy=root.policy,
                rules=root.rules,
            )
            for root in self._settings.library_roots
        ]
        LibraryPolicyResolver(
            TypedLibrarySettings(
                library_roots=roots,
                staging_path=self._settings.staging_path,
                naming_template=self._settings.naming_template,
                acoustid_api_key=self._settings.acoustid_api_key,
            )
        )

    @staticmethod
    def _source_states(rows: list[dict[str, Any]]) -> list[str]:
        states = []
        for row in rows:
            raw = str(row.get("file_path") or "")
            if not Path(raw).is_absolute():
                states.append("inaccessible")
                continue
            try:
                mode = Path(raw).stat().st_mode
            except OSError as error:
                states.append(
                    "absent"
                    if error.errno in {errno.ENOENT, errno.ENOTDIR}
                    else "inaccessible"
                )
                continue
            states.append("present" if stat.S_ISREG(mode) else "inaccessible")
        return states

    @staticmethod
    def _destination_state(path: Path, expected_size: int) -> str:
        try:
            info = path.stat()
        except OSError as error:
            return (
                "missing"
                if error.errno in {errno.ENOENT, errno.ENOTDIR}
                else "inaccessible"
            )
        if not stat.S_ISREG(info.st_mode):
            return "mismatch"
        return "match" if info.st_size == expected_size else "mismatch"

    @staticmethod
    def _canonical_source(path: str) -> Path | None:
        candidate = Path(path)
        if not candidate.is_absolute():
            return None
        try:
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    async def _bounded_canonical(self, path: str) -> Path | None:
        try:
            return await self._bounded_probe(lambda: Path(path).resolve(strict=False))
        except _ProbeTimeoutError:
            raise
        except (OSError, RuntimeError):
            return None

    async def _bounded_destination_state(self, path: Path, expected_size: int) -> str:
        return await self._bounded_probe(lambda: self._destination_state(path, expected_size))

    @staticmethod
    def _allowed_prefix(path: Path) -> bool:
        canonical = path.resolve(strict=False)
        return not any(
            canonical == blocked
            or (blocked != Path("/") and canonical.is_relative_to(blocked))
            for blocked in _BLOCKED_ROOTS
        )

    async def _library_batches(self):
        after_id = ""
        while True:
            rows = await self._store.get_bounded_legacy_library_file_preflight_batch(
                after_id=after_id,
                limit=self._batch_size,
            )
            if not rows:
                return
            after_id = str(rows[-1].get("id") or "")
            yield rows

    async def _review_batches(self):
        after_rowid = MIN_SQLITE_ROWID
        while True:
            rows = await self._store.get_bounded_legacy_rows(
                "manual_review_queue",
                after_rowid=after_rowid,
                limit=self._batch_size,
            )
            if not rows:
                return
            after_rowid = int(rows[-1]["__migration_rowid"])
            yield rows

    @staticmethod
    def _blocked(
        reason: str, library_file_count: int, review_row_count: int
    ) -> LegacyPathReconciliationResult:
        return LegacyPathReconciliationResult(
            mode="blocked",
            library_file_count=library_file_count,
            review_row_count=review_row_count,
            failure_reason=reason,
        )
