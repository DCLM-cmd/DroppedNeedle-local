"""ST1 helper unit tests: invalidate_catalog_scope deletes the EXACT entity
key set (casefolded cross-product), honors include_lists, mirrors disk/cover
legs, and proves the stale-identity guard - a scoped invalidation leaves no
old identity behind for the next reader."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from infrastructure.cache.cache_keys import (
    catalog_entity_prefixes,
    catalog_list_prefixes,
)
from infrastructure.cache.catalog_invalidation import invalidate_catalog_scope
from infrastructure.cache.memory_cache import InMemoryCache

ALBUM = "667DAB68-E5F0-40A8-8BF1-7AC99E35CEBE"  # uppercase on purpose
ARTIST = "88d17133-abbc-42db-9526-4e2c1db60336"


async def _seed(cache: InMemoryCache) -> set[str]:
    entity_keys = {
        f"{prefix}{mbid.casefold()}"
        for prefix in catalog_entity_prefixes()
        for mbid in (ALBUM, ARTIST)
    }
    list_keys = {f"{prefix}bulk" for prefix in catalog_list_prefixes()}
    unrelated = {"unrelated:key"}
    for key in entity_keys | list_keys | unrelated:
        await cache.set(key, "payload")
    return entity_keys | list_keys | unrelated


class TestEntityDeleteExactness:
    @pytest.mark.asyncio
    async def test_deletes_exact_casefolded_cross_product(self):
        cache = InMemoryCache(max_entries=500)
        seeded = await _seed(cache)

        await invalidate_catalog_scope(
            cache,
            album_mbids={ALBUM},
            artist_mbids={ARTIST},
            include_lists=False,
        )

        expected_gone = {
            f"{prefix}{mbid.casefold()}"
            for prefix in catalog_entity_prefixes()
            for mbid in (ALBUM, ARTIST)
        }
        for key in expected_gone:
            assert await cache.get(key) is None, key
        # Everything else survives (lists untouched without include_lists).
        for prefix in catalog_list_prefixes():
            assert await cache.get(f"{prefix}bulk") == "payload"
        assert await cache.get("unrelated:key") == "payload"

    @pytest.mark.asyncio
    async def test_include_lists_sweeps_snapshot_partition(self):
        cache = InMemoryCache(max_entries=500)
        seeded = await _seed(cache)

        await invalidate_catalog_scope(
            cache,
            album_mbids={ALBUM},
            artist_mbids={ARTIST},
            include_lists=True,
        )

        for prefix in catalog_list_prefixes():
            assert await cache.get(f"{prefix}bulk") is None
        for prefix in catalog_entity_prefixes():
            assert await cache.get(f"{prefix}{ARTIST}") is None

    @pytest.mark.asyncio
    async def test_duplicate_and_mixed_case_ids_deduplicate(self):
        cache = InMemoryCache(max_entries=500)
        seeded = await _seed(cache)

        # Same ids twice with case drift must behave like one id.
        await invalidate_catalog_scope(
            cache,
            album_mbids=[ALBUM.lower(), ALBUM.upper()],
            artist_mbids=[ARTIST],
        )

        gone = sum(
            1
            for prefix in catalog_entity_prefixes()
            if cache._cache.get(f"{prefix}{ALBUM.lower()}") is None
        )
        assert gone == len(catalog_entity_prefixes())

    @pytest.mark.asyncio
    async def test_no_ids_with_lists_disabled_is_a_noop(self):
        cache = InMemoryCache(max_entries=100)
        seeded = await _seed(cache)

        # No ids AND lists disabled -> strictly nothing may happen.
        await invalidate_catalog_scope(cache, include_lists=False)

        for key in seeded:
            assert await cache.get(key) == "payload"


class TestMirrorLegs:
    @pytest.mark.asyncio
    async def test_disk_leg_receives_casefolded_ids(self):
        cache = InMemoryCache(max_entries=100)
        disk = AsyncMock()

        await invalidate_catalog_scope(
            cache,
            disk_cache=disk,
            album_mbids={ALBUM},
            artist_mbids={ARTIST},
            include_lists=False,
        )

        disk.delete_album.assert_awaited_once_with(ALBUM.casefold())
        disk.delete_artist.assert_awaited_once_with(ARTIST.casefold())

    @pytest.mark.asyncio
    async def test_cover_leg_uses_precedent_calls(self):
        cache = InMemoryCache(max_entries=100)
        cover_repo = AsyncMock()

        await invalidate_catalog_scope(
            cache,
            cover_repo=cover_repo,
            album_mbids={ALBUM},
            artist_mbids={ARTIST},
            include_lists=False,
        )

        cover_repo.delete_covers_for_album.assert_awaited_once_with(ALBUM.casefold())
        cover_repo.delete_covers_for_artist.assert_awaited_once_with(ARTIST.casefold())


class TestStaleIdentityGuard:
    @pytest.mark.asyncio
    async def test_scoped_invalidation_serves_new_identity_synchronously(self):
        """Re-identification contract: after the scoped delete there is NO old
        identity left under any touched key - the very next read rebuilds from
        upstream (i.e. serves the NEW identity)."""
        cache = InMemoryCache(max_entries=100)
        old_identity_key = f"artist_info:{ARTIST}"
        await cache.set(old_identity_key, {"stale": True})

        await invalidate_catalog_scope(
            cache, artist_mbids={ARTIST}, include_lists=False
        )

        assert await cache.get(old_identity_key) is None
