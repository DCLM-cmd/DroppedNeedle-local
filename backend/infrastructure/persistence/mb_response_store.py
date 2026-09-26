"""Disposable, source-scoped MusicBrainz wire responses in the shared database."""

import asyncio
import time
from typing import Any

from infrastructure.persistence._database import PersistenceBase

MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_LOGICAL_BYTES = 128 * 1024 * 1024
MAX_ROWS = 10_000
FRESH_SECONDS = 24 * 3600
RETENTION_SECONDS = 7 * 24 * 3600


class MbResponseStore(PersistenceBase):
    connection_label = "mb_response_store"

    def _ensure_tables(self) -> None:
        self._hits = 0
        self._evictions = 0
        self._speculative_used = 0
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS mb_response_epoch (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO mb_response_epoch VALUES(1,0);
                CREATE TABLE IF NOT EXISTS mb_responses (
                    key TEXT PRIMARY KEY, source_mode TEXT NOT NULL, source_id TEXT NOT NULL,
                    generation INTEGER NOT NULL, payload BLOB NOT NULL,
                    fetched REAL NOT NULL, fresh REAL NOT NULL, retention REAL NOT NULL,
                    accessed REAL NOT NULL, logical_bytes INTEGER NOT NULL,
                    speculative INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_mb_responses_retention ON mb_responses(retention);
                CREATE INDEX IF NOT EXISTS idx_mb_responses_access ON mb_responses(accessed);
            """)

    async def epoch(self) -> int:
        return await self._read(lambda c: c.execute(
            "SELECT epoch FROM mb_response_epoch WHERE singleton=1"
        ).fetchone()[0])

    async def get(self, key: str, epoch: int) -> dict[str, Any] | None:
        def read(conn):
            conn.execute("BEGIN")
            if conn.execute("SELECT epoch FROM mb_response_epoch WHERE singleton=1").fetchone()[0] != epoch:
                return None
            row = conn.execute("SELECT * FROM mb_responses WHERE key=? AND retention>?", (key, time.time())).fetchone()
            return dict(row) if row else None
        return await self._read(read)

    async def admit(self, key: str, payload: bytes, source: Any, epoch: int, fetched: float, *, speculative: bool = False) -> bool:
        if not source.source_id or len(payload) > MAX_PAYLOAD_BYTES:
            return False
        logical = len(payload) + len(key.encode()) + len(source.source_id.encode()) + len(source.source_mode.encode()) + 128
        def write(conn):
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT epoch FROM mb_response_epoch WHERE singleton=1").fetchone()[0] != epoch:
                return False
            self._evictions += conn.execute("DELETE FROM mb_responses WHERE retention<=?", (time.time(),)).rowcount
            conn.execute("DELETE FROM mb_responses WHERE key=?", (key,))
            count, size = conn.execute("SELECT COUNT(*),COALESCE(SUM(logical_bytes),0) FROM mb_responses").fetchone()
            if count >= MAX_ROWS or size + logical > MAX_LOGICAL_BYTES:
                self._evictions += conn.execute("""
                    DELETE FROM mb_responses WHERE key IN (
                        SELECT key FROM (
                            SELECT key,
                                ROW_NUMBER() OVER (ORDER BY accessed,key) AS ordinal,
                                COALESCE(SUM(logical_bytes) OVER (
                                    ORDER BY accessed,key ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                                ),0) AS preceding_bytes
                            FROM mb_responses
                        ) WHERE ordinal<=? OR preceding_bytes<?
                    )
                """, (max(0, count + 1 - MAX_ROWS), max(0, size + logical - MAX_LOGICAL_BYTES))).rowcount
            conn.execute("INSERT INTO mb_responses VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                key, source.source_mode, source.source_id, source.generation, payload,
                fetched, fetched + FRESH_SECONDS, fetched + RETENTION_SECONDS, fetched, logical,
                int(speculative),
            ))
            return True
        return await self._write(write)

    async def discard(self, key: str) -> None:
        def discard(conn):
            self._evictions += conn.execute("DELETE FROM mb_responses WHERE key=?", (key,)).rowcount
        await self._write(discard)

    async def note_hit(self, row: dict[str, Any], *, foreground: bool) -> None:
        self._hits += 1
        now = time.time()
        used = foreground and bool(row["speculative"])
        if not used and row["accessed"] > now - 3600:
            return
        def update(conn):
            if used:
                changed = conn.execute(
                    "UPDATE mb_responses SET speculative=0,accessed=? WHERE key=? AND fetched=? AND speculative=1",
                    (now, row["key"], row["fetched"]),
                ).rowcount
                self._speculative_used += changed
            else:
                conn.execute("UPDATE mb_responses SET accessed=? WHERE key=? AND accessed<?", (now, row["key"], now - 3600))
        await self._background_write(update)

    async def clear(self) -> int:
        """Caller holding the source fence may invoke this without lock recursion."""
        def clear(conn):
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("SELECT COUNT(*) FROM mb_responses").fetchone()[0]
            conn.execute("UPDATE mb_response_epoch SET epoch=epoch+1 WHERE singleton=1")
            conn.execute("DELETE FROM mb_responses")
            return count
        task = asyncio.create_task(self._write(clear))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def stats(self) -> dict[str, int]:
        def read(conn):
            entries, logical = conn.execute("SELECT COUNT(*),COALESCE(SUM(logical_bytes),0) FROM mb_responses").fetchone()
            allocated = conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]
            wal = self.db_path.with_name(self.db_path.name + "-wal")
            try:
                wal_bytes = wal.stat().st_size
            except FileNotFoundError:
                wal_bytes = 0
            return dict(response_entries=entries, response_logical_bytes=logical,
                        database_allocated_bytes=allocated, database_wal_bytes=wal_bytes,
                        response_hits=self._hits, response_evictions=self._evictions,
                        response_speculative_used=self._speculative_used)
        return self._execute(read, False)
