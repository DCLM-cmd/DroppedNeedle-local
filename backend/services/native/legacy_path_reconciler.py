from __future__ import annotations

import asyncio
import errno
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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


class _PathInaccessibleError(RuntimeError):
    pass


class LegacyPathReconciler:
    def __init__(
        self,
        store: NativeLibraryStore,
        settings: TypedLibrarySettings,
        *,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._store = store
        self._settings = settings
        self._resolver = LibraryPolicyResolver(settings)
        self._batch_size = max(1, batch_size)

    async def reconcile(self) -> LegacyPathReconciliationResult:
        inventory = await self._inventory()
        outside_files = inventory["outside_files"]
        outside_reviews = inventory["outside_reviews"]
        if outside_files == 0 and outside_reviews == 0:
            return LegacyPathReconciliationResult(mode="unchanged")
        if outside_files == 0:
            return self._blocked(
                "review_paths_without_catalog_proof", outside_files, outside_reviews
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
                unused_roots, outside_files, outside_reviews, used_root_ids
            )
        if absent == outside_files:
            return await self._reconcile_moved(
                unused_roots, outside_files, outside_reviews, used_root_ids
            )
        return self._blocked("legacy_path_inaccessible", outside_files, outside_reviews)

    async def _inventory(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "outside_files": 0,
            "outside_reviews": 0,
            "present_files": 0,
            "absent_files": 0,
            "inaccessible_files": 0,
            "used_root_ids": set(),
        }
        async for rows in self._library_batches():
            outside: list[dict[str, Any]] = []
            for row in rows:
                resolved = self._resolver.resolve(str(row.get("file_path") or ""))
                if resolved is not None:
                    result["used_root_ids"].add(resolved.root_id)
                    continue
                outside.append(row)
            states = await asyncio.to_thread(self._source_states, outside)
            result["outside_files"] += len(outside)
            for value in states:
                result[f"{value}_files"] += 1

        async for rows in self._review_batches():
            for row in rows:
                resolved = self._resolver.resolve(str(row.get("file_path") or ""))
                if resolved is not None:
                    result["used_root_ids"].add(resolved.root_id)
                else:
                    result["outside_reviews"] += 1
        return result

    async def _reconcile_exact(
        self,
        roots: list[LibraryRootSettings],
        outside_files: int,
        outside_reviews: int,
        used_root_ids: set[str],
    ) -> LegacyPathReconciliationResult:
        basenames = {
            root.id: Path(root.path).resolve(strict=False).name.casefold()
            for root in roots
        }
        candidates: set[_Candidate] = set()
        async for rows in self._library_batches():
            try:
                batch_candidates = await asyncio.to_thread(
                    self._exact_candidates, rows, basenames
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
        selected = await self._select_candidates(
            candidates,
            moved=False,
            outside_files=outside_files,
            outside_reviews=outside_reviews,
            used_root_ids=used_root_ids,
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
    ) -> LegacyPathReconciliationResult:
        candidates: set[_Candidate] = set()
        async for rows in self._library_batches():
            try:
                discoveries = await asyncio.to_thread(
                    self._moved_candidates, rows, roots
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
    ) -> list[_Candidate] | str:
        if not candidates:
            return "unverified_path_remap" if moved else "no_historical_root_match"
        used: set[_Candidate] = set()
        async for rows in self._library_batches():
            matches, inaccessible = await asyncio.to_thread(
                self._matching_candidates, rows, candidates, moved
            )
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

    def _exact_candidates(
        self, rows: list[dict[str, Any]], basenames: dict[str, str]
    ) -> set[_Candidate]:
        candidates: set[_Candidate] = set()
        for row in rows:
            path = str(row.get("file_path") or "")
            if self._resolver.resolve(path) is not None:
                continue
            canonical = self._canonical_source(path)
            if canonical is None:
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

    def _moved_candidates(
        self,
        rows: list[dict[str, Any]],
        roots: list[LibraryRootSettings],
    ) -> set[_Candidate]:
        candidates: set[_Candidate] = set()
        for row in rows:
            path = str(row.get("file_path") or "")
            if self._resolver.resolve(path) is not None:
                continue
            source = self._canonical_source(path)
            if source is None:
                continue
            expected_size = int(row.get("file_size_bytes") or 0)
            for ancestor in (source.parent, *source.parents):
                if not self._allowed_prefix(ancestor):
                    continue
                relative = source.relative_to(ancestor)
                for root in roots:
                    target_root = Path(root.path).resolve(strict=False)
                    target = target_root / relative
                    destination_state = self._destination_state(target, expected_size)
                    if destination_state == "inaccessible":
                        raise _PathInaccessibleError
                    if destination_state == "match":
                        candidates.add(
                            _Candidate(str(ancestor), root.id, str(target_root))
                        )
                        if len(candidates) > MAX_CANDIDATES:
                            raise _CandidateLimitError
        return candidates

    def _matching_candidates(
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
                destination_state = (
                    self._destination_state(Path(target), expected_size)
                    if moved
                    else "match"
                )
                inaccessible = inaccessible or destination_state == "inaccessible"
                if destination_state == "match":
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
