import json
from pathlib import Path

import pytest

from models.identification import (
    AlbumCandidate,
    CandidateTrack,
    ExistingAlbumMembership,
    GroupingTrack,
    ProposedLocalAlbum,
)
from services.native.album_evidence_engine import (
    CANDIDATE_MARGIN_FLOOR,
    LARGE_UNKNOWN_LIMIT,
    MATCHER_VERSION,
    ORDINARY_UNKNOWN_LIMIT,
    AlbumEvidenceEngine,
)
from services.native.local_album_grouper import (
    LocalAlbumGrouper,
    assign_album_continuity,
)

FIXTURE = (
    Path(__file__).parents[2] / "fixtures" / "feedback_fixes" / "grouping_golden.json"
)


def _track(
    track_id: str,
    title: str,
    *,
    number: int = 1,
    disc: int = 1,
    duration: float | None = 180,
    recording: str | None = None,
    release_track: str | None = None,
    album: str = "Album",
    artist: str = "Artist",
    compilation: bool = False,
    readable: bool = True,
) -> GroupingTrack:
    return GroupingTrack(
        local_track_id=track_id,
        root_id="root",
        relative_path=f"Artist/Album/{number:02}.flac",
        title=title,
        artist_name=artist,
        album_title=album,
        album_artist_name=artist,
        track_number=number,
        disc_number=disc,
        duration_seconds=duration,
        recording_mbid=recording,
        release_track_mbid=release_track,
        is_compilation=compilation,
        tags_readable=readable,
    )


def _candidate(
    group: str,
    tracks: list[CandidateTrack],
    *,
    title: str = "Album",
    artist: str = "Artist",
    secondary: list[str] | None = None,
) -> AlbumCandidate:
    return AlbumCandidate(
        release_group_mbid=group,
        release_mbid=f"release-{group}",
        album_title=title,
        album_artist_name=artist,
        tracks=tracks,
        release_type="album",
        secondary_types=secondary or [],
    )


def _candidate_track(
    title: str,
    number: int,
    *,
    duration: float | None = 180,
    recording: str | None = None,
    release_track: str | None = None,
) -> CandidateTrack:
    return CandidateTrack(
        title=title,
        position=number,
        absolute_position=number,
        duration_seconds=duration,
        recording_mbid=recording,
        release_track_mbid=release_track,
    )


def test_release_track_match_cannot_hide_recording_conflict() -> None:
    evidence = AlbumEvidenceEngine().evaluate_candidate(
        [_track("one", "One", recording="recording-a", release_track="track-a")],
        _candidate(
            "group",
            [
                _candidate_track(
                    "One", 1, recording="recording-b", release_track="track-a"
                )
            ],
        ),
    )

    assert evidence.reason_code == "CONFLICTING_TRACK_EVIDENCE"
    assert evidence.track_evidence[0].classification == "contradictory"
    assert evidence.track_evidence[0].evidence_kinds == ["recording_mbid_conflict"]


def test_recording_match_cannot_hide_release_track_conflict() -> None:
    evidence = AlbumEvidenceEngine().evaluate_candidate(
        [_track("one", "One", recording="recording-a", release_track="track-a")],
        _candidate(
            "group",
            [
                _candidate_track(
                    "One", 1, recording="recording-a", release_track="track-b"
                )
            ],
        ),
    )

    assert evidence.reason_code == "CONFLICTING_TRACK_EVIDENCE"
    assert evidence.track_evidence[0].classification == "contradictory"
    assert evidence.track_evidence[0].evidence_kinds == ["release_track_mbid_conflict"]


def test_committed_grouping_golden_corpus() -> None:
    cases = json.loads(FIXTURE.read_text())
    grouper = LocalAlbumGrouper()
    for case in cases:
        tracks = [
            GroupingTrack(
                local_track_id=item["id"],
                root_id="root",
                relative_path=item["path"],
                title=item.get("id", ""),
                artist_name=item.get("artist", item.get("album_artist", "")),
                album_title=item.get("album", ""),
                album_artist_name=item.get("album_artist", ""),
                track_number=item.get("number", 0),
                disc_number=item.get("disc", 1),
                is_compilation=item.get("compilation", False),
                tags_readable=item.get("readable", True),
            )
            for item in case["tracks"]
        ]
        actual = sorted(sorted(group.track_ids) for group in grouper.group(tracks))
        assert actual == sorted(case["groups"]), case["name"]


