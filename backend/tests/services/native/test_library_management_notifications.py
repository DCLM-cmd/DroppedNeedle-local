import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from api.v1.schemas.library_management import (
    LibraryManagementProfile,
    NamingScriptSettings,
    ProfileNotificationSettings,
)
from core.exceptions import ExternalServiceError, JellyfinAuthError
from infrastructure.cache.catalog_invalidation import (
    catalog_entity_prefixes,
    catalog_list_prefixes,
)
from infrastructure.cache.cache_keys import library_identification_prefixes
from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_management import LibraryManagementExternalRefreshDelivery
from models.local_catalog import (
    CatalogMembership,
    LocalAlbum,
    LocalAlbumExternalIdentity,
    LocalArtist,
    LocalArtistCredit,
    LocalArtistExternalIdentity,
    LocalTrack,
    LocalTrackExternalIdentity,
)
from models.library_management_planning import PinnedLibraryManagementProfile
from services.native.library_management_notification_service import (
    LibraryManagementNotificationService,
)
from services.native.library_operation_supervisor import LibraryOperationSupervisor
from services.native.library_management_post_commit_service import (
    LibraryManagementPostCommitService,
)


def _pending() -> LibraryManagementExternalRefreshDelivery:
    return LibraryManagementExternalRefreshDelivery(
        id="delivery-1",
        operation_job_id="operation-1",
        target="jellyfin",
        max_attempts=4,
        retry_delay_seconds=30,
        created_at=1,
        updated_at=1,
    )


@pytest.mark.asyncio
async def test_notification_failure_is_retryable_and_does_not_touch_parent() -> None:
    store = AsyncMock()
    store.claim_library_management_external_refresh.return_value = _pending()
    jellyfin = AsyncMock()
    jellyfin.refresh_library.side_effect = ExternalServiceError("offline")
    service = LibraryManagementNotificationService(store, lambda: jellyfin)

    operation_id = await service.run_once("worker-1", now=10.0)

    assert operation_id == "operation-1"
    store.finish_library_management_external_refresh.assert_awaited_once_with(
        "delivery-1",
        "worker-1",
        succeeded=False,
        retryable=True,
        failure_code="EXTERNAL_REFRESH_FAILED",
        now=10.0,
    )
    store.finish_operation_job.assert_not_called()


@pytest.mark.asyncio
async def test_notification_auth_failure_is_permanent() -> None:
    store = AsyncMock()
    store.claim_library_management_external_refresh.return_value = _pending()
    jellyfin = AsyncMock()
    jellyfin.refresh_library.side_effect = JellyfinAuthError("unauthorized")
    service = LibraryManagementNotificationService(store, lambda: jellyfin)

    await service.run_once("worker-1", now=10.0)

    store.finish_library_management_external_refresh.assert_awaited_once_with(
        "delivery-1",
        "worker-1",
        succeeded=False,
        retryable=False,
        failure_code="EXTERNAL_REFRESH_AUTH_FAILED",
        now=10.0,
    )


