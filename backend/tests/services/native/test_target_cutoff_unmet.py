"""F-TARGETCATALOG-03: one aggregate read replaces the cutoff N+1.

Parity: for every quality band (lossless extensions, MP4-family with/without
depth evidence, bitrate boundaries, missing metadata), the store aggregate must
return the same worklist as a ``tier_for``/``tier_rank`` reference over indexed
tracks. Query count: the repository awaits the aggregate exactly once and never
issues per-album track reads (pre-fix baseline measured against HEAD~1 with a
read spy: 12 albums -> 1 list_target_albums + 12 get_target_album_tracks).
"""

import sqlite3
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.local_catalog import (
    CatalogMembership,
    LocalAlbum,
    LocalArtist,
    LocalArtistCredit,
    LocalTrack,
)
from services.native.quality_tiers import TIER_KEYS, tier_for, tier_rank
from services.native.target_library_repository import TargetLibraryRepository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users VALUES ('admin')")
    return path


@pytest.fixture
def store(db_path: Path) -> NativeLibraryStore:
    return NativeLibraryStore(db_path, threading.Lock())


def _membership(
    suffix: str,
    *,
    file_format: str = "mp3",
    bitrate: int | None = 256,
    bit_depth: int | None = None,
) -> CatalogMembership:
    artist = LocalArtist(
        id=f"artist-{suffix}",
        display_name="Artist",
        folded_name="artist",
        normalized_name="artist",
        kind="group",
        created_at=1,
        updated_at=1,
    )
    album = LocalAlbum(
        id=f"album-{suffix}",
        root_id="root-1",
        grouping_key=f"group-{suffix}",
        title=f"Album {suffix}",
        album_artist_id=artist.id,
        album_artist_name="Artist",
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
        tag_revision=f"tag-{suffix}",
        title="Track",
        artist_name="Artist",
        album_title=f"Album {suffix}",
        album_artist_name="Artist",
        file_format=file_format,
        bit_rate=bitrate,
        bit_depth=bit_depth,
        duration_seconds=180.0,
        imported_at=1,
        applied_policy="automatic",
    )
    credit = LocalArtistCredit(local_artist_id=artist.id, position=0)
    return CatalogMembership(
        album=album,
        artists=[artist],
        tracks=[track],
        album_credits=[credit],
        track_credits={track.id: [credit]},
    )


# One album per quality band + boundary + missing-metadata cases.
_BANDS = [
    ("flac-lossless", dict(file_format="flac", bitrate=900)),
    ("alac-ext", dict(file_format="alac", bitrate=None)),
    ("wav", dict(file_format="wav", bitrate=1411)),
    ("ape", dict(file_format="ape", bitrate=None)),
    ("wv", dict(file_format="wv", bitrate=None)),
    ("alac-m4a-depth", dict(file_format="m4a", bitrate=900, bit_depth=16)),
    ("aac-m4a-nodepth", dict(file_format="m4a", bitrate=256, bit_depth=None)),
    ("aac-mp4-nodepth", dict(file_format="mp4", bitrate=192, bit_depth=None)),
    ("mov-withdepth", dict(file_format="mov", bitrate=None, bit_depth=24)),
    ("mp3-320-boundary", dict(file_format="mp3", bitrate=320)),
    ("mp3-319-below", dict(file_format="mp3", bitrate=319)),
    ("mp3-256-boundary", dict(file_format="mp3", bitrate=256)),
    ("mp3-255-below", dict(file_format="mp3", bitrate=255)),
    ("mp3-192-boundary", dict(file_format="mp3", bitrate=192)),
    ("mp3-191-below", dict(file_format="mp3", bitrate=191)),
    ("ogg-low", dict(file_format="ogg", bitrate=64)),
    ("no-bitrate", dict(file_format="m4a", bitrate=None)),
    ("uppercase-flac", dict(file_format="FLAC", bitrate=900)),
]


