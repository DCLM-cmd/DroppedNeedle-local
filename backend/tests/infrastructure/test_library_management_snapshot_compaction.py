import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import msgspec
import pytest

from infrastructure.audio.metadata_engine import AudioMetadataEngine
from maintenance.library_management_snapshot_compaction import (
    SnapshotCompactionError,
    compact_snapshot_copy,
    main,
)
from models.audio_metadata import EmbeddedArtworkDescriptor


FIXTURE = Path(__file__).parents[1] / "fixtures/library/management_full.flac"


def _snapshot_bytes() -> tuple[bytes, str]:
    content = b"embedded-artwork-bytes" * 1024
    artwork = EmbeddedArtworkDescriptor(
        image_type="front",
        mime_type="image/jpeg",
        description="Original cover",
        width=600,
        height=600,
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
        format_supported=True,
    )
    snapshot = msgspec.structs.replace(
        AudioMetadataEngine().snapshot(FIXTURE), artwork=(artwork,)
    )
    return (
        msgspec.json.encode(snapshot),
        json.dumps(
            msgspec.to_builtins(snapshot.artwork),
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def _seed_copy(root: Path) -> tuple[Path, Path, str]:
    database = root / "library.db"
    blob_root = root / "management-blobs"
    snapshot_bytes, artwork_json = _snapshot_bytes()
    sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    relative_path = Path("objects", sha256[:2], sha256[2:4], f"{sha256}.blob")
    path = blob_root / relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(snapshot_bytes)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE library_management_blobs (
                sha256 TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                byte_length INTEGER NOT NULL,
                relative_path TEXT NOT NULL
            );
            CREATE TABLE library_management_baselines (
                id TEXT PRIMARY KEY,
                semantic_snapshot_blob_sha256 TEXT NOT NULL,
                image_snapshot_json TEXT NOT NULL
            );
            CREATE TABLE library_management_operation_snapshots (
                id TEXT PRIMARY KEY,
                semantic_snapshot_blob_sha256 TEXT NOT NULL,
                image_snapshot_json TEXT NOT NULL
            );
            CREATE TABLE library_management_import_journal (
                bundle_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                baseline_blob_sha256 TEXT,
                baseline_image_snapshot_json TEXT NOT NULL,
                PRIMARY KEY(bundle_id, ordinal)
            );
            """
        )
        connection.execute(
            "INSERT INTO library_management_blobs VALUES (?,?,?,?)",
            (sha256, "tag_snapshot", len(snapshot_bytes), relative_path.as_posix()),
        )
        connection.execute(
            "INSERT INTO library_management_baselines VALUES (?,?,?)",
            ("baseline-1", sha256, artwork_json),
        )
        connection.execute(
            "INSERT INTO library_management_operation_snapshots VALUES (?,?,?)",
            ("operation-1", sha256, artwork_json),
        )
        connection.execute(
            "INSERT INTO library_management_import_journal VALUES (?,?,?,?)",
            ("bundle-1", 0, sha256, artwork_json),
        )
    return database, blob_root, artwork_json


def _image_values(database: Path) -> tuple[str, str, str]:
    with sqlite3.connect(database) as connection:
        return (
            str(
                connection.execute(
                    "SELECT image_snapshot_json FROM library_management_baselines"
                ).fetchone()[0]
            ),
            str(
                connection.execute(
                    "SELECT image_snapshot_json "
                    "FROM library_management_operation_snapshots"
                ).fetchone()[0]
            ),
            str(
                connection.execute(
                    "SELECT baseline_image_snapshot_json "
                    "FROM library_management_import_journal"
                ).fetchone()[0]
            ),
        )


def test_copy_compaction_is_bounded_restart_safe_and_idempotent(tmp_path: Path) -> None:
    source_database, source_blobs, artwork_json = _seed_copy(tmp_path / "source")
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    database = isolated / "library.db"
    blobs = isolated / "management-blobs"
    shutil.copy2(source_database, database)
    shutil.copytree(source_blobs, blobs)
    source_hash = hashlib.sha256(source_database.read_bytes()).hexdigest()
    original_size = database.stat().st_size

    dry_run = compact_snapshot_copy(database, blobs, batch_size=1)
    assert dry_run.rows_scanned == 1
    assert dry_run.rows_cleared == 0
    assert _image_values(database) == (artwork_json,) * 3

    results = [
        compact_snapshot_copy(database, blobs, apply=True, batch_size=1)
        for _value in range(3)
    ]
    repeated = compact_snapshot_copy(database, blobs, apply=True, batch_size=1)

    assert [value.rows_cleared for value in results] == [1, 1, 1]
    assert [value.remaining_candidates for value in results] == [True, True, False]
    assert results[-1].foreign_key_check_passed is True
    assert results[-1].integrity_check_passed is True
    assert repeated.rows_scanned == repeated.rows_cleared == 0
    assert repeated.remaining_candidates is False
    assert _image_values(database) == ("[]", "[]", "[]")
    assert database.stat().st_size == original_size
    assert hashlib.sha256(source_database.read_bytes()).hexdigest() == source_hash
    assert _image_values(source_database) == (artwork_json,) * 3


@pytest.mark.parametrize("failure", ("mismatch", "tampered_blob"))
def test_copy_compaction_preserves_unproven_rows(tmp_path: Path, failure: str) -> None:
    database, blobs, artwork_json = _seed_copy(tmp_path)
    if failure == "mismatch":
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE library_management_operation_snapshots "
                "SET image_snapshot_json='[{}]'"
            )
    else:
        blob = next((blobs / "objects").glob("*/*/*.blob"))
        blob.write_bytes(b"tampered")

    with pytest.raises(SnapshotCompactionError):
        compact_snapshot_copy(database, blobs, apply=True)

    values = _image_values(database)
    assert values[0] == artwork_json
    assert values[1] == ("[{}]" if failure == "mismatch" else artwork_json)
    assert values[2] == artwork_json


def test_cli_requires_explicit_isolated_copy_confirmation(tmp_path: Path) -> None:
    database, blobs, _artwork_json = _seed_copy(tmp_path)

    with pytest.raises(SystemExit):
        main(
            [
                "--database",
                str(database),
                "--blob-root",
                str(blobs),
                "--apply",
            ]
        )