@pytest.mark.asyncio
async def test_existing_operation_supervisor_dispatches_delivery_when_idle() -> None:
    store = AsyncMock()
    store.claim_operation_job.return_value = None
    operations = AsyncMock()
    expected = MagicMock()
    operations.get.return_value = expected
    notifications = AsyncMock()
    notifications.run_once.return_value = "operation-1"
    supervisor = LibraryOperationSupervisor(
        store,
        operations,
        AsyncMock(),
        AsyncMock(),
        notifications=notifications,
    )

    result = await supervisor.run_once("worker-1", now=10.0)

    assert result is expected
    notifications.run_once.assert_awaited_once_with("worker-1", now=10.0)
    operations.get.assert_awaited_once_with("operation-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "expected_state", "expected_failure"),
    [
        (True, "pending", None),
        (False, "unavailable", "EXTERNAL_REFRESH_NOT_CONFIGURED"),
    ],
)
async def test_post_commit_enqueues_verified_jellyfin_delivery(
    configured: bool, expected_state: str, expected_failure: str | None
) -> None:
    pinned = PinnedLibraryManagementProfile(
        profile=LibraryManagementProfile(
            id="profile-1",
            name="Profile",
            notification=ProfileNotificationSettings(refresh_external_servers=True),
        ),
        naming_script=NamingScriptSettings(
            id="naming-1", name="Naming", source="$title"
        ),
    )
    store = AsyncMock()
    store.get_target_tracks_by_ids.return_value = {
        "track-1": {
            "provider_release_group_mbid": "release-group-1",
            "provider_album_artist_mbid": "artist-1",
            "provider_artist_mbid": "artist-1",
        }
    }
    store.get_track_management_state.return_value = SimpleNamespace(
        last_operation_job_id="operation-1"
    )
    store.get_library_management_job_snapshot.return_value = SimpleNamespace(
        profile_snapshot_json=msgspec.json.encode(pinned).decode()
    )
    preferences = MagicMock()
    preferences.get_library_management_settings_raw.return_value = SimpleNamespace(
        external_refresh=SimpleNamespace(
            enabled=True,
            jellyfin_enabled=True,
            plex_enabled=False,
            navidrome_enabled=False,
            retry_attempts=3,
            retry_delay_seconds=30,
        )
    )
    memory_cache = AsyncMock()
    disk_cache = AsyncMock()
    discovery = AsyncMock()
    jellyfin = MagicMock()
    jellyfin.is_configured.return_value = configured
    service = LibraryManagementPostCommitService(
        store,
        preferences,
        memory_cache,
        disk_cache,
        discovery,
        lambda: jellyfin,
    )

    await service.after_commit({"track-1"}, {"album-1"})

    delivery = store.ensure_library_management_external_refresh.await_args.args[0]
    assert delivery.operation_job_id == "operation-1"
    assert delivery.target == "jellyfin"
    assert delivery.state == expected_state
    assert delivery.failure_code == expected_failure
    assert delivery.max_attempts == 4
    memory_cache.clear_prefix.assert_awaited()
    disk_cache.delete_album.assert_awaited_once_with("release-group-1")
    disk_cache.delete_artist.assert_awaited_once_with("artist-1")
    discovery.mark_discover_stale.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_commit_skips_external_delivery_when_profile_opted_out() -> None:
    pinned = PinnedLibraryManagementProfile(
        profile=LibraryManagementProfile(id="profile-1", name="Profile"),
        naming_script=NamingScriptSettings(
            id="naming-1", name="Naming", source="$title"
        ),
    )
    store = AsyncMock()
    store.get_target_tracks_by_ids.return_value = {}
    store.get_track_management_state.return_value = SimpleNamespace(
        last_operation_job_id="operation-1"
    )
    store.get_library_management_job_snapshot.return_value = SimpleNamespace(
        profile_snapshot_json=msgspec.json.encode(pinned).decode()
    )
    service = LibraryManagementPostCommitService(
        store,
        MagicMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        lambda: MagicMock(),
    )

    await service.after_commit({"track-1"}, {"album-1"})

    store.ensure_library_management_external_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_commit_derives_import_album_for_artist_reconciliation() -> None:
    store = AsyncMock()
    store.get_target_tracks_by_ids.return_value = {
        "track-1": {"local_album_id": "album-from-import"}
    }
    store.get_track_management_state.return_value = None
    reconcile_album = AsyncMock()
    service = LibraryManagementPostCommitService(
        store,
        MagicMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        lambda: MagicMock(),
        reconcile_album,
    )

    await service.after_commit({"track-1"}, set())

    reconcile_album.assert_awaited_once_with("album-from-import")


