import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import msgspec

from infrastructure.persistence._database import PersistenceBase

logger = logging.getLogger(__name__)

_REIMPORTABLE_CONDITION = (
    "status = 'failed'"
    " AND download_task_id IS NOT NULL"
    " AND EXISTS ("
    "SELECT 1 FROM download_tasks"
    " WHERE download_tasks.id = request_history.download_task_id"
    " AND download_tasks.status IN ('failed', 'partial')"
    " AND download_tasks.source_username IS NOT NULL"
    " AND download_tasks.search_job_id IS NOT NULL"
    " AND download_tasks.candidate_index IS NOT NULL"
    ")"
)

_REIMPORTABLE_JOIN_CONDITION = (
    "rh.status = 'failed'"
    " AND rh.download_task_id IS NOT NULL"
    " AND EXISTS ("
    "SELECT 1 FROM download_tasks"
    " WHERE download_tasks.id = rh.download_task_id"
    " AND download_tasks.status IN ('failed', 'partial')"
    " AND download_tasks.source_username IS NOT NULL"
    " AND download_tasks.search_job_id IS NOT NULL"
    " AND download_tasks.candidate_index IS NOT NULL"
    ")"
)

_TERMINAL_STATUSES = frozenset(
    {"imported", "failed", "cancelled", "incomplete", "rejected"}
)
_TASK_ACTIVE_STATUSES = ("pending", "downloading", "queued")


def _request_kind(value: str | None) -> str:
    """Normalize the two persisted request entity kinds."""
    normalized = (value or "album").strip().casefold()
    if normalized not in {"album", "track"}:
        raise ValueError(f"Unsupported request kind: {value!r}")
    return normalized


def _request_key(musicbrainz_id: str, request_kind: str = "album") -> str:
    normalized_id = musicbrainz_id.strip().casefold()
    kind = _request_kind(request_kind)
    return normalized_id if kind == "album" else f"track:{normalized_id}"


class RequesterCancelDecision(msgspec.Struct, frozen=True):
    action: str
    prior_status: str | None
    generation: int | None = None


class RequestBeginResult(msgspec.Struct, frozen=True):
    """The immutable request generation won by one atomic mutation."""

    musicbrainz_id: str
    request_kind: str
    generation: int


class RequestHistoryRecord(msgspec.Struct):
    musicbrainz_id: str
    artist_name: str
    album_title: str
    requested_at: str
    status: str
    artist_mbid: str | None = None
    year: int | None = None
    cover_url: str | None = None
    completed_at: str | None = None
    download_task_id: str | None = None
    monitor_artist: bool = False
    auto_download_artist: bool = False
    user_id: str | None = None
    requested_by_name: str | None = None
    release_mbid: str | None = None
    reviewed_by_id: str | None = None
    reviewed_by_name: str | None = None
    reviewed_at: str | None = None
    request_kind: str = "album"
    track_title: str | None = None
    duration_seconds: int | None = None
    track_release_group_mbid: str | None = None
    dispatch_authorized: bool = False
    generation: int = 1


