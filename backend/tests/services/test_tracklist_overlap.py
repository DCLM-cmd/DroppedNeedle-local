"""Grab-time tracklist overlap (TracklistAwareAcquisition Slice 1).

Pins the pure judge in ``scoring_core`` (position-aware claims, loose
fallback, fail-open rules, version veto) and its wiring into
``AlbumPreflightScorer.rank`` (multiplicative discount, manual floor,
``track_overlap`` persistence). Incident-shaped fixtures mirror the live
2026-09 holds; the full peer file lists additionally replay through the
acquisition corpus (``incident-flux-sessions``,
``incident-platinum-greatest-hits``).
"""

from unittest.mock import AsyncMock

import pytest

from api.v1.schemas.settings import DownloadPolicySettings
from models.download import TargetAlbum
from models.download_manifest import ExpectedTrack
from repositories.protocols.download_client import DownloadSearchResult
from services.native.acquisition.quality import build_snapshot
from services.native.acquisition.scoring_core import (
    artist_words,
    filename_stem,
    leading_track_number,
    normalize_folder_identity,
    pair_serves_track,
    stem_title_signal,
    tracklist_overlap,
)
from services.native.album_preflight_scorer import AlbumPreflightScorer

_POPPY = [
    (1, "The Cutting Edge", 132.0),
    (2, "The Day I Walked Away", 188.0),
    (3, "Rot In LA", 175.0),
    (4, "I Started Smoking", 180.0),
    (5, "Kitty", 180.0),
]
# The wrong album's folder, as grabbed live (attempt 26b7db74 + held rows).
_FLUX_FOLDER = [
    ("01. Flux.flac", 301.0),
    ("02. Lessen the Damage.flac", 142.0),
    ("03. So Mean.flac", 177.0),
    ("04. On the Level.flac", 205.0),
    ("05. Hysteria.flac", 261.0),
]
_EXACT_FOLDER = [
    ("01. The Cutting Edge.flac", 132.0),
    ("02. The Day I Walked Away.flac", 188.0),
    ("03. Rot In LA.flac", 175.0),
    ("04. I Started Smoking.flac", 180.0),
    ("05. Kitty.flac", 180.0),
]


def test_wrong_album_scores_zero():
    assert tracklist_overlap(_FLUX_FOLDER, _POPPY) == 0.0


def test_exact_folder_scores_one():
    assert tracklist_overlap(_EXACT_FOLDER, _POPPY) == 1.0


def test_no_tracklist_is_none_not_zero():
    # None keeps today's behavior; 0.0 would wrongly demote.
    assert tracklist_overlap(_EXACT_FOLDER, []) is None


def test_partial_correct_folder_keeps_full_overlap():
    # Completeness is coherence's job (count_ratio), not overlap's: a
    # partial-but-correct folder must not be demoted here.
    assert tracklist_overlap(_EXACT_FOLDER[:3], _POPPY) == 1.0


def test_obfuscated_numbered_rip_fails_open():
    assert tracklist_overlap([(f"0{n}.flac", None) for n in range(1, 6)], _POPPY) == 1.0


def test_obfuscated_with_durations_judges_on_length():
    ok = [
        ("01.flac", 132.0),
        ("02.flac", 188.0),
        ("03.flac", 175.0),
        ("04.flac", 180.0),
        ("05.flac", 180.0),
    ]
    assert tracklist_overlap(ok, _POPPY) == 1.0
    wrong_lengths = [(f"0{n}.flac", 30.0) for n in range(1, 6)]
    assert tracklist_overlap(wrong_lengths, _POPPY) == 0.0


def test_deluxe_with_bonus_tracks_stays_high():
    bonus = _EXACT_FOLDER + [
        ("06. Demo Take.flac", 150.0),
        ("07. Studio Chat.flac", 60.0),
    ]
    assert tracklist_overlap(bonus, _POPPY) == pytest.approx(0.857, abs=0.01)


def test_live_suffixed_claim_vetoes_only_that_position():
    live = [("01. The Cutting Edge (Live).flac", 135.0), *_EXACT_FOLDER[1:]]
    assert tracklist_overlap(live, _POPPY) == pytest.approx(0.8)


