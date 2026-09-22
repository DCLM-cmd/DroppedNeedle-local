"""Shared album-scoring core for the drop and scan lanes (LibraryFindings-All M-04).

Step 2.3a extracts the drop lane's scoring math here - one Hungarian
assignment, one track/album distance, one winner-margin rule - behind which
both lanes converge (the scan lane adopts it in 2.3b). ``album_matcher.py``
is the core donor: it keeps candidate recall, ``select_edition``, and the
``AlbumIdentifier`` shell, and re-exports the moved scoring symbols so
existing importers keep working. The drop importer reaches the core through
its thin ``_Entry`` -> ``LocalTrack`` projection adapter
(``drop_import_service._to_local``) plus the ``release_tracks()`` seam.

The core exposes both a distance view (``AlbumMatch.distance``) and a score
view (``match_score``) with ONE accept/refuse boundary (``accept_candidate``
/ ``match_accepted``): the oracle asserts boundaries only, never numeric
margins - the lanes' scales differ by design.
"""

import re
from collections import Counter

from rapidfuzz.distance import Levenshtein

from infrastructure.msgspec_fastapi import AppStruct
from models.audio import FingerprintResult
from services.native.musicbrainz_matcher import MusicBrainzMatcher

ALBUM_ACCEPT_THRESHOLD = 0.20
MIN_MAPPED_FRACTION = 0.6
ARTIST_ACCEPT_FLOOR = 0.6

# Winner-margin floor between the two best ACCEPTED candidates: a gap
# smaller than this means a silent min-distance pick is a coin flip, so the
# matcher reports unresolved (None) and the drop importer routes the folder
# to review. Mirrors the native scan lane's CANDIDATE_MARGIN_FLOOR
# (album_evidence_engine.py). Owner sign-off: EditionsEtc S-4. The floor is
# exclusive-below: a gap of exactly the floor still wins (pinned by
# test_album_matcher.py::test_identify_boundary_exact_floor_gap_still_wins).
WINNER_MARGIN_FLOOR = 0.05

# Runner-up pair-support ceilings: a runner-up that leaves any mapped local
# file without an acceptable counterpart is a live alternative, not a beaten
# one, so the win is not decisive and the folder goes to review. The values
# mirror the scan lane's pair-sufficiency notion
# (album_evidence_engine.py PAIR_COST_CEILING + the ``title_cost <= 0.35``
# arm of ``_descriptive_pair``) so both lanes mean the same thing by
# "supported" once the scan lane adopts this core in step 2.3b.
PAIR_SUPPORT_CEILING = 0.40
TITLE_SUPPORT_CEILING = 0.35

_WEIGHTS = {
    "artist": 3.0,
    "album": 3.0,
    "year": 1.0,
    "tracks": 2.0,
    "missing_tracks": 0.6,
    "unmatched_tracks": 0.9,
    "track_title": 3.0,
    "track_length": 2.0,
    "track_index": 1.0,
    "recording_id": 10.0,
    "release_type": 3.0,
}

# Phase 3 (ScannerAlbumIdentity): prefer a studio Album over a compilation/live/other when
# the same recordings appear on several release groups, so a folder isn't attributed to a
# compilation. A ranking-only signal - excluded from the acceptance gate so a genuine
# compilation folder still matches its compilation.
_STUDIO_ALBUM = "album"
_COMP_OR_LIVE_TYPES = frozenset({"compilation", "live"})

_DURATION_GRACE_S = 10.0
_DURATION_WINDOW_S = 30.0

_PAD_COST = 2.0

_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)


class LocalTrack(AppStruct):
    """One audio file in a folder, projected to what the matcher needs."""

    path: str
    title: str
    artist: str
    album: str
    track_number: int = 0
    disc_number: int = 1
    year: int | None = None
    duration_seconds: float | None = None
    recording_mbid: str | None = None


class MBTrack(AppStruct):
    title: str
    position: int
    disc: int
    absolute_position: int
    length_ms: int | None = None
    recording_mbid: str | None = None
    release_track_mbid: str | None = None