def test_manual_membership_is_restored_before_automatic_grouping() -> None:
    tracks = [
        _track("one", "One"),
        _track("two", "Two"),
    ]
    tracks[0].membership_locked = True
    tracks[0].current_album_id = "manual-a"
    tracks[1].membership_locked = True
    tracks[1].current_album_id = "manual-b"
    groups = LocalAlbumGrouper().group(tracks)
    assert sorted(group.track_ids for group in groups) == [["one"], ["two"]]
    assert {group.reason_code for group in groups} == {"MANUAL_MEMBERSHIP_RESTORED"}


def test_continuity_is_maximum_overlap_one_to_one_for_split_merge_and_ties() -> None:
    existing = [
        ExistingAlbumMembership("old-a", ["1", "2", "3"], created_at=1),
        ExistingAlbumMembership("old-b", ["4", "5"], created_at=2),
    ]
    proposed = [
        ProposedLocalAlbum("new-a", "A", "Artist", ["1", "2", "4"], "test"),
        ProposedLocalAlbum("new-b", "B", "Artist", ["3", "5"], "test"),
        ProposedLocalAlbum("new-c", "C", "Artist", ["6"], "test"),
    ]
    result = {
        item.grouping_key: item for item in assign_album_continuity(existing, proposed)
    }
    assert result["new-a"].retained_album_id == "old-a"
    assert result["new-b"].retained_album_id == "old-b"
    assert result["new-c"].retained_album_id is None

    tie = assign_album_continuity(
        [
            ExistingAlbumMembership("old-a", ["1", "2"], created_at=1),
            ExistingAlbumMembership("old-b", ["1", "2"], created_at=2),
        ],
        [
            ProposedLocalAlbum("a", "A", "Artist", ["1"], "test"),
            ProposedLocalAlbum("b", "B", "Artist", ["2"], "test"),
        ],
    )
    assert [(item.grouping_key, item.retained_album_id) for item in tie] == [
        ("a", "old-a"),
        ("b", "old-b"),
    ]
    assert all(item.continuity_reason_code == "CONTINUITY_TIE_BROKEN" for item in tie)


def test_continuity_handles_ten_thousand_disjoint_flat_groups_sparsely() -> None:
    existing = [
        ExistingAlbumMembership(
            local_album_id=f"old-{index}",
            track_ids=[f"track-{index}"],
            created_at=float(index),
        )
        for index in range(10_000)
    ]
    proposed = [
        ProposedLocalAlbum(
            grouping_key=f"group-{index:05d}",
            title=f"Album {index}",
            album_artist_name="Artist",
            track_ids=[f"track-{index}"],
            reason_code="AMBIGUOUS_FALLBACK_GROUP",
        )
        for index in range(10_000)
    ]

    result = assign_album_continuity(existing, proposed)

    assert len(result) == 10_000
    assert all(group.retained_album_id is not None for group in result)
    assert {group.retained_album_id for group in result} == {
        album.local_album_id for album in existing
    }


def test_renamed_path_with_zero_overlap_retains_no_album_id() -> None:
    [result] = assign_album_continuity(
        [ExistingAlbumMembership("old", ["old-track"], created_at=1)],
        [ProposedLocalAlbum("renamed", "Album", "Artist", ["new-track"], "test")],
    )
    assert result.retained_album_id is None


def test_zero_support_and_forced_bad_assignments_are_never_accepted() -> None:
    engine = AlbumEvidenceEngine()
    local = [_track("one", "Completely Different", duration=400)]
    candidate = _candidate("rg", [_candidate_track("Target", 1, duration=100)])
    decision = engine.decide(local, [candidate])
    assert decision.outcome in ("contradictory", "insufficient_evidence")
    assert decision.selected_candidate_key is None
    assert decision.candidates[0].track_evidence[0].classification == "contradictory"


