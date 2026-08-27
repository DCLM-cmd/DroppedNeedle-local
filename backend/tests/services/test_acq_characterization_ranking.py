"""Phase-1 characterization: pin TODAY'S acquisition scoring orders.

Freezes the pre-snapshot ranking behaviour of the three scorers so the Phase-3
snapshot cutover can prove byte-for-behavior equivalence:

- Album: ``_candidate_rank_key`` order =
  acceptance band >> canonical tier >> folder hi-res (depth/rate) >>
  availability tuple >> final_score.
- Track: ``TrackMatcher.rank`` order = acceptance >> tier >> (bit_depth,
  sample_rate) >> free-slot >> queue-known >> queue-length >> upload >>
  -size >> score, with the artist-evidence gate capping unevidenced files at
  'manual' and one candidate per peer.
- Usenet: ``NewznabReleaseScorer.rank`` weights final = 0.40*identity +
  0.45*quality + 0.15*health, category precedence (3040 > title markers),
  video-category drop, unknown-tier pass-through, and (final, _hires_rank)
  descending sort.

These are snapshots of current behaviour, not desired behaviour.
"""

import pytest

# NOTE (Acquisition cutover, 2026-08-27): production rank() now composes
# identity band -> snapshot preference step -> certainty/target-distance on top
# of the legacy tuple below; the direct-sort pins in this file deliberately
# exercise the preserved LEGACY composite (`_legacy_candidate_rank_key`) so the
# pre-cutover ordering contract stays executable history.
from unittest.mock import AsyncMock

from models.download import ScoredCandidate, TargetAlbum, TargetTrack
from repositories.protocols.download_client import DownloadSearchResult
from repositories.protocols.indexer import UsenetRelease
from services.native.album_preflight_scorer import _legacy_candidate_rank_key as _candidate_rank_key
from services.native.newznab_release_scorer import NewznabReleaseScorer
from services.native.track_matcher import TrackMatcher

from api.v1.schemas.settings import DownloadPolicySettings
from services.native.acquisition.quality import build_snapshot


def LEGACY_SNAPSHOT():
    """Pre-feature default policy as an explicit snapshot: [mp3_320..lossless],
    hi-res-first lossless detail, unknown evidence passes as final fallback -
    so every pin below exercises the same eligibility surface as before."""
    return build_snapshot(DownloadPolicySettings())




# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _store(quarantine=None):
    store = AsyncMock()
    store.load_quarantine_set.return_value = set(quarantine or set())
    return store


def _album_file(**kw):
    base = dict(
        username="peer",
        filename="Nova Spark - Aurora Lights/01 Aurora Lights.flac",
        parent_directory="Nova Spark - Aurora Lights",
        size=40_000_000,
        extension="flac",
        bitrate=None,
        bit_depth=None,
        sample_rate=None,
        duration=284.0,
        has_free_slot=False,
        upload_speed=0,
        queue_length=None,
    )
    base.update(kw)
    return DownloadSearchResult(**base)


def _album_candidate(tier, files, *, final_score=0.5, **kw):
    base = dict(
        source="soulseek",
        username=files[0].username,
        parent_directory=files[0].parent_directory,
        files=list(files),
        coherence=final_score,
        file_confidence=final_score,
        final_score=final_score,
        tier=tier,
    )
    base.update(kw)
    return ScoredCandidate(**base)


def _track_file(
    filename,
    parent,
    *,
    ext="flac",
    bitrate=900,
    duration=284.0,
    username="alice",
    bit_depth=None,
    sample_rate=None,
    free=False,
    queue_length=None,
    speed=0,
):
    return DownloadSearchResult(
        username=username,
        filename=filename,
        parent_directory=parent,
        size=20_000_000,
        extension=ext,
        bitrate=bitrate,
        bit_depth=bit_depth,
        sample_rate=sample_rate,
        duration=duration,
        has_free_slot=free,
        queue_length=queue_length,
        upload_speed=speed,
    )


_TRACK_TARGET = TargetTrack(
    artist_name="Nova Spark", track_title="Aurora Lights", duration_seconds=284.0
)


def _release(title, cats, *, size=600_000_000, grabs=200, usenet_date=None):
    return UsenetRelease(
        indexer_id="ds",
        indexer_name="DS",
        guid=f"g-{abs(hash(title))}",
        title=title,
        nzb_url="https://idx/nzb",
        size_bytes=size,
        category_ids=list(cats),
        grabs=grabs,
        password=0,
        usenet_date=usenet_date,
    )


_USNET_TARGET = TargetAlbum(artist_name="Radiohead", album_title="In Rainbows")
# track_count/duration stay None so the size-plausibility and 8MB/track gates
# are indeterminate and never reject - every release below is album-sized.


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Album: _candidate_rank_key (direct sort - pure, no I/O)
# --------------------------------------------------------------------------