class _ReleaseMeta(AppStruct):
    release_group_mbid: str
    release_mbid: str
    album_title: str
    artist: str
    is_various: bool
    artist_mbid: str | None = None
    year: int | None = None
    primary_type: str | None = None
    secondary_types: frozenset[str] = frozenset()


class AlbumMatch(AppStruct):
    accepted: bool
    distance: float
    release_group_mbid: str
    release_mbid: str
    assignments: dict[str, str]
    artist_mbid: str | None = None
    artist_name: str | None = None


def _clean(text: str) -> str:
    """Lidarr-style fold for the edit-distance metric."""
    folded = MusicBrainzMatcher._fold(text or "").lower()
    return _NON_ALNUM.sub("", folded)


def _string_penalty(a: str, b: str) -> float:
    """1 - Levenshtein coefficient over the cleaned strings."""
    ca, cb = _clean(a), _clean(b)
    if not ca and not cb:
        return 0.0
    if not ca or not cb:
        return 1.0
    return Levenshtein.normalized_distance(ca, cb)


class _Distance:
    """Per-key penalty lists, normalised like Lidarr's Distance."""

    def __init__(self) -> None:
        self._d: dict[str, list[float]] = {}

    def add(self, key: str, penalty: float) -> None:
        self._d.setdefault(key, []).append(penalty)

    def add_string(self, key: str, a: str, b: str) -> None:
        self.add(key, _string_penalty(a, b))

    def add_bool(self, key: str, mismatch: bool) -> None:
        self.add(key, 1.0 if mismatch else 0.0)

    def add_ratio(self, key: str, value: float, target: float) -> None:
        if target <= 0:
            return
        self.add(key, max(0.0, min(value, target)) / target)

    def normalized(self, exclude: tuple[str, ...] = ()) -> float:
        num = den = 0.0
        for key, penalties in self._d.items():
            if key in exclude:
                continue
            weight = _WEIGHTS.get(key, 1.0)
            num += sum(penalties) * weight
            den += len(penalties) * weight
        return num / den if den > 0 else 1.0


