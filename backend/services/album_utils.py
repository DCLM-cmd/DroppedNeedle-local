from typing import Optional
from api.v1.schemas.album import Track
from services.native.edition_suffix import (
    RATING_CLEAN,
    RATING_EXPLICIT,
    edition_rating,
)

EXPLICIT_RANK_EXPLICIT = 0
EXPLICIT_RANK_UNMARKED = 1
EXPLICIT_RANK_CLEAN = 2

_RANK_BY_RATING = {
    RATING_EXPLICIT: EXPLICIT_RANK_EXPLICIT,
    RATING_CLEAN: EXPLICIT_RANK_CLEAN,
}


def release_edition_rating(release: dict) -> str:
    """MusicBrainz marks a censored cut on the RELEASE, by convention in the
    disambiguation comment ("clean" / "explicit"); some editors put it in the
    title instead. Both are checked."""
    return edition_rating(release.get("disambiguation"), release.get("title"))


def explicit_rank(release: dict) -> int:
    """Sort rank for a release's edition: explicit first, censored last.

    A release group routinely holds a clean and an explicit cut of the same
    record, identical in title, country and format. Nothing else in the sort key
    separated them, so the choice fell to the lexicographic MBID tiebreak - which
    handed "Long Term Effects of SUFFERING" its CLEAN edition purely because
    020fc885 sorts before 4c5084cc. Ranking the edition makes that deterministic
    and right instead of alphabetical.

    An unmarked release sits between the two: it is usually the original, and
    demoting it below a known-clean cut would be worse than leaving it alone.
    """
    return _RANK_BY_RATING.get(release_edition_rating(release), EXPLICIT_RANK_UNMARKED)


def is_clean_release(release: dict) -> bool:
    """Whether this release is a censored cut, by MusicBrainz's own marking."""
    return release_edition_rating(release) == RATING_CLEAN


