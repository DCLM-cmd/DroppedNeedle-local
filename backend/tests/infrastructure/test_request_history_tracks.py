"""Real-SQLite coverage for typed request identity and request generations."""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from infrastructure.persistence.request_history import RequestHistoryStore


@pytest.mark.asyncio
async def test_exact_track_metadata_and_raw_id_survive_round_trip(tmp_path: Path) -> None:
    store = RequestHistoryStore(tmp_path / "requests.db")

    created = await store.async_record_request(
        musicbrainz_id="RECORDING-1",
        artist_name="Radiohead",
        album_title="OK Computer",
        artist_mbid="artist-1",
        user_id="listener-1",
        requested_by_name="Listener",
        release_mbid="release-1",
        initial_status="awaiting_approval",
        request_kind="track",
        track_title="Airbag",
        duration_seconds=287,
        track_release_group_mbid="release-group-1",
    )

    assert created is not None
    assert created.musicbrainz_id == "RECORDING-1"
    assert created.request_kind == "track"
    assert created.generation == 1
    record = await store.async_get_record("recording-1", request_kind="track")

    assert record is not None
    assert record.musicbrainz_id == "RECORDING-1"
    assert record.request_kind == "track"
    assert record.track_title == "Airbag"
    assert record.duration_seconds == 287
    assert record.track_release_group_mbid == "release-group-1"
    assert record.dispatch_authorized is False

    with sqlite3.connect(store.db_path) as conn:
        keys = conn.execute(
            "SELECT musicbrainz_id_lower FROM request_history"
        ).fetchall()
    assert keys == [("track:recording-1",)]


@pytest.mark.asyncio
async def test_album_and_track_with_same_uuid_are_isolated_by_typed_key(
    tmp_path: Path,
) -> None:
    store = RequestHistoryStore(tmp_path / "requests.db")

    album_begin = await store.async_record_request(
        "SAME-UUID",
        "Album Artist",
        "Album",
        user_id="album-user",
        initial_status="pending",
        request_kind="album",
    )
    assert album_begin is not None
    track_begin = await store.async_record_request(
        "same-uuid",
        "Track Artist",
        "Track Album",
        user_id="track-user",
        initial_status="awaiting_approval",
        request_kind="track",
        track_title="Same UUID",
        duration_seconds=123,
        track_release_group_mbid="rg-track",
    )
    assert track_begin is not None

    album = await store.async_get_record("same-uuid")
    track = await store.async_get_record("SAME-UUID", request_kind="track")
    assert album is not None and track is not None
    assert album.request_kind == "album"
    assert album.musicbrainz_id == "SAME-UUID"
    assert album.artist_name == "Album Artist"
    assert track.request_kind == "track"
    assert track.musicbrainz_id == "same-uuid"
    assert track.artist_name == "Track Artist"

    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT musicbrainz_id_lower, request_kind FROM request_history "
            "ORDER BY musicbrainz_id_lower"
        ).fetchall()
    assert rows == [("same-uuid", "album"), ("track:same-uuid", "track")]


@pytest.mark.asyncio
async def test_typed_mutators_and_task_lookup_do_not_cross_same_uuid(
    tmp_path: Path,
) -> None:
    store = RequestHistoryStore(tmp_path / "requests.db")
    await store.async_record_request(
        "same-uuid",
        "Album Artist",
        "Album",
        user_id="album-user",
        request_kind="album",
        initial_status="pending",
    )
    await store.async_record_request(
        "same-uuid",
        "Track Artist",
        "Album",
        user_id="track-user",
        request_kind="track",
        initial_status="awaiting_approval",
        track_title="Song",
    )

    await store.async_update_status("same-uuid", "downloading", request_kind="track")
    await store.async_update_download_task_id(
        "same-uuid", "track-task", request_kind="track"
    )
    await store.async_update_download_task_id(
        "same-uuid", "album-task", request_kind="album"
    )

    track_task = await store.async_get_record_by_download_task_id(
        "track-task", request_kind="track"
    )
    album_task = await store.async_get_record_by_download_task_id(
        "album-task", request_kind="album"
    )
    assert track_task is not None and track_task.request_kind == "track"
    assert album_task is not None and album_task.request_kind == "album"
    assert (await store.async_get_record("same-uuid", request_kind="album")).status == (
        "pending"
    )
    assert (
        await store.async_get_record("same-uuid", request_kind="track")
    ).status == "downloading"

    await store.async_record_review(
        "same-uuid",
        "approved",
        "admin-1",
        "Admin",
        request_kind="track",
    )
    track = await store.async_get_record("same-uuid", request_kind="track")
    album = await store.async_get_record("same-uuid", request_kind="album")
    assert track is not None and track.status == "approved"
    assert track.reviewed_by_id == "admin-1"
    assert album is not None and album.reviewed_by_id is None


