"""AlbumIdentifier - per-folder track-list identification (Lidarr/beets-style).

Scoring math lives in the shared core (``match_scoring_core.py``, step 2.3a):
this module keeps candidate recall, ``select_edition``, and the
``AlbumIdentifier`` shell, and re-exports the moved scoring symbols so
existing importers keep working. The canonical home of each re-export is
the core - import new code from there.
"""

import logging
from collections import Counter
from typing import TYPE_CHECKING

from infrastructure.queue.priority_queue import RequestPriority
from repositories.musicbrainz_base import (
    extract_artist_name,
    select_edition,
)
from services.native.match_scoring_core import (
    ALBUM_ACCEPT_THRESHOLD as ALBUM_ACCEPT_THRESHOLD,
    ARTIST_ACCEPT_FLOOR as ARTIST_ACCEPT_FLOOR,
    MIN_MAPPED_FRACTION as MIN_MAPPED_FRACTION,
    WINNER_MARGIN_FLOOR as WINNER_MARGIN_FLOOR,
    AlbumMatch,
    LocalTrack,
    MBTrack,
    _clean as _clean,
    _Distance as _Distance,
    _hungarian as _hungarian,
    _most_common,
    _most_common_year as _most_common_year,
    _ReleaseMeta,
    _release_type_penalty as _release_type_penalty,
    _string_penalty as _string_penalty,
    album_distance as album_distance,
    assign_tracks as assign_tracks,
    score_release,
    select_decisive_winner,
    track_distance as track_distance,
)

if TYPE_CHECKING:
    from repositories.musicbrainz_repository import MusicBrainzRepository

logger = logging.getLogger(__name__)

_MAX_CANDIDATE_RGS = 10
_ALBUM_SEARCH_LIMIT = 8
_TRACK_SAMPLE = 4

_VA_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"
_VA_NAMES = {"various artists", "various", "va"}