@pytest.mark.asyncio
async def test_reconciliation_failure_does_not_skip_durable_external_refresh(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pinned = PinnedLibraryManagementProfile(
        profile=LibraryManagementProfile(
            id="profile-1",
            name="Profile",
            notification=ProfileNotificationSettings(refresh_external_servers=True),
        ),
        naming_script=NamingScriptSettings(
            id="naming-1", name="Naming", source="$title"
        ),
    )
    store = AsyncMock()
    store.get_target_tracks_by_ids.return_value = {
        "track-1": {"local_album_id": "album-1"}
    }
    store.get_track_management_state.return_value = SimpleNamespace(
        last_operation_job_id="operation-1"
    )
    store.get_library_management_job_snapshot.return_value = SimpleNamespace(
        profile_snapshot_json=msgspec.json.encode(pinned).decode()
    )
    preferences = MagicMock()
    preferences.get_library_management_settings_raw.return_value = SimpleNamespace(
        external_refresh=SimpleNamespace(
            enabled=True,
            jellyfin_enabled=True,
            plex_enabled=False,
            navidrome_enabled=False,
            retry_attempts=3,
            retry_delay_seconds=30,
        )
    )
    jellyfin = MagicMock()
    jellyfin.is_configured.return_value = True
    reconcile_album = AsyncMock(side_effect=RuntimeError("queue unavailable"))
    service = LibraryManagementPostCommitService(
        store,
        preferences,
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        lambda: jellyfin,
        reconcile_album,
    )

    await service.after_commit({"track-1"}, {"album-1"})

    store.ensure_library_management_external_refresh.assert_awaited_once()
    reconcile_album.assert_awaited_once_with("album-1")
    assert "Artist identity reconciliation enqueue failed" in caplog.text


# F-209: post-commit delivery isolation and invalidation breadth


def _local_membership(suffix: str) -> CatalogMembership:
    artist = LocalArtist(
        id=f"artist-{suffix}",
        display_name=suffix.upper(),
        folded_name=suffix,
        normalized_name=suffix,
        kind="group",
        created_at=1.0,
        updated_at=1.0,
    )
    album = LocalAlbum(
        id=f"album-{suffix}",
        root_id="root-1",
        grouping_key=f"group-{suffix}",
        title="Release",
        album_artist_id=artist.id,
        album_artist_name=suffix.upper(),
        created_at=1,
        updated_at=1,
    )
    track = LocalTrack(
        id=f"track-{suffix}",
        local_album_id=album.id,
        root_id="root-1",
        file_path=f"/music/{suffix}.flac",
        relative_path=f"{suffix}.flac",
        path_hash=f"hash-{suffix}",
        file_size_bytes=100,
        file_mtime_ns=200,
        stat_revision=f"stat-{suffix}",
        title="Track",
        artist_name=suffix.upper(),
        album_title="Release",
        album_artist_name=suffix.upper(),
        file_format="flac",
        imported_at=1,
    )
    credit = LocalArtistCredit(local_artist_id=artist.id, position=0)
    return CatalogMembership(
        album=album,
        artists=[artist],
        tracks=[track],
        album_credits=[credit],
        track_credits={track.id: [credit]},
    )


async def _seed_two_subjects(store) -> tuple[str, str]:
    """Two managed tracks in two operations, each with provider identities."""
    for suffix in ("a", "b"):
        await store.create_catalog_membership(_local_membership(suffix))
        await store.attach_album_identity(
            LocalAlbumExternalIdentity(
                local_album_id=f"album-{suffix}",
                release_group_mbid=f"rg-{suffix}",
                release_mbid=f"rel-{suffix}",
                selected_at=2,
            ),
            expected_album_revision=1,
        )
        await store.attach_track_identity(
            LocalTrackExternalIdentity(
                local_track_id=f"track-{suffix}",
                recording_mbid=f"rec-{suffix}",
                release_mbid=f"rel-{suffix}",
                release_track_mbid=f"rt-{suffix}",
                medium_position=1,
                release_track_position=1,
                selected_at=2,
            ),
            expected_track_revision=1,
        )
        await store.attach_artist_identity_with_aliases(
            LocalArtistExternalIdentity(
                local_artist_id=f"artist-{suffix}",
                provider_artist_id=f"amb-{suffix}",
                decision_source="automatic",
                selected_at=2,
            ),
            [],
            expected_artist_revision=1,
        )


@pytest.fixture
def real_store(tmp_path: Path):
    database = tmp_path / "library.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users VALUES ('admin')")
    return NativeLibraryStore(database, threading.Lock())


