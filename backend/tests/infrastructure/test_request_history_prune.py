"""prune_old_terminal_requests × wanted watches (Wanted plan §4.4): a terminal
request past retention survives while its mbid has a live (watching/dormant)
watch - pruning it would orphan the watch and break the status-flip linkage -
and the guard degrades cleanly on a DB without the wanted_watches table."""

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from infrastructure.persistence.request_history import RequestHistoryStore
from infrastructure.persistence.wanted_store import WantedStore


def _old_iso(days: int = 300) -> str:
    return datetime.fromtimestamp(
        time.time() - days * 86400, tz=timezone.utc
    ).isoformat()


async def _seed_terminal_request(store: RequestHistoryStore, mbid: str) -> None:
    await store.async_record_request(mbid, "Artist", "Album", user_id="user-a")
    # push it terminal + old with raw sqlite (requested_at drives the age check)
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE request_history SET status = 'failed', requested_at = ?,"
        " completed_at = ? WHERE musicbrainz_id_lower = ?",
        (_old_iso(), _old_iso(), mbid.lower()),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_watched_terminal_request_survives_prune_unwatched_twin_dies(
    tmp_path: Path,
):
    lock = threading.Lock()
    db_path = tmp_path / "library.db"
    requests = RequestHistoryStore(db_path=db_path, write_lock=lock)
    wanted = WantedStore(db_path=db_path, write_lock=lock)

    await _seed_terminal_request(requests, "rg-watched")
    await _seed_terminal_request(requests, "rg-dormant")
    await _seed_terminal_request(requests, "rg-stopped")
    await _seed_terminal_request(requests, "rg-unwatched")

    for mbid in ("rg-watched", "rg-dormant", "rg-stopped"):
        await wanted.create_watch(
            release_group_mbid=mbid,
            user_id="user-a",
            artist_name="Artist",
            album_title="Album",
            kind="missing",
            next_check_at=time.time(),
        )
    await wanted.record_cycle(
        "rg-dormant",
        outcome="no_results",
        next_check_at=time.time(),
        quiet=True,
        go_dormant=True,
    )
    await wanted.stop_watch("rg-stopped")

    pruned = await requests.prune_old_terminal_requests(180)

    # live watches (watching + dormant) protect their rows; stopped does not
    assert pruned == 2
    assert await requests.async_get_record("rg-watched") is not None
    assert await requests.async_get_record("rg-dormant") is not None
    assert await requests.async_get_record("rg-stopped") is None
    assert await requests.async_get_record("rg-unwatched") is None


@pytest.mark.asyncio
async def test_prune_still_works_without_the_wanted_table(tmp_path: Path):
    requests = RequestHistoryStore(
        db_path=tmp_path / "solo.db", write_lock=threading.Lock()
    )
    await _seed_terminal_request(requests, "rg-old")
    assert await requests.prune_old_terminal_requests(180) == 1
    assert await requests.async_get_record("rg-old") is None


@pytest.mark.asyncio
async def test_requested_mbids_include_every_nonterminal_ui_state(tmp_path: Path):
    requests = RequestHistoryStore(
        db_path=tmp_path / "requests.db", write_lock=threading.Lock()
    )
    statuses = {
        "rg-pending": "pending",
        "rg-downloading": "downloading",
        "rg-awaiting": "awaiting_approval",
        "rg-queued": "queued",
        "rg-failed": "failed",
    }
    for mbid, status in statuses.items():
        await requests.async_record_request(mbid, "Artist", "Album")
        await requests.async_update_status(mbid, status)

    assert await requests.async_get_requested_mbids() == {
        "rg-pending",
        "rg-downloading",
        "rg-awaiting",
        "rg-queued",
    }