@pytest.mark.asyncio
async def test_legacy_album_rows_backfill_requesters_idempotently(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE request_history (
                musicbrainz_id_lower TEXT PRIMARY KEY,
                musicbrainz_id TEXT NOT NULL,
                artist_name TEXT NOT NULL,
                album_title TEXT NOT NULL,
                artist_mbid TEXT,
                year INTEGER,
                cover_url TEXT,
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                monitor_artist INTEGER NOT NULL DEFAULT 0,
                auto_download_artist INTEGER NOT NULL DEFAULT 0,
                user_id TEXT,
                requested_by_name TEXT,
                reviewed_by_id TEXT,
                reviewed_by_name TEXT,
                reviewed_at TEXT,
                download_task_id TEXT,
                release_mbid TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO request_history ("
            "musicbrainz_id_lower, musicbrainz_id, artist_name, album_title, "
            "requested_at, status, user_id, requested_by_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-rg",
                "LEGACY-RG",
                "Artist",
                "Album",
                "2026-01-01T00:00:00+00:00",
                "pending",
                "legacy-user",
                "Legacy listener",
            ),
        )

    first = RequestHistoryStore(db_path)
    record = await first.async_get_record("legacy-rg")
    assert record is not None
    assert record.request_kind == "album"
    assert record.musicbrainz_id == "LEGACY-RG"
    assert await first.async_is_requester("legacy-user", "legacy-rg")

    # Construction is the migration boundary. Re-running it must not duplicate
    # the backfilled listener row or change its attribution.
    second = RequestHistoryStore(db_path)
    with sqlite3.connect(db_path) as conn:
        requesters = conn.execute(
            "SELECT user_id, musicbrainz_id_lower, requested_by_name "
            "FROM request_history_requesters"
        ).fetchall()
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(request_history)")
        }
    assert requesters == [("legacy-user", "legacy-rg", "Legacy listener")]
    assert {"request_kind", "track_title", "duration_seconds", "track_release_group_mbid"} <= columns
    assert await second.async_is_requester("legacy-user", "legacy-rg")


@pytest.mark.asyncio
async def test_terminal_reactivation_resets_private_listeners_and_dismissals(
    tmp_path: Path,
) -> None:
    store = RequestHistoryStore(tmp_path / "requests.db")
    initial = await store.async_record_request(
        "release-group-1",
        "Artist",
        "Album",
        user_id="old-user",
        requested_by_name="Old listener",
        initial_status="failed",
        dispatch_authorized=True,
    )
    assert initial is not None
    assert await store.async_dismiss_record("old-user", "release-group-1")

    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM request_history_dismissals"
        ).fetchone()[0] == 1

    created = await store.async_record_request(
        "release-group-1",
        "New Artist",
        "New Album",
        user_id="new-user",
        requested_by_name="New listener",
        initial_status="awaiting_approval",
        dispatch_authorized=False,
    )
    assert created is not None
    assert created.generation == 2
    record = await store.async_get_record("release-group-1")
    assert record is not None
    assert record.status == "awaiting_approval"
    assert record.artist_name == "New Artist"
    assert record.user_id == "new-user"
    assert record.requested_by_name == "New listener"
    assert record.dispatch_authorized is False
    assert await store.async_requester_count("release-group-1") == 1
    assert not await store.async_is_requester("old-user", "release-group-1")
    assert await store.async_is_requester("new-user", "release-group-1")

    old_history, old_total = await store.async_get_history_for_user("old-user")
    new_active = await store.async_get_active_requests_for_user("new-user")
    assert old_history == [] and old_total == 0
    assert [item.user_id for item in new_active] == ["new-user"]
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM request_history_dismissals"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_active_duplicate_begin_is_atomic_noop_and_never_overwrites_generation(
    tmp_path: Path,
) -> None:
    store = RequestHistoryStore(tmp_path / "requests.db")
    original_begin = await store.async_record_request(
        "recording-1",
        "Original Artist",
        "Original Album",
        user_id="first-user",
        requested_by_name="First",
        initial_status="pending",
        request_kind="track",
        track_title="Original Track",
        duration_seconds=240,
        dispatch_authorized=True,
    )
    assert original_begin is not None
    original = await store.async_get_record("recording-1", request_kind="track")
    assert original is not None

    results = await asyncio.gather(
        store.async_record_request(
            "RECORDING-1",
            "Replacement Artist",
            "Replacement Album",
            user_id="second-user",
            requested_by_name="Second",
            initial_status="downloading",
            request_kind="track",
            track_title="Replacement Track",
            duration_seconds=999,
            dispatch_authorized=False,
        ),
        store.async_record_request(
            "recording-1",
            "Another Artist",
            "Another Album",
            user_id="third-user",
            initial_status="awaiting_approval",
            request_kind="track",
            track_title="Another Track",
        ),
    )
    assert results == [None, None]

    current = await store.async_get_record("recording-1", request_kind="track")
    assert current is not None
    assert current.status == "pending"
    assert current.artist_name == "Original Artist"
    assert current.track_title == "Original Track"
    assert current.duration_seconds == 240
    assert current.user_id == "first-user"
    assert current.dispatch_authorized is True
    assert await store.async_requester_count("recording-1", request_kind="track") == 1