def _wire_two_operations(real_store) -> None:
    with sqlite3.connect(real_store.db_path) as connection:
        for suffix in ("a", "b"):
            connection.execute(
                "INSERT INTO library_operation_jobs "
                "(id, kind, state, expected_work_count, completed_count, "
                "succeeded_count, failed_count, skipped_count, control_request, "
                "reidentification_attempt_count, created_at, phase_timings_json, "
                "updated_at, row_revision, event_revision) VALUES "
                "(?, 'library_management', 'running', 1, 0, 0, 0, 0, 'none', 0, "
                "100, '{}', 100, 1, 0)",
                (f"operation-{suffix}",),
            )
            connection.execute(
                "INSERT INTO library_track_management_state "
                "(local_track_id, last_operation_job_id, row_revision) "
                "VALUES (?, ?, 1)",
                (f"track-{suffix}", f"operation-{suffix}"),
            )
            connection.execute(
                "INSERT INTO library_management_job_snapshots "
                "(job_id, mode, origin, phase, selection_json, profile_revision, "
                "settings_revision, naming_revision, policy_revision, "
                "catalog_revision, profile_snapshot_json, intent_json, summary_json, "
                "warnings_json, created_at, updated_at, row_revision) VALUES "
                "(?, 'apply', 'manual', 'applying', '{}', 'profile-rev', "
                "'settings-rev', 'naming-rev', 'policy-rev', 0, ?, '{}', '{}', '[]', "
                "100, 100, 1)",
                (f"operation-{suffix}", _refresh_profile_snapshot_json()),
            )


def _refresh_profile_snapshot_json() -> str:
    pinned = PinnedLibraryManagementProfile(
        profile=LibraryManagementProfile(
            id="profile-1",
            name="Profile",
            notification=ProfileNotificationSettings(refresh_external_servers=True),
        ),
        naming_script=NamingScriptSettings(
            id="naming-1", name="Naming", source="$title"
        ),
    )
    return msgspec.json.encode(pinned).decode()


def _post_commit_service(real_store, **overrides):
    preferences = MagicMock()
    preferences.get_library_management_settings_raw.return_value = SimpleNamespace(
        external_refresh=SimpleNamespace(
            enabled=True,
            jellyfin_enabled=True,
            plex_enabled=False,
            navidrome_enabled=False,
            retry_attempts=3,
            retry_delay_seconds=30,
        )
    )
    return LibraryManagementPostCommitService(
        real_store,
        preferences,
        overrides.get("memory_cache", AsyncMock()),
        overrides.get("disk_cache", AsyncMock()),
        overrides.get("discovery", AsyncMock()),
        lambda: overrides.get("jellyfin", MagicMock()),
        overrides.get("reconcile_album"),
    )