class AlbumIdentifier:
    def __init__(self, mb_repo: "MusicBrainzRepository") -> None:
        self._mb_repo = mb_repo

    async def identify(
        self, locals_: list[LocalTrack], *, seed_release_groups: list[str] | None = None
    ) -> AlbumMatch | None:
        """Identify a folder's release, or None if nothing clears the gate or the
        best two accepted candidates sit closer together than ``WINNER_MARGIN_FLOOR``.

        ``seed_release_groups`` are scored first and unconditionally: the scanner passes
        the release groups its AUDIO FINGERPRINTS resolved to, so a folder whose tags are
        junk (wrong album, compilation track numbers) still gets its real album evaluated.
        A tag-derived text search alone would never surface it - which is exactly how one
        folder's files used to scatter across release groups."""
        if len(locals_) < 2:
            return None
        target_count = len(locals_)
        rg_ids = await self._candidate_release_groups(locals_, seed_release_groups)
        # Score every candidate before deciding: breaking early on a 0.0-distance hit
        # could hide a runner-up inside the margin floor.
        scored: list[tuple[_ReleaseMeta, list[MBTrack], AlbumMatch]] = []
        for rg_id in rg_ids:
            release = await self._best_release(rg_id, target_count)
            if release is None:
                continue
            meta, mb_tracks = release
            if not mb_tracks:
                continue
            match = score_release(locals_, mb_tracks, meta)
            # Choose among ACCEPTED candidates only, by ranking distance (which now favours
            # a studio Album over a compilation/live for the same recordings). This stops a
            # type-preferred-but-poorly-matching album from shadowing a well-matching one.
            if match.accepted:
                scored.append((meta, mb_tracks, match))
        # The shared core owns the winner-margin rule (one margin for both lanes):
        # sole accepted candidate wins outright, otherwise the best needs both a
        # clear distance gap and a fully-supported runner-up to be decisive.
        return select_decisive_winner(locals_, scored)

    async def release_group_type(
        self, release_group_mbid: str
    ) -> tuple[str | None, frozenset[str]]:
        """``(primary_type, {secondary_types})`` for a release group, both lower-cased;
        e.g. ``("album", frozenset())`` is a studio album, ``("album", {"compilation"})``
        a compilation. Reads the release-group detail the matcher already fetches (cached
        1h at the repo). Fails open to ``(None, frozenset())`` - type is advisory."""
        if not release_group_mbid:
            return None, frozenset()
        try:
            detail = await self._mb_repo.get_release_group_by_id(
                release_group_mbid, priority=RequestPriority.BACKGROUND_SYNC
            )
        except Exception as exc:  # noqa: BLE001 - type is advisory; fail open
            logger.warning("RG type fetch failed for %s: %s", release_group_mbid, exc)
            return None, frozenset()
        if not detail:
            return None, frozenset()
        primary = (detail.get("primary-type") or "").lower() or None
        secondary = frozenset(s.lower() for s in (detail.get("secondary-types") or []))
        return primary, secondary

    async def resolve_release_group_artist(
        self, release_group_mbid: str
    ) -> tuple[str | None, str | None]:
        """The canonical primary artist (MBID, name) of a release group."""
        if not release_group_mbid:
            return None, None
        try:
            detail = await self._mb_repo.get_release_group_by_id(
                release_group_mbid,
                includes=["artist-credits"],
                priority=RequestPriority.BACKGROUND_SYNC,
            )
        except Exception as exc:  # noqa: BLE001 - leave the tag-derived artist untouched
            logger.warning("Artist resolve failed for %s: %s", release_group_mbid, exc)
            return None, None
        if not detail:
            return None, None
        credit = detail.get("artist-credit") or []
        if not credit:
            return None, None
        artist = credit[0].get("artist") or {}
        mbid = artist.get("id")
        name = artist.get("name")
        return (mbid or None), (name or None)

    async def _candidate_release_groups(
        self, locals_: list[LocalTrack], seed_release_groups: list[str] | None = None
    ) -> list[str]:
        """Release groups to score: fingerprint-derived seeds first (audio truth), then
        album-title and per-track recording searches ranked by recurrence."""
        seeds = list(dict.fromkeys(m for m in (seed_release_groups or []) if m))
        seed_set = set(seeds)
        artist = _most_common([t.artist for t in locals_])
        album = _most_common([t.album for t in locals_])
        order: list[str] = []
        freq: Counter[str] = Counter()

        def bump(mbid: str | None) -> None:
            if not mbid or mbid in seed_set:
                return
            if mbid not in freq:
                order.append(mbid)
            freq[mbid] += 1

        if artist and album:
            try:
                for result in await self._mb_repo.search_release_groups(
                    artist,
                    album,
                    limit=_ALBUM_SEARCH_LIMIT,
                    include_all_types=True,
                    priority=RequestPriority.BACKGROUND_SYNC,
                ):
                    bump(result.musicbrainz_id)
            except Exception as exc:  # noqa: BLE001 - fail open to recording search
                logger.warning("Album-candidate search failed: %s", exc)

        for local in _sample(locals_, _TRACK_SAMPLE):
            if not local.title:
                continue
            try:
                recordings = await self._mb_repo.search_recordings(
                    artist or local.artist,
                    local.title,
                    limit=5,
                    priority=RequestPriority.BACKGROUND_SYNC,
                )
            except Exception as exc:  # noqa: BLE001 - one bad track must not abort
                logger.warning("Recording-candidate search failed: %s", exc)
                continue
            for rec in recordings:
                for group in rec.release_groups:
                    bump(group.release_group_mbid)

        text_ranked = sorted(set(order), key=lambda mbid: -freq[mbid])
        return (seeds + text_ranked)[:_MAX_CANDIDATE_RGS]

    async def release_tracks(
        self, rg_id: str, target_count: int
    ) -> tuple[_ReleaseMeta, list[MBTrack]] | None:
        """Public wrapper over ``_best_release``: the release-group's best-fitting
        release and its tracklist, for callers (the drop importer) that need the
        per-track metadata behind an identified or manually chosen release group."""
        return await self._best_release(rg_id, target_count)

    async def _best_release(
        self, rg_id: str, target_count: int
    ) -> tuple[_ReleaseMeta, list[MBTrack]] | None:
        """Pick the release whose track count is closest to the folder's, then fetch its tracklist."""
        try:
            # MusicBrainz WS/2 (live-verified 2026-07-21) omits a release-group's
            # artist credit unless artist-credits is requested explicitly.
            detail = await self._mb_repo.get_release_group_by_id(
                rg_id,
                includes=["artist-credits", "releases", "media"],
                priority=RequestPriority.BACKGROUND_SYNC,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Release-group detail fetch failed for %s: %s", rg_id, exc)
            return None
        if not detail:
            return None
        rg_title = detail.get("title", "") or ""
        rg_artist = extract_artist_name(detail) or ""
        primary_type = (detail.get("primary-type") or "").lower() or None
        secondary_types = frozenset(
            s.lower() for s in (detail.get("secondary-types") or [])
        )
        credit = detail.get("artist-credit") or []
        rg_artist_mbid = (credit[0].get("artist") or {}).get("id") if credit else None
        if not rg_artist or not rg_artist_mbid:
            resolved_mbid, resolved_name = await self.resolve_release_group_artist(
                rg_id
            )
            rg_artist = rg_artist or resolved_name or ""
            rg_artist_mbid = rg_artist_mbid or resolved_mbid
        is_various = self._is_various(detail, rg_artist_mbid, rg_artist)

        # F-062: shared best-edition policy - identical ranking to the native
        # identification lane, including the consistent zero-track-count skip
        # (the old first-listed fallback is what made lanes drift).
        release_id = select_edition(detail.get("releases") or [], target_count)
        if release_id is None:
            return None

        try:
            release = await self._mb_repo.get_release_by_id(
                release_id,
                includes=["recordings"],
                priority=RequestPriority.BACKGROUND_SYNC,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Release fetch failed for %s: %s", release_id, exc)
            return None
        if not release:
            return None

        meta = _ReleaseMeta(
            release_group_mbid=rg_id,
            release_mbid=release_id,
            album_title=rg_title,
            artist=rg_artist,
            is_various=is_various,
            artist_mbid=rg_artist_mbid or None,
            year=_parse_year(release.get("date")),
            primary_type=primary_type,
            secondary_types=secondary_types,
        )
        return meta, _build_mb_tracks(release)

    @staticmethod
    def _is_various(rg_detail: dict, artist_mbid: str | None, artist_name: str) -> bool:
        for credit in rg_detail.get("artist-credit") or []:
            artist = credit.get("artist") or {}
            if artist.get("id") == _VA_MBID:
                return True
        return artist_mbid == _VA_MBID or artist_name.strip().lower() in _VA_NAMES


def _build_mb_tracks(release: dict) -> list[MBTrack]:
    tracks: list[MBTrack] = []
    absolute = 0
    for medium in release.get("media") or []:
        disc = int(medium.get("position") or 1)
        for track in medium.get("tracks") or []:
            absolute += 1
            recording = track.get("recording") or {}
            tracks.append(
                MBTrack(
                    title=track.get("title") or recording.get("title") or "",
                    position=int(track.get("position") or 0),
                    disc=disc,
                    absolute_position=absolute,
                    length_ms=track.get("length") or recording.get("length"),
                    recording_mbid=recording.get("id"),
                    release_track_mbid=track.get("id"),
                )
            )
    return tracks


def _parse_year(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        return int(date_str[:4])
    except ValueError:
        return None


def _sample(items: list[LocalTrack], k: int) -> list[LocalTrack]:
    """Up to ``k`` tracks spread evenly across the folder."""
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]
