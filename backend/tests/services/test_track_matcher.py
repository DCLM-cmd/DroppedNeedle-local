"""TrackMatcher tests - per-track scoring, no group phase, quarantine + floor."""

from unittest.mock import AsyncMock

import pytest

from api.v1.schemas.settings import DownloadPolicySettings
from models.download import TargetTrack
from repositories.protocols.download_client import DownloadSearchResult
from services.native.acquisition.quality import build_snapshot
from services.native.track_matcher import TrackMatcher


def _store(quarantine=None):
    store = AsyncMock()
    store.load_quarantine_set.return_value = set(quarantine or set())
    return store


def policy_snapshot(**over):
    """Legacy quality kwargs moved onto the required keyword-only snapshot."""
    base = dict(quality_min="mp3_320", quality_max="lossless")
    base.update(over)
    return build_snapshot(DownloadPolicySettings(**base))


def _file(
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


_TARGET = TargetTrack(
    artist_name="Radiohead", track_title="Airbag", duration_seconds=284.0
)


@pytest.mark.asyncio
async def test_match_returns_single_file_candidate():
    results = [_file("Radiohead - OK Computer/Airbag.flac", "Radiohead - OK Computer")]
    matcher = TrackMatcher(_store())
    candidate = await matcher.match(_TARGET, results, snapshot=policy_snapshot())
    assert candidate is not None
    assert len(candidate.files) == 1
    assert candidate.tier == "auto"
    assert candidate.final_score >= 0.70


@pytest.mark.asyncio
async def test_match_no_results_returns_none():
    matcher = TrackMatcher(_store())
    assert await matcher.match(_TARGET, [], snapshot=policy_snapshot()) is None


@pytest.mark.asyncio
async def test_match_excludes_quarantined():
    from models.download_identity import soulseek_identity

    results = [_file("Radiohead - OK Computer/Airbag.flac", "Radiohead - OK Computer")]
    quarantined = {
        ("soulseek", soulseek_identity(results[0].username, results[0].filename))
    }
    matcher = TrackMatcher(_store(quarantine=quarantined))
    assert await matcher.match(_TARGET, results, snapshot=policy_snapshot()) is None


@pytest.mark.asyncio
async def test_match_picks_highest_confidence():
    good = _file("Radiohead - OK Computer/Airbag.flac", "Radiohead - OK Computer")
    wrong = _file("Misc/Some Other Track.flac", "Misc", duration=120.0)
    matcher = TrackMatcher(_store())
    candidate = await matcher.match(
        _TARGET, [wrong, good], snapshot=policy_snapshot()
    )
    assert candidate.files[0].filename == good.filename


@pytest.mark.asyncio
async def test_match_flac_mp3_only_excludes_other_codecs():
    ogg = _file("Artist/Airbag.ogg", "Artist", ext="ogg", bitrate=320)
    assert (
        await TrackMatcher(_store()).match(_TARGET, [ogg], snapshot=policy_snapshot())
        is None
    )  # default: flac_mp3_only
    assert (
        await TrackMatcher(_store()).match(
            _TARGET, [ogg], snapshot=policy_snapshot(flac_mp3_only=False)
        )
        is not None
    )


@pytest.mark.asyncio
async def test_match_only_lossless_drops_mp3():
    mp3 = _file(
        "Radiohead - OK Computer/Airbag.mp3",
        "Radiohead - OK Computer",
        ext="mp3",
        bitrate=320,
    )
    matcher = TrackMatcher(_store())
    assert (
        await matcher.match(
            _TARGET,
            [mp3],
            snapshot=policy_snapshot(quality_min="lossless", quality_max="lossless"),
        )
        is None
    )


@pytest.mark.asyncio
async def test_match_prefers_higher_identity_band_before_format():
    mp3 = _file(
        "Radiohead - OK Computer/Airbag.mp3",
        "Radiohead - OK Computer",
        ext="mp3",
        bitrate=320,
    )
    flac = _file("OKC/Airbag.flac", "OKC", ext="flac", username="bob")
    candidate = await TrackMatcher(_store()).match(
        _TARGET, [mp3, flac], snapshot=policy_snapshot()
    )
    assert candidate is not None
    assert candidate.files[0].username == "alice"


@pytest.mark.asyncio
async def test_match_prefers_hires_lossless_over_free_redbook_peer():
    queued_hires = _file(
        "Radiohead - OK Computer/Airbag.flac",
        "Radiohead - OK Computer",
        username="queued-hires",
        bit_depth=24,
        sample_rate=48_000,
        free=False,
        queue_length=2710,
        speed=500_000,
    )
    free_redbook = _file(
        "Radiohead - OK Computer/Airbag.flac",
        "Radiohead - OK Computer",
        username="free-redbook",
        bit_depth=16,
        sample_rate=44_100,
        free=True,
        queue_length=0,
        speed=20_000_000,
    )

    ranked = await TrackMatcher(_store()).rank(
        _TARGET, [free_redbook, queued_hires], snapshot=policy_snapshot()
    )

    assert [candidate.username for candidate in ranked] == [
        "queued-hires",
        "free-redbook",
    ]


@pytest.mark.asyncio
async def test_match_uses_slot_queue_and_speed_within_identical_resolution():
    def peer(username, *, free, queue_length, speed):
        return _file(
            "Radiohead - OK Computer/Airbag.flac",
            "Radiohead - OK Computer",
            username=username,
            bit_depth=24,
            sample_rate=48_000,
            free=free,
            queue_length=queue_length,
            speed=speed,
        )

    ranked = await TrackMatcher(_store()).rank(
        _TARGET,
        [
            peer("long-fast", free=False, queue_length=5, speed=20_000_000),
            peer("short-slow", free=False, queue_length=1, speed=1_000_000),
            peer("short-fast", free=False, queue_length=1, speed=5_000_000),
            peer("free-slow", free=True, queue_length=0, speed=500_000),
        ],
        snapshot=policy_snapshot(),
    )

    assert [candidate.username for candidate in ranked] == [
        "free-slow",
        "short-fast",
        "short-slow",
        "long-fast",
    ]


@pytest.mark.asyncio
async def test_rank_held_tier_floor_keeps_only_strictly_better():
    """Per-track upgrade floor (D12): with held_tier set, only files STRICTLY above
    the recording's held tier survive - an equal-tier copy is never a wasted grab."""
    results = [
        _file("A/Airbag.flac", "A", ext="flac", bitrate=900, username="flac-peer"),
        _file("B/Airbag.mp3", "B", ext="mp3", bitrate=320, username="mp3320-peer"),
        _file("C/Airbag.mp3", "C", ext="mp3", bitrate=192, username="mp3192-peer"),
    ]
    matcher = TrackMatcher(_store())

    ranked = await matcher.rank(
        _TARGET,
        results,
        snapshot=policy_snapshot(quality_min="low"),
        held_tier="mp3_320",
    )

    assert [c.username for c in ranked] == ["flac-peer"]  # only lossless beats mp3_320

    # no floor (not an upgrade run): everything in range still ranks
    ranked = await matcher.rank(
        _TARGET, results, snapshot=policy_snapshot(quality_min="low")
    )
    assert {c.username for c in ranked} == {"flac-peer", "mp3320-peer", "mp3192-peer"}


# --- tier='auto' requires artist evidence (D2, 2026-07-05 wrong-single incident) ------


@pytest.mark.asyncio
async def test_auto_requires_artist_evidence_in_path():
    """A wrong-artist file with an exact title + matching duration clears 0.70 on
    score alone (0.55 title + fabricated/low artist + 0.25 duration) - without the
    evidence gate it would silently auto-download. It must cap at 'manual'."""
    wrong = _file(
        "Dan Romer - Some Soundtrack/Airbag.flac", "Dan Romer - Some Soundtrack"
    )
    ranked = await TrackMatcher(_store()).rank(
        _TARGET, [wrong], snapshot=policy_snapshot()
    )
    assert len(ranked) == 1
    assert ranked[0].tier == "manual"


@pytest.mark.asyncio
async def test_auto_kept_when_artist_is_a_grandparent_dir():
    # Artist/Album share layout: evidence lives in the full remote path, not the
    # parent folder name.
    nested = _file("@@x\\Radiohead\\OK Computer\\Airbag.flac", "OK Computer")
    ranked = await TrackMatcher(_store()).rank(
        _TARGET, [nested], snapshot=policy_snapshot()
    )
    assert ranked[0].tier == "auto"


@pytest.mark.asyncio
async def test_obfuscated_folder_caps_at_manual_not_fabricated_auto():
    """R4 pin: an artist-less folder used to score a FABRICATED artist match of 1.0
    (``_artist_from_path`` falls back to the target artist). Score may stay high,
    but the tier must be 'manual' - absence of evidence is unknown, not a match."""
    bare = _file("@@abc\\the arrival\\Airbag.flac", "the arrival")
    ranked = await TrackMatcher(_store()).rank(
        _TARGET, [bare], snapshot=policy_snapshot()
    )
    assert ranked[0].tier == "manual"


@pytest.mark.asyncio
async def test_incident_candidate_replay_never_auto():
    """The 2026-07-05 candidate byte-for-byte: 'the arrival' by Yan Qing (155.556 s
    canonical) vs the Dan Romer soundtrack file (137 s advertised). Under the P3.4
    containment title term ("in ashford" is a foreign work marker, not ignorable
    extra tokens) it sinks to the REJECTED band - and the evidence gate would cap
    it below auto regardless."""
    target = TargetTrack(
        artist_name="Yan Qing", track_title="the arrival", duration_seconds=155.556
    )
    incident = _file(
        "@@yuqfj\\Fab \\Dan Romer\\Dan Romer - A Knight of the Seven Kingdoms "
        "(Season 1)_2026_FLAC 24bit-48kHz\\02. Arrival in Ashford.flac",
        "Dan Romer - A Knight of the Seven Kingdoms (Season 1)_2026_FLAC 24bit-48kHz",
        duration=137.0,
        username="Fabrizio83a",
    )
    ranked = await TrackMatcher(_store()).rank(
        target, [incident], snapshot=policy_snapshot()
    )
    assert len(ranked) == 1
    assert ranked[0].tier != "auto"
    assert ranked[0].final_score < 0.70