@pytest.mark.asyncio
async def test_requester_helpers_are_typed_and_dismissal_is_typed(
    tmp_path: Path,
) -> None:
    store = RequestHistoryStore(tmp_path / "requests.db")
    await store.async_record_request(
        "same-id",
        "Artist",
        "Album",
        user_id="album-user",
        request_kind="album",
    )
    await store.async_record_request(
        "same-id",
        "Artist",
        "Album",
        user_id="track-user",
        request_kind="track",
        track_title="Track",
    )

    await store.async_add_requester("same-id", "shared-user", request_kind="track")
    assert await store.async_requester_count("same-id", request_kind="track") == 2
    assert await store.async_requester_count("same-id") == 1
    assert await store.async_is_requester(
        "shared-user", "same-id", request_kind="track"
    )
    assert not await store.async_is_requester("shared-user", "same-id")

    assert await store.async_dismiss_record(
        "shared-user", "same-id", request_kind="track"
    )
    track_history, track_total = await store.async_get_history_for_user("shared-user")
    album_history, album_total = await store.async_get_history_for_user("album-user")
    assert track_history == [] and track_total == 0
    assert [record.request_kind for record in album_history] == ["album"]
    assert album_total == 1

    assert await store.async_delete_record("same-id", request_kind="track")
    assert await store.async_get_record("same-id", request_kind="track") is None
    assert await store.async_get_record("same-id", request_kind="album") is not None
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM request_history_dismissals"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM request_history_requesters "
            "WHERE musicbrainz_id_lower = 'track:same-id'"
        ).fetchone()[0] == 0
@pytest.mark.asyncio
async def test_removing_primary_listener_does_not_transfer_primary_attribution(
    tmp_path: Path,
) -> None:
    store = RequestHistoryStore(tmp_path / "requests.db")
    initial = await store.async_record_request(
        "release-group-1",
        "Artist",
        "Album",
        user_id="primary-user",
        requested_by_name="Primary",
    )
    assert initial is not None
    await store.async_add_requester(
        "release-group-1", "second-user", "Second listener"
    )

    assert await store.async_remove_requester("primary-user", "release-group-1")
    record = await store.async_get_record("release-group-1")
    assert record is not None
    assert record.user_id == "primary-user"
    assert await store.async_is_requester("second-user", "release-group-1")
    assert not await store.async_is_requester("primary-user", "release-group-1")
    active = await store.async_get_active_requests_for_user("second-user")
    assert [item.user_id for item in active] == ["second-user"]


