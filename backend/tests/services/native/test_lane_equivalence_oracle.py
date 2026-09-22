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

Phase 0 (LibraryFindings-All X-04 step 0.1, M-04 pre-work) extends the two-case
corpus below with a shared golden corpus (``_CORPUS_CASES``): every layout
projects the identical folder tags to ``LocalTrack`` (lane A) and
``GroupingTrack`` (lane B) rows, serves the identical two-edition universe to
both lanes, and asserts the same release group + the same accept/refuse
boundary. Locks (plain tests) pin layouts where the lanes already agree;
divergent-boundary cases carry a strict xfail naming the converging step
(2.3a when lane B must move to the shared core, 2.3b when lane A must move).

Both stubs mirror production emptiness semantics so agreement is never a mock
artifact: lane A's ``search_recordings`` serves hits only for non-empty
artist+title (``musicbrainz_album.py:search_recordings`` returns ``[]``
otherwise), lane B's provider answers album/recording searches only for
non-empty queries, and detail fetches serve the same payloads both lanes
rank. Seed tag values below mirror the committed real-audio fixtures in
``tests/fixtures/library/`` (see ``test_corpus_seed_values_match_library_fixtures``).
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from models.identification import AlbumCandidate, CandidateTrack, GroupingTrack
from models.search import SearchResult
from repositories.musicbrainz_album import RecordingMatch, RecordingReleaseGroup
from services.native.album_candidate_service import AlbumCandidateService
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
            title_provenance="tag",
            album_title_provenance="tag",
            album_artist_provenance="tag",
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


# ---------------------------------------------------------------------------
# Phase-0 shared golden corpus (LibraryFindings-All X-04 step 0.1, M-04 pre).
# ---------------------------------------------------------------------------

_LIBRARY_FIXTURES = Path(__file__).parents[2] / "fixtures" / "library"

# Seed tag values mirrored from the committed real-audio fixtures; the
# grounding test below pins them to the files so fixture drift breaks loudly.
_SEED_OK_COMPUTER = {
    "file": "flac_full_01.flac",
    "artist": "Radiohead",
    "album": "OK Computer",
}
_SEED_COMPILATION = {
    "file": "flac_compilation_01.flac",
    "artist": "Various Artists",
    "album": "Now Thats Music",
    "albumartist": "Various Artists",
}
_SEED_CJK = {"file": "flac_cjk_01.flac", "artist": "ユキ", "album": "望厚"}
_SEED_MEZZANINE = {
    "file": "m4a_full_01.m4a",
    "artist": "Massive Attack",
    "album": "Mezzanine",
}
_SEED_UNTAGGED = {"file": "flac_no_tags.flac", "artist": "", "album": ""}

_OK_TITLES = ["Airbag", "Paranoid Android", "Subterranean Homesick Alien"]
_OK_DURATION_S = 200.0


def _flac_tags(name: str) -> dict[str, str]:
    from mutagen.flac import FLAC

    audio = FLAC(str(_LIBRARY_FIXTURES / name))
    return {
        key: (values[0] if values else "")
        for key, values in audio.items()
        for key in [key.lower()]
    }


def _m4a_tags(name: str) -> dict[str, str]:
    from mutagen.mp4 import MP4

    audio = MP4(str(_LIBRARY_FIXTURES / name))
    raw = audio.tags or {}
    first = lambda values: values[0] if values else ""  # noqa: E731
    return {
        "artist": str(first(raw.get("©ART", []))),
        "album": str(first(raw.get("©alb", []))),
    }


