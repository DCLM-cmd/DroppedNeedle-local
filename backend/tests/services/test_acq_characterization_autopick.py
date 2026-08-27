"""Phase 1 characterization: download-orchestrator auto-pick routing, the shared
quality-pool fallback deadline, and Free Music ranking.

These pin TODAY'S behavior of ``DownloadOrchestrator._search_score_autopick``
(source-priority walk, review pooling, upgrade special-case), the
``quality_pool_key`` zero-byte fallback deadline in ``_prepare_candidate_state``,
and ``FreeMusicService._rank`` - BEFORE any acquisition-policy change. Production
modules are untouched; collaborators are stubs/mock strategies, plus the REAL
``DownloadStore`` against a tmp_path SQLite file wherever durable state matters
(house pattern from test_download_orchestrator.py).

Characterization style note: where an assertion looks arbitrary (exact error
strings, lowercase published payloads), it copies what the code does today on
purpose - do not "fix" either side without checking.
"""

import sqlite3
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.native.download_orchestrator as orchestrator_module
from infrastructure.persistence.download_store import DownloadStore
from models.download import DownloadTask, ScoredCandidate
from models.download_manifest import ManifestCodec
from models.free_music import FreeMusicCandidate
from repositories.protocols.download_client import DownloadSearchResult
from services.native.acquisition.status import DownloadStatus
from services.native.download_orchestrator import DownloadOrchestrator
from services.native.free_music_service import FreeMusicService


# --- builders -------------------------------------------------------------------


def _seed_auth_users(db_path) -> None:
    """DownloadStore turns on foreign_keys (task.user_id -> auth_users ON DELETE
    CASCADE), so every test database needs its user row first - same as the
    sibling orchestrator tests."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS auth_users "
            "(id TEXT PRIMARY KEY, username TEXT, role TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO auth_users VALUES ('user-a', 'alice', 'user')"
        )
        conn.commit()
    finally:
        conn.close()


def _store(tmp_path) -> DownloadStore:
    db_path = tmp_path / "library.db"
    store = DownloadStore(db_path=db_path, write_lock=threading.Lock())
    _seed_auth_users(db_path)
    return store


def _candidate(
    tier: str,
    username: str,
    *,
    final_score: float,
    source: str = "soulseek",
    extension: str = "flac",
) -> ScoredCandidate:
    """One minimal scored candidate: a single file in its own folder."""
    result = DownloadSearchResult(
        username=username,
        filename=f"{username}/01.{extension}",
        parent_directory=username,
        size=100,
        extension=extension,
        duration=None,
    )
    return ScoredCandidate(
        source=source,
        username=username,
        parent_directory=username,
        files=[result],
        coherence=final_score,
        file_confidence=final_score,
        final_score=final_score,
        tier=tier,
    )


def _quality_candidate(
    username: str,
    *,
    bit_depth: int,
    sample_rate: int,
    queue_length: int,
    final_score: float = 0.9,
) -> ScoredCandidate:
    result = DownloadSearchResult(
        username=username,
        filename=f"{username}/01.flac",
        parent_directory=username,
        size=100,
        extension="flac",
        bit_depth=bit_depth,
        sample_rate=sample_rate,
        queue_length=queue_length,
        duration=None,
    )
    return ScoredCandidate(
        username=username,
        parent_directory=username,
        files=[result],
        coherence=final_score,
        file_confidence=final_score,
        final_score=final_score,
        tier="auto",
    )


class _FrozenClock:
    """Replaces the time module seen by the orchestrator so epoch deadlines are
    exact. Only ``time()`` is accessed on this object; the store keeps its own
    stdlib reference."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def time(self) -> float:
        return self.now