def test_album_acceptance_band_dominates_everything():
    busy_auto = _album_candidate(
        "auto",
        [_album_file(bit_depth=16, sample_rate=44100, has_free_slot=False,
                     queue_length=99, upload_speed=0)],
        final_score=0.4,
    )
    super_manual = _album_candidate(
        "manual",
        [_album_file(username="other", bit_depth=24, sample_rate=96000,
                     has_free_slot=True, queue_length=0, upload_speed=999_999)],
        final_score=1.0,
    )
    ranked = sorted([super_manual, busy_auto], key=_candidate_rank_key, reverse=True)
    assert [c.tier for c in ranked] == ["auto", "manual"]


def test_album_canonical_tier_dominates_availability_and_score():
    lossless = _album_candidate("auto", [_album_file()], final_score=0.1)
    lossy_perfect = _album_candidate(
        "auto",
        [
            _album_file(
                username="lossy",
                filename="Nova Spark - Aurora Lights/01 Aurora Lights.mp3",
                extension="mp3",
                bitrate=320,
                has_free_slot=True,
                queue_length=0,
                upload_speed=999_999,
                size=999_999_999,
            )
        ],
        final_score=1.0,
    )
    ranked = sorted([lossy_perfect, lossless], key=_candidate_rank_key, reverse=True)
    assert [c.files[0].extension for c in ranked] == ["flac", "mp3"]


def test_album_hires_depth_rate_orders_equal_tiers():
    hires = _album_candidate("auto", [_album_file(bit_depth=24, sample_rate=96000)],
                             final_score=0.5)
    redbook = _album_candidate("auto", [_album_file(bit_depth=16, sample_rate=44100)],
                               final_score=0.5)
    ranked = sorted([redbook, hires], key=_candidate_rank_key, reverse=True)
    assert [c.files[0].bit_depth for c in ranked] == [24, 16]


def test_album_free_slot_pool_beats_higher_final_score():
    winner_free = _album_candidate(
        "auto",
        [_album_file(parent_directory="winner-free", queue_length=0,
                     has_free_slot=True, upload_speed=100_000)],
        final_score=0.05,
    )
    queue_unknown = _album_candidate(
        "auto",
        [_album_file(username="u2", parent_directory="queue-unknown",
                     has_free_slot=True)],
        final_score=0.95,
    )
    busy = _album_candidate(
        "auto",
        [_album_file(username="u3", parent_directory="busy-peer",
                     has_free_slot=False, queue_length=500)],
        final_score=0.95,
    )
    ranked = sorted([busy, queue_unknown, winner_free],
                    key=_candidate_rank_key, reverse=True)
    assert [c.parent_directory for c in ranked] == [
        "winner-free", "queue-unknown", "busy-peer",
    ]


_USNET_TARGET = TargetAlbum(artist_name="Radiohead", album_title="In Rainbows")


@pytest.mark.asyncio
async def test_track_artist_evidence_gate_caps_manual():
    evidenced = _track_file(
        "@@x\\Nova Spark\\Aurora Lights\\01 Aurora Lights.flac", "Aurora Lights",
        username="evidenced",
    )
    bare = _track_file(
        "@@abc\\Aurora Collection\\01 Aurora Lights.flac", "Aurora Collection",
        username="bare",
    )
    ranked = await TrackMatcher(_store()).rank(
        _TRACK_TARGET, [bare, evidenced],
        snapshot=LEGACY_SNAPSHOT(),
    )
    assert [c.username for c in ranked] == ["evidenced", "bare"]
    assert ranked[0].tier == "auto"
    assert ranked[1].tier == "manual"


@pytest.mark.asyncio
async def test_track_tier_then_hires_ordering_within_auto():
    mp3 = _track_file(
        "Nova Spark - Aurora Lights/01 Aurora Lights.mp3", "Nova Spark - Aurora Lights",
        ext="mp3", bitrate=320, username="mp320",
    )
    redbook = _track_file(
        "Nova Spark - Aurora Lights/01 Aurora Lights.flac", "Nova Spark - Aurora Lights",
        bit_depth=16, sample_rate=44100, username="cd16",
    )
    hires = _track_file(
        "Nova Spark - Aurora Lights/01 Aurora Lights.flac", "Nova Spark - Aurora Lights",
        bit_depth=24, sample_rate=96000, username="hi24",
    )
    ranked = await TrackMatcher(_store()).rank(
        _TRACK_TARGET, [mp3, redbook, hires],
        snapshot=LEGACY_SNAPSHOT(),
    )
    assert [c.username for c in ranked] == ["hi24", "cd16", "mp320"]