def test_corpus_seed_values_match_library_fixtures() -> None:
    """The corpus layouts project the real fixture tags - drift breaks here."""
    for seed in (_SEED_OK_COMPUTER, _SEED_CJK):
        tags = _flac_tags(seed["file"])
        assert tags.get("artist", "") == seed["artist"], seed["file"]
        assert tags.get("album", "") == seed["album"], seed["file"]
    compilation_tags = _flac_tags(_SEED_COMPILATION["file"])
    # The file's per-track ARTIST is a contributor; the corpus projects the
    # album-level ALBUMARTIST both lanes group and score on.
    assert compilation_tags.get("albumartist", "") == (_SEED_COMPILATION["albumartist"])
    assert compilation_tags.get("album", "") == _SEED_COMPILATION["album"]
    assert (
        _flac_tags("flac_full_02.flac").get("album", "") == (_SEED_OK_COMPUTER["album"])
    )
    assert _flac_tags(_SEED_UNTAGGED["file"]).get("title", "") == ""
    assert _flac_tags(_SEED_UNTAGGED["file"]).get("album", "") == ""
    assert _m4a_tags(_SEED_MEZZANINE["file"]) == {
        "artist": _SEED_MEZZANINE["artist"],
        "album": _SEED_MEZZANINE["album"],
    }


class _CorpusCase:
    """One layout: identical folder tags projected to both lane inputs."""

    def __init__(
        self,
        *,
        titles: list[str],
        artist: str,
        album: str,
        durations: list[float] | None = None,
        compilation: bool = False,
        disc_numbers: list[int] | None = None,
        relative_paths: list[str] | None = None,
        album_artist_override: dict[int, str] | None = None,
        twin_titles: list[str] | None = None,
        twin_durations: list[float] | None = None,
        twin_album: str | None = None,
        twin_artist: str | None = None,
        release_type: str = "album",
    ) -> None:
        self.titles = titles
        self.artist = artist
        self.album = album
        self.durations = durations or [_OK_DURATION_S] * len(titles)
        self.compilation = compilation
        self.disc_numbers = disc_numbers or [1] * len(titles)
        self.relative_paths = relative_paths or [
            f"{artist}/{album}/{index + 1:02}.flac" for index in range(len(titles))
        ]
        self.album_artist_override = album_artist_override or {}
        self.twin_titles = twin_titles if twin_titles is not None else titles
        self.twin_durations = twin_durations or [_OK_DURATION_S] * len(self.twin_titles)
        self.twin_album = twin_album if twin_album is not None else album
        self.twin_artist = twin_artist if twin_artist is not None else artist
        self.release_type = release_type

    def local_tracks(self) -> list[LocalTrack]:
        return [
            LocalTrack(
                path=f"/corpus/{index:02d}.flac",
                title=title,
                artist=self.artist,
                album=self.album,
                track_number=index + 1,
                disc_number=self.disc_numbers[index],
                duration_seconds=self.durations[index],
            )
            for index, title in enumerate(self.titles)
        ]

    def grouping_tracks(self) -> list[GroupingTrack]:
        return [
            GroupingTrack(
                local_track_id=f"c{index}",
                root_id="root",
                relative_path=self.relative_paths[index],
                title=title,
                artist_name=self.artist,
                album_title=self.album,
                album_artist_name=self.album_artist_override.get(index, self.artist),
                # Corpus cases predate provenance: face-value (tag-strength)
                # scoring is the baseline the 0.1 locks were calibrated on.
                title_provenance="tag",
                album_title_provenance="tag",
                album_artist_provenance="tag",
                track_number=index + 1,
                disc_number=self.disc_numbers[index],
                duration_seconds=self.durations[index],
                is_compilation=self.compilation,
            )
            for index, title in enumerate(self.titles)
        ]

    def candidates(self) -> list[AlbumCandidate]:
        def _tracks(titles: list[str], durations: list[float]) -> list[CandidateTrack]:
            return [
                CandidateTrack(
                    title=title,
                    position=index + 1,
                    absolute_position=index + 1,
                    duration_seconds=durations[index],
                )
                for index, title in enumerate(titles)
            ]

        return [
            AlbumCandidate(
                release_group_mbid=_RG_A,
                release_mbid=_REL_A,
                album_title=self.album,
                album_artist_name=self.artist,
                tracks=_tracks(self.titles, self.durations),
                release_type=self.release_type,
            ),
            AlbumCandidate(
                release_group_mbid=_RG_B,
                release_mbid=_REL_B,
                album_title=self.twin_album,
                album_artist_name=self.twin_artist,
                tracks=_tracks(self.twin_titles, self.twin_durations),
                release_type=self.release_type,
            ),
        ]