def _orch(
    tmp_path,
    *,
    store: DownloadStore | None = None,
    soulseek_enabled: bool = True,
    usenet_enabled: bool = False,
    attach_usenet: bool = False,
    preferred_minutes: float = 15.0,
    max_failover: int = 3,
) -> DownloadOrchestrator:
    """Minimal orchestrator for the autopick / candidate-state seams. Source
    strategies keep their identities; their ``search_and_score`` gets swapped for
    an AsyncMock that starts un-awaited."""
    kwargs = {}
    if attach_usenet:
        kwargs.update(
            usenet_indexer=MagicMock(),
            usenet_client=MagicMock(),
            usenet_scorer=MagicMock(),
        )
    return DownloadOrchestrator(
        client=MagicMock(),
        indexer=MagicMock(),
        download_store=store or _store(tmp_path),
        file_processor=MagicMock(),
        library_manager=MagicMock(),
        scorer=MagicMock(),
        track_matcher=MagicMock(),
        manifest_codec=ManifestCodec(),
        event_bus=AsyncMock(),
        staging_path=tmp_path / "staging",
        naming_template="{artist}/{album}/{title}.{ext}",
        soulseek_enabled=soulseek_enabled,
        usenet_enabled=usenet_enabled,
        preferred_quality_wait_minutes=preferred_minutes,
        max_failover_attempts=max_failover,
        **kwargs,
    )


def _wire_search(orch: DownloadOrchestrator, soulseek=(), usenet=None) -> None:
    orch._strategies["soulseek"].search_and_score = AsyncMock(
        return_value=list(soulseek)
    )
    if "usenet" in orch._strategies:
        orch._strategies["usenet"].search_and_score = AsyncMock(
            return_value=list(usenet or [])
        )


async def _task(store: DownloadStore, **overrides) -> DownloadTask:
    kwargs = dict(
        user_id="user-a",
        release_group_mbid="rg-1",
        artist_name="Artist",
        album_title="Album",
        year=2020,
        track_count=1,
    )
    kwargs.update(overrides)
    return await store.create_task(**kwargs)


async def _link(store: DownloadStore, task_id: str, candidates: list):
    job = await store.create_search_job(
        user_id="user-a",
        artist_name="Artist",
        album_title="Album",
        year=2020,
        track_count=1,
        release_group_mbid="rg-1",
        search_query="Artist - Album",
    )
    await store.set_search_job_candidates(job.id, candidates)
    first = candidates[0]
    await store.link_picked_candidate(
        task_id,
        job.id,
        0,
        first.username,
        first.parent_directory,
        first.final_score,
    )
    return job


# --- _search_score_autopick: source-priority walk -------------------------------


@pytest.mark.asyncio
async def test_autopick_stops_at_first_auto_and_never_searches_later_source(
    tmp_path,
):
    store = _store(tmp_path)
    orch = _orch(tmp_path, store=store, usenet_enabled=True, attach_usenet=True)
    manual_ss = _candidate("manual", "ss-manual", final_score=0.6)
    auto_ss = _candidate("auto", "ss-auto", final_score=0.9)
    auto_u = _candidate("auto", "u-auto", final_score=0.95, source="usenet")
    _wire_search(orch, soulseek=[manual_ss, auto_ss], usenet=[auto_u])
    task = await _task(store)

    picked = await orch._search_score_autopick(task)

    assert picked is True
    assert not orch._strategies["usenet"].search_and_score.await_count

    # Pooled candidates preserve both searched groups, in search order...
    job_id = (await store.get_tasks([task.id]))[task.id].search_job_id
    job = await store.get_search_job(job_id)
    pooled = await store.get_search_job_candidates(job.id)
    assert [c.username for c in pooled] == ["ss-manual", "ss-auto"]
    # ...and the GLOBAL pooled index of the winner (soulseek group offset + spot).
    final = await store.get_task(task.id)
    assert final.search_job_id == job.id
    assert final.candidate_index == 1
    assert final.source_username == "ss-auto"
    assert final.source_directory == "ss-auto"
    assert final.preflight_score == pytest.approx(0.9)
    assert final.source == "soulseek"
    assert final.download_client == "slskd"


