"""RequestHistoryStore schema idempotency.

House rule: every store gets a construct-twice idempotency test
(``tests/infrastructure/test_auth_store.py`` pattern). This store also carries
the ALTER TABLE ADD COLUMN ratchet loop, so the second construction must be a
clean no-op and the ratcheted columns must land.
"""

import sqlite3
import threading
from pathlib import Path

from infrastructure.persistence.request_history import RequestHistoryStore


def test_migration_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "library.db"
    lock = threading.Lock()
    RequestHistoryStore(db_path, write_lock=lock)
    # Second construction re-runs _ensure_tables (CREATE TABLE IF NOT EXISTS +
    # the guarded ADD COLUMN ratchet loop); it must not raise.
    RequestHistoryStore(db_path, write_lock=lock)
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(request_history)")}
    finally:
        conn.close()
    assert {
        "musicbrainz_id_lower",
        "musicbrainz_id",
        "artist_name",
        "album_title",
        "requested_at",
        "completed_at",
        "status",
        "monitor_artist",
        "auto_download_artist",
        "user_id",
        "requested_by_name",
        "reviewed_by_id",
        "reviewed_by_name",
        "reviewed_at",
        "download_task_id",
        "release_mbid",
    } <= columns