def test_shuffled_same_songs_scores_zero():
    shuffled = [
        ("01. Kitty.flac", 180.0),
        ("02. The Cutting Edge.flac", 132.0),
        ("03. The Day I Walked Away.flac", 188.0),
        ("04. Rot In LA.flac", 175.0),
        ("05. I Started Smoking.flac", 180.0),
    ]
    # Set-overlap would call this a perfect match; the positional import
    # would hold every file, so overlap must agree with the import.
    assert tracklist_overlap(shuffled, _POPPY) == 0.0


def test_duration_coincidence_does_not_rescue_conflicting_title():
    # "So Mean" at 177s vs "Rot In LA" at 175s: 2s apart, different songs.
    files = [("03. So Mean.flac", 177.0)]
    expected = [(3, "Rot In LA", 175.0)]
    assert tracklist_overlap(files, expected) == 0.0


def test_artist_prefix_does_not_sink_single_word_titles():
    prefixed = [
        ("Poppy - 01. The Cutting Edge.flac", 132.0),
        ("Poppy - 02. The Day I Walked Away.flac", 188.0),
        ("Poppy - 03. Rot In LA.flac", 175.0),
        ("Poppy - 04. I Started Smoking.flac", 180.0),
        ("Poppy - 05. Kitty.flac", 180.0),
    ]
    assert tracklist_overlap(prefixed, _POPPY, ignore=frozenset({"poppy"})) == 1.0


def test_space_separated_track_numbers_parse():
    assert leading_track_number("04 Tom Jones - Delilah.mp3") == 4
    assert leading_track_number("01 It's Not Unusual.mp3") == 1


def test_leading_number_ignores_years_and_glued_digits():
    assert leading_track_number("2006 Remaster.mp3") is None
    assert leading_track_number("10cc Greatest.mp3") is None
    assert leading_track_number("track01.mp3") is None


def test_filename_stem_splits_soulseek_paths():
    assert filename_stem("@@share\\Music\\Poppy\\01. Flux.flac") == "01. Flux"
    assert filename_stem("no-extension") == "no-extension"


def test_stem_signal_drops_numbers_and_fillers():
    assert stem_title_signal("01") == ""
    assert stem_title_signal("Track 05") == ""
    assert stem_title_signal("03. So Mean") == "so mean"


def test_pair_rescue_true_preserves_legacy_semantics():
    # Duration rescues a conflicting title (failover-filter behavior).
    assert pair_serves_track("so mean", 177.0, "Rot In LA", 175.0) is True
    # ...but a duration veto is never rescued, and silence passes.
    assert pair_serves_track("flux", 301.0, "The Cutting Edge", 132.0) is False
    assert pair_serves_track("", None, "The Cutting Edge", 132.0) is True


def test_pair_rescue_false_is_strict_on_titles():
    assert (
        pair_serves_track("so mean", 177.0, "Rot In LA", 175.0, rescue=False) is False
    )
    assert (
        pair_serves_track("rot in la", 175.0, "Rot In LA", 175.0, rescue=False) is True
    )
    # Zero-signal still fails open (obfuscated claims).
    assert pair_serves_track("", None, "Rot In LA", 175.0, rescue=False) is True


def _search_result(parent, name, duration, *, username="alice"):
    return DownloadSearchResult(
        username=username,
        filename=f"{parent}/{name}",
        parent_directory=parent,
        size=30_000_000,
        extension="flac",
        bitrate=900,
        bit_depth=16,
        sample_rate=44100,
        duration=duration,
        has_free_slot=True,
        upload_speed=2_000_000,
        queue_length=0,
    )


def _expected(pop):
    return [
        ExpectedTrack(track_number=n, disc_number=1, duration_seconds=d, title=t)
        for n, t, d in pop
    ]


def _snapshot():
    return build_snapshot(
        DownloadPolicySettings(quality_min="mp3_320", quality_max="lossless")
    )


def _store():
    store = AsyncMock()
    store.load_quarantine_set.return_value = set()
    return store


@pytest.mark.asyncio
async def test_rank_demotes_wrong_album_below_auto_and_floors_manual():
    parent = "Poppy - Flux - Sessions"
    target = TargetAlbum(
        artist_name="Poppy",
        album_title="Flux - Sessions",
        year=2021,
        track_count=5,
    )
    files = [
        _search_result(parent, f"Poppy - {name}", duration)
        for name, duration in _FLUX_FOLDER
    ]
    scorer = AlbumPreflightScorer(_store())
    (candidate,) = await scorer.rank(
        target, files, snapshot=_snapshot(), expected_tracks=_expected(_POPPY)
    )
    assert candidate.track_overlap == 0.0
    assert candidate.tier == "manual"
    assert candidate.final_score < 0.70