def _corpus_mb_repo(case: _CorpusCase) -> AsyncMock:
    """Lane-A metadata mock serving the case's two-edition universe.

    Recording hits are served only for non-empty artist+title, mirroring
    ``musicbrainz_album.py:search_recordings`` (which returns ``[]``
    otherwise); album hits only for non-empty artist+album, mirroring lane
    A's own ``if artist and album`` guard in ``_candidate_release_groups``.
    """

    def rg_detail(mbid: str, includes=None, priority=None) -> dict:
        is_a = mbid == _RG_A
        return {
            "title": case.album if is_a else case.twin_album,
            "primary-type": "Album",
            "secondary-types": [],
            "artist-credit": [
                {
                    "name": case.artist if is_a else case.twin_artist,
                    "artist": {
                        "id": "a1",
                        "name": case.artist if is_a else case.twin_artist,
                    },
                }
            ],
            "releases": [
                {
                    "id": _REL_A if is_a else _REL_B,
                    "status": "Official",
                    "date": "1997",
                    "media": [{"track-count": len(case.titles)}],
                }
            ],
        }

    async def release(rel_id: str, includes=None, priority=None) -> dict:
        is_a = rel_id == _REL_A
        titles = case.titles if is_a else case.twin_titles
        durations = case.durations if is_a else case.twin_durations
        return {
            "date": "1997",
            "media": [
                {
                    "position": 1,
                    "tracks": [
                        {
                            "title": title,
                            "position": index + 1,
                            "length": int(duration * 1000),
                            "recording": {
                                "id": f"rec-{index + 1}",
                                "title": title,
                            },
                        }
                        for index, (title, duration) in enumerate(
                            zip(titles, durations)
                        )
                    ],
                }
            ],
        }

    def recording_hits(artist: str, title: str) -> list[RecordingMatch]:
        if not artist.strip() or not title.strip():
            return []
        return [
            RecordingMatch(
                recording_mbid=f"rec-corpus-{index}",
                title=title,
                artist=artist,
                score=100,
                release_groups=[
                    RecordingReleaseGroup(
                        release_group_mbid=rg,
                        release_group_title=case.album,
                        release_mbid=None,
                        primary_type="Album",
                        secondary_types=(),
                    )
                    for rg in (_RG_A, _RG_B)
                ],
            )
            for index in range(2)
        ]

    repo = AsyncMock()
    repo.search_release_groups = AsyncMock(
        side_effect=lambda artist, title, **kwargs: (
            [
                SearchResult(
                    type="album",
                    title=case.album,
                    musicbrainz_id=_RG_A,
                    artist=case.artist,
                ),
                SearchResult(
                    type="album",
                    title=case.twin_album,
                    musicbrainz_id=_RG_B,
                    artist=case.twin_artist,
                ),
            ]
            if artist.strip() and title.strip()
            else []
        )
    )
    repo.search_recordings = AsyncMock(
        side_effect=lambda artist, title, **kwargs: recording_hits(artist, title)
    )
    repo.get_release_group_by_id = AsyncMock(side_effect=rg_detail)
    repo.get_release_by_id = AsyncMock(side_effect=release)
    return repo


class _CorpusProvider:
    """Lane-B recall provider serving the case's two candidates.

    Empty-query gating mirrors production: album search answers only for
    non-empty artist+title (recall only calls it then), recording search
    only for non-empty artist+title (the underlying ``search_recordings``
    returns ``[]`` otherwise).
    """

    def __init__(self, case: _CorpusCase) -> None:
        self._candidates = case.candidates()

    async def search_album_candidate_ids(
        self, artist: str, title: str, limit: int, priority
    ) -> list[str]:
        if not artist.strip() or not title.strip():
            return []
        return [candidate.release_group_mbid for candidate in self._candidates[:limit]]

    async def search_recording_candidate_ids(
        self, artist: str, title: str, limit: int, priority
    ) -> list[str]:
        if not artist.strip() or not title.strip():
            return []
        return [candidate.release_group_mbid for candidate in self._candidates[:limit]]

    async def get_album_candidate(
        self, release_group_mbid: str, target_track_count: int, priority
    ):
        return next(
            (
                candidate
                for candidate in self._candidates
                if candidate.release_group_mbid == release_group_mbid
            ),
            None,
        )

    async def get_album_candidate_editions(
        self,
        release_group_mbid: str,
        target_track_count: int,
        priority,
        **kwargs,
    ):
        candidate = await self.get_album_candidate(
            release_group_mbid, target_track_count, priority
        )
        return [] if candidate is None else [candidate]

    async def get_exact_release_candidate(self, release_mbid: str, priority):
        return None