def track_distance(local: LocalTrack, mb: MBTrack) -> _Distance:
    """Per-pair cost over title, duration, position, recording MBID."""
    d = _Distance()
    d.add_string("track_title", local.title, mb.title)
    if mb.length_ms and local.duration_seconds:
        diff = abs(local.duration_seconds - mb.length_ms / 1000.0) - _DURATION_GRACE_S
        d.add_ratio("track_length", diff, _DURATION_WINDOW_S)
    if local.track_number > 0 and mb.absolute_position > 0:
        matches = local.track_number == mb.absolute_position or (
            mb.position > 0 and local.track_number == mb.position
        )
        d.add_bool("track_index", not matches)
    if local.recording_mbid and mb.recording_mbid:
        d.add_bool("recording_id", local.recording_mbid != mb.recording_mbid)
    return d


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Minimum-cost perfect assignment on a square matrix (Kuhn–Munkres)."""
    n = len(cost)
    if n == 0:
        return []
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = -1
            for j in range(1, n + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    assignment = [0] * n
    for j in range(1, n + 1):
        if p[j]:
            assignment[p[j] - 1] = j - 1
    return assignment


def assign_tracks(
    locals_: list[LocalTrack], mb_tracks: list[MBTrack]
) -> tuple[dict[int, int], list[int], list[int], dict[tuple[int, int], _Distance]]:
    """Optimally map local files to release tracks."""
    n_local, n_mb = len(locals_), len(mb_tracks)
    if n_local == 0 or n_mb == 0:
        return {}, list(range(n_local)), list(range(n_mb)), {}
    size = max(n_local, n_mb)
    cost = [[_PAD_COST] * size for _ in range(size)]
    cache: dict[tuple[int, int], _Distance] = {}
    for i in range(n_local):
        for j in range(n_mb):
            d = track_distance(locals_[i], mb_tracks[j])
            cache[(i, j)] = d
            cost[i][j] = d.normalized()
    assignment = _hungarian(cost)
    mapping: dict[int, int] = {}
    local_extra: list[int] = []
    used_mb: set[int] = set()
    for i in range(n_local):
        j = assignment[i]
        if j < n_mb:
            mapping[i] = j
            used_mb.add(j)
        else:
            local_extra.append(i)
    mb_extra = [j for j in range(n_mb) if j not in used_mb]
    return mapping, local_extra, mb_extra, cache


def _most_common(values: list[str]) -> str:
    cleaned = [v.strip() for v in values if v and v.strip()]
    return Counter(cleaned).most_common(1)[0][0] if cleaned else ""


def _most_common_year(values: list[int | None]) -> int | None:
    years = [y for y in values if y]
    return Counter(years).most_common(1)[0][0] if years else None


def _release_type_penalty(meta: "_ReleaseMeta") -> float:
    """Prefer a studio Album over a compilation/live/other for the same recordings."""
    if meta.secondary_types & _COMP_OR_LIVE_TYPES:
        return 1.0  # compilation / live - strongly deprioritised
    primary = (meta.primary_type or "").lower()
    if primary == _STUDIO_ALBUM:
        return 0.0  # studio album - preferred
    if primary == "ep":
        return 0.3
    return 0.5  # single / broadcast / other / unknown


def album_distance(
    locals_: list[LocalTrack],
    mb_tracks: list[MBTrack],
    meta: _ReleaseMeta,
    mapping: dict[int, int],
    local_extra: list[int],
    mb_extra: list[int],
    track_dists: dict[tuple[int, int], _Distance],
) -> _Distance:
    """Weighted album-level distance."""
    d = _Distance()
    if not meta.is_various:
        d.add_string("artist", _most_common([t.artist for t in locals_]), meta.artist)
    d.add_string("album", _most_common([t.album for t in locals_]), meta.album_title)
    local_year = _most_common_year([t.year for t in locals_])
    if local_year and meta.year:
        d.add_ratio("year", abs(local_year - meta.year), 10.0)
    for i, j in mapping.items():
        d.add("tracks", track_dists[(i, j)].normalized())
    cap = len(locals_)
    for _ in range(min(len(mb_extra), cap)):
        d.add("missing_tracks", 1.0)
    for _ in range(min(len(local_extra), cap)):
        d.add("unmatched_tracks", 1.0)
    d.add("release_type", _release_type_penalty(meta))
    return d


def accept_candidate(
    *, gate_distance: float, mapped_fraction: float, artist_ok: bool
) -> bool:
    """The ONE accept/refuse boundary both views share: a candidate is
    actionable only inside the album gate, with enough files mapped, and
    without a clearly-different artist."""
    return (
        gate_distance <= ALBUM_ACCEPT_THRESHOLD
        and mapped_fraction >= MIN_MAPPED_FRACTION
        and artist_ok
    )


def match_accepted(match: AlbumMatch) -> bool:
    """Read the shared boundary off a scored match (the F-01 gate predicate)."""
    return match.accepted


def match_score(distance: float) -> float:
    """Score view of a distance on the shared 0-1 scale (mirrors the scan
    lane's ``score = 1 - distance``); the boundary, not the number, decides."""
    return max(0.0, 1.0 - distance)


def score_release(
    locals_: list[LocalTrack], mb_tracks: list[MBTrack], meta: _ReleaseMeta
) -> AlbumMatch:
    """Assign + score one candidate release."""
    mapping, local_extra, mb_extra, track_dists = assign_tracks(locals_, mb_tracks)
    dist = album_distance(
        locals_, mb_tracks, meta, mapping, local_extra, mb_extra, track_dists
    )
    # Missing tracks (a release we only partly hold) count neither for ranking nor
    # acceptance, so an incomplete studio album isn't out-ranked by a smaller release.
    # release_type ranks (studio Album > compilation/live) but must NOT gate acceptance,
    # or a genuine compilation folder would fail to match its own compilation.
    full = dist.normalized(exclude=("missing_tracks",))
    gate = dist.normalized(exclude=("missing_tracks", "release_type"))
    mapped_fraction = len(mapping) / len(locals_) if locals_ else 0.0
    accepted = accept_candidate(
        gate_distance=gate,
        mapped_fraction=mapped_fraction,
        artist_ok=_artist_ok(locals_, meta),
    )
    assignments = {
        locals_[i].path: (mb_tracks[j].recording_mbid or "") for i, j in mapping.items()
    }
    return AlbumMatch(
        accepted=accepted,
        distance=round(full, 4),
        release_group_mbid=meta.release_group_mbid,
        release_mbid=meta.release_mbid,
        assignments=assignments,
        artist_mbid=None if meta.is_various else meta.artist_mbid,
        artist_name=None if meta.is_various else (meta.artist or None),
    )


def _artist_ok(locals_: list[LocalTrack], meta: _ReleaseMeta) -> bool:
    """A clearly-different artist blocks acceptance."""
    if meta.is_various or not meta.artist:
        return True
    penalty = _string_penalty(_most_common([t.artist for t in locals_]), meta.artist)
    return penalty <= (1.0 - ARTIST_ACCEPT_FLOOR)


def _runner_up_supported(
    locals_: list[LocalTrack], mb_tracks: list[MBTrack]
) -> bool:
    """True when every mapped runner-up pair is descriptively supported.

    Unmapped extras on either side do not veto: the acceptance gate already
    prices them (``missing_tracks``/``unmatched_tracks``), and this clause
    answers only "does some local file contradict the runner-up", the
    question the scan lane's near-miss rescue asks before keeping an
    alternative alive.
    """
    mapping, _, _, track_dists = assign_tracks(locals_, mb_tracks)
    for i, j in mapping.items():
        if (
            _string_penalty(locals_[i].title, mb_tracks[j].title)
            > TITLE_SUPPORT_CEILING
        ):
            return False
        if track_dists[(i, j)].normalized() > PAIR_SUPPORT_CEILING:
            return False
    return True


def select_decisive_winner(
    locals_: list[LocalTrack],
    scored: list[tuple[_ReleaseMeta, list[MBTrack], AlbumMatch]],
) -> AlbumMatch | None:
    """The ONE winner-margin rule: the best ACCEPTED candidate wins outright
    only when the runner-up sits at least ``WINNER_MARGIN_FLOOR`` behind
    (exclusive-below) AND every runner-up pair is supported - otherwise the
    pick would be a coin flip and the folder goes to review. A lone accepted
    candidate is unaffected (the floor is vacuous, absolute distance moot)."""
    accepted = [entry for entry in scored if entry[2].accepted]
    if not accepted:
        return None
    # Stable sort: the earlier-recalled candidate wins exact ties, as before.
    accepted.sort(key=lambda entry: entry[2].distance)
    if len(accepted) == 1:
        return accepted[0][2]
    best = accepted[0][2]
    runner_tracks, runner = accepted[1][1], accepted[1][2]
    if runner.distance - best.distance < WINNER_MARGIN_FLOOR:
        return None
    if not _runner_up_supported(locals_, runner_tracks):
        return None
    return best


def fingerprint_hit(
    fp: FingerprintResult, *, threshold: float
) -> str | None:
    """Shared audio-proof predicate for the drop lane's fingerprint paths:
    the confirmed recording MBID when the lookup passed at or above the
    confidence floor, else None. Fail-open per file - callers degrade to
    tag-only. A None lookup raises AttributeError exactly like the inline
    check it replaces, so the enrich loop's fail-open handler still logs it.
    """
    if (
        fp.status == "pass"
        and (fp.score or 0.0) >= threshold
        and fp.recording_id
    ):
        return fp.recording_id
    return None
