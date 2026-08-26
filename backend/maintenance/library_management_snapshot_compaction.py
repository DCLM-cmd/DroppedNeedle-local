"""Bounded offline compaction of redundant Library Management artwork JSON.

This command is intentionally separate from application startup. Apply mode requires
an explicit isolated-copy confirmation and never runs VACUUM or replaces a database.
Each row is cleared only after its registered content-addressed semantic snapshot has
been path, length, hash, type, decode, and exact-artwork verified.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import sys
from typing import Sequence

import msgspec

from models.audio_metadata import SemanticTagSnapshot


DEFAULT_BATCH_SIZE = 10
MAX_BATCH_SIZE = 100


class SnapshotCompactionError(RuntimeError):
    """The offline copy could not be compacted without weakening recovery."""


class SnapshotCompactionResult(msgspec.Struct, frozen=True, kw_only=True):
    dry_run: bool
    rows_scanned: int
    rows_cleared: int
    logical_bytes_cleared: int
    remaining_candidates: bool
    foreign_key_check_passed: bool | None = None
    integrity_check_passed: bool | None = None


@dataclass(frozen=True, slots=True)
class _SnapshotTable:
    table: str
    keys: tuple[str, ...]
    blob_column: str
    image_column: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    snapshot_table: _SnapshotTable
    keys: tuple[str | int, ...]
    blob_sha256: str
    image_snapshot_json: str


_SNAPSHOT_TABLES = (
    _SnapshotTable(
        table="library_management_baselines",
        keys=("id",),
        blob_column="semantic_snapshot_blob_sha256",
        image_column="image_snapshot_json",
    ),
    _SnapshotTable(
        table="library_management_operation_snapshots",
        keys=("id",),
        blob_column="semantic_snapshot_blob_sha256",
        image_column="image_snapshot_json",
    ),
    _SnapshotTable(
        table="library_management_import_journal",
        keys=("bundle_id", "ordinal"),
        blob_column="baseline_blob_sha256",
        image_column="baseline_image_snapshot_json",
    ),
)


def _validate_isolated_paths(database: Path, blob_root: Path) -> tuple[Path, Path]:
    if database.is_symlink() or not database.is_file():
        raise SnapshotCompactionError(
            "The isolated database must be a regular, non-symlink file."
        )
    if blob_root.is_symlink() or not blob_root.is_dir():
        raise SnapshotCompactionError(
            "The isolated blob root must be a non-symlink directory."
        )
    return database.resolve(), blob_root.resolve()


def _connect(database: Path, *, dry_run: bool) -> sqlite3.Connection:
    if dry_run:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro", uri=True, timeout=5
        )
    else:
        connection = sqlite3.connect(database, timeout=5)
        connection.execute("PRAGMA foreign_keys=ON")
    connection.row_factory = sqlite3.Row
    return connection


def _load_candidates(connection: sqlite3.Connection, limit: int) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for snapshot_table in _SNAPSHOT_TABLES:
        remaining = limit - len(candidates)
        if remaining <= 0:
            break
        selected = ",".join(
            (
                *snapshot_table.keys,
                snapshot_table.blob_column,
                snapshot_table.image_column,
            )
        )
        order = ",".join(snapshot_table.keys)
        rows = connection.execute(
            f"SELECT {selected} FROM {snapshot_table.table} "
            f"WHERE {snapshot_table.image_column}<>'[]' "
            f"ORDER BY {order} LIMIT ?",
            (remaining,),
        ).fetchall()
        for row in rows:
            blob_sha256 = row[snapshot_table.blob_column]
            if not isinstance(blob_sha256, str):
                raise SnapshotCompactionError(
                    "A redundant snapshot row has no immutable semantic blob."
                )
            candidates.append(
                _Candidate(
                    snapshot_table=snapshot_table,
                    keys=tuple(row[key] for key in snapshot_table.keys),
                    blob_sha256=blob_sha256,
                    image_snapshot_json=str(row[snapshot_table.image_column]),
                )
            )
    return candidates


def _reject_symlink_components(blob_root: Path, path: Path) -> None:
    current = blob_root
    for part in path.relative_to(blob_root).parts:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise SnapshotCompactionError(
                    "A semantic snapshot blob path contains a symbolic link."
                )
        except FileNotFoundError as error:
            raise SnapshotCompactionError(
                "A semantic snapshot blob is missing."
            ) from error


def _verified_snapshot_artwork_json(
    connection: sqlite3.Connection, blob_root: Path, sha256: str
) -> str:
    if (
        len(sha256) != 64
        or sha256 != sha256.lower()
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise SnapshotCompactionError("A semantic snapshot hash is invalid.")
    blob = connection.execute(
        "SELECT kind,byte_length,relative_path FROM library_management_blobs "
        "WHERE sha256=?",
        (sha256,),
    ).fetchone()
    if blob is None or str(blob["kind"]) != "tag_snapshot":
        raise SnapshotCompactionError(
            "A redundant snapshot row does not reference a registered tag snapshot."
        )
    expected_relative = Path("objects", sha256[:2], sha256[2:4], f"{sha256}.blob")
    if Path(str(blob["relative_path"])) != expected_relative:
        raise SnapshotCompactionError(
            "A semantic snapshot blob path is not content-addressed."
        )
    path = blob_root / expected_relative
    _reject_symlink_components(blob_root, path)
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotCompactionError("A semantic snapshot blob is missing.") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise SnapshotCompactionError("A semantic snapshot blob is not a regular file.")
    digest = hashlib.sha256()
    content = bytearray()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                content.extend(chunk)
    except OSError as error:
        raise SnapshotCompactionError(
            "A semantic snapshot blob could not be read."
        ) from error
    if len(content) != int(blob["byte_length"]) or digest.hexdigest() != sha256:
        raise SnapshotCompactionError(
            "A semantic snapshot blob failed length or hash validation."
        )
    try:
        snapshot = msgspec.json.decode(content, type=SemanticTagSnapshot)
    except (msgspec.DecodeError, msgspec.ValidationError) as error:
        raise SnapshotCompactionError(
            "A semantic snapshot blob could not be decoded."
        ) from error
    return json.dumps(
        msgspec.to_builtins(snapshot.artwork),
        separators=(",", ":"),
        sort_keys=True,
    )


def _verify_candidates(
    connection: sqlite3.Connection,
    blob_root: Path,
    candidates: Sequence[_Candidate],
) -> int:
    logical_bytes = 0
    for candidate in candidates:
        expected = _verified_snapshot_artwork_json(
            connection, blob_root, candidate.blob_sha256
        )
        if candidate.image_snapshot_json != expected:
            raise SnapshotCompactionError(
                "A redundant artwork snapshot is not an exact copy of its semantic blob."
            )
        logical_bytes += len(candidate.image_snapshot_json.encode()) - 2
    return logical_bytes


def _clear_candidates(
    connection: sqlite3.Connection, candidates: Sequence[_Candidate]
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for candidate in candidates:
            snapshot_table = candidate.snapshot_table
            key_predicate = " AND ".join(f"{key}=?" for key in snapshot_table.keys)
            updated = connection.execute(
                f"UPDATE {snapshot_table.table} SET {snapshot_table.image_column}='[]' "
                f"WHERE {key_predicate} AND {snapshot_table.blob_column}=? "
                f"AND {snapshot_table.image_column}=?",
                (
                    *candidate.keys,
                    candidate.blob_sha256,
                    candidate.image_snapshot_json,
                ),
            ).rowcount
            if updated != 1:
                raise SnapshotCompactionError(
                    "A snapshot row changed after verification; no batch was cleared."
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _has_candidates(connection: sqlite3.Connection) -> bool:
    return any(
        connection.execute(
            f"SELECT 1 FROM {snapshot_table.table} "
            f"WHERE {snapshot_table.image_column}<>'[]' LIMIT 1"
        ).fetchone()
        is not None
        for snapshot_table in _SNAPSHOT_TABLES
    )


def compact_snapshot_copy(
    database: Path,
    blob_root: Path,
    *,
    apply: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> SnapshotCompactionResult:
    """Verify and optionally clear one bounded batch on an isolated copy."""

    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise SnapshotCompactionError(
            f"Batch size must be between 1 and {MAX_BATCH_SIZE}."
        )
    database, blob_root = _validate_isolated_paths(database, blob_root)
    with _connect(database, dry_run=not apply) as connection:
        candidates = _load_candidates(connection, batch_size)
        logical_bytes = _verify_candidates(connection, blob_root, candidates)
        if apply and candidates:
            _clear_candidates(connection, candidates)
        remaining = _has_candidates(connection)
        foreign_keys_passed: bool | None = None
        integrity_passed: bool | None = None
        if apply and not remaining:
            foreign_keys_passed = (
                connection.execute("PRAGMA foreign_key_check").fetchone() is None
            )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            integrity_passed = integrity is not None and str(integrity[0]) == "ok"
            if not foreign_keys_passed or not integrity_passed:
                raise SnapshotCompactionError(
                    "The compacted copy failed SQLite integrity validation."
                )
        return SnapshotCompactionResult(
            dry_run=not apply,
            rows_scanned=len(candidates),
            rows_cleared=len(candidates) if apply else 0,
            logical_bytes_cleared=logical_bytes if apply else 0,
            remaining_candidates=remaining,
            foreign_key_check_passed=foreign_keys_passed,
            integrity_check_passed=integrity_passed,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify one bounded batch of redundant Library Management snapshot JSON "
            "on an isolated database and blob-store copy"
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--blob-root", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-isolated-copy", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.apply and not args.confirm_isolated_copy:
        parser.error("--apply requires --confirm-isolated-copy")
    try:
        result = compact_snapshot_copy(
            args.database,
            args.blob_root,
            apply=args.apply,
            batch_size=args.batch_size,
        )
    except (SnapshotCompactionError, sqlite3.Error) as error:
        print(f"Snapshot compaction refused: {error}", file=sys.stderr)
        return 1
    print(msgspec.json.encode(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