async def _seed_bands(store: NativeLibraryStore) -> dict[str, str]:
    expected: dict[str, str] = {}
    for index, (name, kwargs) in enumerate(_BANDS):
        suffix = f"band{index:02d}-{name}"
        await store.create_catalog_membership(_membership(suffix, **kwargs))
        expected[f"album-{suffix}"] = tier_for(
            kwargs.get("file_format", "mp3"),
            kwargs.get("bitrate", 256),
            kwargs.get("bit_depth"),
        )
    return expected


@pytest.mark.asyncio
async def test_store_aggregate_matches_tier_for_reference(store: NativeLibraryStore):
    """Every band: the aggregate lists exactly the albums whose worst indexed
    track rank is below the lossless cutoff, with the correct worst_rank."""
    from services.native.quality_tiers import tier_rank

    expected = await _seed_bands(store)
    lossless_rank = tier_rank("lossless")

    rows = await store.list_target_cutoff_unmet(lossless_rank, limit=100_000)

    by_rg = {str(r["release_group_mbid"]): r for r in rows}
    # Unmet = strictly below the cutoff; satisfied bands must be absent.
    unmet_expected = {
        rg: tier for rg, tier in expected.items() if tier_rank(tier) < lossless_rank
    }
    assert set(by_rg) == set(unmet_expected)
    for rg in by_rg:
        assert rank_to_tier(int(by_rg[rg]["worst_rank"])) == unmet_expected[rg]

    # A tighter cutoff (mp3_320) drops the 320-boundary band but keeps lower ones.
    tight = await store.list_target_cutoff_unmet(tier_rank("mp3_320"), limit=100_000)
    tiers_at_320 = {
        str(r["release_group_mbid"]): rank_to_tier(int(r["worst_rank"]))
        for r in tight
    }
    assert "album-band09-mp3-320-boundary" not in tiers_at_320
    assert "album-band12-mp3-255-below" in tiers_at_320


def rank_to_tier(rank: int) -> str:
    return list(TIER_KEYS)[::-1][rank]


@pytest.mark.asyncio
async def test_aggregate_respects_limit_bound(store: NativeLibraryStore):
    await _seed_bands(store)
    # Only 2 of the 18 bands are below-cutoff at rank>=3... use a tight window.
    rows = await store.list_target_cutoff_unmet(tier_rank("low"), limit=2)
    assert len(rows) <= 2


@pytest.mark.asyncio
async def test_repository_single_read_and_correct_current_tier(
    db_path: Path,
):
    """Query-count contract: list_cutoff_unmet awaits ONE aggregate call and
    never touches get_target_album_tracks / list_target_albums."""
    spy_store = AsyncMock(spec=NativeLibraryStore)
    aggregate_rows = [
        {
            "release_group_mbid": "rg-a",
            "album_title": "A",
            "album_artist_name": "Artist A",
            "provider_artist_mbid": "pa-1",
            "worst_rank": 1,
        },
        {
            "release_group_mbid": "rg-b",
            "album_title": "B",
            "album_artist_name": "Artist B",
            "provider_artist_mbid": None,
            "worst_rank": 0,
        },
    ]

    async def aggregate(cutoff_rank, *, limit):
        assert limit == 100_000
        return aggregate_rows

    spy_store.list_target_cutoff_unmet = AsyncMock(side_effect=aggregate)

    repository = TargetLibraryRepository(spy_store)
    result = await repository.list_cutoff_unmet("mp3_192")

    spy_store.list_target_cutoff_unmet.assert_awaited_once_with(
        tier_rank("mp3_192"), limit=100_000
    )
    spy_store.list_target_albums.assert_not_called()
    spy_store.get_target_album_tracks.assert_not_called()
    tiers = {row["release_group_mbid"]: row["current_tier"] for row in result}
    assert tiers == {"rg-a": "mp3_192", "rg-b": "low"}
    artist_ids = {row["artist_mbid"]: row["artist_name"] for row in result}
    assert artist_ids == {"pa-1": "Artist A", None: "Artist B"}