def test_recording_id_conflict_blocks_even_perfect_fuzzy_metadata() -> None:
    decision = AlbumEvidenceEngine().decide(
        [_track("one", "Same", recording="local-recording")],
        [_candidate("rg", [_candidate_track("Same", 1, recording="other-recording")])],
    )
    assert decision.outcome == "contradictory"
    assert decision.reason_code == "CONFLICTING_TRACK_EVIDENCE"


@pytest.mark.parametrize(
    ("file_count", "unknown_count", "expected"),
    [
        (10, ORDINARY_UNKNOWN_LIMIT, "identified"),
        (10, ORDINARY_UNKNOWN_LIMIT + 1, "insufficient_evidence"),
        (21, LARGE_UNKNOWN_LIMIT, "identified"),
        (21, LARGE_UNKNOWN_LIMIT + 1, "insufficient_evidence"),
    ],
)
def test_unknown_extra_caps_are_exact(
    file_count: int, unknown_count: int, expected: str
) -> None:
    comparable_count = file_count - unknown_count
    local = [
        _track(str(index), f"Track {index}", number=index)
        for index in range(1, comparable_count + 1)
    ] + [
        _track(
            f"unknown-{index}",
            "",
            number=0,
            duration=None,
            album="",
            artist="",
            readable=False,
        )
        for index in range(unknown_count)
    ]
    candidate = _candidate(
        "rg",
        [
            _candidate_track(f"Track {index}", index)
            for index in range(1, comparable_count + 1)
        ],
    )
    assert AlbumEvidenceEngine().decide(local, [candidate]).outcome == expected


def test_partial_holding_can_match_without_fabricating_missing_tracks() -> None:
    local = [_track("one", "One", number=1), _track("two", "Two", number=2)]
    candidate = _candidate(
        "rg",
        [_candidate_track(str(number), number) for number in range(1, 11)],
    )
    candidate.tracks[0].title = "One"
    candidate.tracks[1].title = "Two"
    decision = AlbumEvidenceEngine().decide(local, [candidate])
    assert decision.outcome == "identified"
    assert len(decision.candidates[0].unmatched_expected_tracks) == 8


def test_duplicate_local_files_cannot_claim_one_candidate_track_twice() -> None:
    local = [_track("one", "Same"), _track("two", "Same")]
    decision = AlbumEvidenceEngine().decide(
        local, [_candidate("rg", [_candidate_track("Same", 1)])]
    )
    classes = [item.classification for item in decision.candidates[0].track_evidence]
    assert classes.count("supported") == 1
    assert classes.count("contradictory") == 1
    assert decision.outcome == "contradictory"


def test_duplicate_recording_position_and_absolute_overlap_is_ambiguous() -> None:
    local = [_track("one", "Repeated", number=10, disc=2, recording="recording")]
    candidate = _candidate(
        "rg",
        [
            CandidateTrack(
                title="Repeated",
                position=1,
                disc_number=2,
                absolute_position=10,
                recording_mbid="recording",
                release_track_mbid="release-track-a",
            ),
            CandidateTrack(
                title="Repeated",
                position=10,
                disc_number=2,
                absolute_position=19,
                recording_mbid="recording",
                release_track_mbid="release-track-b",
            ),
        ],
    )

    decision = AlbumEvidenceEngine().decide(local, [candidate])

    assert decision.outcome == "contradictory"
    assert decision.candidates[0].track_evidence[0].evidence_kinds == [
        "ambiguous_release_track_identity"
    ]


def test_equal_safe_candidates_are_ambiguous_at_the_signed_margin() -> None:
    local = [_track("one", "One")]
    candidates = [
        _candidate("a", [_candidate_track("One", 1)]),
        _candidate("b", [_candidate_track("One", 1)]),
    ]
    decision = AlbumEvidenceEngine().decide(local, candidates)
    assert decision.outcome == "ambiguous"
    assert (
        decision.candidates[0].score - decision.candidates[1].score
        < CANDIDATE_MARGIN_FLOOR
    )


def test_release_type_confirmation_and_genuine_compilation_acceptance() -> None:
    normal = [_track("one", "One")]
    unsafe = _candidate("live", [_candidate_track("One", 1)], secondary=["live"])
    assert (
        AlbumEvidenceEngine().decide(normal, [unsafe]).reason_code
        == "RELEASE_TYPE_REQUIRES_CONFIRMATION"
    )

    compilation = [_track("one", "One", compilation=True, artist="Various Artists")]
    genuine = _candidate(
        "compilation",
        [_candidate_track("One", 1)],
        artist="Various Artists",
        secondary=["compilation"],
    )
    assert AlbumEvidenceEngine().decide(compilation, [genuine]).outcome == "identified"