@pytest.mark.asyncio
async def test_invalidation_breadth_covers_every_prefix_and_subject(real_store):
    """Breadth pin: after a move/restore commit EVERY identification cache
    prefix is cleared and every affected release-group/artist disk entry is
    dropped - not just the first subject's."""
    await _seed_two_subjects(real_store)
    _wire_two_operations(real_store)
    original_ensure = real_store.ensure_library_management_external_refresh
    real_store.ensure_library_management_external_refresh = AsyncMock(
        side_effect=original_ensure
    )
    memory_cache, disk_cache, discovery = AsyncMock(), AsyncMock(), AsyncMock()
    service = _post_commit_service(
        real_store,
        memory_cache=memory_cache,
        disk_cache=disk_cache,
        discovery=discovery,
    )

    await service.after_commit({"track-a", "track-b"}, set())

    # ST1 contract: entity keys are DELETED (cross-product of the touched
    # rg/artist ids x catalog_entity_prefixes), only list snapshots are
    # clear_prefix-ed; the wholesale identification sweep is gone.
    cleared = [call.args[0] for call in memory_cache.clear_prefix.await_args_list]
    assert sorted(cleared) == sorted(catalog_list_prefixes())
    deleted_entity_keys = {call.args[0] for call in memory_cache.delete.await_args_list}
    expected_entity_keys = {
        f"{prefix}{mbid}"
        for prefix in catalog_entity_prefixes()
        for mbid in {"rg-a", "rg-b", "amb-a", "amb-b"}
    }
    assert deleted_entity_keys == expected_entity_keys
    deleted_albums = {call.args[0] for call in disk_cache.delete_album.await_args_list}
    assert deleted_albums == {"rg-a", "rg-b"}
    deleted_artists = {
        call.args[0] for call in disk_cache.delete_artist.await_args_list
    }
    assert deleted_artists == {"amb-a", "amb-b"}
    discovery.mark_discover_stale.assert_awaited_once()
    # both operations got a durable delivery row
    assert real_store.ensure_library_management_external_refresh.await_count == 2


@pytest.mark.asyncio
async def test_delivery_enqueue_failure_leaves_invalidation_complete(real_store):
    """Isolation pin: a raising external-refresh enqueue must never starve the
    cache invalidation that already ran - stale UI is the failure mode this
    guards against, so invalidator counts must be complete when it blows up."""
    await _seed_two_subjects(real_store)
    _wire_two_operations(real_store)
    memory_cache, disk_cache, discovery = AsyncMock(), AsyncMock(), AsyncMock()
    service = _post_commit_service(
        real_store,
        memory_cache=memory_cache,
        disk_cache=disk_cache,
        discovery=discovery,
    )
    real_store.ensure_library_management_external_refresh = AsyncMock(
        side_effect=OSError("delivery queue down")
    )
    with pytest.raises(OSError, match="delivery queue down"):
        await service.after_commit({"track-a", "track-b"}, set())

    # ST1: only list snapshots are clear_prefix-ed now; entity keys ride the
    # delete path asserted above.
    cleared = [call.args[0] for call in memory_cache.clear_prefix.await_args_list]
    assert sorted(cleared) == sorted(catalog_list_prefixes())
    assert disk_cache.delete_album.await_count == 2
    assert disk_cache.delete_artist.await_count == 2
    discovery.mark_discover_stale.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_commit_cache_failure_warnings_carry_the_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F-108: gathered cache/invalidation failures must be logged with their
    exception detail, not as a bare message."""
    import logging

    store = AsyncMock()
    store.get_target_tracks_by_ids.return_value = {
        "track-1": {
            "provider_release_group_mbid": "release-group-1",
            "provider_album_artist_mbid": "artist-1",
            "provider_artist_mbid": "artist-1",
        }
    }
    store.get_track_management_state.return_value = None
    failing_memory_cache = AsyncMock()
    failing_memory_cache.clear_prefix.side_effect = RuntimeError(
        "redis connection reset"
    )
    failing_disk_cache = AsyncMock()
    failing_disk_cache.delete_album.side_effect = OSError("mb cache dir gone")
    discovery = AsyncMock()
    service = LibraryManagementPostCommitService(
        store,
        MagicMock(),
        failing_memory_cache,
        failing_disk_cache,
        discovery,
        lambda: MagicMock(),
    )

    with caplog.at_level(
        logging.WARNING,
        logger="services.native.library_management_post_commit_service",
    ):
        await service.after_commit({"track-1"}, {"album-1"})

    warnings = [
        record
        for record in caplog.records
        if "cache invalidation failed" in record.getMessage()
    ]
    # one warning per identification prefix plus the disk-cache pair
    assert len(warnings) >= 2
    assert all(record.exc_info is not None for record in warnings)
    assert any(isinstance(record.exc_info[1], RuntimeError) for record in warnings)
    assert any(isinstance(record.exc_info[1], OSError) for record in warnings)
