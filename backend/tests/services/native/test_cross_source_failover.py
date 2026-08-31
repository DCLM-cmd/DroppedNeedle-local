"""An exhausted source falls over to the other one instead of giving up.

A Usenet release that stalls or gets aborted says nothing about whether Soulseek has
the album, but failover refused to leave the task's own source, so the task settled
straight to "No working source found on Soulseek or Usenet" while scored Soulseek
candidates sat unused in the very same pooled search job.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.native.download_orchestrator import DownloadOrchestrator


def _candidate(source, username, index):
    return SimpleNamespace(
        source=source, username=username, parent_directory=f"{username}/dir",
        final_score=0.9 - index / 100, filename=f"{username}.flac",
    )


def _orchestrator(candidates, *, enabled=("soulseek", "usenet")):
    orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
    orch._store = AsyncMock()
    orch._store.get_search_job_candidates = AsyncMock(return_value=candidates)
    orch._source_enabled = lambda source: source in enabled
    orch._candidate_source_identity = lambda cand: f"{cand.source}:{cand.username}"
    # async, and (task, cand): upstream moved the quality re-gate onto the task's
    # STORED snapshot, so it reads persisted state rather than a bare track count.
    async def _passes(task, cand):  # noqa: ANN001, ARG001
        return True

    orch._candidate_passes_quality = _passes
    orch._candidate_quality_details = lambda cand: {"rank": 1}
    return orch


def _task(source, candidate_index):
    return SimpleNamespace(
        id="task-1", search_job_id="job-1", source=source,
        candidate_index=candidate_index, track_count=10,
    )


# Pooled jobs are source-GROUPED: Soulseek first, then Usenet.
POOL = [
    _candidate("soulseek", "peer-a", 0),
    _candidate("soulseek", "peer-b", 1),
    _candidate("usenet", "rls-a", 2),
    _candidate("usenet", "rls-b", 3),
]


@pytest.mark.asyncio
async def test_the_same_source_is_preferred_while_it_has_candidates() -> None:
    orch = _orchestrator(POOL)

    idx, cand = await orch._next_candidate_entry(_task("usenet", 2), set())

    assert (idx, cand.source, cand.username) == (3, "usenet", "rls-b")


@pytest.mark.asyncio
async def test_an_exhausted_usenet_task_crosses_to_soulseek() -> None:
    """The regression. Note the Soulseek candidates sit at LOWER indices, so the
    cross-source pass has to scan from the start of the pool."""
    orch = _orchestrator(POOL)

    idx, cand = await orch._next_candidate_entry(_task("usenet", 3), set())

    assert (idx, cand.source, cand.username) == (0, "soulseek", "peer-a")


@pytest.mark.asyncio
async def test_crossing_skips_identities_already_tried() -> None:
    orch = _orchestrator(POOL)

    idx, cand = await orch._next_candidate_entry(
        _task("usenet", 3), {"soulseek:peer-a"}
    )

    assert (idx, cand.username) == (1, "peer-b")


@pytest.mark.asyncio
async def test_a_disabled_source_is_never_crossed_to() -> None:
    orch = _orchestrator(POOL, enabled=("usenet",))

    assert await orch._next_candidate_entry(_task("usenet", 3), set()) is None


@pytest.mark.asyncio
async def test_nothing_left_anywhere_still_gives_up() -> None:
    orch = _orchestrator(POOL)

    assert await orch._next_candidate_entry(
        _task("usenet", 3), {"soulseek:peer-a", "soulseek:peer-b"}
    ) is None


@pytest.mark.asyncio
async def test_the_preferred_quality_wait_never_crosses_sources() -> None:
    """``lower_than`` is the within-pool quality wait: it trades quality down inside
    one source, which says nothing about switching source."""
    orch = _orchestrator(POOL)
    orch._candidate_quality_details = lambda cand: {"rank": 5}

    assert await orch._next_candidate_entry(
        _task("usenet", 3), set(), lower_than=1
    ) is None


@pytest.mark.asyncio
async def test_a_soulseek_task_crosses_to_usenet_too() -> None:
    """The rule is symmetric - it is about exhaustion, not about which source."""
    orch = _orchestrator(POOL)

    idx, cand = await orch._next_candidate_entry(_task("soulseek", 1), set())

    assert (idx, cand.source) == (2, "usenet")


# ---- searching the sources that were never searched ----------------------------

def _scored(source, username, score=0.8):
    return SimpleNamespace(
        source=source, username=username, parent_directory=f"{username}/dir",
        final_score=score, filename=f"{username}.flac", tier="auto",
    )


def _exhausting_orchestrator(pool, found, *, enabled=("soulseek", "usenet")):
    orch = _orchestrator(pool, enabled=enabled)
    orch._source_priority = ["usenet", "soulseek"]
    orch._store.get_search_job_candidates = AsyncMock(return_value=pool)
    orch._store.set_search_job_candidates = AsyncMock()
    orch._search_and_score = AsyncMock(side_effect=lambda task, source: found.get(source, []))
    return orch


@pytest.mark.asyncio
async def test_a_source_that_was_never_searched_is_searched_at_exhaustion() -> None:
    """The live regression: source_priority is ['usenet','soulseek'] and the Usenet
    auto-accept meant Soulseek was never searched at all. When the Usenet release
    failed, the task reported "no working source on Soulseek or Usenet" having asked
    exactly one of them."""
    pool = [_candidate("usenet", "rls-a", 0)]
    orch = _exhausting_orchestrator(pool, {"soulseek": [_scored("soulseek", "peer-a")]})

    grew = await orch._extend_pool_with_unsearched_sources(_task("usenet", 0))

    assert grew
    orch._search_and_score.assert_awaited_once()
    assert orch._search_and_score.await_args.args[1] == "soulseek"
    saved = orch._store.set_search_job_candidates.await_args.args[1]
    assert [c.source for c in saved] == ["usenet", "soulseek"]


@pytest.mark.asyncio
async def test_a_source_already_in_the_pool_is_not_searched_again() -> None:
    pool = [_candidate("usenet", "rls-a", 0), _candidate("soulseek", "peer-a", 1)]
    orch = _exhausting_orchestrator(pool, {"soulseek": [_scored("soulseek", "peer-b")]})

    assert not await orch._extend_pool_with_unsearched_sources(_task("usenet", 0))
    orch._search_and_score.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_disabled_source_is_never_searched_late() -> None:
    pool = [_candidate("usenet", "rls-a", 0)]
    orch = _exhausting_orchestrator(pool, {"soulseek": [_scored("soulseek", "p")]},
                                    enabled=("usenet",))

    assert not await orch._extend_pool_with_unsearched_sources(_task("usenet", 0))
    orch._search_and_score.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_late_search_that_finds_nothing_does_not_grow_the_pool() -> None:
    """Then the caller settles exactly as before - no retry loop."""
    pool = [_candidate("usenet", "rls-a", 0)]
    orch = _exhausting_orchestrator(pool, {"soulseek": []})

    assert not await orch._extend_pool_with_unsearched_sources(_task("usenet", 0))
    orch._store.set_search_job_candidates.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_late_search_is_survivable() -> None:
    """One dead source must not sink the settle path."""
    pool = [_candidate("usenet", "rls-a", 0)]
    orch = _exhausting_orchestrator(pool, {})
    orch._search_and_score = AsyncMock(side_effect=RuntimeError("slskd down"))

    assert not await orch._extend_pool_with_unsearched_sources(_task("usenet", 0))


@pytest.mark.asyncio
async def test_without_a_search_job_there_is_nothing_to_extend() -> None:
    orch = _exhausting_orchestrator([], {})
    task = _task("usenet", 0)
    task.search_job_id = None

    assert not await orch._extend_pool_with_unsearched_sources(task)