class RequestHistoryStore(PersistenceBase):
    _ACTIVE_STATUSES = ("pending", "downloading")
    # Statuses a non-admin user sees in their "active" view (includes awaiting approval)
    _USER_ACTIVE_STATUSES = ("pending", "downloading", "awaiting_approval", "queued")

    # foreign_keys intentionally omitted: this store never set it, and adding FK
    # enforcement could raise IntegrityErrors on legacy rows (out-of-scope to
    # enable here). busy_timeout stays unpinned like the other compat-era stores.
    busy_timeout_ms: int | None = None

    def __init__(self, db_path: Path, write_lock: threading.Lock | None = None):
        super().__init__(db_path, write_lock or threading.Lock())

    def _ensure_tables(self) -> None:
        conn = self._connect()
        try:
            requesters_table_existed = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'request_history_requesters'"
                ).fetchone()
                is not None
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_history (
                    musicbrainz_id_lower TEXT PRIMARY KEY,
                    musicbrainz_id TEXT NOT NULL,
                    artist_name TEXT NOT NULL,
                    album_title TEXT NOT NULL,
                    artist_mbid TEXT,
                    year INTEGER,
                    cover_url TEXT,
                    requested_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    monitor_artist INTEGER NOT NULL DEFAULT 0,
                    auto_download_artist INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            for col, definition in [
                ("monitor_artist", "INTEGER NOT NULL DEFAULT 0"),
                ("auto_download_artist", "INTEGER NOT NULL DEFAULT 0"),
                ("user_id", "TEXT"),
                ("requested_by_name", "TEXT"),
                ("reviewed_by_id", "TEXT"),
                ("reviewed_by_name", "TEXT"),
                ("reviewed_at", "TEXT"),
                ("download_task_id", "TEXT"),
                ("release_mbid", "TEXT"),
                ("request_kind", "TEXT NOT NULL DEFAULT 'album'"),
                ("track_title", "TEXT"),
                ("duration_seconds", "INTEGER"),
                ("track_release_group_mbid", "TEXT"),
                ("dispatch_authorized", "INTEGER NOT NULL DEFAULT 0"),
                ("generation", "INTEGER NOT NULL DEFAULT 1"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE request_history ADD COLUMN {col} {definition}"
                    )
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        logger.warning("Unexpected error adding column %s: %s", col, e)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_history_dismissals (
                    user_id TEXT NOT NULL,
                    musicbrainz_id_lower TEXT NOT NULL,
                    dismissed_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (user_id, musicbrainz_id_lower)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_history_requesters (
                    user_id TEXT NOT NULL,
                    musicbrainz_id_lower TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    requested_by_name TEXT,
                    PRIMARY KEY (user_id, musicbrainz_id_lower)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_history_retrying_keyset "
                "ON request_history(status, requested_at DESC, musicbrainz_id_lower DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_history_requesters_mbid "
                "ON request_history_requesters(musicbrainz_id_lower)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_history_requesters_user_time "
                "ON request_history_requesters(user_id, requested_at DESC, musicbrainz_id_lower DESC)"
            )
            # The requester table is introduced alongside this store. Backfill
            # legacy primary listeners only for that first introduction; a later
            # construction must not resurrect deliberately detached listeners.
            if not requesters_table_existed:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO request_history_requesters (
                        user_id, musicbrainz_id_lower, requested_at, requested_by_name
                    )
                    SELECT user_id, musicbrainz_id_lower, requested_at, requested_by_name
                    FROM request_history WHERE user_id IS NOT NULL
                    """
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row | None) -> RequestHistoryRecord | None:
        if row is None:
            return None
        keys = row.keys()
        return RequestHistoryRecord(
            musicbrainz_id=row["musicbrainz_id"],
            artist_name=row["artist_name"],
            album_title=row["album_title"],
            artist_mbid=row["artist_mbid"],
            year=row["year"],
            cover_url=row["cover_url"],
            requested_at=(
                row["requester_requested_at"]
                if "requester_requested_at" in keys
                else row["requested_at"]
            ),
            completed_at=row["completed_at"],
            status=row["status"],
            download_task_id=row["download_task_id"]
            if "download_task_id" in keys
            else None,
            monitor_artist=bool(row["monitor_artist"])
            if row["monitor_artist"] is not None
            else False,
            auto_download_artist=bool(row["auto_download_artist"])
            if row["auto_download_artist"] is not None
            else False,
            user_id=(
                row["requester_user_id"]
                if "requester_user_id" in keys
                else (row["user_id"] if "user_id" in keys else None)
            ),
            requested_by_name=(
                row["requester_name"]
                if "requester_name" in keys
                else (row["requested_by_name"] if "requested_by_name" in keys else None)
            ),
            release_mbid=row["release_mbid"] if "release_mbid" in keys else None,
            reviewed_by_id=row["reviewed_by_id"] if "reviewed_by_id" in keys else None,
            reviewed_by_name=row["reviewed_by_name"]
            if "reviewed_by_name" in keys
            else None,
            reviewed_at=row["reviewed_at"] if "reviewed_at" in keys else None,
            request_kind=(row["request_kind"] if "request_kind" in keys else "album")
            or "album",
            track_title=row["track_title"] if "track_title" in keys else None,
            duration_seconds=(
                row["duration_seconds"] if "duration_seconds" in keys else None
            ),
            track_release_group_mbid=(
                row["track_release_group_mbid"]
                if "track_release_group_mbid" in keys
                else None
            ),
            dispatch_authorized=bool(row["dispatch_authorized"])
            if "dispatch_authorized" in keys and row["dispatch_authorized"] is not None
            else False,
            generation=(
                int(row["generation"])
                if "generation" in keys and row["generation"] is not None
                else 1
            ),
        )



    async def async_record_request(
        self,
        musicbrainz_id: str,
        artist_name: str,
        album_title: str,
        year: int | None = None,
        cover_url: str | None = None,
        artist_mbid: str | None = None,
        monitor_artist: bool = False,
        auto_download_artist: bool = False,
        user_id: str | None = None,
        requested_by_name: str | None = None,
        release_mbid: str | None = None,
        initial_status: str = "pending",
        request_kind: str = "album",
        track_title: str | None = None,
        duration_seconds: int | None = None,
        track_release_group_mbid: str | None = None,
        dispatch_authorized: bool | None = None,
    ) -> RequestBeginResult | None:
        """Atomically claim a request key for a new generation.

        ``None`` means another non-terminal generation already owns the key.
        A terminal row is reused only after incrementing its generation and
        clearing the previous generation's listener and dismissal state.
        """
        kind = _request_kind(request_kind)
        request_key = _request_key(musicbrainz_id, kind)
        requested_at = datetime.now(timezone.utc).isoformat()
        authorized = (
            initial_status != "awaiting_approval"
            if dispatch_authorized is None
            else bool(dispatch_authorized)
        )

        def operation(conn: sqlite3.Connection) -> RequestBeginResult | None:
            existing = conn.execute(
                "SELECT status, generation FROM request_history "
                "WHERE musicbrainz_id_lower = ?",
                (request_key,),
            ).fetchone()
            if existing is not None and str(existing["status"]) not in _TERMINAL_STATUSES:
                return None
            generation = (
                int(existing["generation"] or 1) + 1 if existing is not None else 1
            )

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO request_history (
                        musicbrainz_id_lower, musicbrainz_id, artist_name, album_title,
                        artist_mbid, year, cover_url, requested_at, completed_at, status,
                        monitor_artist, auto_download_artist, user_id, requested_by_name,
                        release_mbid, request_kind, track_title, duration_seconds,
                        track_release_group_mbid, dispatch_authorized, generation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_key,
                        musicbrainz_id,
                        artist_name,
                        album_title,
                        artist_mbid,
                        year,
                        cover_url,
                        requested_at,
                        initial_status,
                        int(monitor_artist),
                        int(auto_download_artist),
                        user_id,
                        requested_by_name,
                        release_mbid,
                        kind,
                        track_title,
                        duration_seconds,
                        track_release_group_mbid,
                        int(authorized),
                        generation,
                    ),
                )
            else:
                # A terminal row starts a fresh generation. Old listener
                # membership and dismissals must never leak into it.
                conn.execute(
                    "DELETE FROM request_history_requesters WHERE musicbrainz_id_lower = ?",
                    (request_key,),
                )
                conn.execute(
                    "DELETE FROM request_history_dismissals WHERE musicbrainz_id_lower = ?",
                    (request_key,),
                )
                conn.execute(
                    """
                    UPDATE request_history
                    SET musicbrainz_id = ?, artist_name = ?, album_title = ?,
                        artist_mbid = ?, year = ?, cover_url = ?, requested_at = ?,
                        completed_at = NULL, status = ?, monitor_artist = ?,
                        auto_download_artist = ?, user_id = ?, requested_by_name = ?,
                        release_mbid = ?, request_kind = ?, track_title = ?,
                        duration_seconds = ?, track_release_group_mbid = ?,
                        download_task_id = NULL, reviewed_by_id = NULL,
                        reviewed_by_name = NULL, reviewed_at = NULL,
                        dispatch_authorized = ?, generation = ?
                    WHERE musicbrainz_id_lower = ?
                    """,
                    (
                        musicbrainz_id,
                        artist_name,
                        album_title,
                        artist_mbid,
                        year,
                        cover_url,
                        requested_at,
                        initial_status,
                        int(monitor_artist),
                        int(auto_download_artist),
                        user_id,
                        requested_by_name,
                        release_mbid,
                        kind,
                        track_title,
                        duration_seconds,
                        track_release_group_mbid,
                        int(authorized),
                        generation,
                        request_key,
                    ),
                )
            if user_id is not None:
                conn.execute(
                    """
                    INSERT INTO request_history_requesters (
                        user_id, musicbrainz_id_lower, requested_at, requested_by_name
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (user_id, request_key, requested_at, requested_by_name),
                )
            return RequestBeginResult(musicbrainz_id, kind, generation)

        return await self._write(operation)

    async def async_bulk_record_requests(
        self,
        items: list[dict],
        monitor_artist: bool = False,
        auto_download_artist: bool = False,
        user_id: str | None = None,
        requested_by_name: str | None = None,
        initial_status: str = "pending",
        request_kind: str = "album",
        dispatch_authorized: bool | None = None,
    ) -> list[RequestBeginResult]:
        """Atomically claim each eligible item and return the exact winners."""
        kind = _request_kind(request_kind)
        requested_at = datetime.now(timezone.utc).isoformat()
        authorized = (
            initial_status != "awaiting_approval"
            if dispatch_authorized is None
            else bool(dispatch_authorized)
        )

        def operation(conn: sqlite3.Connection) -> list[RequestBeginResult]:
            winners: list[RequestBeginResult] = []
            seen_keys: set[str] = set()
            for item in items:
                item_kind = _request_kind(str(item.get("request_kind", kind)))
                raw_id = str(item["musicbrainz_id"])
                request_key = _request_key(raw_id, item_kind)
                if request_key in seen_keys:
                    continue
                seen_keys.add(request_key)
                existing = conn.execute(
                    "SELECT status, generation FROM request_history "
                    "WHERE musicbrainz_id_lower = ?",
                    (request_key,),
                ).fetchone()
                if existing is not None and str(existing["status"]) not in _TERMINAL_STATUSES:
                    continue
                generation = (
                    int(existing["generation"] or 1) + 1
                    if existing is not None
                    else 1
                )

                item_authorized = (
                    bool(item["dispatch_authorized"])
                    if "dispatch_authorized" in item
                    else authorized
                )
                values = (
                    request_key,
                    raw_id,
                    item.get("artist_name", "Unknown"),
                    item.get("album_title", "Unknown"),
                    item.get("artist_mbid"),
                    item.get("year"),
                    item.get("cover_url"),
                    requested_at,
                    initial_status,
                    int(item.get("monitor_artist", monitor_artist)),
                    int(item.get("auto_download_artist", auto_download_artist)),
                    user_id,
                    requested_by_name,
                    item.get("release_mbid"),
                    item_kind,
                    item.get("track_title"),
                    item.get("duration_seconds"),
                    item.get("track_release_group_mbid"),
                    int(item_authorized),
                    generation,
                )
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO request_history (
                            musicbrainz_id_lower, musicbrainz_id, artist_name, album_title,
                            artist_mbid, year, cover_url, requested_at, completed_at, status,
                            monitor_artist, auto_download_artist, user_id, requested_by_name,
                            release_mbid, request_kind, track_title, duration_seconds,
                            track_release_group_mbid, dispatch_authorized, generation
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                else:
                    conn.execute(
                        "DELETE FROM request_history_requesters WHERE musicbrainz_id_lower = ?",
                        (request_key,),
                    )
                    conn.execute(
                        "DELETE FROM request_history_dismissals WHERE musicbrainz_id_lower = ?",
                        (request_key,),
                    )
                    conn.execute(
                        """
                        UPDATE request_history
                        SET musicbrainz_id = ?, artist_name = ?, album_title = ?,
                            artist_mbid = ?, year = ?, cover_url = ?, requested_at = ?,
                            completed_at = NULL, status = ?, monitor_artist = ?,
                            auto_download_artist = ?, user_id = ?, requested_by_name = ?,
                            release_mbid = ?, request_kind = ?, track_title = ?,
                            duration_seconds = ?, track_release_group_mbid = ?,
                            download_task_id = NULL, reviewed_by_id = NULL,
                            reviewed_by_name = NULL, reviewed_at = NULL,
                            dispatch_authorized = ?, generation = ?
                        WHERE musicbrainz_id_lower = ?
                        """,
                        (*values[1:], request_key),
                    )
                if user_id is not None:
                    conn.execute(
                        """
                        INSERT INTO request_history_requesters (
                            user_id, musicbrainz_id_lower, requested_at, requested_by_name
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (user_id, request_key, requested_at, requested_by_name),
                    )
                winners.append(RequestBeginResult(raw_id, item_kind, generation))
            return winners

        return await self._write(operation)

    async def async_add_requester(
        self,
        musicbrainz_id: str,
        user_id: str | None,
        requested_by_name: str | None = None,
        request_kind: str = "album",
    ) -> bool:
        """Attach one listener to an existing non-terminal request atomically."""
        if user_id is None:
            return False
        request_key = _request_key(musicbrainz_id, request_kind)
        requested_at = datetime.now(timezone.utc).isoformat()

        def operation(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT status FROM request_history WHERE musicbrainz_id_lower = ?",
                (request_key,),
            ).fetchone()
            if row is None or str(row["status"]) in _TERMINAL_STATUSES:
                return False
            if str(row["status"]) == "cancelling":
                return False
            conn.execute(
                """
                INSERT INTO request_history_requesters (
                    user_id, musicbrainz_id_lower, requested_at, requested_by_name
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (user_id, musicbrainz_id_lower) DO UPDATE SET
                    requested_by_name = COALESCE(
                        excluded.requested_by_name,
                        request_history_requesters.requested_by_name
                    )
                """,
                (user_id, request_key, requested_at, requested_by_name),
            )
            return True

        return await self._write(operation)

    async def async_add_requesters(
        self,
        musicbrainz_ids: list[str],
        user_id: str | None,
        requested_by_name: str | None = None,
        request_kind: str = "album",
    ) -> int:
        """Attach a listener to each eligible request in one write transaction."""
        if user_id is None:
            return 0
        kind = _request_kind(request_kind)
        requested_at = datetime.now(timezone.utc).isoformat()
        normalized = list(
            dict.fromkeys(_request_key(value, kind) for value in musicbrainz_ids if value)
        )
        if not normalized:
            return 0

        def operation(conn: sqlite3.Connection) -> int:
            attached = 0
            for request_key in normalized:
                row = conn.execute(
                    "SELECT status FROM request_history WHERE musicbrainz_id_lower = ?",
                    (request_key,),
                ).fetchone()
                if row is None or str(row["status"]) in (
                    *_TERMINAL_STATUSES,
                    "cancelling",
                ):
                    continue
                conn.execute(
                    """
                    INSERT INTO request_history_requesters (
                        user_id, musicbrainz_id_lower, requested_at, requested_by_name
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT (user_id, musicbrainz_id_lower) DO UPDATE SET
                        requested_by_name = COALESCE(
                            excluded.requested_by_name,
                            request_history_requesters.requested_by_name
                        )
                    """,
                    (user_id, request_key, requested_at, requested_by_name),
                )
                attached += 1
            return attached

        return await self._write(operation)

    async def async_is_requester(
        self, user_id: str, musicbrainz_id: str, request_kind: str = "album"
    ) -> bool:
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                """
                SELECT 1 FROM request_history_requesters
                WHERE user_id = ? AND musicbrainz_id_lower = ?
                """,
                (user_id, request_key),
            ).fetchone()
            return row is not None

        return await self._read(operation)

    async def async_requester_count(
        self, musicbrainz_id: str, request_kind: str = "album"
    ) -> int:
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM request_history_requesters "
                "WHERE musicbrainz_id_lower = ?",
                (request_key,),
            ).fetchone()
            return int(row["count"] if row is not None else 0)

        return await self._read(operation)

    async def async_remove_requester(
        self, user_id: str, musicbrainz_id: str, request_kind: str = "album"
    ) -> bool:
        """Detach one listener without changing primary attribution."""
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "DELETE FROM request_history_requesters "
                "WHERE user_id = ? AND musicbrainz_id_lower = ?",
                (user_id, request_key),
            )
            return cursor.rowcount > 0

        return await self._write(operation)

    async def async_prepare_requester_cancel(
        self, user_id: str, musicbrainz_id: str, request_kind: str = "album"
    ) -> RequesterCancelDecision:
        """Serialize listener cancellation with attachment and peer cancellation."""
        request_key = _request_key(musicbrainz_id, request_kind)
        completed_at = datetime.now(timezone.utc).isoformat()

        def operation(conn: sqlite3.Connection) -> RequesterCancelDecision:
            row = conn.execute(
                "SELECT status, generation FROM request_history "
                "WHERE musicbrainz_id_lower = ?",
                (request_key,),
            ).fetchone()
            if row is None:
                return RequesterCancelDecision("denied", None)
            prior_status = str(row["status"])
            generation = int(row["generation"] or 1)

            # Validate the generation state before counting or detaching
            # listeners. Terminal and cancelling rows must never lose a
            # listener merely because another listener races cancellation.
            if prior_status not in {
                *_TASK_ACTIVE_STATUSES,
                "awaiting_approval",
            }:
                return RequesterCancelDecision("denied", prior_status, generation)

            member = conn.execute(
                """
                SELECT 1 FROM request_history_requesters
                WHERE user_id = ? AND musicbrainz_id_lower = ?
                """,
                (user_id, request_key),
            ).fetchone()
            if member is None:
                return RequesterCancelDecision("denied", prior_status, generation)

            requester_count_row = conn.execute(
                "SELECT COUNT(*) AS count FROM request_history_requesters "
                "WHERE musicbrainz_id_lower = ?",
                (request_key,),
            ).fetchone()
            requester_count = int(
                requester_count_row["count"] if requester_count_row is not None else 0
            )
            if requester_count > 1:
                conn.execute(
                    "DELETE FROM request_history_requesters "
                    "WHERE user_id = ? AND musicbrainz_id_lower = ?",
                    (user_id, request_key),
                )
                return RequesterCancelDecision("detached", prior_status, generation)

            if prior_status == "awaiting_approval":
                conn.execute(
                    """
                    UPDATE request_history
                    SET status = 'cancelled', completed_at = ?,
                        dispatch_authorized = 0
                    WHERE musicbrainz_id_lower = ? AND generation = ?
                    """,
                    (completed_at, request_key, generation),
                )
                return RequesterCancelDecision("cancelled", prior_status, generation)
            conn.execute(
                """
                UPDATE request_history
                SET status = 'cancelling', completed_at = NULL
                WHERE musicbrainz_id_lower = ? AND generation = ?
                """,
                (request_key, generation),
            )
            return RequesterCancelDecision("cancel_task", prior_status, generation)

        return await self._write(operation)

    async def async_get_record(
        self, musicbrainz_id: str, request_kind: str = "album"
    ) -> RequestHistoryRecord | None:
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> RequestHistoryRecord | None:
            row = conn.execute(
                "SELECT * FROM request_history WHERE musicbrainz_id_lower = ?",
                (request_key,),
            ).fetchone()
            return self._row_to_record(row)

        return await self._read(operation)

    async def async_canonicalize_known_release_aliases(
        self, source_mbids: list[str] | None = None
    ) -> int:
        status_rank = {
            "downloading": 5,
            "pending": 4,
            "queued": 3,
            "awaiting_approval": 2,
        }
        active_statuses = frozenset(status_rank)

        def operation(conn: sqlite3.Connection) -> int:
            has_map = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'mbid_resolution_map'"
            ).fetchone()
            if has_map is None:
                return 0
            filters = (
                "release_group_mbid IS NOT NULL AND trim(release_group_mbid) != '' "
                "AND source_mbid_lower != lower(release_group_mbid)"
            )
            parameters: tuple[str, ...] = ()
            if source_mbids is not None:
                normalized = list(
                    dict.fromkeys(value.casefold() for value in source_mbids if value)
                )
                if not normalized:
                    return 0
                filters += (
                    f" AND source_mbid_lower IN ({','.join('?' for _ in normalized)})"
                )
                parameters = tuple(normalized)
            mappings = conn.execute(
                "SELECT source_mbid_lower, source_mbid, release_group_mbid "
                f"FROM mbid_resolution_map WHERE {filters} ORDER BY source_mbid_lower",
                parameters,
            ).fetchall()
            changed = 0
            for mapping in mappings:
                source_key = str(mapping["source_mbid_lower"])
                target_id = str(mapping["release_group_mbid"])
                target_key = target_id.casefold()
                source = conn.execute(
                    "SELECT * FROM request_history WHERE musicbrainz_id_lower = ?",
                    (source_key,),
                ).fetchone()
                if source is None:
                    continue
                target = conn.execute(
                    "SELECT * FROM request_history WHERE musicbrainz_id_lower = ?",
                    (target_key,),
                ).fetchone()
                source_status = str(source["status"])
                target_status = str(target["status"]) if target is not None else None
                source_wins = target is None or (
                    status_rank.get(source_status, 1),
                    source["download_task_id"] is not None,
                    str(source["requested_at"]),
                ) > (
                    status_rank.get(target_status or "", 1),
                    target["download_task_id"] is not None if target else False,
                    str(target["requested_at"]) if target else "",
                )
                winner = source if source_wins else target
                loser = target if source_wins else source
                values = dict(winner)
                for column in (
                    "artist_mbid",
                    "year",
                    "cover_url",
                    "download_task_id",
                    "reviewed_by_id",
                    "reviewed_by_name",
                    "reviewed_at",
                ):
                    if values[column] is None and loser is not None:
                        values[column] = loser[column]
                values["monitor_artist"] = int(
                    bool(source["monitor_artist"])
                    or bool(target and target["monitor_artist"])
                )
                values["auto_download_artist"] = int(
                    bool(source["auto_download_artist"])
                    or bool(target and target["auto_download_artist"])
                )
                if values["release_mbid"] is None:
                    values["release_mbid"] = (
                        source["release_mbid"] or mapping["source_mbid"]
                    )
                values["musicbrainz_id_lower"] = target_key
                values["musicbrainz_id"] = target_id

                both_active = (
                    target is not None
                    and source_status in active_statuses
                    and target_status in active_statuses
                )
                if both_active:
                    # Two live generations become one live canonical
                    # generation. Preserve every listener, de-duplicating a
                    # user who requested through both aliases.
                    conn.execute(
                        """
                        INSERT INTO request_history_requesters (
                            user_id, musicbrainz_id_lower, requested_at, requested_by_name
                        )
                        SELECT user_id, ?, requested_at, requested_by_name
                        FROM request_history_requesters
                        WHERE musicbrainz_id_lower = ?
                        ON CONFLICT (user_id, musicbrainz_id_lower) DO UPDATE SET
                            requested_at = CASE
                                WHEN excluded.requested_at <
                                    request_history_requesters.requested_at
                                THEN excluded.requested_at
                                ELSE request_history_requesters.requested_at
                            END,
                            requested_by_name = COALESCE(
                                request_history_requesters.requested_by_name,
                                excluded.requested_by_name
                            )
                        """,
                        (target_key, source_key),
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO request_history_dismissals (
                            user_id, musicbrainz_id_lower, dismissed_at
                        )
                        SELECT user_id, ?, dismissed_at
                        FROM request_history_dismissals
                        WHERE musicbrainz_id_lower = ?
                        """,
                        (target_key, source_key),
                    )
                    conn.execute(
                        "DELETE FROM request_history_requesters "
                        "WHERE musicbrainz_id_lower = ?",
                        (source_key,),
                    )
                    conn.execute(
                        "DELETE FROM request_history_dismissals "
                        "WHERE musicbrainz_id_lower = ?",
                        (source_key,),
                    )
                elif source_wins:
                    # The source generation is the only surviving generation.
                    # Remove canonical rows from the losing generation before
                    # rekeying source listeners/dismissals.
                    conn.execute(
                        "DELETE FROM request_history_requesters "
                        "WHERE musicbrainz_id_lower = ?",
                        (target_key,),
                    )
                    conn.execute(
                        "DELETE FROM request_history_dismissals "
                        "WHERE musicbrainz_id_lower = ?",
                        (target_key,),
                    )
                    conn.execute(
                        "UPDATE request_history_requesters SET musicbrainz_id_lower = ? "
                        "WHERE musicbrainz_id_lower = ?",
                        (target_key, source_key),
                    )
                    conn.execute(
                        "UPDATE request_history_dismissals SET musicbrainz_id_lower = ? "
                        "WHERE musicbrainz_id_lower = ?",
                        (target_key, source_key),
                    )
                else:
                    # The canonical generation wins; source listeners and
                    # dismissals belong to a discarded alias generation.
                    conn.execute(
                        "DELETE FROM request_history_requesters "
                        "WHERE musicbrainz_id_lower = ?",
                        (source_key,),
                    )
                    conn.execute(
                        "DELETE FROM request_history_dismissals "
                        "WHERE musicbrainz_id_lower = ?",
                        (source_key,),
                    )

                conn.execute(
                    "DELETE FROM request_history WHERE musicbrainz_id_lower IN (?, ?)",
                    (source_key, target_key),
                )
                columns = list(values)
                conn.execute(
                    f"INSERT INTO request_history ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    tuple(values[column] for column in columns),
                )
                changed += 1
            return changed

        return await self._write(operation)

    async def async_update_monitoring_flags(
        self,
        musicbrainz_id: str,
        *,
        monitor_artist: bool,
        auto_download_artist: bool,
        request_kind: str = "album",
    ) -> None:
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE request_history SET monitor_artist = ?, auto_download_artist = ? "
                "WHERE musicbrainz_id_lower = ?",
                (int(monitor_artist), int(auto_download_artist), request_key),
            )

        await self._write(operation)

    async def async_get_active_mbids(
        self, request_kind: str | None = None
    ) -> set[str]:
        """Return keys with active (pending/downloading) requests."""
        params: tuple[object, ...] = (*self._ACTIVE_STATUSES,)
        kind_clause = ""
        if request_kind is not None:
            kind_clause = " AND request_kind = ?"
            params += (_request_kind(request_kind),)

        def operation(conn: sqlite3.Connection) -> set[str]:
            rows = conn.execute(
                "SELECT musicbrainz_id_lower FROM request_history "
                "WHERE status IN (?, ?)" + kind_clause,
                params,
            ).fetchall()
            return {str(row["musicbrainz_id_lower"]) for row in rows}

        return await self._read(operation)

    async def async_get_requested_mbids(
        self, request_kind: str | None = None
    ) -> set[str]:
        """Return every key that should still appear requested in the UI."""
        placeholders = ",".join("?" for _ in self._USER_ACTIVE_STATUSES)
        params: tuple[object, ...] = (*self._USER_ACTIVE_STATUSES,)
        kind_clause = ""
        if request_kind is not None:
            kind_clause = " AND request_kind = ?"
            params += (_request_kind(request_kind),)

        def operation(conn: sqlite3.Connection) -> set[str]:
            rows = conn.execute(
                "SELECT musicbrainz_id_lower FROM request_history "
                f"WHERE status IN ({placeholders})" + kind_clause,
                params,
            ).fetchall()
            return {str(row["musicbrainz_id_lower"]) for row in rows}

        return await self._read(operation)


    async def async_existing_requested_mbids(
        self, ids: list[str], request_kind: str = "album"
    ) -> set[str]:
        """Return active request keys only for the supplied entity IDs."""
        kind = _request_kind(request_kind)
        normalized = list(
            dict.fromkeys(
                _request_key(value, kind) for value in ids if value and value.strip()
            )
        )
        if not normalized:
            return set()
        status_placeholders = ",".join("?" for _ in self._USER_ACTIVE_STATUSES)

        def operation(conn: sqlite3.Connection) -> set[str]:
            found: set[str] = set()
            for offset in range(0, len(normalized), 500):
                batch = normalized[offset : offset + 500]
                id_placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    "SELECT musicbrainz_id_lower FROM request_history "
                    f"WHERE musicbrainz_id_lower IN ({id_placeholders}) "
                    f"AND status IN ({status_placeholders})",
                    (*batch, *self._USER_ACTIVE_STATUSES),
                ).fetchall()
                found.update(str(row["musicbrainz_id_lower"]) for row in rows)
            return found

        return await self._read(operation)

    async def async_get_active_requests(
        self, request_kind: str | None = None
    ) -> list[RequestHistoryRecord]:
        params: tuple[object, ...] = (*self._ACTIVE_STATUSES,)
        kind_clause = ""
        if request_kind is not None:
            kind_clause = " AND request_kind = ?"
            params += (_request_kind(request_kind),)

        def operation(conn: sqlite3.Connection) -> list[RequestHistoryRecord]:
            rows = conn.execute(
                "SELECT * FROM request_history "
                "WHERE status IN (?, ?)" + kind_clause
                + " ORDER BY requested_at DESC",
                params,
            ).fetchall()
            return [
                record
                for row in rows
                if (record := self._row_to_record(row)) is not None
            ]

        return await self._read(operation)

    async def async_get_active_count(self, request_kind: str | None = None) -> int:
        params: tuple[object, ...] = (*self._ACTIVE_STATUSES,)
        kind_clause = ""
        if request_kind is not None:
            kind_clause = " AND request_kind = ?"
            params += (_request_kind(request_kind),)

        def operation(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM request_history "
                "WHERE status IN (?, ?)" + kind_clause,
                params,
            ).fetchone()
            return int(row["count"] if row is not None else 0)

        return await self._read(operation)

    async def async_get_active_count_for_user(
        self, user_id: str, request_kind: str | None = None
    ) -> int:
        placeholders = ",".join("?" for _ in self._USER_ACTIVE_STATUSES)
        params: list[object] = [user_id, *self._USER_ACTIVE_STATUSES]
        kind_clause = ""
        if request_kind is not None:
            kind_clause = " AND rh.request_kind = ?"
            params.append(_request_kind(request_kind))

        def operation(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM request_history AS rh "
                "JOIN request_history_requesters AS rr "
                "ON rr.musicbrainz_id_lower = rh.musicbrainz_id_lower "
                f"WHERE rr.user_id = ? AND rh.status IN ({placeholders})"
                + kind_clause,
                params,
            ).fetchone()
            return int(row["count"] if row is not None else 0)

        return await self._read(operation)

    async def async_get_active_requests_for_user(
        self, user_id: str, request_kind: str | None = None
    ) -> list[RequestHistoryRecord]:
        """Active requests for one listener, including approval queue items."""
        placeholders = ",".join("?" for _ in self._USER_ACTIVE_STATUSES)
        params: list[object] = [user_id, *self._USER_ACTIVE_STATUSES]
        kind_clause = ""
        if request_kind is not None:
            kind_clause = " AND rh.request_kind = ?"
            params.append(_request_kind(request_kind))

        def operation(conn: sqlite3.Connection) -> list[RequestHistoryRecord]:
            rows = conn.execute(
                "SELECT rh.*, rr.user_id AS requester_user_id, "
                "rr.requested_by_name AS requester_name, "
                "rr.requested_at AS requester_requested_at "
                "FROM request_history AS rh "
                "JOIN request_history_requesters AS rr "
                "ON rr.musicbrainz_id_lower = rh.musicbrainz_id_lower "
                f"WHERE rr.user_id = ? AND rh.status IN ({placeholders})"
                + kind_clause
                + " ORDER BY rr.requested_at DESC",
                params,
            ).fetchall()
            return [
                record
                for row in rows
                if (record := self._row_to_record(row)) is not None
            ]

        return await self._read(operation)

    async def async_get_pending_approvals(
        self, request_kind: str | None = None
    ) -> list[RequestHistoryRecord]:
        """All requests awaiting admin approval."""
        params: list[object] = []
        kind_clause = ""
        if request_kind is not None:
            kind_clause = " AND request_kind = ?"
            params.append(_request_kind(request_kind))

        def operation(conn: sqlite3.Connection) -> list[RequestHistoryRecord]:
            rows = conn.execute(
                "SELECT * FROM request_history "
                "WHERE status = 'awaiting_approval'" + kind_clause
                + " ORDER BY requested_at ASC",
                params,
            ).fetchall()
            return [
                record
                for row in rows
                if (record := self._row_to_record(row)) is not None
            ]

        return await self._read(operation)

    async def async_get_pending_approval_count(
        self, request_kind: str | None = None
    ) -> int:
        params: list[object] = []
        kind_clause = ""
        if request_kind is not None:
            kind_clause = " AND request_kind = ?"
            params.append(_request_kind(request_kind))

        def operation(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM request_history "
                "WHERE status = 'awaiting_approval'" + kind_clause,
                params,
            ).fetchone()
            return int(row["count"] if row is not None else 0)

        return await self._read(operation)

    async def async_count_user_requests_since(
        self,
        user_id: str,
        since_iso: str,
        request_kind: str | None = None,
    ) -> int:
        """Count request generations submitted by one listener in a time window."""
        params: list[object] = [user_id, since_iso]
        kind_clause = ""
        if request_kind is not None:
            kind_clause = " AND rh.request_kind = ?"
            params.append(_request_kind(request_kind))

        def operation(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM request_history AS rh "
                "JOIN request_history_requesters AS rr "
                "ON rr.musicbrainz_id_lower = rh.musicbrainz_id_lower "
                "WHERE rr.user_id = ? AND rr.requested_at >= ?" + kind_clause,
                params,
            ).fetchone()
            return int(row["count"] if row is not None else 0)

        return await self._read(operation)

    async def async_count_linked_track_requests_since(
        self,
        user_id: str,
        since_iso: str,
    ) -> int:
        """Count the user's window track asks whose generation carries a
        download-task link owned by that same listener - the subset already
        represented by a direct task. Unlinked generations (pending approvals,
        task-less terminal rows) are excluded so they never mask unrepresented
        legacy tasks, and co-requesters never inherit another user's link."""

        def operation(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM request_history AS rh "
                "JOIN request_history_requesters AS rr "
                "ON rr.musicbrainz_id_lower = rh.musicbrainz_id_lower "
                "JOIN download_tasks AS dt ON dt.id = rh.download_task_id "
                "WHERE rr.user_id = ? AND rr.requested_at >= ? "
                "AND rh.request_kind = ? AND dt.user_id = rr.user_id",
                (user_id, since_iso, _request_kind("track")),
            ).fetchone()
            return int(row["count"] if row is not None else 0)

        return await self._read(operation)

    async def async_record_review(
        self,
        musicbrainz_id: str,
        status: str,
        reviewed_by_id: str,
        reviewed_by_name: str | None,
        completed_at: str | None = None,
        request_kind: str = "album",
        dispatch_authorized: bool | None = None,
    ) -> None:
        request_key = _request_key(musicbrainz_id, request_kind)
        reviewed_at = datetime.now(timezone.utc).isoformat()

        def operation(conn: sqlite3.Connection) -> None:
            authorization = dispatch_authorized
            if authorization is None and status in _TASK_ACTIVE_STATUSES:
                authorization = True
            if authorization is None:
                conn.execute(
                    """
                    UPDATE request_history
                    SET status = ?, completed_at = COALESCE(?, completed_at),
                        reviewed_by_id = ?, reviewed_by_name = ?, reviewed_at = ?
                    WHERE musicbrainz_id_lower = ?
                    """,
                    (
                        status,
                        completed_at,
                        reviewed_by_id,
                        reviewed_by_name,
                        reviewed_at,
                        request_key,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE request_history
                    SET status = ?, completed_at = COALESCE(?, completed_at),
                        reviewed_by_id = ?, reviewed_by_name = ?, reviewed_at = ?,
                        dispatch_authorized = ?
                    WHERE musicbrainz_id_lower = ?
                    """,
                    (
                        status,
                        completed_at,
                        reviewed_by_id,
                        reviewed_by_name,
                        reviewed_at,
                        int(authorization),
                        request_key,
                    ),
                )

        await self._write(operation)
    async def async_claim_approval(
        self,
        musicbrainz_id: str,
        reviewer_id: str,
        reviewer_name: str | None = None,
        request_kind: str = "album",
        expected_generation: int | None = None,
        target_status: str = "pending",
        completed_at: str | None = None,
    ) -> RequestBeginResult | None:
        """Claim an approval/rejection exactly once for one generation."""
        request_key = _request_key(musicbrainz_id, request_kind)
        reviewed_at = datetime.now(timezone.utc).isoformat()
        kind = _request_kind(request_kind)

        def operation(conn: sqlite3.Connection) -> RequestBeginResult | None:
            row = conn.execute(
                "SELECT status, generation FROM request_history "
                "WHERE musicbrainz_id_lower = ?",
                (request_key,),
            ).fetchone()
            if row is None or str(row["status"]) != "awaiting_approval":
                return None
            generation = int(row["generation"] or 1)
            if (
                expected_generation is not None
                and generation != expected_generation
            ):
                return None
            authorization = target_status in _TASK_ACTIVE_STATUSES
            cursor = conn.execute(
                """
                UPDATE request_history
                SET status = ?, completed_at = ?, reviewed_by_id = ?,
                    reviewed_by_name = ?, reviewed_at = ?, dispatch_authorized = ?
                WHERE musicbrainz_id_lower = ? AND status = 'awaiting_approval'
                    AND generation = ?
                """,
                (
                    target_status,
                    completed_at,
                    reviewer_id,
                    reviewer_name,
                    reviewed_at,
                    int(authorization),
                    request_key,
                    generation,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return RequestBeginResult(musicbrainz_id, kind, generation)

        return await self._write(operation)

    async def async_claim_retry(
        self,
        musicbrainz_id: str,
        user_id: str | None,
        request_kind: str = "album",
        allowed_statuses: tuple[str, ...] = (
            "failed",
            "cancelled",
            "incomplete",
        ),
        target_status: str = "pending",
        expected_generation: int | None = None,
        dispatch_authorized: bool | None = True,
        require_membership: bool = True,
    ) -> RequestBeginResult | None:
        """Atomically claim one terminal retry for a listener."""
        request_key = _request_key(musicbrainz_id, request_kind)
        kind = _request_kind(request_kind)
        allowed = tuple(dict.fromkeys(allowed_statuses))
        if not allowed:
            return None
        status_placeholders = ",".join("?" for _ in allowed)

        def operation(conn: sqlite3.Connection) -> RequestBeginResult | None:
            row = conn.execute(
                "SELECT status, generation FROM request_history "
                "WHERE musicbrainz_id_lower = ?",
                (request_key,),
            ).fetchone()
            if row is None or str(row["status"]) not in allowed:
                return None
            generation = int(row["generation"] or 1)
            if (
                expected_generation is not None
                and generation != expected_generation
            ):
                return None
            if require_membership:
                if user_id is None:
                    return None
                member = conn.execute(
                    """
                    SELECT 1 FROM request_history_requesters
                    WHERE user_id = ? AND musicbrainz_id_lower = ?
                    """,
                    (user_id, request_key),
                ).fetchone()
                if member is None:
                    return None
            authorization = (
                int(dispatch_authorized)
                if dispatch_authorized is not None
                else None
            )
            if authorization is None:
                cursor = conn.execute(
                    f"""
                    UPDATE request_history
                    SET status = ?, completed_at = NULL
                    WHERE musicbrainz_id_lower = ? AND status IN (
                        {status_placeholders}
                    ) AND generation = ?
                    """,
                    (target_status, request_key, *allowed, generation),
                )
            else:
                cursor = conn.execute(
                    f"""
                    UPDATE request_history
                    SET status = ?, completed_at = NULL, dispatch_authorized = ?
                    WHERE musicbrainz_id_lower = ? AND status IN (
                        {status_placeholders}
                    ) AND generation = ?
                    """,
                    (
                        target_status,
                        authorization,
                        request_key,
                        *allowed,
                        generation,
                    ),
                )
            if cursor.rowcount != 1:
                return None
            return RequestBeginResult(musicbrainz_id, kind, generation)

        return await self._write(operation)

    async def async_get_history_for_user(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
        status_filter: str | None = None,
        sort: str | None = None,
        request_kind: str | None = None,
    ) -> tuple[list[RequestHistoryRecord], int]:
        """Paginated history joined to the listener's own request rows."""
        safe_page = max(page, 1)
        safe_page_size = max(page_size, 1)
        offset = (safe_page - 1) * safe_page_size
        params: list[object]
        kind_clause = ""
        kind_param: str | None = None
        if request_kind is not None:
            kind_param = _request_kind(request_kind)
            kind_clause = " AND rh.request_kind = ?"

        _SORT_MAP = {
            "newest": "rr.requested_at DESC",
            "oldest": "rr.requested_at ASC",
            "status": "rh.status ASC, rr.requested_at DESC",
        }
        order_clause = _SORT_MAP.get(sort or "", "rr.requested_at DESC")

        def operation(
            conn: sqlite3.Connection,
        ) -> tuple[list[RequestHistoryRecord], int]:
            dismiss_clause = (
                "AND rh.musicbrainz_id_lower NOT IN "
                "(SELECT musicbrainz_id_lower FROM request_history_dismissals "
                "WHERE user_id = ?)"
            )
            if status_filter == "reimportable":
                where = (
                    f"WHERE rr.user_id = ? AND {_REIMPORTABLE_JOIN_CONDITION} "
                    f"{dismiss_clause}{kind_clause}"
                )
                params = [user_id, user_id]
            elif status_filter:
                where = (
                    f"WHERE rr.user_id = ? AND rh.status = ? "
                    f"{dismiss_clause}{kind_clause}"
                )
                params = [user_id, status_filter, user_id]
            else:
                where = f"WHERE rr.user_id = ? {dismiss_clause}{kind_clause}"
                params = [user_id, user_id]
            if kind_param is not None:
                params.append(kind_param)

            from_clause = (
                "FROM request_history AS rh "
                "JOIN request_history_requesters AS rr "
                "ON rr.musicbrainz_id_lower = rh.musicbrainz_id_lower "
            )
            total_row = conn.execute(
                "SELECT COUNT(*) AS count " + from_clause + where,
                params,
            ).fetchone()
            rows = conn.execute(
                "SELECT rh.*, rr.user_id AS requester_user_id, "
                "rr.requested_by_name AS requester_name, "
                "rr.requested_at AS requester_requested_at "
                + from_clause
                + where
                + f" ORDER BY {order_clause} LIMIT ? OFFSET ?",
                (*params, safe_page_size, offset),
            ).fetchall()
            records = [
                record
                for row in rows
                if (record := self._row_to_record(row)) is not None
            ]
            total = int(total_row["count"] if total_row is not None else 0)
            return records, total

        return await self._read(operation)

    async def async_get_history(
        self,
        page: int = 1,
        page_size: int = 20,
        status_filter: str | None = None,
        sort: str | None = None,
        request_kind: str | None = None,
    ) -> tuple[list[RequestHistoryRecord], int]:
        safe_page = max(page, 1)
        safe_page_size = max(page_size, 1)
        offset = (safe_page - 1) * safe_page_size
        kind_clause = ""
        kind_param: str | None = None
        if request_kind is not None:
            kind_param = _request_kind(request_kind)
            kind_clause = " AND request_kind = ?"

        _SORT_MAP = {
            "newest": "requested_at DESC",
            "oldest": "requested_at ASC",
            "status": "status ASC, requested_at DESC",
        }
        order_clause = _SORT_MAP.get(sort or "", "requested_at DESC")

        def operation(
            conn: sqlite3.Connection,
        ) -> tuple[list[RequestHistoryRecord], int]:
            params: list[object] = []
            if status_filter == "reimportable":
                where_clause = f"WHERE {_REIMPORTABLE_CONDITION}{kind_clause}"
            elif status_filter:
                where_clause = "WHERE status = ?" + kind_clause
                params.append(status_filter)
            else:
                where_clause = "WHERE 1 = 1" + kind_clause
            if kind_param is not None:
                params.append(kind_param)
            total_row = conn.execute(
                "SELECT COUNT(*) AS count FROM request_history " + where_clause,
                params,
            ).fetchone()
            rows = conn.execute(
                "SELECT * FROM request_history "
                + where_clause
                + f" ORDER BY {order_clause} LIMIT ? OFFSET ?",
                (*params, safe_page_size, offset),
            ).fetchall()
            records = [
                record
                for row in rows
                if (record := self._row_to_record(row)) is not None
            ]
            total = int(total_row["count"] if total_row is not None else 0)
            return records, total

        return await self._read(operation)

    async def async_get_retrying_page(
        self,
        status_filter: str,
        page_size: int = 200,
        cursor: tuple[str, str] | None = None,
        owner_id: str | None = None,
        request_kind: str | None = None,
    ) -> tuple[list[RequestHistoryRecord], tuple[str, str] | None]:
        """Return one bounded keyset page of retryable request history."""
        safe_page_size = max(page_size, 1)
        kind_param = _request_kind(request_kind) if request_kind is not None else None

        def operation(
            conn: sqlite3.Connection,
        ) -> tuple[list[RequestHistoryRecord], tuple[str, str] | None]:
            owner_join = ""
            select_requester = ""
            order_time = "rh.requested_at"
            if owner_id is not None:
                owner_join = (
                    " JOIN request_history_requesters AS rr "
                    "ON rr.musicbrainz_id_lower = rh.musicbrainz_id_lower"
                )
                select_requester = (
                    ", rr.user_id AS requester_user_id, "
                    "rr.requested_by_name AS requester_name, "
                    "rr.requested_at AS requester_requested_at"
                )
                order_time = "rr.requested_at"
            clauses = ["rh.status = ?"]
            params: list[object] = [status_filter]
            if owner_id is not None:
                clauses.append("rr.user_id = ?")
                params.append(owner_id)
            if kind_param is not None:
                clauses.append("rh.request_kind = ?")
                params.append(kind_param)
            if cursor is not None:
                last_requested_at, last_key = cursor
                clauses.append(
                    f"({order_time} < ? OR ({order_time} = ? "
                    "AND rh.musicbrainz_id_lower < ?))"
                )
                params.extend([last_requested_at, last_requested_at, last_key])
            where_clause = f"WHERE {' AND '.join(clauses)}"
            rows = conn.execute(
                "SELECT rh.*" + select_requester
                + " FROM request_history AS rh"
                + owner_join
                + f" {where_clause} ORDER BY {order_time} DESC, "
                "rh.musicbrainz_id_lower DESC LIMIT ?",
                (*params, safe_page_size),
            ).fetchall()
            if not rows:
                return [], None
            next_cursor: tuple[str, str] | None = None
            if len(rows) == safe_page_size:
                next_cursor = (
                    str(
                        rows[-1]["requester_requested_at"]
                        if owner_id is not None
                        else rows[-1]["requested_at"]
                    ),
                    str(rows[-1]["musicbrainz_id_lower"]),
                )
            records = [
                record
                for row in rows
                if (record := self._row_to_record(row)) is not None
            ]
            return records, next_cursor

        return await self._read(operation)

    async def async_update_status(
        self,
        musicbrainz_id: str,
        status: str,
        completed_at: str | None = None,
        request_kind: str = "album",
        expected_generation: int | None = None,
    ) -> bool:
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> bool:
            generation_clause = (
                "" if expected_generation is None else " AND generation = ?"
            )
            if status in _TASK_ACTIVE_STATUSES and completed_at is None:
                cursor = conn.execute(
                    "UPDATE request_history SET status = ?, completed_at = NULL "
                    "WHERE musicbrainz_id_lower = ?" + generation_clause,
                    (
                        (status, request_key)
                        if expected_generation is None
                        else (status, request_key, expected_generation)
                    ),
                )
            else:
                cursor = conn.execute(
                    "UPDATE request_history SET status = ?, "
                    "completed_at = COALESCE(?, completed_at) "
                    "WHERE musicbrainz_id_lower = ?" + generation_clause,
                    (
                        (status, completed_at, request_key)
                        if expected_generation is None
                        else (status, completed_at, request_key, expected_generation)
                    ),
                )
            return cursor.rowcount > 0

        return await self._write(operation)

    async def async_restore_request_status(
        self,
        musicbrainz_id: str,
        status: str,
        request_kind: str = "album",
        expected_status: str = "cancelling",
        expected_generation: int | None = None,
    ) -> bool:
        """Restore a status only if cancellation still owns this generation."""
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> bool:
            generation_clause = (
                "" if expected_generation is None else " AND generation = ?"
            )
            cursor = conn.execute(
                "UPDATE request_history SET status = ?, completed_at = NULL "
                "WHERE musicbrainz_id_lower = ? AND status = ?" + generation_clause,
                (
                    (status, request_key, expected_status)
                    if expected_generation is None
                    else (status, request_key, expected_status, expected_generation)
                ),
            )
            return cursor.rowcount > 0

        return await self._write(operation)

    async def async_update_dispatch_authorized(
        self,
        musicbrainz_id: str,
        dispatch_authorized: bool,
        request_kind: str = "album",
        expected_generation: int | None = None,
    ) -> bool:
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> bool:
            generation_clause = (
                "" if expected_generation is None else " AND generation = ?"
            )
            cursor = conn.execute(
                "UPDATE request_history SET dispatch_authorized = ? "
                "WHERE musicbrainz_id_lower = ?" + generation_clause,
                (
                    (int(dispatch_authorized), request_key)
                    if expected_generation is None
                    else (int(dispatch_authorized), request_key, expected_generation)
                ),
            )
            return cursor.rowcount > 0

        return await self._write(operation)

    async def async_update_cover_url(
        self, musicbrainz_id: str, cover_url: str, request_kind: str = "album"
    ) -> None:
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE request_history SET cover_url = ? "
                "WHERE musicbrainz_id_lower = ?",
                (cover_url, request_key),
            )

        await self._write(operation)

    async def async_update_artist_mbid(
        self, musicbrainz_id: str, artist_mbid: str, request_kind: str = "album"
    ) -> None:
        """Backfill the artist MBID without resetting other fields."""
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE request_history SET artist_mbid = ? "
                "WHERE musicbrainz_id_lower = ? "
                "AND (artist_mbid IS NULL OR artist_mbid = '')",
                (artist_mbid, request_key),
            )

        await self._write(operation)

    async def async_update_download_task_id(
        self,
        musicbrainz_id: str,
        download_task_id: str,
        request_kind: str = "album",
        expected_generation: int | None = None,
    ) -> bool:
        """Link a request to its native download task for one generation."""
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> bool:
            generation_clause = (
                "" if expected_generation is None else " AND generation = ?"
            )
            cursor = conn.execute(
                "UPDATE request_history SET download_task_id = ? "
                "WHERE musicbrainz_id_lower = ?" + generation_clause,
                (
                    (download_task_id, request_key)
                    if expected_generation is None
                    else (download_task_id, request_key, expected_generation)
                ),
            )
            return cursor.rowcount > 0

        return await self._write(operation)

    async def async_get_record_by_download_task_id(
        self, download_task_id: str, request_kind: str | None = None
    ) -> RequestHistoryRecord | None:
        """Resolve the request owning a native task."""
        params: tuple[str, ...] = (download_task_id,)
        kind_clause = ""
        if request_kind is not None:
            kind_clause = " AND request_kind = ?"
            params += (_request_kind(request_kind),)

        def operation(conn: sqlite3.Connection) -> RequestHistoryRecord | None:
            row = conn.execute(
                "SELECT * FROM request_history "
                "WHERE download_task_id = ?" + kind_clause + " LIMIT 1",
                params,
            ).fetchone()
            return self._row_to_record(row)

        return await self._read(operation)

    async def async_delete_record(
        self, musicbrainz_id: str, request_kind: str = "album"
    ) -> bool:
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "DELETE FROM request_history WHERE musicbrainz_id_lower = ?",
                (request_key,),
            )
            conn.execute(
                "DELETE FROM request_history_dismissals "
                "WHERE musicbrainz_id_lower = ?",
                (request_key,),
            )
            conn.execute(
                "DELETE FROM request_history_requesters "
                "WHERE musicbrainz_id_lower = ?",
                (request_key,),
            )
            return cursor.rowcount > 0

        return await self._write(operation)

    async def async_dismiss_record(
        self,
        user_id: str,
        musicbrainz_id: str,
        request_kind: str = "album",
    ) -> bool:
        request_key = _request_key(musicbrainz_id, request_kind)

        def operation(conn: sqlite3.Connection) -> bool:
            record = conn.execute(
                "SELECT 1 FROM request_history WHERE musicbrainz_id_lower = ?",
                (request_key,),
            ).fetchone()
            if record is None:
                return False
            conn.execute(
                """
                INSERT INTO request_history_dismissals (user_id, musicbrainz_id_lower)
                VALUES (?, ?)
                ON CONFLICT (user_id, musicbrainz_id_lower) DO NOTHING
                """,
                (user_id, request_key),
            )
            return True

        return await self._write(operation)

    async def prune_old_terminal_requests(self, days: int) -> int:
        """Delete old terminal requests without orphaning wanted watches."""
        import time as _time

        cutoff_iso = datetime.fromtimestamp(
            _time.time() - days * 86400, tz=timezone.utc
        ).isoformat()
        terminal_statuses = tuple(_TERMINAL_STATUSES)
        base = (
            f"DELETE FROM request_history WHERE status IN "
            f"({','.join('?' for _ in terminal_statuses)}) "
            "AND COALESCE(completed_at, requested_at) < ?"
        )
        watch_guard = (
            " AND musicbrainz_id_lower NOT IN (SELECT release_group_mbid_lower"
            " FROM wanted_watches WHERE state IN ('watching','dormant'))"
        )

        def operation(conn: sqlite3.Connection) -> int:
            try:
                cursor = conn.execute(
                    base + watch_guard, (*terminal_statuses, cutoff_iso)
                )
            except sqlite3.OperationalError:
                cursor = conn.execute(base, (*terminal_statuses, cutoff_iso))
            conn.execute(
                "DELETE FROM request_history_requesters WHERE musicbrainz_id_lower "
                "NOT IN (SELECT musicbrainz_id_lower FROM request_history)"
            )
            conn.execute(
                "DELETE FROM request_history_dismissals WHERE musicbrainz_id_lower "
                "NOT IN (SELECT musicbrainz_id_lower FROM request_history)"
            )
            return cursor.rowcount

        return await self._write(operation)
