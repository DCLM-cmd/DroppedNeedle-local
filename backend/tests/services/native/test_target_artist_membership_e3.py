"""E3: batch artist ownership agrees with the single-artist reference.

``target_existing_provider_artist_ids`` (used for candidate-scoped home,
discovery and artist-page flags) must match ``target_provider_artist_relationship``
on genuine primary owners, contributor-only appearances, and retired artists:
primary album credit with an indexed track is owned; a track-only credit is an
appearance, not ownership; a retired artist's old provider ID is not owned.
"""

import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.local_catalog import (
    CatalogMembership,
    LocalAlbum,
    LocalArtist,
    LocalArtistCredit,
    LocalTrack,
)

OWNER_MBID = "61000000-0000-4000-8000-000000000001"
GUEST_MBID = "61000000-0000-4000-8000-000000000002"
OLD_MBID = "61000000-0000-4000-8000-000000000003"
NEW_MBID = "61000000-0000-4000-8000-000000000004"


@pytest.fixture
def store(tmp_path: Path) -> NativeLibraryStore:
    db_path = tmp_path / "target.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users(id) VALUES ('user-1')")
    return NativeLibraryStore(db_path, threading.Lock())


def _artist(artist_id: str, name: str) -> LocalArtist:
    return LocalArtist(
        id=artist_id,
        display_name=name,
        folded_name=name.casefold(),
        kind="person",
        created_at=1,
        updated_at=1,
    )


def _album(album_id: str, artist_id: str, title: str) -> LocalAlbum:
    return LocalAlbum(
        id=album_id,
        root_id="root-1",
        grouping_key=f"group:{album_id}",
        title=title,
        album_artist_id=artist_id,
        album_artist_name=f"{title} Artist",
        created_at=1,
        updated_at=1,
    )


def _track(track_id: str, album_id: str, root: Path, title: str) -> LocalTrack:
    path = root / f"{track_id}.flac"
    path.write_bytes(b"fLaC" + b"\0" * 64)
    return LocalTrack(
        id=track_id,
        local_album_id=album_id,
        root_id="root-1",
        file_path=str(path),
        relative_path=path.name,
        path_hash=f"hash:{track_id}",
        file_size_bytes=path.stat().st_size,
        file_mtime_ns=path.stat().st_mtime_ns,
        stat_revision=f"stat:{track_id}",
        title=f"{title} Track",
        artist_name=f"{title} Artist",
        album_title=title,
        album_artist_name=f"{title} Artist",
        file_format="flac",
        imported_at=2,
    )


def _identify_artist(store: NativeLibraryStore, local_id: str, mbid: str) -> None:
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "INSERT INTO local_artist_external_identities "
            "(local_artist_id, provider, provider_artist_id, decision_source, selected_at) "
            "VALUES (?, 'musicbrainz', ?, 'manual', 4)",
            (local_id, mbid),
        )


@pytest.mark.asyncio
async def test_batch_matches_reference_for_primary_owner(
    store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "Music"
    root.mkdir()
    await store.create_catalog_membership(
        CatalogMembership(
            album=_album("album-owner", "artist-owner", "Owned"),
            artists=[_artist("artist-owner", "Owned Artist")],
            tracks=[_track("track-owner", "album-owner", root, "Owned")],
            album_credits=[LocalArtistCredit(local_artist_id="artist-owner", position=0)],
            track_credits={
                "track-owner": [LocalArtistCredit(local_artist_id="artist-owner", position=0)]
            },
        )
    )
    _identify_artist(store, "artist-owner", OWNER_MBID)

    owned, _appears = await store.target_provider_artist_relationship(OWNER_MBID)
    assert owned is True
    assert await store.target_existing_provider_artist_ids(
        [OWNER_MBID, "missing-id", ""]
    ) == {OWNER_MBID.casefold()}
    assert await store.target_existing_provider_artist_ids([]) == set()


@pytest.mark.asyncio
async def test_contributor_only_artist_is_appearance_not_owned(
    store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "Music"
    root.mkdir()
    await store.create_catalog_membership(
        CatalogMembership(
            album=_album("album-feat", "artist-host", "Featured"),
            artists=[
                _artist("artist-host", "Host Artist"),
                _artist("artist-guest", "Guest Artist"),
            ],
            tracks=[_track("track-feat", "album-feat", root, "Featured")],
            album_credits=[LocalArtistCredit(local_artist_id="artist-host", position=0)],
            track_credits={
                "track-feat": [
                    LocalArtistCredit(local_artist_id="artist-host", position=0),
                    LocalArtistCredit(local_artist_id="artist-guest", position=1),
                ]
            },
        )
    )
    _identify_artist(store, "artist-host", OWNER_MBID)
    _identify_artist(store, "artist-guest", GUEST_MBID)

    assert await store.target_provider_artist_relationship(GUEST_MBID) == (False, True)
    assert await store.target_existing_provider_artist_ids(
        [OWNER_MBID, GUEST_MBID]
    ) == {OWNER_MBID.casefold()}


@pytest.mark.asyncio
async def test_retired_artist_id_is_not_owned_after_merge(
    store: NativeLibraryStore, tmp_path: Path
) -> None:
    root = tmp_path / "Music"
    root.mkdir()
    await store.create_catalog_membership(
        CatalogMembership(
            album=_album("album-old", "artist-old", "Old"),
            artists=[_artist("artist-old", "Old Artist")],
            tracks=[_track("track-old", "album-old", root, "Old")],
            album_credits=[LocalArtistCredit(local_artist_id="artist-old", position=0)],
            track_credits={
                "track-old": [LocalArtistCredit(local_artist_id="artist-old", position=0)]
            },
        )
    )
    await store.create_catalog_membership(
        CatalogMembership(
            album=_album("album-new", "artist-new", "New"),
            artists=[_artist("artist-new", "New Artist")],
            tracks=[_track("track-new", "album-new", root, "New")],
            album_credits=[LocalArtistCredit(local_artist_id="artist-new", position=0)],
            track_credits={
                "track-new": [LocalArtistCredit(local_artist_id="artist-new", position=0)]
            },
        )
    )
    _identify_artist(store, "artist-old", OLD_MBID)
    _identify_artist(store, "artist-new", NEW_MBID)
    assert await store.target_existing_provider_artist_ids([OLD_MBID, NEW_MBID]) == {
        OLD_MBID.casefold(),
        NEW_MBID.casefold(),
    }

    with sqlite3.connect(store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        revisions = {
            row["id"]: int(row["row_revision"])
            for row in connection.execute(
                "SELECT id, row_revision FROM local_artists"
            ).fetchall()
        }
    await store.merge_local_artists(
        source_artist_ids=["artist-old"],
        surviving_artist_id="artist-new",
        expected_revisions=revisions,
        provider_choice="keep",
        actor_user_id="user-1",
        idempotency_key="e3-retire-test",
        now=9,
    )

    assert await store.target_provider_artist_relationship(OLD_MBID) == (False, False)
    owned_new, _ = await store.target_provider_artist_relationship(NEW_MBID)
    assert owned_new is True
    assert await store.target_existing_provider_artist_ids([OLD_MBID, NEW_MBID]) == {
        NEW_MBID.casefold()
    }