def test_compilation_flag_cannot_make_a_live_release_safe() -> None:
    tagged_compilation = [_track("one", "One", compilation=True)]
    live = _candidate("live", [_candidate_track("One", 1)], secondary=["live"])

    assert AlbumEvidenceEngine().decide(tagged_compilation, [live]).reason_code == (
        "RELEASE_TYPE_REQUIRES_CONFIRMATION"
    )


@pytest.mark.parametrize("secondary_type", ["compilation", "live"])
def test_complete_release_track_ids_prove_an_exact_special_release(
    secondary_type: str,
) -> None:
    local = [
        _track(
            "one",
            "One",
            recording="recording-1",
            release_track="release-track-1",
        ),
        _track(
            "two",
            "Two",
            number=2,
            recording="recording-2",
            release_track="release-track-2",
        ),
    ]
    candidate = _candidate(
        secondary_type,
        [
            _candidate_track(
                "One", 1, recording="recording-1", release_track="release-track-1"
            ),
            _candidate_track(
                "Two", 2, recording="recording-2", release_track="release-track-2"
            ),
        ],
        secondary=[secondary_type],
    )

    assert AlbumEvidenceEngine().decide(local, [candidate]).outcome == "identified"


def test_incomplete_release_track_ids_do_not_bypass_release_type_confirmation() -> None:
    local = [
        _track(
            "one",
            "One",
            recording="recording-1",
            release_track="release-track-1",
        ),
        _track("two", "Two", number=2, recording="recording-2"),
    ]
    candidate = _candidate(
        "compilation",
        [
            _candidate_track(
                "One", 1, recording="recording-1", release_track="release-track-1"
            ),
            _candidate_track(
                "Two", 2, recording="recording-2", release_track="release-track-2"
            ),
        ],
        secondary=["compilation"],
    )

    assert AlbumEvidenceEngine().decide(local, [candidate]).reason_code == (
        "RELEASE_TYPE_REQUIRES_CONFIRMATION"
    )


def test_unicode_punctuation_and_duration_grace_are_supported() -> None:
    local = [_track("one", "Beyoncé – Café!", duration=180)]
    candidate = _candidate("rg", [_candidate_track("Beyonce Cafe", 1, duration=190)])
    decision = AlbumEvidenceEngine().decide(local, [candidate])
    assert decision.outcome == "identified"
    assert decision.candidates[0].matcher_version == MATCHER_VERSION


def test_administrator_exact_release_can_map_title_variants_by_position_and_duration() -> (
    None
):
    engine = AlbumEvidenceEngine()
    local = [
        _track("one", "English title", number=1, duration=180),
        _track("two", "Title (Remastered)", number=2, duration=200),
    ]
    candidate = _candidate(
        "rg",
        [
            _candidate_track(
                "현지 제목",
                1,
                duration=182,
                recording="recording-1",
                release_track="release-track-1",
            ),
            _candidate_track(
                "Title",
                2,
                duration=200.1,
                recording="recording-2",
                release_track="release-track-2",
            ),
        ],
    )
    evidence = engine.evaluate_candidate(local, candidate)
    assert evidence.track_evidence[0].release_track_mbid is None

    assert engine.complete_administrator_exact_release_mapping(
        local, candidate, evidence
    )
    assert evidence.unmatched_expected_tracks == []
    assert [item.recording_mbid for item in evidence.track_evidence] == [
        "recording-1",
        "recording-2",
    ]
    assert [item.release_track_mbid for item in evidence.track_evidence] == [
        "release-track-1",
        "release-track-2",
    ]
    assert all(
        "administrator_exact_release_position_duration" in item.evidence_kinds
        for item in evidence.track_evidence
    )