_FAR_DURATIONS = [_OK_DURATION_S + 30.0] * len(_OK_TITLES)


def _well_tagged_album(**overrides) -> _CorpusCase:
    kwargs: dict = {
        "titles": list(_OK_TITLES),
        "artist": _SEED_OK_COMPUTER["artist"],
        "album": _SEED_OK_COMPUTER["album"],
        "twin_durations": list(_FAR_DURATIONS),
    }
    kwargs.update(overrides)
    return _CorpusCase(**kwargs)


async def _run_both_lanes(case: _CorpusCase, *, expect_universe: bool = True):
    """Run the identical projection through lane A and lane B (recall +
    evaluate + decide); return the raw verdicts for boundary comparison."""
    match = await AlbumIdentifier(_corpus_mb_repo(case)).identify(case.local_tracks())
    grouping = case.grouping_tracks()
    recalled = await AlbumCandidateService(_CorpusProvider(case)).recall(grouping)
    # The oracle only compares boundaries over a shared universe: both lanes
    # must rank the same two editions. Cases with no evidence anywhere
    # (untagged dir) recall nothing on either lane by construction.
    if expect_universe:
        assert {candidate.release_group_mbid for candidate in recalled} == {
            _RG_A,
            _RG_B,
        }
    else:
        assert recalled == []
    decision = AlbumEvidenceEngine().decide(grouping, recalled)
    return match, decision


def _assert_same_boundary(match, decision) -> None:
    """Same release group + same accept/refuse boundary - never margins."""
    lane_a_accepts = match is not None
    lane_b_accepts = decision.outcome == "identified"
    assert lane_a_accepts == lane_b_accepts
    if lane_a_accepts:
        assert match.release_group_mbid == _RG_A
        assert decision.selected_candidate_key == f"{_RG_A}:{_REL_A}"
    else:
        assert decision.selected_candidate_key is None


@pytest.mark.asyncio
async def test_corpus_well_tagged_album_clear_winner_both_accept_a() -> None:
    match, decision = await _run_both_lanes(_well_tagged_album())
    assert match is not None
    _assert_same_boundary(match, decision)


@pytest.mark.asyncio
async def test_corpus_well_tagged_album_near_tie_both_refuse() -> None:
    near = list(_OK_TITLES)
    near[2] = "Subterranean Homesick Alen"
    case = _well_tagged_album(
        twin_titles=near, twin_durations=[_OK_DURATION_S] * len(_OK_TITLES)
    )
    match, decision = await _run_both_lanes(case)
    assert match is None
    _assert_same_boundary(match, decision)
    assert decision.outcome == "ambiguous"


@pytest.mark.asyncio
async def test_corpus_untagged_but_organized_dir_both_refuse() -> None:
    case = _CorpusCase(
        titles=[""] * 3,
        artist="",
        album="",
        relative_paths=[
            f"Radiohead/OK Computer/{index + 1:02}.flac" for index in range(3)
        ],
    )
    match, decision = await _run_both_lanes(case, expect_universe=False)
    # No tag or recording evidence anywhere: lane A scores nothing, lane B
    # recalls nothing - agreement by emptiness, pre-M-01 filename parsing.
    assert match is None
    assert decision.outcome == "no_candidate"
    _assert_same_boundary(match, decision)


