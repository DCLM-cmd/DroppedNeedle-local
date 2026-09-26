"""Bounded, priority-honest candidate recall for target identification."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Collection
from infrastructure.queue.priority_queue import RequestPriority
from models.identification import AlbumCandidate, GroupingTrack
from repositories.protocols.identification import IdentificationProviderProtocol
from services.native.album_evidence_engine import MAX_CANDIDATES

logger = logging.getLogger(__name__)

ALBUM_SEARCH_LIMIT = 8
RECORDING_SEARCH_LIMIT = 5
TRACK_SAMPLE_LIMIT = 4


RECALL_SOURCE_KINDS = frozenset(
    {"cached_fingerprint", "embedded", "album_tags", "recording_search"}
)


def _consensus(values: list[str]) -> str:
    usable = [value.strip() for value in values if value.strip()]
    return Counter(usable).most_common(1)[0][0] if usable else ""


# M-06: `placeholder`/`absent` claims abstain from recall - only present
# (`tag`/`parsed`) claims drive album/artist consensus and recording-search
# sampling, so newly-`insufficient_evidence` groups never burn
# sibling-trial fetches on stems and `"Unknown Artist"` garbage.
_PRESENT_PROVENANCE = ("tag", "parsed")


class AlbumCandidateService:
    def __init__(self, provider: IdentificationProviderProtocol) -> None:
        self._provider = provider

    async def recall(
        self,
        tracks: list[GroupingTrack],
        *,
        cached_fingerprint_release_groups: list[str] | None = None,
        exact_release_mbid: str | None = None,
        explicit: bool = False,
        checkpoint: Callable[[], Awaitable[bool]] | None = None,
        sibling_release_group_ids: Collection[str] | None = None,
    ) -> list[AlbumCandidate]:
        priority = (
            RequestPriority.USER_INITIATED
            if explicit
            else RequestPriority.BACKGROUND_SYNC
        )
        if exact_release_mbid is not None:
            if checkpoint is not None and not await checkpoint():
                return []
            exact = await self._provider.get_exact_release_candidate(
                exact_release_mbid, priority
            )
            if exact is None:
                return []
            exact.source_kinds = ["administrator_exact_release"]
            return [exact]

        # Folded: tag MBIDs keep their verbatim case, so mixed-case
        # unanimous tags must still count as unanimous (both display lanes
        # casefold too). Lookups below use the original strings.
        embedded_groups = {
            track.release_group_mbid.casefold()
            for track in tracks
            if track.release_group_mbid
        }
        embedded_releases = [track.release_mbid for track in tracks]
        present_releases = [value for value in embedded_releases if value]
        if present_releases:
            # Blanks abstain (matching the release-group seed below): only
            # genuine disagreement between populated tags refuses the lookup.
            if len({str(value).casefold() for value in present_releases}) != 1:
                return []
            if checkpoint is not None and not await checkpoint():
                return []
            exact = await self._provider.get_exact_release_candidate(
                str(present_releases[0]), priority
            )
            if exact is not None:
                exact.source_kinds = ["embedded_exact_release"]
                return [exact]
            # Stale embedded tags (merged/deleted releases) must not orphan
            # the album: fall through to full recall instead of returning no
            # candidates. The embedded release-group seed below still applies,
            # and sealing still needs proof. NOTE: the explicit
            # administrator_exact_release branch above keeps returning [] on a
            # miss - an admin demanding one release must never silently get
            # another.
            logger.debug(
                "recall: agreed embedded release %s unfetchable; "
                "falling through to full recall",
                str(present_releases[0]),
            )
        # F-MATCH-02 (owner-signed): non-exact recall orders deduplicated
        # cached-fingerprint seeds first (audio truth, mirroring
        # ``AlbumIdentifier._candidate_release_groups``), then the single
        # embedded release-group seed, then album-tag and recording-search IDs.
        ids: list[tuple[str, str]] = [
            (release_group_id, "cached_fingerprint")
            for release_group_id in dict.fromkeys(
                value for value in (cached_fingerprint_release_groups or []) if value
            )
        ]
        if len(embedded_groups) == 1:
            ids.append((next(iter(embedded_groups)), "embedded"))

        album = _consensus(
            [
                track.album_title
                for track in tracks
                if track.album_title_provenance in _PRESENT_PROVENANCE
            ]
        )
        artist = _consensus(
            [
                track.album_artist_name
                for track in tracks
                if track.album_artist_provenance in _PRESENT_PROVENANCE
            ]
        )
        if album and artist:
            if checkpoint is not None and not await checkpoint():
                return []
            for release_group_id in await self._provider.search_album_candidate_ids(
                artist, album, ALBUM_SEARCH_LIMIT, priority
            ):
                ids.append((release_group_id, "album_tags"))
            if checkpoint is not None and not await checkpoint():
                return []

        # The recording-search fallback triggers on sparse TEXT evidence, as it
        # did before F-MATCH-02 reordered the list: fingerprint seeds satisfy
        # the ordering contract without changing when recordings are searched.
        if (
            not album
            or not artist
            or len({identifier for identifier, source in ids if source != "cached_fingerprint"}) < 2
        ):
            # P1-C(c): shrink the recording-search sample to 2 only when album
            # and artist are present with >=2 distinct non-fingerprint ids.
            # Sparse (album/artist empty) and compilation-shaped recall keep the
            # full sample since the recording lane is the only VA lane.
            _distinct_non_fp = len({identifier for identifier, source in ids if source != "cached_fingerprint"})
            _sample_limit = 2 if (album and artist and _distinct_non_fp >= 2) else TRACK_SAMPLE_LIMIT
            samples = sorted(
                (
                    track
                    for track in tracks
                    if track.title
                    and track.title_provenance in _PRESENT_PROVENANCE
                ),
                key=lambda track: (
                    track.disc_number,
                    track.track_number,
                    track.local_track_id,
                ),
            )[:_sample_limit]
            for track in samples:
                if checkpoint is not None and not await checkpoint():
                    return []
                for (
                    release_group_id
                ) in await self._provider.search_recording_candidate_ids(
                    artist or track.artist_name,
                    track.title,
                    RECORDING_SEARCH_LIMIT,
                    priority,
                ):
                    ids.append((release_group_id, "recording_search"))
                if checkpoint is not None and not await checkpoint():
                    return []
        ordered: list[str] = []
        sources: dict[str, list[str]] = {}
        for release_group_id, source in ids:
            if release_group_id not in sources:
                ordered.append(release_group_id)
                sources[release_group_id] = []
            if source not in sources[release_group_id]:
                sources[release_group_id].append(source)
        candidates: list[AlbumCandidate] = []
        canonical_candidates: dict[tuple[str, str], AlbumCandidate] = {}
        sibling_ids = set(sibling_release_group_ids or ())
        for release_group_id in ordered[:MAX_CANDIDATES]:
            if checkpoint is not None and not await checkpoint():
                return []
            if release_group_id in sibling_ids:
                # EditionsEtc Phase 2 within-group sibling trial: qualifying
                # groups fetch their ranked top pick plus one sibling edition
                # in a single call (owner-approved <= 1 extra full-release
                # fetch per group). Without sibling ids this branch is dead
                # and recall behaves exactly as before.
                fetched = await self._provider.get_album_candidate_editions(
                    release_group_id, len(tracks), priority
                )
            else:
                top_pick = await self._provider.get_album_candidate(
                    release_group_id, len(tracks), priority
                )
                fetched = [] if top_pick is None else [top_pick]
            for candidate in fetched:
                canonical_key = (
                    candidate.release_group_mbid,
                    (candidate.release_mbid or "").casefold(),
                )
                existing = canonical_candidates.get(canonical_key)
                if existing is not None:
                    existing.source_kinds = list(
                        dict.fromkeys(
                            [*existing.source_kinds, *sources[release_group_id]]
                        )
                    )
                    continue
                if len(candidates) < MAX_CANDIDATES:
                    candidate.source_kinds = sources[release_group_id]
                    canonical_candidates[canonical_key] = candidate
                    candidates.append(candidate)
                if checkpoint is not None and not await checkpoint():
                    return []
            if len(candidates) >= MAX_CANDIDATES:
                break
        return candidates