@pytest.mark.asyncio
async def test_rank_keeps_exact_match_auto():
    parent = "Poppy - Flux - Sessions"
    target = TargetAlbum(
        artist_name="Poppy",
        album_title="Flux - Sessions",
        year=2021,
        track_count=5,
    )
    files = [
        _search_result(parent, f"Poppy - {name}", duration)
        for name, duration in _EXACT_FOLDER
    ]
    scorer = AlbumPreflightScorer(_store())
    (candidate,) = await scorer.rank(
        target, files, snapshot=_snapshot(), expected_tracks=_expected(_POPPY)
    )
    assert candidate.track_overlap == 1.0
    assert candidate.tier == "auto"


@pytest.mark.asyncio
async def test_rank_without_tracklist_is_unchanged():
    parent = "Poppy - Flux - Sessions"
    target = TargetAlbum(
        artist_name="Poppy",
        album_title="Flux - Sessions",
        year=2021,
        track_count=5,
    )
    files = [
        _search_result(parent, f"Poppy - {name}", duration)
        for name, duration in _FLUX_FOLDER
    ]
    scorer = AlbumPreflightScorer(_store())
    (candidate,) = await scorer.rank(target, files, snapshot=_snapshot())
    assert candidate.track_overlap is None
    # The legacy blind rank still auto-accepts this folder (the incident).
    assert candidate.tier == "auto"


def test_normalize_folder_identity_merges_peer_naming_variants():
    assert normalize_folder_identity("2021. Flux") == "flux"
    assert normalize_folder_identity("Flux (2021)") == "flux"
    assert normalize_folder_identity("Flux") == "flux"
    assert normalize_folder_identity("Flux [FLAC]") == "flux"
    assert (
        normalize_folder_identity("Poppy - Flux (2021) [FLAC]", artist_words=frozenset({"poppy"}))
        == "flux"
    )


def test_normalize_folder_identity_keeps_products_distinct():
    platinum = normalize_folder_identity("2006 Greatest Hits - Platinum Edition")
    platinum_peer = normalize_folder_identity("Greatest Hits - Platinum Edition (2006)")
    assert platinum == platinum_peer
    assert normalize_folder_identity("Greatest Hits (CD+DVD)") != platinum
    assert normalize_folder_identity("Greatest Hits [2006]") != platinum
    assert normalize_folder_identity("Flux - Sessions") == "flux sessions"


def test_normalize_folder_identity_accepts_year_only_caveat():
    # Same name distinguished only by a bare year shares an identity
    # (bounded by RG-scoping + TTL + re-request clearing).
    assert normalize_folder_identity("NOW 2006") == normalize_folder_identity("NOW 2007")
    # Nothing identity-carrying -> empty (caller skips learning).
    assert normalize_folder_identity("24") == ""
    assert normalize_folder_identity("") == ""


def test_artist_words_rule():
    assert artist_words("Tom Jones") == frozenset({"tom", "jones"})
    assert artist_words(None) == frozenset()
    assert "u" not in artist_words("U2")  # single letters drop


@pytest.mark.asyncio
async def test_rank_single_with_overlap_keeps_strict_path_ungated():
    """Singles keep the existing strict TrackMatcher path: a correct single
    ranks EXACTLY as before with overlap neutral (1.0), never double-gated."""
    parent = "Poppy - Kitty"
    target = TargetAlbum(
        artist_name="Poppy", album_title="Kitty", year=2021, track_count=1
    )
    files = [_search_result(parent, "Poppy - Kitty.flac", 180.0)]
    expected = [
        ExpectedTrack(
            track_number=1, disc_number=1, duration_seconds=180.0, title="Kitty"
        )
    ]
    scorer = AlbumPreflightScorer(_store())

    (candidate,) = await scorer.rank(
        target, files, snapshot=_snapshot(), expected_tracks=expected
    )
    (legacy,) = await scorer.rank(target, files, snapshot=_snapshot())

    assert candidate.track_overlap == 1.0
    assert candidate.tier == "auto"
    assert candidate.final_score == legacy.final_score