@pytest.mark.asyncio
async def test_detached_primary_listener_stays_detached_after_store_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "requests.db"
    store = RequestHistoryStore(db_path)
    begun = await store.async_record_request(
        "release-group-1",
        "Artist",
        "Album",
        user_id="primary-user",
        requested_by_name="Primary",
        initial_status="pending",
    )
    assert begun is not None
    assert await store.async_add_requester(
        "release-group-1", "second-user", "Second listener"
    )
    assert await store.async_remove_requester("primary-user", "release-group-1")

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT user_id FROM request_history WHERE musicbrainz_id_lower = ?",
            ("release-group-1",),
        ).fetchone() == ("primary-user",)

    restarted = RequestHistoryStore(db_path)
    assert not await restarted.async_is_requester("primary-user", "release-group-1")
    assert await restarted.async_is_requester("second-user", "release-group-1")
    primary_active = await restarted.async_get_active_requests_for_user("primary-user")
    primary_history, primary_total = await restarted.async_get_history_for_user(
        "primary-user"
    )
    assert primary_active == []
    assert primary_history == [] and primary_total == 0


@pytest.mark.asyncio
async def test_stale_generation_cannot_update_successor_status_or_task(
    tmp_path: Path,
) -> None:
    store = RequestHistoryStore(tmp_path / "requests.db")
    first = await store.async_record_request(
        "recording-1",
        "Artist",
        "Album",
        user_id="listener",
        initial_status="pending",
        request_kind="track",
        track_title="Track",
    )
    assert first is not None
    assert first.generation == 1
    assert await store.async_update_status(
        "recording-1",
        "failed",
        request_kind="track",
        expected_generation=first.generation,
    )

    successor = await store.async_record_request(
        "recording-1",
        "Artist",
        "Album",
        user_id="listener",
        initial_status="pending",
        request_kind="track",
        track_title="Track",
    )
    assert successor is not None
    assert successor.generation == first.generation + 1
    assert not await store.async_update_status(
        "recording-1",
        "failed",
        request_kind="track",
        expected_generation=first.generation,
    )
    assert not await store.async_update_download_task_id(
        "recording-1",
        "stale-task",
        request_kind="track",
        expected_generation=first.generation,
    )
    current = await store.async_get_record("recording-1", request_kind="track")
    assert current is not None
    assert current.generation == successor.generation
    assert current.status == "pending"
    assert current.download_task_id is None
    assert await store.async_update_download_task_id(
        "recording-1",
        "current-task",
        request_kind="track",
        expected_generation=successor.generation,
    )


@pytest.mark.asyncio
async def test_concurrent_approval_claim_has_one_winner(tmp_path: Path) -> None:
    store = RequestHistoryStore(tmp_path / "requests.db")
    begun = await store.async_record_request(
        "recording-approval",
        "Artist",
        "Album",
        user_id="listener",
        initial_status="awaiting_approval",
        request_kind="track",
        track_title="Track",
    )
    assert begun is not None

    claims = await asyncio.gather(
        store.async_claim_approval(
            "recording-approval",
            reviewer_id="admin-a",
            reviewer_name="Admin A",
            request_kind="track",
            expected_generation=begun.generation,
        ),
        store.async_claim_approval(
            "recording-approval",
            reviewer_id="admin-b",
            reviewer_name="Admin B",
            request_kind="track",
            expected_generation=begun.generation,
        ),
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].generation == begun.generation
    record = await store.async_get_record("recording-approval", request_kind="track")
    assert record is not None
    assert record.status == "pending"
    assert record.dispatch_authorized is True
    assert record.reviewed_by_id in {"admin-a", "admin-b"}


@pytest.mark.asyncio
async def test_concurrent_retry_claim_has_one_member_winner(tmp_path: Path) -> None:
    store = RequestHistoryStore(tmp_path / "requests.db")
    begun = await store.async_record_request(
        "recording-retry",
        "Artist",
        "Album",
        user_id="listener",
        initial_status="failed",
        request_kind="track",
        track_title="Track",
    )
    assert begun is not None
    outsider = await store.async_claim_retry(
        "recording-retry",
        "outsider",
        request_kind="track",
        expected_generation=begun.generation,
        allowed_statuses=("failed",),
    )
    assert outsider is None

    claims = await asyncio.gather(
        store.async_claim_retry(
            "recording-retry",
            "listener",
            request_kind="track",
            expected_generation=begun.generation,
            allowed_statuses=("failed",),
        ),
        store.async_claim_retry(
            "recording-retry",
            "listener",
            request_kind="track",
            expected_generation=begun.generation,
            allowed_statuses=("failed",),
        ),
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].generation == begun.generation
    record = await store.async_get_record("recording-retry", request_kind="track")
    assert record is not None
    assert record.status == "pending"
    assert record.dispatch_authorized is True
