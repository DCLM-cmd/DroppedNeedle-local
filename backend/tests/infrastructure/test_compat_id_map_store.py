"""CompatIdMapStore schema idempotency.

House rule: every store gets a construct-twice idempotency test
(``tests/infrastructure/test_auth_store.py`` pattern).
"""

import sqlite3
import threading
from pathlib import Path

from infrastructure.persistence.compat_id_map_store import CompatIdMapStore


def test_migration_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "library.db"
    lock = threading.Lock()
    CompatIdMapStore(db_path, write_lock=lock)
    # Second construction re-runs _ensure_tables (all CREATE TABLE IF NOT EXISTS);
    # it must not raise.
    CompatIdMapStore(db_path, write_lock=lock)
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(compat_id_map)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(compat_id_map)")}
    finally:
        conn.close()
    assert {"jf_id", "kind", "internal_id"} <= columns
    # the (kind, internal_id) UNIQUE constraint survives double construction
    assert any("sqlite_autoindex" in name for name in indexes)
