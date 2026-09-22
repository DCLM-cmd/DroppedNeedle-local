import asyncio
import threading

import pytest

from infrastructure.persistence.discovery_snapshot_store import DiscoverySnapshotStore


@pytest.fixture
def store(tmp_path):
    return DiscoverySnapshotStore(tmp_path / "library.db", threading.Lock())


@pytest.mark.asyncio
async def test_demand_and_revision_progress_survive_reconstruction(tmp_path):
    path = tmp_path / "library.db"
    lock = threading.Lock()
    first = DiscoverySnapshotStore(path, lock)
    await first.record_activity("user", "artist", "source-a", 1000, "artist", "similar", "lastfm")
    await first.save_progress("user", "artist:similar", "revision-a", 7, 1000)

    second = DiscoverySnapshotStore(path, lock)
    rows = await second.get_due_activity("source-a", 1001)
    assert [(row["user_id"], row["artist_mbid"], row["section"], row["provider"]) for row in rows] == [
        ("user", "artist", "similar", "lastfm")
    ]
    assert await second.get_progress("user", "artist:similar", "revision-a") == 7
    assert await second.get_progress("user", "artist:similar", "revision-b") == 0
    await second.save_progress("user", "artist:similar", "revision-b", 2, 1002)
    assert await first.get_progress("user", "artist:similar", "revision-a") == 0
    assert await first.get_progress("user", "artist:similar", "revision-b") == 2


@pytest.mark.asyncio
async def test_activity_coalesces_five_minutes_and_expires_at_twenty_four_hours(store):
    await store.record_activity("user", "home", "source", 1000)
    await store.record_activity("user", "home", "source", 1299)
    rows = await store.get_due_activity("source", 1299)
    assert [row["last_used"] for row in rows] == [1000]

    await store.record_activity("user", "home", "source", 1300)
    rows = await store.get_due_activity("source", 1300 + 86400 - 1)
    assert [row["last_used"] for row in rows] == [1300]
    assert await store.get_due_activity("source", 1300 + 86400) == []


@pytest.mark.asyncio
async def test_recent_demand_cap_is_per_user_and_retains_newest_sections(store):
    await store.record_activity("other", "home", "source", 1000)
    for index in range(102):
        await store.record_activity(
            "user", "artist", "source", 1000 + index,
            f"artist-{index:03}", "similar", "listenbrainz",
        )

    rows = await store.get_due_activity("source", 1102, user_id="user")
    assert {row["artist_mbid"] for row in rows} == {
        f"artist-{index:03}" for index in range(2, 102)
    }
    other = await store.get_due_activity("source", 1102, user_id="other")
    assert [(row["user_id"], row["feature"]) for row in other] == [("other", "home")]


@pytest.mark.asyncio
async def test_serviced_demand_yields_to_peers_and_retry_preserves_last_success(store):
    await store.record_activity("a", "home", "source", 1000)
    await store.record_activity("b", "home", "source", 1000)
    first = (await store.get_due_activity("source", 1000, limit=1))[0]
    assert first["user_id"] == "a"
    await store.finish_activity(first, 1001, success=True, retry_seconds=10)
    assert [row["user_id"] for row in await store.get_due_activity("source", 1002)] == ["b"]
    assert [row["user_id"] for row in await store.get_due_activity("source", 1011)] == ["b", "a"]

    await store.finish_activity(first, 1011, success=False, retry_seconds=20)
    assert await store.get_due_activity("source", 1030, user_id="a") == []
    retried = (await store.get_due_activity("source", 1031, user_id="a"))[0]
    assert retried["last_success"] == 1001
    assert retried["serviced_at"] == 1011


@pytest.mark.asyncio
async def test_source_change_bypasses_coalescing_and_rejects_old_completion(store):
    await store.record_activity("user", "home", "source-a", 1000)
    old = (await store.get_due_activity("source-a", 1000))[0]
    await store.finish_activity(old, 1001, success=True, retry_seconds=600)
    await store.record_activity("user", "home", "source-b", 1002)
    await store.finish_activity(old, 1003, success=True, retry_seconds=600)

    assert await store.get_due_activity("source-a", 1003) == []
    current = (await store.get_due_activity("source-b", 1003))[0]
    assert current["last_used"] == 1002
    assert current["last_success"] == 0
    assert current["retry_at"] == 0


@pytest.mark.asyncio
async def test_user_deletion_isolated_then_source_clear_removes_remaining_demand(store):
    for user in ("a", "b"):
        await store.record_activity(user, "home", "source", 1000)
        await store.save_progress(user, "home", "revision", 4, 1000)
        await store.save(f"discover_response:{user}", user, user.encode(), 1000)
    await store.save("unrelated:b", "b", b"retained", 1000)

    await store.delete_user("a")
    assert [row["user_id"] for row in await store.get_due_activity("source", 1001)] == ["b"]
    assert await store.get_progress("a", "home", "revision") == 0
    assert await store.get_progress("b", "home", "revision") == 4
    assert await store.get("discover_response:a") is None
    assert await store.get("discover_response:b") == b"b"

    await store.delete_source_dependent_snapshots()
    assert await store.get_due_activity("source", 1001) == []
    assert await store.get_progress("b", "home", "revision") == 0
    assert await store.get("discover_response:b") is None
    assert await store.get("unrelated:b") == b"retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["snapshot", "activity", "progress"])
async def test_user_lease_fences_write_paused_before_account_deletion(store, monkeypatch, kind):
    lease = store.user_lease("deleted")
    started = asyncio.Event()
    release = asyncio.Event()
    original_write = store._write
    pause_next = True

    async def paused_write(operation):
        nonlocal pause_next
        if pause_next:
            pause_next = False
            started.set()
            await release.wait()
        return await original_write(operation)

    monkeypatch.setattr(store, "_write", paused_write)
    if kind == "snapshot":
        pending = store.save("discover_response:deleted", "deleted", b"late", 1000)
    elif kind == "activity":
        pending = store.record_activity("deleted", "home", "source", 1000)
    else:
        pending = store.save_progress("deleted", "home", "revision", 8, 1000)
    task = asyncio.create_task(pending)
    await started.wait()
    try:
        await store.delete_user("deleted")
        assert lease.active is False
    finally:
        release.set()
        await task

    assert await store.get("discover_response:deleted") is None
    assert await store.get_due_activity("source", 1001, user_id="deleted") == []
    assert await store.get_progress("deleted", "home", "revision") == 0
