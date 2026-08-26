"""AppPasswordStore schema idempotency.

House rule: every store gets a construct-twice idempotency test
(``tests/infrastructure/test_auth_store.py`` pattern).
"""

import sqlite3
import threading
from pathlib import Path

from infrastructure.persistence.app_password_store import AppPasswordStore


def test_migration_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "library.db"
    lock = threading.Lock()
    AppPasswordStore(db_path, write_lock=lock)
    # Second construction re-runs _ensure_tables (all CREATE TABLE IF NOT EXISTS);
    # it must not raise.
    AppPasswordStore(db_path, write_lock=lock)
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(connect_app_passwords)")
        }
    finally:
        conn.close()
    assert {
        "id",
        "user_id",
        "name",
        "secret_sha256",
        "secret_encrypted",
        "created_at",
        "last_used_at",
        "last_client",
        "revoked",
    } <= columns
