"""Cross-lane equivalence oracle - EditionsEtc intent 5, PLAN.md §2 Phase 3 (bullet 2).

Pins cross-lane AGREEMENT on canonical inputs: the same folder tags and the same
two MusicBrainz candidate tracklists are resolved independently by

- Lane A: the drop-import folder lane, ``AlbumIdentifier.identify`` -> ``AlbumMatch | None``
- Lane B: the native scan lane, ``AlbumEvidenceEngine.decide`` -> ``IdentificationDecision``

and must reach the same verdict:

1. Clear winner (one candidate plainly better): BOTH lanes select the SAME
   release group + release edition.
2. Near-tie: NEITHER lane writes a silent pick - the drop lane returns None
   (routed to review by ``DropImportService._try_identify``) and the engine
   returns outcome ``ambiguous``.

This is deliberately NOT a claim of identical scoring math: the lanes weight
evidence differently (Levenshtein over a Hungarian weighted distance vs a
difflib pair-cost blend), so their numeric margins differ. Only the decision
boundary (clear winner vs near-tie) and the selected identity are pinned here,
so future drift between the lanes surfaces as a test failure.
"""

from unittest.mock import AsyncMock

import pytest

from models.identification import AlbumCandidate, CandidateTrack, GroupingTrack
from models.search import SearchResult
from services.native.album_evidence_engine import (
    CANDIDATE_MARGIN_FLOOR,
    AlbumEvidenceEngine,
)
from services.native.album_matcher import AlbumIdentifier, LocalTrack

_ARTIST = "Santana"
_ALBUM = "Santana"
_TITLES = ["Waiting", "Evil Ways", "Shades of Time", "Savor", "Jingo"]
_DURATION_S = 240.0
_RG_A, _REL_A = "rg-oracle-a", "rel-oracle-a"
_RG_B, _REL_B = "rg-oracle-b", "rel-oracle-b"

# The ONE canonical fixture set: exact edition A plus twin edition B that
# diverges in exactly one dimension per case.
# - clear_winner: B's tracks all run 270s vs the folder's 240s (outside the
#   10s grace, inside both lanes' hard ceilings) -> a real but decisive gap.
# - near_tie: B differs by one close title edit ("Savor" -> "Savour").
_CLEAR_WINNER = {"titles": _TITLES, "durations": [270.0] * len(_TITLES)}
_NEAR_TIE = {
    "titles": [_TITLES[0], _TITLES[1], _TITLES[2], "Savour", _TITLES[4]],
    "durations": [_DURATION_S] * len(_TITLES),
}


def _local_tracks():
    """Folder tags shared verbatim by both lanes."""
    return [
        LocalTrack(
            path=f"/m/{i:02d}.flac",
            title=t,
            artist=_ARTIST,
            album=_ALBUM,
            track_number=i + 1,
            duration_seconds=_DURATION_S,
        )
        for i, t in enumerate(_TITLES)
    ]


def _grouping_tracks():
    """The same folder projected for the evidence engine."""
    return [
        GroupingTrack(
            local_track_id=f"t{i}",
            root_id="root",
            relative_path=f"{_ARTIST}/{_ALBUM}/{i + 1:02}.flac",
            title=t,
            artist_name=_ARTIST,
            album_title=_ALBUM,
            album_artist_name=_ARTIST,
            track_number=i + 1,
            disc_number=1,
            duration_seconds=_DURATION_S,
        )
        for i, t in enumerate(_TITLES)
    ]


def _candidate_tracks(titles, durations):
    return [
        CandidateTrack(
            title=t,
            position=i + 1,
            absolute_position=i + 1,
            duration_seconds=d,
        )
        for i, (t, d) in enumerate(zip(titles, durations))
    ]


def _candidates(case):
    """Both canonical candidates, built from the SAME payload set as lane A."""
    return [
        AlbumCandidate(
            release_group_mbid=_RG_A,
            release_mbid=_REL_A,
            album_title=_ALBUM,
            album_artist_name=_ARTIST,
            tracks=_candidate_tracks(_TITLES, [_DURATION_S] * len(_TITLES)),
            release_type="album",
        ),
        AlbumCandidate(
            release_group_mbid=_RG_B,
            release_mbid=_REL_B,
            album_title=_ALBUM,
            album_artist_name=_ARTIST,
            tracks=_candidate_tracks(case["titles"], case["durations"]),
            release_type="album",
        ),
    ]


