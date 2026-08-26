"""FavoritesStore schema idempotency.

House rule: every store gets a construct-twice idempotency test
(``tests/infrastructure/test_auth_store.py`` pattern).
"""

import sqlite3
import threading
from pathlib import Path

from infrastructure.persistence.favorites_store import FavoritesStore


def test_migration_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "library.db"
    lock = threading.Lock()
    FavoritesStore(db_path, write_lock=lock)
    # Second construction re-runs _ensure_tables (all CREATE TABLE IF NOT EXISTS);
    # it must not raise.
    FavoritesStore(db_path, write_lock=lock)
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(user_favorites)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(user_favorites)")}
    finally:
        conn.close()
    assert {"user_id", "item_kind", "item_id", "created_at"} <= columns
    assert "idx_fav_user_kind" in indexes