def parse_year(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    year = date_str.split("-", 1)[0]
    return int(year) if year.isdigit() else None


def find_primary_release(release_group: dict) -> Optional[dict]:
    ranked = get_ranked_releases(release_group)
    return ranked[0] if ranked else None


def get_ranked_releases(release_group: dict) -> list[dict]:
    """Return official releases sorted so digital/mainstream formats come first.

    A release whose own title exactly matches the release group's title is
    preferred above all else. MusicBrainz has no "this is the complete/main
    release" flag, and a release group can legitimately hold releases with
    entirely different tracklists - e.g. a "Side A"/"Side B" pair released
    separately under one EP. Ranking on format/country alone leaves those tied,
    falling back to comparing MBIDs lexicographically - arbitrary, and just as
    likely to land on the partial release as the complete one.

    The EDITION is ranked next (``explicit_rank``): a clean and an explicit cut
    of the same record agree on title, country and format, so without this the
    lexicographic tiebreak decided which one the library got. Edition outranks
    format deliberately - a censored tracklist is the wrong music, whereas a
    physical pressing is merely a less convenient source of the right music.
    """
    releases = release_group.get("releases") or release_group.get("release-list", [])
    official = [r for r in releases if r.get("status") == "Official"]
    if not official:
        official = list(releases)

    group_title = (release_group.get("title") or "").strip().casefold()

    def _release_sort_key(r: dict) -> tuple[int, int, int, str]:
        title_rank = 0 if (r.get("title") or "").strip().casefold() == group_title else 1
        country = (r.get("country") or "").upper()
        packaging = (r.get("packaging") or "").lower()
        physical_keywords = {"vinyl", "cassette", "gatefold"}
        if country == "XW":
            rank = 0
        elif any(kw in packaging for kw in physical_keywords):
            rank = 2
        else:
            rank = 1
        return (title_rank, explicit_rank(r), rank, r.get("id", ""))

    official.sort(key=_release_sort_key)
    return official


def extract_artist_info(release_group: dict) -> tuple[str, str]:
    artist_credit = release_group.get("artist-credit", [])
    artist_name = "Unknown Artist"
    artist_id = ""
    if artist_credit and isinstance(artist_credit, list):
        first_artist = artist_credit[0]
        if isinstance(first_artist, dict):
            artist_obj = first_artist.get("artist", {})
            artist_name = first_artist.get("name") or artist_obj.get(
                "name", "Unknown Artist"
            )
            artist_id = artist_obj.get("id", "")
    return artist_name, artist_id


def extract_tracks(release_data: dict) -> tuple[list[Track], int]:
    tracks = []
    total_length = 0
    medium_list = release_data.get("media") or release_data.get("medium-list", [])
    for medium in medium_list:
        try:
            disc_number = int(medium.get("position") or medium.get("number") or 1)
        except (TypeError, ValueError):
            disc_number = 1
        medium_format = medium.get("format")
        if not isinstance(medium_format, str):
            medium_format = None
        track_list = medium.get("tracks") or medium.get("track-list", [])
        for track in track_list:
            recording = track.get("recording", {})
            length_ms = track.get("length") or recording.get("length")
            if length_ms:
                try:
                    total_length += int(length_ms)
                except (ValueError, TypeError):
                    pass
            tracks.append(
                Track(
                    position=int(track.get("position") or track.get("number", 0)),
                    disc_number=disc_number,
                    title=track.get("title") or recording.get("title") or "Unknown",
                    length=int(length_ms) if length_ms else None,
                    recording_id=recording.get("id"),
                    release_track_id=track.get("id"),
                    media_format=medium_format,
                )
            )
    return tracks, total_length


# MusicBrainz medium-format vocabulary names video carriers explicitly ("DVD",
# "DVD-Video", "Blu-ray", ...); DVD-Audio discs are labeled "DVD-Audio" and stay.
# Live-verified 2026-09-06: release 0b683a6a (Tom Jones DE 2006 Greatest Hits)
# returns media formats "CD" (16 tracks) + "DVD" (14 tracks).
_VIDEO_MEDIUM_FORMATS = frozenset(
    {
        "dvd",
        "dvd-video",
        "blu-ray",
        "hd-dvd",
        "hdv",
        "vhs",
        "betamax",
        "video cd",
        "video-cd",
        "vcd",
        "svcd",
        "laserdisc",
    }
)


def is_audio_medium(medium_format: str | None) -> bool:
    """Whether a MusicBrainz medium carries downloadable audio.

    Anything unrecognized - or missing, as on local-library tracklists and old
    cache blobs - reads as audio: acquisition must fail open, never strand.
    """
    if not medium_format:
        return True
    return medium_format.strip().casefold() not in _VIDEO_MEDIUM_FORMATS


def audio_tracks(tracks: list) -> list:  # noqa: ANN001 - Track shape, duck-typed
    """The canonical audio-only acquisition target set (Slices 4+5).

    Every Soulseek/Usenet acquisition denominator - request-time
    ``track_count``, the enqueue manifest, the completion coverage gate, the
    wanted watcher's satisfaction check - filters through this one helper so
    request targets and coverage can never measure different editions (the
    CD+DVD 16-vs-30 split). Display paths (album page) keep the full list, as
    do explicit per-track requests and the Free Music backend's soft
    count-matching signal.
    """
    return [
        track
        for track in tracks
        if is_audio_medium(getattr(track, "media_format", None))
    ]


def extract_label(release_data: dict) -> Optional[str]:
    label_info_list = release_data.get("label-info") or release_data.get(
        "label-info-list", []
    )
    if label_info_list:
        label_obj = label_info_list[0].get("label")
        if label_obj:
            return label_obj.get("name")
    return None


def build_album_basic_info(
    release_group: dict,
    release_group_id: str,
    artist_name: str,
    artist_id: str,
    in_library: bool,
) -> dict:
    return {
        "title": release_group.get("title", "Unknown Album"),
        "musicbrainz_id": release_group_id,
        "artist_name": artist_name,
        "artist_id": artist_id,
        "release_date": release_group.get("first-release-date"),
        "year": parse_year(release_group.get("first-release-date")),
        "type": release_group.get("primary-type"),
        "disambiguation": release_group.get("disambiguation"),
        "tracks": [],
        "total_tracks": 0,
        "in_library": in_library,
    }


def mb_to_basic_info(
    release_group: dict, release_group_id: str, in_library: bool, is_requested: bool
) -> dict:
    artist_name, artist_id = extract_artist_info(release_group)
    return {
        "title": release_group.get("title", "Unknown Album"),
        "musicbrainz_id": release_group_id,
        "artist_name": artist_name,
        "artist_id": artist_id,
        "release_date": release_group.get("first-release-date"),
        "year": parse_year(release_group.get("first-release-date")),
        "type": release_group.get("primary-type"),
        "disambiguation": release_group.get("disambiguation"),
        "in_library": in_library,
        "requested": is_requested and not in_library,
        "cover_url": None,
    }