def _mb_repo(case):
    """Mock MB repo serving both canonical editions to the folder lane."""

    def rg_detail(mbid, includes=None, priority=None):
        rel = _REL_A if mbid == _RG_A else _REL_B
        return {
            "title": _ALBUM,
            "primary-type": "Album",
            "secondary-types": [],
            "artist-credit": [
                {"name": _ARTIST, "artist": {"id": "a1", "name": _ARTIST}}
            ],
            "releases": [
                {
                    "id": rel,
                    "status": "Official",
                    "date": "1969",
                    "media": [{"track-count": len(_TITLES)}],
                }
            ],
        }

    async def release(rel_id, includes=None, priority=None):
        titles = _TITLES if rel_id == _REL_A else case["titles"]
        durations = (
            [_DURATION_S] * len(_TITLES) if rel_id == _REL_A else case["durations"]
        )
        return {
            "date": "1969",
            "media": [
                {
                    "position": 1,
                    "tracks": [
                        {
                            "title": t,
                            "position": i + 1,
                            "length": int(d * 1000),
                            "recording": {"id": f"rec-{i + 1}", "title": t},
                        }
                        for i, (t, d) in enumerate(zip(titles, durations))
                    ],
                }
            ],
        }

    repo = AsyncMock()
    repo.search_release_groups = AsyncMock(
        return_value=[
            SearchResult(
                type="album", title=_ALBUM, musicbrainz_id=_RG_A, artist=_ARTIST
            ),
            SearchResult(
                type="album", title=_ALBUM, musicbrainz_id=_RG_B, artist=_ARTIST
            ),
        ]
    )
    repo.search_recordings = AsyncMock(return_value=[])
    repo.get_release_group_by_id = AsyncMock(side_effect=rg_detail)
    repo.get_release_by_id = AsyncMock(side_effect=release)
    return repo


@pytest.mark.asyncio
async def test_clear_winner_both_lanes_select_the_same_edition():
    case = _CLEAR_WINNER

    # Lane A: drop-import folder lane picks edition A outright.
    repo = _mb_repo(case)
    match = await AlbumIdentifier(repo).identify(_local_tracks())
    assert match is not None
    assert (match.release_group_mbid, match.release_mbid) == (_RG_A, _REL_A)
    # The twin was genuinely scored as a runner-up, not gated out beforehand.
    assert repo.get_release_by_id.await_count == 2

    # Lane B: the evidence engine agrees on the same group + edition.
    decision = AlbumEvidenceEngine().decide(_grouping_tracks(), _candidates(case))
    assert decision.outcome == "identified"
    assert decision.selected_candidate_key == f"{_RG_A}:{_REL_A}"
    # Both candidates were eligible (SUPPORTED): a real race with a clear margin.
    assert {c.reason_code for c in decision.candidates} == {"SUPPORTED"}
    best = max(decision.candidates, key=lambda c: c.score)
    assert best.margin >= CANDIDATE_MARGIN_FLOOR


@pytest.mark.asyncio
async def test_near_tie_sends_both_lanes_to_review_instead_of_a_silent_pick():
    case = _NEAR_TIE

    # Lane A: drop-import folder lane refuses to guess -> needs_review.
    repo = _mb_repo(case)
    match = await AlbumIdentifier(repo).identify(_local_tracks())
    assert match is None
    assert repo.get_release_by_id.await_count == 2  # both twins were scored

    # Lane B: the evidence engine likewise declines -> ambiguous.
    decision = AlbumEvidenceEngine().decide(_grouping_tracks(), _candidates(case))
    assert decision.outcome == "ambiguous"
    assert decision.selected_candidate_key is None
    # Ambiguity is margin-driven, not a gate rejection on either side.
    assert {c.reason_code for c in decision.candidates} == {"SUPPORTED"}
    scores = {c.release_group_mbid: c.score for c in decision.candidates}
    assert abs(scores[_RG_A] - scores[_RG_B]) < CANDIDATE_MARGIN_FLOOR
