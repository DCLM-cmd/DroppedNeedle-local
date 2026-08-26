import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.mbid_store import MBIDStore
from infrastructure.persistence.request_history import RequestHistoryStore


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["awaiting_approval", "queued"])
async def test_known_release_aliases_merge_into_canonical_request_history(
    tmp_path: Path, status: str
) -> None:
    path = tmp_path / "library.db"
    lock = threading.Lock()
    history = RequestHistoryStore(path, lock)
    aliases = MBIDStore(path, lock)
    await aliases.save_mbid_resolution_map({"release-1": "release-group-1"})
    await history.async_record_request(
        "release-group-1",
        "Old artist",
        "Old album",
        user_id="old-user",
        requested_by_name="Old user",
        initial_status="failed",
    )
    await history.async_record_request(
        "release-1",
        "Current artist",
        "Current album",
        user_id="requester",
        requested_by_name="Requester",
        initial_status=status,
    )

    assert await history.async_canonicalize_known_release_aliases() == 1
    assert await history.async_canonicalize_known_release_aliases() == 0
    assert await history.async_get_record("release-1") is None
    canonical = await history.async_get_record("release-group-1")
    assert canonical is not None
    assert canonical.status == status
    assert canonical.user_id == "requester"
    assert canonical.requested_by_name == "Requester"
    assert canonical.release_mbid == "release-1"
    assert await history.async_requester_count("release-group-1") == 1
    assert await history.async_is_requester("requester", "release-group-1")
    assert not await history.async_is_requester("old-user", "release-group-1")
    with sqlite3.connect(path) as conn:
        requesters = conn.execute(
            "SELECT user_id, musicbrainz_id_lower "
            "FROM request_history_requesters ORDER BY user_id"
        ).fetchall()
    assert requesters == [("requester", "release-group-1")]



@pytest.mark.asyncio
async def test_canonical_winner_keeps_the_source_release_as_edition(
    tmp_path: Path,
) -> None:
    path = tmp_path / "library.db"
    lock = threading.Lock()
    history = RequestHistoryStore(path, lock)
    aliases = MBIDStore(path, lock)
    await aliases.save_mbid_resolution_map({"release-1": "release-group-1"})
    await history.async_record_request(
        "release-1", "Artist", "Album", user_id="alias-user", initial_status="pending"
    )
    await history.async_record_request(
        "release-group-1",
        "Artist",
        "Album",
        user_id="canonical-user",
        initial_status="downloading",
    )
    await history.async_update_download_task_id("release-group-1", "task-1")

    assert await history.async_canonicalize_known_release_aliases(["release-1"]) == 1
    canonical = await history.async_get_record("release-group-1")
    assert canonical is not None
    assert canonical.status == "downloading"
    assert canonical.user_id == "canonical-user"
    assert canonical.download_task_id == "task-1"
    assert await history.async_requester_count("release-group-1") == 2
    assert await history.async_is_requester("alias-user", "release-group-1")
    assert await history.async_is_requester("canonical-user", "release-group-1")


@pytest.mark.asyncio
async def test_active_alias_winner_discards_terminal_canonical_listener(
    tmp_path: Path,
) -> None:
    path = tmp_path / "library.db"
    lock = threading.Lock()
    history = RequestHistoryStore(path, lock)
    aliases = MBIDStore(path, lock)
    await aliases.save_mbid_resolution_map({"release-1": "release-group-1"})
    await history.async_record_request(
        "release-1",
        "Active artist",
        "Active album",
        user_id="active-user",
        initial_status="pending",
    )
    await history.async_record_request(
        "release-group-1",
        "Terminal artist",
        "Terminal album",
        user_id="terminal-user",
        initial_status="failed",
    )

    assert await history.async_canonicalize_known_release_aliases(["release-1"]) == 1
    canonical = await history.async_get_record("release-group-1")
    assert canonical is not None
    assert canonical.status == "pending"
    assert canonical.user_id == "active-user"
    assert await history.async_requester_count("release-group-1") == 1
    assert await history.async_is_requester("active-user", "release-group-1")
    assert not await history.async_is_requester("terminal-user", "release-group-1")
    with sqlite3.connect(path) as conn:
        requesters = conn.execute(
            "SELECT user_id, musicbrainz_id_lower "
            "FROM request_history_requesters ORDER BY user_id"
        ).fetchall()
    assert requesters == [("active-user", "release-group-1")]


@pytest.mark.asyncio
async def test_active_active_merge_leaves_no_source_key_membership_orphans(
    tmp_path: Path,
) -> None:
    path = tmp_path / "library.db"
    lock = threading.Lock()
    history = RequestHistoryStore(path, lock)
    aliases = MBIDStore(path, lock)
    await aliases.save_mbid_resolution_map({"release-1": "release-group-1"})
    await history.async_record_request(
        "release-group-1",
        "Canonical artist",
        "Canonical album",
        user_id="canonical-user",
        initial_status="downloading",
    )
    await history.async_update_download_task_id("release-group-1", "task-c")
    await history.async_record_request(
        "release-1",
        "Alias artist",
        "Alias album",
        user_id="alias-user",
        requested_by_name="Alias user",
        initial_status="pending",
    )
    await history.async_dismiss_record("dismiss-user", "release-1")

    assert await history.async_canonicalize_known_release_aliases(["release-1"]) == 1

    canonical = await history.async_get_record("release-group-1")
    assert canonical is not None
    assert canonical.status == "downloading"
    assert await history.async_requester_count("release-group-1") == 2
    assert await history.async_is_requester("canonical-user", "release-group-1")
    assert await history.async_is_requester("alias-user", "release-group-1")
    with sqlite3.connect(path) as conn:
        requesters = conn.execute(
            "SELECT user_id, musicbrainz_id_lower "
            "FROM request_history_requesters ORDER BY user_id"
        ).fetchall()
        dismissals = conn.execute(
            "SELECT user_id, musicbrainz_id_lower "
            "FROM request_history_dismissals ORDER BY user_id"
        ).fetchall()
    # Every membership row lives under the canonical key; nothing is left
    # pointing at the discarded source alias.
    assert requesters == [
        ("alias-user", "release-group-1"),
        ("canonical-user", "release-group-1"),
    ]
    assert dismissals == [("dismiss-user", "release-group-1")]
