import sqlite3
import threading
from pathlib import Path

from infrastructure.persistence.native_library_store import NativeLibraryStore


def test_artist_reconciliation_schema_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")

    NativeLibraryStore(path, threading.Lock())
    NativeLibraryStore(path, threading.Lock())

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "library_artist_credit_proofs",
        "library_artist_reconciliation_state",
        "library_artist_reconciliation_dismissals",
    } <= tables