@pytest.mark.asyncio
async def test_autopick_picks_second_source_with_global_index_and_sabnzbd(
    tmp_path,
):
    store = _store(tmp_path)
    orch = _orch(tmp_path, store=store, usenet_enabled=True, attach_usenet=True)
    manual_ss = _candidate("manual", "ss-manual", final_score=0.6)
    auto_u = _candidate("auto", "u-auto", final_score=0.75, source="usenet")
    tail_u = _candidate("manual", "u-tail", final_score=0.55, source="usenet")
    _wire_search(orch, soulseek=[manual_ss], usenet=[auto_u, tail_u])
    task = await _task(store)

    picked = await orch._search_score_autopick(task)

    assert picked is True
    orch._strategies["soulseek"].search_and_score.assert_awaited_once()
    orch._strategies["usenet"].search_and_score.assert_awaited_once()
    final = await store.get_task(task.id)
    assert final.candidate_index == 1  # len(soulseek group)=1 + position 0
    assert final.source == "usenet"
    assert final.download_client == "sabnzbd"

    # Search-job creation carries the task's identity and the "<artist> - <album>" query.
    job = await store.get_search_job(final.search_job_id)
    assert job.user_id == "user-a"
    assert job.artist_name == "Artist"
    assert job.album_title == "Album"
    assert job.year == 2020
    assert job.track_count == 1
    assert job.release_group_mbid == "rg-1"
    assert job.search_query == "Artist - Album"


# --- _search_score_autopick: review pooling -------------------------------------


@pytest.mark.asyncio
async def test_no_auto_anywhere_pools_all_groups_for_review(tmp_path):
    store = _store(tmp_path)
    orch = _orch(tmp_path, store=store, usenet_enabled=True, attach_usenet=True)
    m_ss = _candidate("manual", "ss-m", final_score=0.6)
    m_u1 = _candidate("manual", "u-m1", final_score=0.55, source="usenet")
    m_u2 = _candidate("rejected", "u-m2", final_score=0.4, source="usenet")
    _wire_search(orch, soulseek=[m_ss], usenet=[m_u1, m_u2])
    task = await _task(store)

    picked = await orch._search_score_autopick(task)

    assert picked is False
    job_id = (await store.get_tasks([task.id]))[task.id].search_job_id
    pooled = await store.get_search_job_candidates(job_id)
    assert [c.username for c in pooled] == ["ss-m", "u-m1", "u-m2"]

    job = await store.get_search_job(job_id)
    assert job.status == "completed"
    # Candidate link stays open (review must pick); AWAITING_REVIEW is SSE-only.
    final = await store.get_task(task.id)
    assert final.search_job_id == job_id
    assert final.candidate_index is None
    assert final.status == "queued"
    orch._bus.publish.assert_awaited_once_with(
        f"download:{task.id}",
        "status",
        {"status": DownloadStatus.AWAITING_REVIEW, "search_job_id": job_id},
    )


# --- _search_score_autopick: nothing usable + upgrade special-case --------------


@pytest.mark.asyncio
async def test_empty_sources_fail_and_name_only_the_enabled_one(tmp_path):
    store = _store(tmp_path)
    # Usenet strategy attached but DISABLED: never searched, never named.
    orch = _orch(tmp_path, store=store, usenet_enabled=False, attach_usenet=True)
    # The completed-job write happens before any task linkage: observe it directly.
    job_statuses: list[str] = []
    store.update_search_job_status = AsyncMock(
        side_effect=lambda _jid, status, **_kw: job_statuses.append(status)
    )
    _wire_search(orch, soulseek=[], usenet=[])
    task = await _task(store)

    picked = await orch._search_score_autopick(task)

    assert picked is False
    assert not orch._strategies["usenet"].search_and_score.await_count
    # An empty walk leaves NO candidate link: the task never learns the job id.
    final = await store.get_task(task.id)
    assert final.search_job_id is None
    assert final.status == "failed"
    assert final.error_message == "No matching release found on Soulseek"
    assert job_statuses == ["completed"]
    orch._bus.publish.assert_awaited_once_with(
        f"download:{task.id}",
        "complete",
        {"status": DownloadStatus.FAILED, "error": "no match"},
    )