@pytest.mark.asyncio
async def test_corpus_compilation_clear_winner_both_accept_a() -> None:
    case = _CorpusCase(
        titles=["Song A", "Song B", "Song C"],
        artist=_SEED_COMPILATION["artist"],
        album=_SEED_COMPILATION["album"],
        compilation=True,
        twin_durations=[_OK_DURATION_S + 30.0] * 3,
    )
    match, decision = await _run_both_lanes(case)
    assert match is not None
    _assert_same_boundary(match, decision)


@pytest.mark.asyncio
async def test_corpus_soundtrack_tagged_album_clear_winner_both_accept_a() -> None:
    titles = ["Main Title", "Chase", "Love Theme"]
    case = _CorpusCase(
        titles=titles,
        artist="Various Artists",
        album="Film OST",
        release_type="soundtrack",
        twin_durations=[_OK_DURATION_S + 30.0] * 3,
    )
    match, decision = await _run_both_lanes(case)
    assert match is not None
    _assert_same_boundary(match, decision)


@pytest.mark.asyncio
async def test_corpus_multi_disc_cd1_and_disc02_both_accept_a() -> None:
    case = _well_tagged_album(
        disc_numbers=[1, 1, 2],
        relative_paths=[
            "Radiohead/OK Computer/CD1/01.flac",
            "Radiohead/OK Computer/CD1/02.flac",
            "Radiohead/OK Computer/Disc-02/01.flac",
        ],
    )
    match, decision = await _run_both_lanes(case)
    assert match is not None
    _assert_same_boundary(match, decision)


@pytest.mark.asyncio
async def test_corpus_mixed_albumartist_dir_clear_winner_both_accept_a() -> None:
    case = _well_tagged_album(album_artist_override={2: "Guest Artist"})
    match, decision = await _run_both_lanes(case)
    assert match is not None
    _assert_same_boundary(match, decision)


@pytest.mark.asyncio
async def test_corpus_cjk_tags_clear_winner_both_accept_a() -> None:
    titles = ["桃源へ", "桜舞", "風音"]
    case = _CorpusCase(
        titles=titles,
        artist=_SEED_CJK["artist"],
        album=_SEED_CJK["album"],
        twin_durations=[_OK_DURATION_S + 30.0] * 3,
    )
    match, decision = await _run_both_lanes(case)
    assert match is not None
    _assert_same_boundary(match, decision)


@pytest.mark.asyncio
async def test_corpus_m4a_tags_clear_winner_both_accept_a() -> None:
    titles = ["Teardrop", "Angel", "Inertia Creeps"]
    case = _CorpusCase(
        titles=titles,
        artist=_SEED_MEZZANINE["artist"],
        album=_SEED_MEZZANINE["album"],
        twin_durations=[_OK_DURATION_S + 30.0] * 3,
    )
    match, decision = await _run_both_lanes(case)
    assert match is not None
    _assert_same_boundary(match, decision)


@pytest.mark.asyncio
async def test_corpus_duration_twin_inside_lane_a_margin_only() -> None:
    """Converged boundary (2.3b): the twin sits 11s/track off - lane A's
    shared margin now refuses alongside lane B's distance margin, so both
    lanes route the near-tie to review instead of a silent pick."""
    case = _well_tagged_album(twin_durations=[_OK_DURATION_S + 11.0] * len(_OK_TITLES))
    match, decision = await _run_both_lanes(case)
    _assert_same_boundary(match, decision)


@pytest.mark.asyncio
async def test_corpus_title_twin_inside_lane_b_margin_only() -> None:
    """Divergent boundary (five-track shape - the three-track corpus cannot
    separate these margins): two close title edits on the twin - lane A
    still accepts the exact edition A outright while lane B's score margin
    calls it ambiguous. Lane B must move to the shared core (2.3a)."""
    twin = ["Savour", "Evil Wayz", "Shades of Time", "Savor", "Jingo"]
    case = _CorpusCase(
        titles=list(_TITLES),
        artist=_ARTIST,
        album=_ALBUM,
        durations=[_DURATION_S] * len(_TITLES),
        twin_titles=twin,
        twin_durations=[_DURATION_S] * len(_TITLES),
    )
    match, decision = await _run_both_lanes(case)
    _assert_same_boundary(match, decision)
