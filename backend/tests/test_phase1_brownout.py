"""Phase 1 boot gate: the app boots with brownout stubs removed, no Lidarr route paths,
and the download-client settings stub is mounted."""

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from core.dependencies.service_providers import get_library_repository, get_local_files_service
from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.local_catalog import CatalogMembership, LocalAlbum, LocalArtist, LocalArtistCredit, LocalTrack
from services.native.target_library_repository import TargetLibraryRepository

# TestClient without `with` skips lifespan startup (no background tasks needed for these checks).
client = TestClient(main.app)


def test_library_providers_are_target_not_stub():
    """LocalFilesService and library repository are DI-wired to TargetLibraryRepository,
    not a silent stub; populated target data is observable via the repository."""
    repo = get_library_repository()
    assert isinstance(repo, TargetLibraryRepository)
    local_files = get_local_files_service()
    assert isinstance(local_files._library_repo, TargetLibraryRepository)


@pytest.mark.asyncio
async def test_seeded_target_catalog_is_observable_via_target_repository(tmp_path: Path) -> None:
    db_path = tmp_path / "seeded.db"
    store = NativeLibraryStore(db_path, threading.Lock())
    repo = TargetLibraryRepository(store, None)
    assert await repo.has_any_files() is False
    assert await repo.get_library() == []
    assert await repo.get_home_albums(limit=5) == []
    artist_id = "60000000-0000-4000-8000-000000000601"
    album_id = "40000000-0000-4000-8000-000000000601"
    track_id = "50000000-0000-4000-8000-000000000601"
    root = tmp_path / "Music"
    root.mkdir()
    path = root / f"{track_id}.flac"
    path.write_bytes(b"fLaC" + b"\0" * 64)
    artist = LocalArtist(
        id=artist_id,
        display_name="Seed Artist",
        folded_name="seed artist",
        kind="person",
        created_at=1,
        updated_at=1,
    )
    album = LocalAlbum(
        id=album_id,
        root_id="root-1",
        grouping_key=f"group:{album_id}",
        title="Seed Album",
        album_artist_id=artist_id,
        album_artist_name=artist.display_name,
        created_at=1,
        updated_at=1,
    )
    track = LocalTrack(
        id=track_id,
        local_album_id=album_id,
        root_id="root-1",
        file_path=str(path),
        relative_path=path.name,
        path_hash=f"hash:{track_id}",
        file_size_bytes=path.stat().st_size,
        file_mtime_ns=path.stat().st_mtime_ns,
        stat_revision=f"stat:{track_id}",
        title="Seed Track",
        artist_name=artist.display_name,
        album_title=album.title,
        album_artist_name=artist.display_name,
        file_format="flac",
        imported_at=2,
    )
    credit = LocalArtistCredit(local_artist_id=artist_id, position=0)
    membership = CatalogMembership(
        album=album,
        artists=[artist],
        tracks=[track],
        album_credits=[credit],
        track_credits={track_id: [credit]},
    )
    await store.create_catalog_membership(membership)
    assert await repo.has_any_files() is True
    assert await repo.has_album(album_id) is True
    assert await repo.has_track(track_id) is True
    home = await repo.get_home_albums(limit=5)
    assert len(home) == 1
    recent = await repo.get_recently_imported(limit=5)
    assert len(recent) == 1
    library = await repo.get_library()
    assert len(library) == 1
    albums = await repo.get_albums()
    assert len(albums) == 1
    stats = await repo.get_stats()
    assert stats.total_albums == 1
    tracks = await repo.get_tracks(album_id)
    assert len(tracks) == 1
    assert tracks[0].id == track_id
    with pytest.raises(NotImplementedError):
        await repo.delete_album(123)
    with pytest.raises(NotImplementedError):
        await repo.delete_artist(456)


def test_app_boots_and_health_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_only_sanctioned_lidarr_routes_mounted():
    """The old Lidarr *management* integration stays deleted (LidarrImport D8). The only
    permitted Lidarr route paths are the read-only migration importer under
    ``/lidarr-import`` - any other ``lidarr`` path would mean the management surface came back."""
    lidarr_paths = [
        path
        for route in main.app.routes
        if "lidarr" in (path := getattr(route, "path", ""))
    ]
    assert lidarr_paths, "expected the lidarr-import routes to be mounted"
    assert all("/lidarr-import" in path for path in lidarr_paths), lidarr_paths


def test_download_client_settings_route_mounted():
    # Phase 6 relocated the download-client config from the P1 brownout stub at
    # /settings/download-client to its canonical home at /download-client/config.
    paths = [getattr(route, "path", "") for route in main.app.routes]
    assert any(path.endswith("/download-client/config") for path in paths)