@pytest.mark.asyncio
async def test_empty_searched_sources_message_names_both(tmp_path):
    store = _store(tmp_path)
    orch = _orch(tmp_path, store=store, usenet_enabled=True, attach_usenet=True)
    _wire_search(orch, soulseek=[], usenet=[])
    task = await _task(store)

    await orch._search_score_autopick(task)

    final = await store.get_task(task.id)
    assert final.status == "failed"
    assert final.error_message == "No matching release found on Soulseek or Usenet"


@pytest.mark.asyncio
async def test_upgrade_origin_cancels_instead_of_failing(tmp_path):
    store = _store(tmp_path)
    orch = _orch(tmp_path, store=store, usenet_enabled=True, attach_usenet=True)
    _wire_search(orch, soulseek=[], usenet=[])
    task = await _task(store, origin="upgrade")

    picked = await orch._search_score_autopick(task)

    assert picked is False
    final = await store.get_task(task.id)
    assert final.status == "cancelled"
    assert final.error_message == "No better copy found"
    assert final.cancelled_at is not None
    orch._bus.publish.assert_awaited_once_with(
        f"download:{task.id}",
        "complete",
        {"status": DownloadStatus.CANCELLED, "error": "no better copy found"},
    )


# --- quality-pool zero-byte fallback deadline (_prepare_candidate_state) --------


