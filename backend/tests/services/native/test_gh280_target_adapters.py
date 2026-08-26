"""GH-280: target adapters for AudioDB enrichment + Discover membership."""

import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.discover.queue_service import DiscoverQueueService
from services.native.target_library_repository import TargetLibraryRepository
from tests.infrastructure.test_native_library_store import (
    _membership,
    _seed_auth,
)


@pytest.fixture
def store(tmp_path: Path) -> NativeLibraryStore:
    path = tmp_path / "library.db"
    _seed_auth(path)
    return NativeLibraryStore(path, threading.Lock())


@pytest.mark.asyncio
async def test_enrichment_candidates_bounded_keyset_with_metadata(
    store: NativeLibraryStore,
) -> None:
    for suffix in ("1", "2"):
        await store.create_catalog_membership(_membership(suffix))
    from models.local_catalog import (
        LocalArtistAlias,
        LocalArtistExternalIdentity,
    )
    for artist_id in ("artist-1", "artist-2"):
        await store.attach_artist_identity_with_aliases(
            LocalArtistExternalIdentity(
                local_artist_id=artist_id,
                provider_artist_id=f"mbid-{artist_id}",
                selected_at=2,
            ),
            [
                LocalArtistAlias(
                    alias=f"legacy-{artist_id}",
                    local_artist_id=artist_id,
                    kind="legacy_artist",
                    created_at=2,
                )
            ],
            expected_artist_revision=1,
        )

    page1 = await store.target_enrichment_candidates(after_mbid=None, limit=2)
    assert len(page1) == 2
    kinds = [(entity, mbid) for entity, mbid, _payload in page1]
    assert kinds == sorted(kinds)
    page2 = await store.target_enrichment_candidates(
        after_mbid=f"{page1[-1][0]}:{page1[-1][1]}", limit=10
    )
    seen = {mbid for _e, mbid, _p in [*page1, *page2]}
    assert "mbid-artist-2" in seen or any("artist-2" in m for m in seen)
    # no overlap between pages
    ids1 = {(e, m) for e, m, _ in page1}
    ids2 = {(e, m) for e, m, _ in page2}
    assert not (ids1 & ids2)


@pytest.mark.asyncio
async def test_existing_library_mbids_case_insensitive_membership(
    store: NativeLibraryStore,
) -> None:
    from models.local_catalog import LocalAlbumExternalIdentity

    await store.create_catalog_membership(_membership("1"))
    release_group_mbid = "11111111-1111-4111-8111-111111111111"
    await store.attach_album_identity(
        LocalAlbumExternalIdentity(
            local_album_id="album-1",
            release_group_mbid=release_group_mbid.upper(),
        ),
        expected_album_revision=1,
    )
    with sqlite3.connect(store.db_path) as conn:
        indexed = conn.execute(
            "SELECT availability FROM local_tracks WHERE local_album_id='album-1'"
        ).fetchall()

    found = await store.target_existing_library_mbids(
        [release_group_mbid.lower(), "99999999-9999-4999-8999-999999999999"]
    )
    if indexed and indexed[0][0] == "indexed":
        assert found == {release_group_mbid.lower()}
    else:
        assert found == set()


@pytest.mark.asyncio
async def test_queue_validation_uses_native_membership_without_fallback(
    store: NativeLibraryStore, caplog
) -> None:
    """DiscoverQueueService wired with TargetLibraryRepository as library_db
    resolves owned MBIDs natively; no 'Failed to load album MBIDs' fallback
    warning fires and the Lidarr path is never reached."""
    import logging
    from unittest.mock import AsyncMock

    await store.create_catalog_membership(_membership("1"))
    from models.local_catalog import LocalAlbumExternalIdentity
    await store.attach_album_identity(
        LocalAlbumExternalIdentity(local_album_id="album-1", release_group_mbid="rg-album-1"),
        expected_album_revision=1,
    )
    with sqlite3.connect(store.db_path) as conn:
        rg = conn.execute(
            "SELECT release_group_mbid FROM local_album_external_identities "
            "WHERE local_album_id='album-1'"
        ).fetchone()[0]

    service = DiscoverQueueService(
        listenbrainz_repo=AsyncMock(),
        jellyfin_repo=AsyncMock(),
        musicbrainz_repo=AsyncMock(),
        integration=AsyncMock(),
        mbid_resolution=AsyncMock(),
        library_db=TargetLibraryRepository(store),
    )
    # The legacy Lidarr fallback must be unreachable: make it loud.
    service._integration.is_library_configured = AsyncMock(
        side_effect=AssertionError("legacy fallback reached")
    )
    service._mbid.get_library_album_mbids = AsyncMock(
        side_effect=AssertionError("legacy fallback reached")
    )

    with caplog.at_level(logging.WARNING):
        valid = await service.validate_queue_mbids([rg.upper(), "unknown-mbid"])

    assert valid == ["RG-ALBUM-1"]  # input casing preserved

    assert not any(
        "Failed to load album MBIDs" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_sweep_cycle_completes_against_target_repository(
    store: NativeLibraryStore,
) -> None:
    """The AudioDB sweep cycle runs end-to-end against the target adapter:
    candidates come back with entity_type/mbid/name and the cursor advances
    without AttributeError (the GH-280 defect)."""
    from types import SimpleNamespace

    await store.create_catalog_membership(_membership("1"))
    from models.local_catalog import LocalAlbumExternalIdentity, LocalArtistExternalIdentity

    await store.attach_album_identity(
        LocalAlbumExternalIdentity(local_album_id="album-1", release_group_mbid="rg-album-1"),
        expected_album_revision=1,
    )
    await store.attach_artist_identity_with_aliases(
        LocalArtistExternalIdentity(
            local_artist_id="artist-1", provider_artist_id="mbid-artist-1"
        ),
        [],  # no aliases
        expected_artist_revision=1,
    )

    repository = TargetLibraryRepository(store)
    cursor = None
    total = 0
    for _page in range(3):
        page = await repository.get_enrichment_candidates(after_mbid=cursor, limit=2)
        for entity_type, mbid, metadata in page:
            assert entity_type in ("artist", "album")
            assert mbid
            assert isinstance(metadata, dict)
            if entity_type == "album":
                assert "artist_name" in metadata
            else:
                assert "name" in metadata
            total += 1
            cursor = f"{entity_type}:{mbid}"
        if len(page) < 2:
            break
    assert total >= 2