@pytest.mark.asyncio
async def test_track_one_candidate_per_peer():
    best = _track_file(
        "Nova Spark - Aurora Lights/01 Aurora Lights.flac", "Nova Spark - Aurora Lights",
        username="dup", bit_depth=24, sample_rate=96000,
    )
    worse_same_peer = _track_file(
        "Nova Spark - Aurora Lights/01 Aurora Lights.mp3", "Nova Spark - Aurora Lights",
        ext="mp3", bitrate=320, username="dup",
    )
    other_peer = _track_file(
        "Nova Spark\\Aurora Lights\\01 Aurora Lights.flac", "Aurora Lights",
        username="solo", bit_depth=16, sample_rate=44100,
    )
    ranked = await TrackMatcher(_store()).rank(
        _TRACK_TARGET, [worse_same_peer, other_peer, best],
        snapshot=LEGACY_SNAPSHOT(),
    )
    assert [c.username for c in ranked] == ["dup", "solo"]
    assert ranked[0].files[0].extension == "flac"


@pytest.mark.asyncio
async def test_track_below_manual_threshold_labeled_rejected():
    junk = _track_file(
        "Various Compilations/data_disc_unrelated.mp3", "Various Compilations",
        ext="mp3", bitrate=320, duration=40.0, username="loner",
    )
    ranked = await TrackMatcher(_store()).rank(
        _TRACK_TARGET, [junk],
        snapshot=LEGACY_SNAPSHOT(),
    )
    assert len(ranked) == 1
    assert ranked[0].tier == "rejected"


@pytest.mark.asyncio
async def test_usenet_category_and_title_quality_bands_order_finals():
    # Quality contributions (same identity tokens + healthy grabs):
    #   cat 3040 lossless      Q=1.00 -> final 0.96   (step 0)
    #   cat 3010 no marker     Q=0.80 -> final 0.87   (step 1)
    #   no cat/no marker       Q=0.50 -> final 0.735  (unknown fallback slot)
    # The composed key orders lossless > mp3_320 > unknown; finals remain pinned.
    lossless = _release("Radiohead - In Rainbows [FLAC]", [3040])
    mp3_nolabel = _release("Radiohead - In Rainbows", [3010])
    unknown = _release("Radiohead - In Rainbows", [])
    ranked = await NewznabReleaseScorer(_store()).rank(
        _USNET_TARGET, [unknown, mp3_nolabel, lossless],
        snapshot=LEGACY_SNAPSHOT(),
    )
    assert [c.tier for c in ranked] == ["auto", "auto", "auto"]
    assert [c.final_score for c in ranked] == pytest.approx([0.96, 0.87, 0.735], abs=0.02)


@pytest.mark.asyncio
async def test_usenet_video_category_dropped_entirely():
    video = _release("Radiohead - In Rainbows [music video]", [3020])
    good = _release("Radiohead - In Rainbows [FLAC]", [3040])
    ranked = await NewznabReleaseScorer(_store()).rank(
        _USNET_TARGET, [video, good],
        snapshot=LEGACY_SNAPSHOT(),
    )
    assert [r.usenet_release.title for r in ranked] == ["Radiohead - In Rainbows [FLAC]"]


@pytest.mark.asyncio
async def test_usenet_health_flips_band_at_manual_auto_boundary():
    def scramble_release(grabs):
        return _release("aHR0cHM6 scrambled xQ.part01.rar", [3010], grabs=grabs)

    ranked = await NewznabReleaseScorer(_store()).rank(
        _USNET_TARGET, [scramble_release(None), scramble_release(250)],
        snapshot=LEGACY_SNAPSHOT(),
    )
    bands = {c.usenet_release.grabs: c.tier for c in ranked}
    assert bands[None] == "manual"
    assert bands[250] == "auto"


@pytest.mark.asyncio
async def test_usenet_weights_dominate_hires_title_tiebreak():
    heavier = _release("Radiohead - In Rainbows FLAC", [3040])
    hires_but_weaker = _release(
        "Radiohead - In Rainbows SACD DSD FLAC", [3040], grabs=None
    )
    ranked = await NewznabReleaseScorer(_store()).rank(
        _USNET_TARGET, [hires_but_weaker, heavier],
        snapshot=LEGACY_SNAPSHOT(),
    )
    assert [r.usenet_release.title for r in ranked] == [
        "Radiohead - In Rainbows FLAC",
        "Radiohead - In Rainbows SACD DSD FLAC",
    ]


@pytest.mark.asyncio
async def test_usenet_garbled_identity_cannot_reach_auto():
    garbled = _release("BFKZ VXQU CJQB FZXG", [], grabs=None)
    coherent_unknown = _release("Radiohead - In Rainbows", [], grabs=None)
    ranked = await NewznabReleaseScorer(_store()).rank(
        _USNET_TARGET, [garbled, coherent_unknown],
        snapshot=LEGACY_SNAPSHOT(),
    )
    assert ranked[-1].usenet_release.title == "BFKZ VXQU CJQB FZXG"
    assert ranked[-1].tier != "auto"
    assert ranked[-1].final_score <= 0.63