@pytest.mark.asyncio
async def test_prune_removes_requester_rows_orphaned_by_terminal_cleanup(
    tmp_path: Path,
) -> None:
    requests = RequestHistoryStore(
        db_path=tmp_path / "requests.db", write_lock=threading.Lock()
    )
    await _seed_terminal_request(requests, "rg-old")
    with sqlite3.connect(requests.db_path) as conn:
        conn.execute(
            "INSERT INTO request_history_requesters "
            "(user_id, musicbrainz_id_lower, requested_at, requested_by_name) "
            "VALUES (?, ?, ?, ?)",
            ("orphan-user", "orphan-rg", _old_iso(), "Orphan"),
        )

    assert await requests.prune_old_terminal_requests(180) == 1
    with sqlite3.connect(requests.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM request_history_requesters "
            "WHERE musicbrainz_id_lower = 'orphan-rg'"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_cancellation_decisions_are_atomic_for_membership_and_task_state(
    tmp_path: Path,
) -> None:
    requests = RequestHistoryStore(
        db_path=tmp_path / "requests.db", write_lock=threading.Lock()
    )
    begun = await requests.async_record_request(
        "rg-shared",
        "Artist",
        "Album",
        user_id="user-a",
        initial_status="pending",
    )
    assert begun is not None
    await requests.async_update_download_task_id("rg-shared", "task-shared")
    assert await requests.async_add_requester("rg-shared", "user-b", "Second")

    outsider = await requests.async_prepare_requester_cancel("outsider", "rg-shared")
    assert outsider.action == "denied"
    assert outsider.prior_status == "pending"

    detached = await requests.async_prepare_requester_cancel("user-b", "rg-shared")
    assert detached.action == "detached"
    assert detached.prior_status == "pending"
    record = await requests.async_get_record("rg-shared")
    assert record is not None and record.status == "pending"
    assert await requests.async_requester_count("rg-shared") == 1

    cancel_task = await requests.async_prepare_requester_cancel("user-a", "rg-shared")
    assert cancel_task.action == "cancel_task"
    assert cancel_task.prior_status == "pending"
    record = await requests.async_get_record("rg-shared")
    assert record is not None and record.status == "cancelling"

    # The final requester remains attached while the native task cancellation
    # is in flight. A concurrent attach and a second cancel cannot revive or
    # duplicate this generation.
    assert not await requests.async_add_requester("rg-shared", "user-c")
    assert not await requests.async_is_requester("user-c", "rg-shared")
    second_cancel = await requests.async_prepare_requester_cancel("user-a", "rg-shared")
    assert second_cancel.action == "denied"
    assert second_cancel.prior_status == "cancelling"

    await requests.async_update_status("rg-shared", "pending")
    restored = await requests.async_get_record("rg-shared")
    assert restored is not None and restored.status == "pending"


@pytest.mark.asyncio
async def test_last_awaiting_approval_cancellation_is_terminal_in_one_step(
    tmp_path: Path,
) -> None:
    requests = RequestHistoryStore(
        db_path=tmp_path / "requests.db", write_lock=threading.Lock()
    )
    begun = await requests.async_record_request(
        "rg-awaiting",
        "Artist",
        "Album",
        user_id="user-a",
        initial_status="awaiting_approval",
    )
    assert begun is not None
    assert begun.generation == 1

    decision = await requests.async_prepare_requester_cancel("user-a", "rg-awaiting")
    assert decision.action == "cancelled"
    assert decision.prior_status == "awaiting_approval"
    record = await requests.async_get_record("rg-awaiting")
    assert record is not None and record.status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    ["imported", "failed", "cancelled", "incomplete", "rejected"],
)
async def test_terminal_shared_cancel_rejects_without_detaching(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    requests = RequestHistoryStore(
        db_path=tmp_path / "requests.db", write_lock=threading.Lock()
    )
    begun = await requests.async_record_request(
        "rg-terminal-shared",
        "Artist",
        "Album",
        user_id="user-a",
        initial_status="pending",
    )
    assert begun is not None
    assert await requests.async_add_requester(
        "rg-terminal-shared", "user-b", "Second listener"
    )
    assert await requests.async_update_status(
        "rg-terminal-shared",
        terminal_status,
        expected_generation=begun.generation,
    )

    decision = await requests.async_prepare_requester_cancel(
        "user-b", "rg-terminal-shared"
    )
    assert decision.generation == begun.generation
    assert decision.action == "denied"
    assert decision.prior_status == terminal_status
    assert await requests.async_requester_count("rg-terminal-shared") == 2
    assert await requests.async_is_requester("user-a", "rg-terminal-shared")
    assert await requests.async_is_requester("user-b", "rg-terminal-shared")


@pytest.mark.asyncio
async def test_queued_request_can_be_claimed_for_cancellation(tmp_path: Path) -> None:
    requests = RequestHistoryStore(
        db_path=tmp_path / "requests.db", write_lock=threading.Lock()
    )
    begun = await requests.async_record_request(
        "rg-queued",
        "Artist",
        "Album",
        user_id="user-a",
        initial_status="queued",
    )
    assert begun is not None

    decision = await requests.async_prepare_requester_cancel("user-a", "rg-queued")
    assert decision.action == "cancel_task"
    assert decision.prior_status == "queued"
    assert decision.generation == begun.generation
    record = await requests.async_get_record("rg-queued")
    assert record is not None and record.status == "cancelling"


@pytest.mark.asyncio
async def test_restore_request_status_is_conditional_on_cancelling_generation(
    tmp_path: Path,
) -> None:
    requests = RequestHistoryStore(
        db_path=tmp_path / "requests.db", write_lock=threading.Lock()
    )
    begun = await requests.async_record_request(
        "rg-restore",
        "Artist",
        "Album",
        user_id="user-a",
        initial_status="downloading",
    )
    assert begun is not None
    decision = await requests.async_prepare_requester_cancel("user-a", "rg-restore")
    assert decision.action == "cancel_task"
    assert decision.prior_status == "downloading"
    assert decision.generation == begun.generation

    assert await requests.async_restore_request_status(
        "rg-restore",
        "downloading",
        expected_generation=begun.generation,
    )
    restored = await requests.async_get_record("rg-restore")
    assert restored is not None and restored.status == "downloading"
    assert not await requests.async_restore_request_status(
        "rg-restore",
        "pending",
        expected_generation=begun.generation,
    )

    assert await requests.async_update_status(
        "rg-restore",
        "cancelled",
        expected_generation=begun.generation,
    )
    successor = await requests.async_record_request(
        "rg-restore",
        "Artist",
        "Album",
        user_id="user-a",
        initial_status="pending",
    )
    assert successor is not None
    assert successor.generation == begun.generation + 1
    assert not await requests.async_restore_request_status(
        "rg-restore",
        "downloading",
        expected_generation=begun.generation,
    )
    current = await requests.async_get_record("rg-restore")
    assert current is not None and current.status == "pending"
