import sqlite3
import threading
from pathlib import Path
from dataclasses import dataclass
from weakref import WeakValueDictionary


@dataclass
class DiscoveryUserLease:
    active: bool = True

from infrastructure.persistence._database import PersistenceBase, _safe_alter


class DiscoverySnapshotStore(PersistenceBase):
    """Last known-good Discover responses, retained across process restarts."""

    def __init__(self, db_path: Path, write_lock: threading.Lock) -> None:
        super().__init__(db_path, write_lock)
        self._user_leases: WeakValueDictionary[str, DiscoveryUserLease] = WeakValueDictionary()

    def user_lease(self, user_id: str) -> DiscoveryUserLease:
        lease = self._user_leases.get(user_id)
        if lease is None:
            lease = DiscoveryUserLease()
            self._user_leases[user_id] = lease
        return lease

    def _ensure_tables(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discovery_snapshots (
                    snapshot_key TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    saved_at REAL NOT NULL,
                    stale INTEGER NOT NULL DEFAULT 0,
                    catalog_revision INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            _safe_alter(
                conn,
                "ALTER TABLE discovery_snapshots "
                "ADD COLUMN stale INTEGER NOT NULL DEFAULT 0",
            )
            _safe_alter(
                conn,
                "ALTER TABLE discovery_snapshots "
                "ADD COLUMN catalog_revision INTEGER NOT NULL DEFAULT 0",
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS library_catalog_revision ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                "value INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO library_catalog_revision(singleton, value) "
                "VALUES (1, 0)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_discovery_snapshots_user "
                "ON discovery_snapshots(user_id)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS discovery_activity (
                    user_id TEXT NOT NULL, feature TEXT NOT NULL,
                    artist_mbid TEXT NOT NULL DEFAULT '', section TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
                    last_used REAL NOT NULL, last_success REAL NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, serviced_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(user_id, feature, artist_mbid, section, provider)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_activity_due ON discovery_activity(retry_at, serviced_at, last_used)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_activity_expiry ON discovery_activity(last_used)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS discovery_optional_progress (
                    user_id TEXT NOT NULL, work_key TEXT NOT NULL,
                    revision TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(user_id, work_key)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_progress_expiry ON discovery_optional_progress(updated_at)")
            conn.commit()
        finally:
            conn.close()

    async def get(self, snapshot_key: str) -> bytes | None:
        def operation(conn: sqlite3.Connection) -> bytes | None:
            row = conn.execute(
                "SELECT snapshot.payload FROM discovery_snapshots snapshot "
                "JOIN library_catalog_revision revision ON revision.singleton = 1 "
                "WHERE snapshot.snapshot_key = ? "
                "AND snapshot.catalog_revision = revision.value",
                (snapshot_key,),
            ).fetchone()
            return bytes(row["payload"]) if row is not None else None

        return await self._read(operation)

    async def get_with_stale(self, snapshot_key: str) -> tuple[bytes, bool] | None:
        def operation(conn: sqlite3.Connection) -> tuple[bytes, bool] | None:
            row = conn.execute(
                "SELECT snapshot.payload, snapshot.stale "
                "FROM discovery_snapshots snapshot "
                "JOIN library_catalog_revision revision ON revision.singleton = 1 "
                "WHERE snapshot.snapshot_key = ? "
                "AND snapshot.catalog_revision = revision.value",
                (snapshot_key,),
            ).fetchone()
            if row is None:
                return None
            return (bytes(row["payload"]), bool(row["stale"]))

        return await self._read(operation)

    async def save(
        self, snapshot_key: str, user_id: str, payload: bytes, saved_at: float
    ) -> None:
        lease = self.user_lease(user_id)
        def operation(conn: sqlite3.Connection) -> None:
            if not lease.active:
                return
            catalog_revision = int(
                conn.execute(
                    "SELECT value FROM library_catalog_revision WHERE singleton = 1"
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO discovery_snapshots
                    (snapshot_key, user_id, payload, saved_at, stale, catalog_revision)
                VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(snapshot_key) DO UPDATE SET
                    user_id = excluded.user_id,
                    payload = excluded.payload,
                    saved_at = excluded.saved_at,
                    stale = 0,
                    catalog_revision = excluded.catalog_revision
                """,
                (snapshot_key, user_id, payload, saved_at, catalog_revision),
            )

        await self._write(operation)

    async def mark_discover_stale(self) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE discovery_activity SET retry_at = 0, last_success = 0")
            conn.execute(
                "UPDATE discovery_snapshots SET stale = 1 "
                "WHERE snapshot_key LIKE 'discover_response:%' "
                "OR snapshot_key LIKE 'discover_queue:%'"
            )

        await self._write(operation)

    async def delete_source_dependent_snapshots(self) -> int:
        """Delete Discover snapshots whose payload depends on MusicBrainz."""

        def operation(conn: sqlite3.Connection) -> int:
            conn.execute("DELETE FROM discovery_activity")
            conn.execute("DELETE FROM discovery_optional_progress")
            cursor = conn.execute(
                "DELETE FROM discovery_snapshots "
                "WHERE snapshot_key LIKE 'discover_response:%' "
                "OR snapshot_key LIKE 'discover_queue:%'"
            )
            return cursor.rowcount

        return await self._write(operation)

    async def delete_user(self, user_id: str) -> None:
        lease = self._user_leases.pop(user_id, None)
        if lease is not None:
            lease.active = False
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM discovery_activity WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM discovery_optional_progress WHERE user_id = ?", (user_id,))
            conn.execute(
                "DELETE FROM discovery_snapshots WHERE user_id = ?", (user_id,)
            )

        await self._write(operation)

    async def delete(self, snapshot_key: str) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM discovery_snapshots WHERE snapshot_key = ?",
                (snapshot_key,),
            )

        await self._write(operation)

    async def record_activity(
        self, user_id: str, feature: str, source: str, now: float,
        artist_mbid: str = "", section: str = "", provider: str = "",
    ) -> None:
        lease = self.user_lease(user_id)
        def operation(conn: sqlite3.Connection) -> None:
            if not lease.active:
                return
            conn.execute("DELETE FROM discovery_activity WHERE last_used < ?", (now - 86400,))
            conn.execute("DELETE FROM discovery_optional_progress WHERE updated_at < ?", (now - 604800,))
            conn.execute("""
                INSERT INTO discovery_activity(user_id, feature, artist_mbid, section, provider, source, last_used)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, feature, artist_mbid, section, provider) DO UPDATE SET
                    last_used = excluded.last_used, source = excluded.source,
                    retry_at = CASE WHEN discovery_activity.source != excluded.source THEN 0 ELSE discovery_activity.retry_at END,
                    last_success = CASE WHEN discovery_activity.source != excluded.source THEN 0 ELSE discovery_activity.last_success END
                WHERE discovery_activity.last_used <= excluded.last_used - 300
                   OR discovery_activity.source != excluded.source
            """, (user_id, feature, artist_mbid, section, provider, source, now))
            conn.execute("""
                DELETE FROM discovery_activity WHERE rowid IN (
                    SELECT rowid FROM discovery_activity WHERE user_id = ?
                    ORDER BY last_used DESC, rowid DESC LIMIT -1 OFFSET 100
                )
            """, (user_id,))
        await self._write(operation)

    async def get_due_activity(self, source: str, now: float, limit: int = 100, user_id: str | None = None) -> list[dict]:
        def prune(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM discovery_activity WHERE last_used <= ?", (now - 86400,))
            conn.execute("DELETE FROM discovery_optional_progress WHERE updated_at <= ?", (now - 604800,))
        await self._background_write(prune)
        def operation(conn: sqlite3.Connection) -> list[dict]:
            return [dict(row) for row in conn.execute("""
                SELECT * FROM discovery_activity
                WHERE source = ? AND last_used > ? AND retry_at <= ?
                    AND (? IS NULL OR user_id = ?)
                ORDER BY serviced_at, user_id, feature, artist_mbid LIMIT ?
            """, (source, now - 86400, now, user_id, user_id, min(limit, 100)))]
        return await self._read(operation)

    async def finish_activity(self, row: dict, now: float, *, success: bool, retry_seconds: float) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute("""
                UPDATE discovery_activity SET serviced_at = ?, retry_at = ?,
                    last_success = CASE WHEN ? THEN ? ELSE last_success END
                WHERE user_id = ? AND feature = ? AND artist_mbid = ?
                    AND section = ? AND provider = ? AND source = ?
            """, (now, now + retry_seconds, success, now, row["user_id"], row["feature"],
                  row["artist_mbid"], row["section"], row["provider"], row["source"]))
        await self._write(operation)

    async def get_progress(self, user_id: str, work_key: str, revision: str) -> int:
        def operation(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT cursor FROM discovery_optional_progress WHERE user_id = ? AND work_key = ? AND revision = ?",
                               (user_id, work_key, revision)).fetchone()
            return int(row[0]) if row else 0
        return await self._read(operation)

    async def save_progress(self, user_id: str, work_key: str, revision: str, cursor: int, now: float) -> None:
        lease = self.user_lease(user_id)
        def operation(conn: sqlite3.Connection) -> None:
            if not lease.active:
                return
            conn.execute("""
                INSERT INTO discovery_optional_progress VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, work_key) DO UPDATE SET
                    revision=excluded.revision, cursor=excluded.cursor, updated_at=excluded.updated_at
            """, (user_id, work_key, revision, cursor, now))
            conn.execute("DELETE FROM discovery_optional_progress WHERE rowid IN (SELECT rowid FROM discovery_optional_progress WHERE user_id = ? ORDER BY updated_at DESC LIMIT -1 OFFSET 100)", (user_id,))
        await self._write(operation)