def test_administrator_exact_release_can_map_flattened_multidisc_by_absolute_position() -> (
    None
):
    engine = AlbumEvidenceEngine()
    local = [
        _track("one", "Local one", number=1, duration=180),
        _track("two", "Local two", number=2, duration=200),
        _track("three", "Local three", number=3, duration=220),
    ]
    candidate = _candidate(
        "rg",
        [
            _candidate_track(
                "Provider one",
                1,
                duration=181,
                recording="recording-1",
                release_track="release-track-1",
            ),
            CandidateTrack(
                title="Provider two",
                position=1,
                disc_number=2,
                absolute_position=2,
                duration_seconds=199,
                recording_mbid="recording-2",
                release_track_mbid="release-track-2",
            ),
            CandidateTrack(
                title="Provider three",
                position=2,
                disc_number=2,
                absolute_position=3,
                duration_seconds=221,
                recording_mbid="recording-3",
                release_track_mbid="release-track-3",
            ),
        ],
    )
    evidence = engine.evaluate_candidate(local, candidate)

    assert engine.complete_administrator_exact_release_mapping(
        local, candidate, evidence
    )
    assert evidence.unmatched_expected_tracks == []
    assert [item.release_track_mbid for item in evidence.track_evidence] == [
        "release-track-1",
        "release-track-2",
        "release-track-3",
    ]
    assert all(
        "administrator_exact_release_absolute_position_duration" in item.evidence_kinds
        for item in evidence.track_evidence
    )


@pytest.mark.parametrize(
    "invalid_case",
    [
        "non_flat_local_discs",
        "duplicate_local_position",
        "missing_provider_absolute_position",
        "duplicate_provider_absolute_position",
    ],
)
def test_administrator_exact_release_flattened_multidisc_mapping_fails_closed(
    invalid_case: str,
) -> None:
    engine = AlbumEvidenceEngine()
    local = [
        _track("one", "Local one", number=1, duration=180),
        _track("two", "Local two", number=2, duration=200),
    ]
    candidate_tracks = [
        _candidate_track(
            "Provider one",
            1,
            duration=180,
            recording="recording-1",
            release_track="release-track-1",
        ),
        CandidateTrack(
            title="Provider two",
            position=1,
            disc_number=2,
            absolute_position=2,
            duration_seconds=200,
            recording_mbid="recording-2",
            release_track_mbid="release-track-2",
        ),
    ]
    if invalid_case == "non_flat_local_discs":
        local[1].disc_number = 2
    elif invalid_case == "duplicate_local_position":
        local[1].track_number = 1
    elif invalid_case == "missing_provider_absolute_position":
        candidate_tracks[1].absolute_position = 0
    else:
        candidate_tracks[1].absolute_position = 1
    candidate = _candidate("rg", candidate_tracks)
    evidence = engine.evaluate_candidate(local, candidate)

    assert not engine.complete_administrator_exact_release_mapping(
        local, candidate, evidence
    )


@pytest.mark.parametrize(
    "invalid_case",
    [
        "missing_duration",
        "duration_conflict",
        "duplicate_position",
        "count_mismatch",
        "missing_provider_identity",
    ],
)
def test_administrator_exact_release_position_mapping_fails_closed(
    invalid_case: str,
) -> None:
    engine = AlbumEvidenceEngine()
    local = [
        _track("one", "Local one", number=1, duration=180),
        _track("two", "Local two", number=2, duration=200),
    ]
    candidate_tracks = [
        _candidate_track(
            "Provider one",
            1,
            duration=180,
            recording="recording-1",
            release_track="release-track-1",
        ),
        _candidate_track(
            "Provider two",
            2,
            duration=200,
            recording="recording-2",
            release_track="release-track-2",
        ),
    ]
    if invalid_case == "missing_duration":
        candidate_tracks[0].duration_seconds = None
    elif invalid_case == "duration_conflict":
        candidate_tracks[0].duration_seconds = 191
    elif invalid_case == "duplicate_position":
        candidate_tracks[1].position = 1
    elif invalid_case == "count_mismatch":
        candidate_tracks.pop()
    else:
        candidate_tracks[0].release_track_mbid = None
    candidate = _candidate("rg", candidate_tracks)
    evidence = engine.evaluate_candidate(local, candidate)

    assert not engine.complete_administrator_exact_release_mapping(
        local, candidate, evidence
    )