@pytest.mark.asyncio
async def test_pool_change_starts_shared_deadline_when_lower_entry_exists(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    orch = _orch(tmp_path, store=store, preferred_minutes=2.0, max_failover=3)
    clock = _FrozenClock(1000.0)
    monkeypatch.setattr(orchestrator_module, "time", clock)
    candidates = [
        _quality_candidate("hires-a", bit_depth=24, sample_rate=48_000, queue_length=8),
        _quality_candidate("hires-b", bit_depth=24, sample_rate=48_000, queue_length=2),
        _quality_candidate("redbook", bit_depth=16, sample_rate=44_100, queue_length=0),
    ]
    task = await _task(store, status="downloading")
    job = await _link(store, task.id, candidates)
    task = await store.get_task(task.id)

    prepared = await orch._prepare_candidate_state(task)

    # Pool changed (fresh task had no key) AND a strictly lower-resolution entry
    # follows within the failover budget -> countdown = frozen now + wait minutes.
    assert prepared.preferred_quality_fallback_at == pytest.approx(1120.0)
    assert prepared.quality_pool_key == "step:9996"
    assert prepared.quality_format == "flac"
    assert prepared.quality_bit_depth == 24
    assert prepared.quality_sample_rate == 48_000
    assert prepared.advertised_queue_depth == 8
    assert prepared.attempt_number == 1
    assert prepared.attempt_total == 3  # 2 same-source successors fit under cap 3
    assert prepared.has_next_source is True
    assert job.id == prepared.search_job_id


@pytest.mark.asyncio
async def test_same_pool_relink_preserves_existing_deadline(tmp_path, monkeypatch):
    store = _store(tmp_path)
    orch = _orch(tmp_path, store=store, preferred_minutes=2.0)
    clock = _FrozenClock(1000.0)
    monkeypatch.setattr(orchestrator_module, "time", clock)
    candidates = [
        _quality_candidate("hires-a", bit_depth=24, sample_rate=48_000, queue_length=8),
        _quality_candidate("hires-b", bit_depth=24, sample_rate=48_000, queue_length=2),
        _quality_candidate("redbook", bit_depth=16, sample_rate=44_100, queue_length=0),
    ]
    task = await _task(store, status="downloading")
    await _link(store, task.id, candidates)
    first = await orch._prepare_candidate_state(await store.get_task(task.id))
    assert first.preferred_quality_fallback_at == pytest.approx(1120.0)

    clock.now += 50.0
    second = await orch._link_candidate_entry(first, (1, candidates[1]))

    # Same resolution pool re-link: NO new countdown despite a later fake clock,
    # zero bytes transferred, and another lower-pool entry still available.
    assert second.preferred_quality_fallback_at == pytest.approx(1120.0)
    assert second.quality_pool_key == first.quality_pool_key == "step:9996"
    assert second.downloaded_bytes == 0
    assert second.has_next_source is True


@pytest.mark.asyncio
async def test_pool_change_without_lower_entry_records_no_deadline(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    orch = _orch(tmp_path, store=store, preferred_minutes=2.0)
    clock = _FrozenClock(1000.0)
    monkeypatch.setattr(orchestrator_module, "time", clock)
    candidates = [
        _quality_candidate("redbook", bit_depth=16, sample_rate=44_100, queue_length=0),
    ]
    task = await _task(store, status="downloading")
    await _link(store, task.id, candidates)
    task = await store.get_task(task.id)

    prepared = await orch._prepare_candidate_state(task)

    assert prepared.quality_pool_key == "step:9996"
    assert prepared.preferred_quality_fallback_at is None
    assert prepared.has_next_source is False


@pytest.mark.asyncio
async def test_transferred_bytes_clear_deadline_but_keep_pool_key(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    orch = _orch(tmp_path, store=store, preferred_minutes=2.0)
    clock = _FrozenClock(1000.0)
    monkeypatch.setattr(orchestrator_module, "time", clock)
    candidates = [
        _quality_candidate("hires-a", bit_depth=24, sample_rate=48_000, queue_length=8),
        _quality_candidate("redbook", bit_depth=16, sample_rate=44_100, queue_length=0),
    ]
    task = await _task(store, status="downloading")
    await _link(store, task.id, candidates)
    task = await store.get_task(task.id)

    established = await orch._prepare_candidate_state(task)
    assert established.preferred_quality_fallback_at is not None

    clock.now += 30.0
    await store.update_status(established.id, established.status, downloaded_bytes=250)
    refreshed = await orch._prepare_candidate_state(await store.get_task(task.id))

    assert refreshed.downloaded_bytes == 250
    assert refreshed.preferred_quality_fallback_at is None
    assert refreshed.quality_pool_key == "step:9996"


@pytest.mark.asyncio
async def test_attempt_total_is_capped_by_max_failover_budget(tmp_path):
    store = _store(tmp_path)
    orch = _orch(
        tmp_path,
        store=store,
        max_failover=2,
        preferred_minutes=2.0,
    )
    candidates = [
        _quality_candidate("hires-a", bit_depth=24, sample_rate=48_000, queue_length=8),
        _quality_candidate("hires-b", bit_depth=24, sample_rate=48_000, queue_length=2),
        _quality_candidate("hires-c", bit_depth=24, sample_rate=48_000, queue_length=1),
    ]
    task = await _task(store, status="downloading")
    await _link(store, task.id, candidates)
    task = await store.get_task(task.id)

    prepared = await orch._prepare_candidate_state(task)

    # Three eligible same-source entries exist, but the budget caps the projection.
    assert prepared.attempt_number == 1
    assert prepared.attempt_total == 2
    assert prepared.has_next_source is True

    # And a budget of 1 promises nothing at all.
    tight_orch = _orch(tmp_path, store=_store(tmp_path), max_failover=1)
    other = [
        _quality_candidate("only", bit_depth=16, sample_rate=44_100, queue_length=0),
    ]
    tight_store = tight_orch._store
    tight_task = await _task(tight_store, status="downloading")
    await _link(tight_store, tight_task.id, other)
    tight = await tight_orch._prepare_candidate_state(
        await tight_store.get_task(tight_task.id)
    )
    assert tight.attempt_number == 1
    assert tight.attempt_total == 1
    assert tight.has_next_source is False


# --- FreeMusicService quality ordering (post-cutover contract) -------------------
# Phase-3 replaced `_rank(preferred, count)` with the snapshot-driven
# `_quality_sort_key(candidate, snapshot, track_count)`: MusicBrainz count
# agreement stays FIRST; the global preference step (then certainty, then size)
# replaces free_music.preferred_format, which is no longer read for ranking.


def _fm(extension: str, track_count: int, size_bytes: int) -> FreeMusicCandidate:
    return FreeMusicCandidate(
        identifier=f"id-{extension}-{track_count}-{size_bytes}",
        title="Album",
        creator="Creator",
        licence_url="https://creativecommons.org/licenses/by-sa/4.0/",
        format=extension.upper(),
        extension=extension,
        track_count=track_count,
        size_bytes=size_bytes,
        filenames=[f"01.{extension}"],
    )


def _snap(**overrides):
    from api.v1.schemas.settings import DownloadPolicySettings
    from services.native.acquisition.quality import build_snapshot

    return build_snapshot(
        DownloadPolicySettings(
            quality_min="low",
            quality_preference_order=[
                "lossless", "mp3_320", "mp3_256", "mp3_192", "low",
            ],
            **overrides,
        )
    )


def _fm_ranked(candidates, snapshot, track_count: int):
    return sorted(
        candidates,
        key=lambda c: FreeMusicService._quality_sort_key(c, snapshot, track_count),
    )


def test_free_music_track_count_agreement_beats_step_and_tier():
    # MB says 10 tracks: a matching MP3 outranks a two-track FLAC sampler even
    # though FLAC sits on the preferred step - agreement is key slot 0.
    mp3_full = _fm("mp3", 10, 500)
    flac_sampler = _fm("flac", 2, 8000)
    snap = _snap()
    assert _fm_ranked([flac_sampler, mp3_full], snap, 10) == [mp3_full, flac_sampler]
    keys = (
        FreeMusicService._quality_sort_key(mp3_full, snap, 10),
        FreeMusicService._quality_sort_key(flac_sampler, snap, 10),
    )
    assert keys[0][0] == 0 and keys[1][0] == 8


def test_free_music_preferred_step_then_certainty_then_size_break_ties():
    full_mp3 = _fm("mp3", 10, 500)
    full_flac = _fm("flac", 10, 9000)
    balanced = _snap(
        lossless_preference="cd",
        lossless_max_bit_depth=16,
        lossless_max_sample_rate_hz=48000,
    )
    # Step 0 lossless beats the MP3's 'low' projection step (4).
    assert _fm_ranked([full_flac, full_mp3], balanced, 10) == [full_flac, full_mp3]
    # Full step tie under 'highest': the larger (better-encoded) copy first.
    small_flac = _fm("flac", 10, 300)
    big_flac = _fm("flac", 10, 900)
    legacy = _snap(lossless_preference="highest")
    assert _fm_ranked([small_flac, big_flac], legacy, 10) == [big_flac, small_flac]


def test_free_music_capped_policy_relegates_proven_hires_to_last():
    # A Balanced-style store proves "24bit Flac" exceeds its 16-bit cap even
    # with no sample-rate evidence: it sorts strictly AFTER lossy unknowns.
    flac24 = _fm("flac", 10, 400_000_000)
    flac24.format = "24bit Flac"
    mp3_full = _fm("mp3", 10, 5_000_000)
    plain_flac = _fm("flac", 10, 250_000_000)
    balanced = _snap(
        lossless_preference="cd",
        lossless_max_bit_depth=16,
        lossless_max_sample_rate_hz=48000,
    )
    assert _fm_ranked([flac24, mp3_full, plain_flac], balanced, 10) == [
        plain_flac,
        mp3_full,
        flac24,
    ]


def test_free_music_flat_first_key_when_musicbrainz_count_unknown():
    # track_count=0 flattens the FIRST key to 0 for every candidate; policy
    # steps decide afterwards exactly as they do when MB agreed.
    three_tracks = _fm("mp3", 3, 300)
    seven_tracks = _fm("mp3", 7, 900)
    snap = _snap()
    k3 = FreeMusicService._quality_sort_key(three_tracks, snap, 0)
    k7 = FreeMusicService._quality_sort_key(seven_tracks, snap, 0)
    assert k3[0] == 0 and k7[0] == 0
    assert _fm_ranked([three_tracks, seven_tracks], snap, 0) == [
        seven_tracks,
        three_tracks,
    ]
