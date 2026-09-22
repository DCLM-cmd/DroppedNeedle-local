"""Compat discovery: owned-local hybrid, no outbound by default.

All results are intersected with the active owned library so everything returned
actually streams. The similar-songs related pool is gated by
``ConnectAppsSettings.discover_mode``:

    local-only            same-artist pool only (no outbound). DEFAULT.
    lazy-mb               + related artists fetched from MusicBrainz ONCE per
                          artist and cached in library_artists.related_artist_mbids.
    use-scrobble-targets  + ArtistDiscoveryService when the user has
                          Last.fm/ListenBrainz configured, else local.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from infrastructure.persistence.auth_store import UserRecord
    from infrastructure.persistence.library_db import LibraryDB
    from infrastructure.persistence.play_history_store import PlayHistoryStore
    from services.artist_discovery_service import ArtistDiscoveryService
    from services.compat.library_view_service import LibraryViewService
    from services.compat.view_models import ViewAlbum, ViewTrack
    from services.per_user_client_factory import PerUserClientFactory
    from services.preferences_service import PreferencesService

logger = logging.getLogger(__name__)

RelatedFetcher = Callable[[str], Awaitable[list[str]]]

_HISTORY_IDS_CAP = 100_000  # distinct played albums fetched for history paging
_HISTORY_TRACK_WINDOW = 2000  # recent plays scanned for history track order
_APPEARS_ON_TRACK_CAP = 200  # track rows fanned out per appears-on lookup


def _album_title_key(album: "ViewAlbum") -> str:
    return ((album.sort_name or album.title) or "").casefold()


def _track_title_key(track: "ViewTrack") -> str:
    return ((track.sort_name or track.title) or "").casefold()


def _looks_like_real_mbid(value: str) -> bool:
    # real MusicBrainz MBIDs are dashed UUIDs; synthetic ids are dashless
    return "-" in value


class CompatDiscoverService:
    def __init__(
        self,
        *,
        library_db: "LibraryDB",
        library_view_service: "LibraryViewService",
        preferences_service: "PreferencesService",
        play_history_store: "PlayHistoryStore",
        artist_discovery_service: "ArtistDiscoveryService | None" = None,
        client_factory: "PerUserClientFactory | None" = None,
        related_artists_fetcher: RelatedFetcher | None = None,
    ) -> None:
        self._db = library_db
        self._view = library_view_service
        self._preferences = preferences_service
        self._play_history = play_history_store
        self._artist_discovery = artist_discovery_service
        self._client_factory = client_factory
        self._related_fetcher = related_artists_fetcher

    async def get_top_songs(
        self, artist_name: str, *, user_id: str, count: int = 50,
        user: "UserRecord | None" = None,
    ) -> list["ViewTrack"]:
        counts = await self._play_history.play_counts_by_artist(user_id, artist_name)
        files = await self._db.get_files_by_artist_name(
            artist_name, limit=max(count * 4, count)
        )

        def plays(row: dict) -> int:
            rec = row.get("recording_mbid")
            if rec and f"rec:{rec}" in counts:
                return counts[f"rec:{rec}"]
            return counts.get(f"name:{(row.get('track_title') or '').lower()}", 0)

        # stable sort: most-played first, recency (query order) breaks ties
        files.sort(key=plays, reverse=True)
        return await self._view.tracks_from_rows(files[:count], user=user)

    async def get_history_albums(
        self,
        *,
        user_id: str,
        frequent: bool,
        limit: int,
        offset: int,
        user: "UserRecord | None" = None,
    ) -> list["ViewAlbum"]:
        ids = await self._play_history.album_ids(
            user_id, frequent=frequent, limit=limit, offset=offset
        )
        return await self._view.get_albums_by_ids(ids, user=user)

    async def get_random_songs(
        self, *, count: int = 50, genre: str | None = None,
        from_year: int | None = None, to_year: int | None = None,
        user: "UserRecord | None" = None,
    ) -> list["ViewTrack"]:
        rows = await self._db.get_random_files(
            limit=count, genre=genre, from_year=from_year, to_year=to_year
        )
        return await self._view.tracks_from_rows(rows, user=user)

    async def get_similar_songs(
        self, artist_mbid: str, *, user_id: str, count: int = 50,
        user: "UserRecord | None" = None,
    ) -> list["ViewTrack"]:
        mbids = [artist_mbid]  # same-artist pool always included
        for m in await self._related_mbids(artist_mbid, user_id):
            if m and m not in mbids:
                mbids.append(m)
        rows = await self._db.get_files_by_artist_mbids(mbids, limit=count)
        return await self._view.tracks_from_rows(rows, user=user)

    async def _related_mbids(self, artist_mbid: str, user_id: str) -> list[str]:
        mode = self._preferences.get_connect_apps_settings().discover_mode
        if mode == "lazy-mb":
            return await self._lazy_mb_related(artist_mbid)
        if mode == "use-scrobble-targets":
            return await self._scrobble_target_related(artist_mbid, user_id)
        return []  # local-only

    async def _lazy_mb_related(self, artist_mbid: str) -> list[str]:
        cached = await self._db.get_related_artist_mbids(artist_mbid)
        if cached is not None:
            return [m for m in cached.split(",") if m]
        # synthetic ids can't resolve in MusicBrainz; cache empty so we don't
        # retry every request.
        if self._related_fetcher is None or not _looks_like_real_mbid(artist_mbid):
            await self._db.set_related_artist_mbids(artist_mbid, "")
            return []
        try:
            related = await self._related_fetcher(artist_mbid) or []
        except Exception:  # noqa: BLE001 - discovery must never fail the request
            logger.warning("lazy-mb related fetch failed for %s", artist_mbid[:8], exc_info=True)
            related = []
        await self._db.set_related_artist_mbids(artist_mbid, ",".join(related))
        return related

    async def _scrobble_target_related(self, artist_mbid: str, user_id: str) -> list[str]:
        if self._artist_discovery is None or not await self._has_scrobble_targets(user_id):
            return []
        resp = await self._artist_discovery.get_similar_artists(
            artist_mbid, user_id=user_id
        )
        return [a.musicbrainz_id for a in resp.similar_artists if a.musicbrainz_id]

    async def _has_scrobble_targets(self, user_id: str) -> bool:
        if self._client_factory is None:
            return False
        if await self._client_factory.resolve_lastfm(user_id) is not None:
            return True
        return await self._client_factory.resolve_listenbrainz(user_id) is not None
    # ===== Jellyfin browse sorts + appears-on (issue #241) =====
    # The Jellyfin router allowlist-maps SortBy onto these helpers. `descending`
    # is absolute (newest-first / Z-A / most-played-first). Native store sorts
    # are reused through the view; only directions the store lacks (title/artist
    # DESC, track year) resolve here over bounded windows. Client text never
    # reaches SQL: only the fixed keys below select a native sort.

    async def get_sorted_albums(
        self, *, sort: str = "recent", descending: bool = False,
        limit: int = 100, offset: int = 0,
        q: str | None = None, user: "UserRecord | None" = None,
    ) -> tuple[list["ViewAlbum"], int]:
        """Albums via native allowlist sorts. `sort` is one of recent/title/
        artist/year/random (anything else behaves as recent). Title/artist
        descending mirror a window from the end, exact at any size."""
        limit = limit if limit > 0 else 100
        offset = max(offset, 0)
        if sort not in ("recent", "title", "artist", "year", "random"):
            sort = "recent"
        if sort == "random":
            return await self._view.get_albums_offset(
                limit=limit, offset=offset, sort="random", q=q, user=user
            )
        if sort == "year":
            native = "year_desc" if descending else "year_asc"
            return await self._view.get_albums_offset(
                limit=limit, offset=offset, sort=native, q=q, user=user
            )
        if sort == "recent":
            native = "recent" if descending else "oldest"
            return await self._view.get_albums_offset(
                limit=limit, offset=offset, sort=native, q=q, user=user
            )
        if not descending:
            return await self._view.get_albums_offset(
                limit=limit, offset=offset, sort=sort, q=q, user=user
            )

        async def fetch(n: int, o: int):
            return await self._view.get_albums_offset(
                limit=n, offset=o, sort=sort, q=q, user=user
            )

        return await self._fetch_desc_mirror(fetch, limit=limit, offset=offset)

    async def get_sorted_tracks(
        self, *, sort: str = "recent", descending: bool = False,
        limit: int = 100, offset: int = 0,
        q: str | None = None, user: "UserRecord | None" = None,
    ) -> tuple[list["ViewTrack"], int]:
        """Tracks via native allowlist sorts (recent/title/artist/album). Year
        has no native sort, so it orders one full fetch in memory; random serves
        a shuffled window (StartIndex-insensitive by nature)."""
        limit = limit if limit > 0 else 100
        offset = max(offset, 0)
        if sort not in ("recent", "title", "artist", "album", "year", "random"):
            sort = "recent"
        if sort == "random":
            _, total = await self._view.get_tracks_page(
                limit=1, offset=0, sort="recent", q=q
            )
            if total == 0:
                return [], 0
            if total <= limit:
                items, _ = await self._view.get_tracks_page(
                    limit=total, offset=0, sort="recent", q=q, user=user
                )
            else:
                start = random.randint(0, total - limit)
                items, _ = await self._view.get_tracks_page(
                    limit=limit, offset=start, sort="recent", q=q, user=user
                )
            random.shuffle(items)
            return items, total
        if sort == "year":
            _, total = await self._view.get_tracks_page(
                limit=1, offset=0, sort="recent", q=q
            )
            if total == 0 or offset >= total:
                return [], total
            items, _ = await self._view.get_tracks_page(
                limit=total, offset=0, sort="recent", q=q, user=user
            )
            ordered = self.sort_tracks(items, sort="year", descending=descending)
            return ordered[offset:offset + limit], total
        if sort == "recent" and not descending:
            # Native "recent" is newest-first; oldest-first mirrors from the end.
            async def fetch_oldest(n: int, o: int):
                return await self._view.get_tracks_page(
                    limit=n, offset=o, sort="recent", q=q, user=user
                )

            return await self._fetch_desc_mirror(
                fetch_oldest, limit=limit, offset=offset
            )
        if not descending:
            return await self._view.get_tracks_page(
                limit=limit, offset=offset, sort=sort, q=q, user=user
            )

        async def fetch(n: int, o: int):
            return await self._view.get_tracks_page(
                limit=n, offset=o, sort=sort, q=q, user=user
            )

        return await self._fetch_desc_mirror(fetch, limit=limit, offset=offset)

    @staticmethod
    async def _fetch_desc_mirror(fetch, *, limit: int, offset: int):
        """Exact descending page over an ascending-only native sort: mirror the
        window from the end and reverse. `fetch(n, o)` returns (items, total)
        in ascending order."""
        _, total = await fetch(1, 0)
        if total == 0 or offset >= total:
            return [], total
        asc_end = total - offset
        asc_start = max(asc_end - limit, 0)
        items, _ = await fetch(asc_end - asc_start, asc_start)
        items.reverse()
        return items, total

    async def get_history_albums_page(
        self, *, user_id: str, frequent: bool, descending: bool = True,
        limit: int = 100, offset: int = 0,
        user: "UserRecord | None" = None,
    ) -> tuple[list["ViewAlbum"], int]:
        """Play-history album order (Jellyfin DatePlayed/PlayCount). The id list
        is cheap strings, so the full ordering is fetched and ascending pages by
        reversing before slicing. Entries whose files left the library resolve
        to nothing (total then overstates the pageable set by those entries)."""
        offset = max(offset, 0)
        ids = await self._play_history.album_ids(
            user_id, frequent=frequent, limit=_HISTORY_IDS_CAP, offset=0
        )
        if not descending:
            ids.reverse()
        total = len(ids)
        albums = await self._view.get_albums_by_ids(
            ids[offset:offset + limit], user=user
        )
        return albums, total

    async def get_history_tracks_page(
        self, *, user_id: str, frequent: bool, descending: bool = True,
        limit: int = 100, offset: int = 0,
        user: "UserRecord | None" = None,
    ) -> tuple[list["ViewTrack"], int]:
        """Play-history track order with no store change: aggregate a bounded
        recent-play window in Python, then resolve recording MBIDs to owned
        files. Plays without a recording MBID cannot join the library and are
        skipped; entries whose files left the library resolve to nothing (total
        then overstates the pageable set by those entries)."""
        limit = limit if limit > 0 else 100
        offset = max(offset, 0)
        records = await self._play_history.recent(
            user_id, limit=_HISTORY_TRACK_WINDOW
        )
        counts: dict[str, int] = {}
        first_seen: dict[str, int] = {}
        for index, rec in enumerate(records):
            mbid = rec.recording_mbid
            if not mbid:
                continue
            counts[mbid] = counts.get(mbid, 0) + 1
            first_seen.setdefault(mbid, index)
        if frequent:
            ordered = sorted(
                first_seen, key=lambda m: (-counts[m], first_seen[m])
            )
        else:
            ordered = sorted(first_seen, key=lambda m: first_seen[m])
        if not descending:
            ordered.reverse()
        total = len(ordered)
        rows = []
        for mbid in ordered[offset:offset + limit]:
            files = await self._db.get_library_files_for_recording(mbid)
            if files:
                rows.append(files[0])
        tracks = await self._view.tracks_from_rows(rows, user=user)
        return tracks, total

    async def get_appears_on_albums(
        self, artist_mbids: list[str], *, sort: str = "recent",
        descending: bool = True, user: "UserRecord | None" = None,
    ) -> list["ViewAlbum"]:
        """Appears-on (Jellyfin ContributingArtistIds): distinct albums where the
        artists hold TRACK credits, minus albums where they are the album
        artist. Track fan-out is capped at the newest imports so the subset is
        deterministic; the small resolved set sorts in memory
        (default: recently added first)."""
        wanted = {m for m in artist_mbids if m}
        if not wanted:
            return []
        rows = await self._db.get_files_by_artist_mbids(
            sorted(wanted), limit=_APPEARS_ON_TRACK_CAP, order="recent"
        )
        own_rgs = {
            r["release_group_mbid"]
            for r in rows
            if r.get("release_group_mbid")
            and r.get("album_artist_mbid") in wanted
        }
        ordered_rgs = list(
            dict.fromkeys(
                r["release_group_mbid"]
                for r in rows
                if r.get("release_group_mbid")
                and r.get("artist_mbid") in wanted
                and r["release_group_mbid"] not in own_rgs
            )
        )
        albums = await self._view.get_albums_by_ids(ordered_rgs, user=user)
        if sort not in ("recent", "title", "year", "played", "playcount", "random"):
            sort = "recent"
        return self.sort_albums(albums, sort=sort, descending=descending)

    def sort_albums(
        self, albums: list["ViewAlbum"], *, sort: str = "recent",
        descending: bool = False,
    ) -> list["ViewAlbum"]:
        """In-memory sort for small resolved sets (appears-on, filtered lists).
        Direction is absolute: descending=True is newest/most-played/Z-A.
        Items lacking the key sort last in both directions."""
        items = list(albums)
        if sort == "random":
            random.shuffle(items)
            return items
        if sort == "played":
            dated = sorted(
                (a for a in items if a.played_at),
                key=lambda a: a.played_at or "",
                reverse=descending,
            )
            return dated + [a for a in items if not a.played_at]
        if sort == "playcount":
            return sorted(
                items, key=lambda a: a.play_count or 0, reverse=descending
            )
        if sort == "year":
            known = sorted(
                (a for a in items if a.year is not None),
                key=lambda a: (a.year or 0, _album_title_key(a)),
                reverse=descending,
            )
            return known + [a for a in items if a.year is None]
        if sort == "title":
            return sorted(items, key=_album_title_key, reverse=descending)
        dated = sorted(
            (a for a in items if a.date_added is not None),
            key=lambda a: a.date_added or 0,
            reverse=descending,
        )
        return dated + [a for a in items if a.date_added is None]

    def sort_tracks(
        self, tracks: list["ViewTrack"], *, sort: str = "recent",
        descending: bool = False,
    ) -> list["ViewTrack"]:
        """In-memory sort for small resolved sets. Same absolute-direction
        contract as sort_albums; unknown keys behave as recent."""
        items = list(tracks)
        if sort == "random":
            random.shuffle(items)
            return items
        if sort == "played":
            dated = sorted(
                (t for t in items if t.played_at),
                key=lambda t: t.played_at or "",
                reverse=descending,
            )
            return dated + [t for t in items if not t.played_at]
        if sort == "playcount":
            return sorted(
                items, key=lambda t: t.play_count or 0, reverse=descending
            )
        if sort == "year":
            known = sorted(
                (t for t in items if t.year is not None),
                key=lambda t: (t.year or 0, _track_title_key(t)),
                reverse=descending,
            )
            return known + [t for t in items if t.year is None]
        if sort in ("title", "album"):
            return sorted(items, key=_track_title_key, reverse=descending)
        dated = sorted(
            (t for t in items if t.created_at is not None),
            key=lambda t: t.created_at or 0,
            reverse=descending,
        )
        return dated + [t for t in items if t.created_at is None]
