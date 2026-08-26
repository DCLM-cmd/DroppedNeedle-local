"""Service-level tests for filter-aware artist release pagination."""

import os
import tempfile
from typing import Any

os.environ.setdefault("ROOT_APP_DIR", tempfile.mkdtemp())

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.exceptions import ClientDisconnectedError
from infrastructure.cache.cache_keys import mb_artist_release_groups_key
from services.artist_service import ArtistService


ARTIST_MBID = "f4a31f0a-51dd-4fa7-986d-3095c40c5ed9"


def _make_release_group(
    rg_id: str, title: str, primary_type: str, date: str = "2020-01-01"
) -> dict:
    return {
        "id": rg_id,
        "title": title,
        "primary-type": primary_type,
        "secondary-types": [],
        "first-release-date": date,
    }


def _make_prefs(
    primary_types: list[str] | None = None, secondary_types: list[str] | None = None
) -> MagicMock:
    p = MagicMock()
    p.get_preferences.return_value = MagicMock(
        primary_types=primary_types
        if primary_types is not None
        else ["Album", "Single", "EP"],
        secondary_types=secondary_types
        if secondary_types is not None
        else ["Studio", "Live", "Compilation"],
    )
    p.get_advanced_settings.return_value = MagicMock(
        cache_ttl_artist_library=21600,
        cache_ttl_artist_non_library=3600,
    )
    return p


def _make_dict_cache() -> tuple[AsyncMock, dict[str, Any]]:
    """AsyncMock cache backed by a real dict, for multi-request tests."""
    store: dict[str, Any] = {}
    cache = AsyncMock()
    cache.get = AsyncMock(side_effect=store.get)
    cache.set = AsyncMock(
        side_effect=lambda key, value, ttl_seconds: store.__setitem__(key, value)
    )
    return cache, store


def _make_service(
    *,
    mb_release_pages: list[tuple[list[dict], int]] | None = None,
    prefs: MagicMock | None = None,
    memory_cache: AsyncMock | None = None,
) -> ArtistService:
    mb_repo = AsyncMock()
    if mb_release_pages is not None:
        mb_repo.get_artist_release_groups = AsyncMock(side_effect=mb_release_pages)
    else:
        mb_repo.get_artist_release_groups = AsyncMock(return_value=([], 0))

    library_repo = MagicMock()
    library_repo.is_configured.return_value = False
    library_repo.get_library_mbids = AsyncMock(return_value=set())
    library_repo.get_requested_mbids = AsyncMock(return_value=set())
    library_repo.get_artist_mbids = AsyncMock(return_value=set())

    wikidata_repo = AsyncMock()

    if memory_cache is None:
        memory_cache = AsyncMock()
        memory_cache.get = AsyncMock(return_value=None)
        memory_cache.set = AsyncMock()

    disk_cache = AsyncMock()
    disk_cache.get_artist = AsyncMock(return_value=None)
    disk_cache.set_artist = AsyncMock()

    return ArtistService(
        mb_repo=mb_repo,
        library_repo=library_repo,
        wikidata_repo=wikidata_repo,
        preferences_service=prefs or _make_prefs(),
        memory_cache=memory_cache,
        disk_cache=disk_cache,
    )


class TestFilterAwarePagination:
    @pytest.mark.asyncio
    async def test_single_page_fits_filter(self):
        rg1 = _make_release_group("rg-1", "Album A", "Album")
        rg2 = _make_release_group("rg-2", "Single B", "Single")
        svc = _make_service(mb_release_pages=[([rg1, rg2], 2)])

        result = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)

        assert len(result.albums) == 1
        assert result.albums[0].title == "Album A"
        assert len(result.singles) == 1
        assert result.singles[0].title == "Single B"
        assert result.has_more is False
        assert result.next_offset is None
        assert result.returned_count == 2
        assert result.source_total_count == 2

    @pytest.mark.asyncio
    async def test_sparse_filter_scans_multiple_batches(self):
        batch1 = [
            _make_release_group(f"rg-{i}", f"Broadcast {i}", "Broadcast")
            for i in range(5)
        ]
        batch2 = [_make_release_group("rg-album", "Real Album", "Album")]
        svc = _make_service(
            mb_release_pages=[
                (batch1, 6),
                (batch2, 6),
            ]
        )

        result = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)

        assert result.albums[0].title == "Real Album"
        assert result.returned_count == 1
        assert result.source_total_count == 1

    @pytest.mark.asyncio
    async def test_empty_result_set(self):
        svc = _make_service(mb_release_pages=[([], 0)])

        result = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)

        assert result.returned_count == 0
        assert result.has_more is False
        assert result.next_offset is None
        assert result.source_total_count == 0

    @pytest.mark.asyncio
    async def test_offset_reflects_client_param(self):
        rg = _make_release_group("rg-1", "Album 1", "Album")
        svc = _make_service(mb_release_pages=[([rg], 1)])

        result = await svc.get_artist_releases(ARTIST_MBID, offset=200, limit=50)

        assert result.offset == 200

    @pytest.mark.asyncio
    async def test_returned_count_across_categories(self):
        rgs = [
            _make_release_group("rg-a1", "Album 1", "Album"),
            _make_release_group("rg-a2", "Album 2", "Album"),
            _make_release_group("rg-s1", "Single 1", "Single"),
            _make_release_group("rg-e1", "EP 1", "EP"),
        ]
        svc = _make_service(mb_release_pages=[(rgs, 4)])

        result = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)

        assert result.returned_count == 4
        assert len(result.albums) == 2
        assert len(result.singles) == 1
        assert len(result.eps) == 1

    @pytest.mark.asyncio
    async def test_limit_controls_returned_items(self):
        rgs = [
            _make_release_group("rg-a1", "Album 1", "Album"),
            _make_release_group("rg-a2", "Album 2", "Album"),
            _make_release_group("rg-a3", "Album 3", "Album"),
            _make_release_group("rg-s1", "Single 1", "Single"),
            _make_release_group("rg-e1", "EP 1", "EP"),
        ]
        svc = _make_service(mb_release_pages=[(rgs, 5)])

        result = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=3)

        assert result.returned_count == 3
        assert len(result.albums) == 3
        assert len(result.singles) == 0
        assert len(result.eps) == 0
        assert result.has_more is True
        assert result.next_offset == 3

    @pytest.mark.asyncio
    async def test_exception_returns_empty_page(self):
        svc = _make_service()
        svc._library_repo.get_library_mbids = AsyncMock(
            side_effect=RuntimeError("boom")
        )

        result = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)

        assert result.returned_count == 0
        assert result.has_more is False
        assert result.next_offset is None

    @pytest.mark.asyncio
    async def test_empty_filter_types_returns_immediately(self):
        svc = _make_service(
            prefs=_make_prefs(primary_types=[], secondary_types=[]),
        )

        result = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)

        assert result.returned_count == 0
        assert result.has_more is False
        assert result.next_offset is None
        svc._mb_repo.get_artist_release_groups.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_types_filtered_out_except_one(self):
        batch = [
            _make_release_group(f"rg-b{i}", f"Broadcast {i}", "Broadcast")
            for i in range(5)
        ] + [_make_release_group("rg-album", "Found Album", "Album")]
        svc = _make_service(
            mb_release_pages=[
                (batch, 6),
            ]
        )

        result = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)

        assert result.returned_count == 1
        assert result.albums[0].title == "Found Album"
        assert result.source_total_count == 1
        assert result.has_more is False

    @pytest.mark.asyncio
    async def test_global_sort_across_batches(self):
        batch1 = [_make_release_group("rg-a", "Old Album", "Album", "2010-01-01")]
        batch2 = [_make_release_group("rg-b", "New Album", "Album", "2020-01-01")]
        svc = _make_service(
            mb_release_pages=[
                (batch1, 2),
                (batch2, 2),
            ]
        )

        result = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)

        assert len(result.albums) == 2
        assert result.albums[0].title == "New Album"
        assert result.albums[1].title == "Old Album"

    @pytest.mark.asyncio
    async def test_next_offset_is_arithmetic(self):
        rgs = [
            _make_release_group(f"rg-{i}", f"Album {i}", "Album") for i in range(100)
        ]
        cache, _ = _make_dict_cache()
        svc = _make_service(mb_release_pages=[(rgs, 100)], memory_cache=cache)

        page1 = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=10)
        page2 = await svc.get_artist_releases(ARTIST_MBID, offset=10, limit=10)

        assert page1.has_more is True
        assert page1.returned_count == 10
        assert page1.next_offset == 10
        assert page2.returned_count == 10
        assert page2.next_offset == 20

    @pytest.mark.asyncio
    async def test_no_drops_across_sequential_pages(self):
        rgs = [_make_release_group(f"rg-{i}", f"Album {i}", "Album") for i in range(10)]
        cache, _ = _make_dict_cache()
        svc = _make_service(mb_release_pages=[(rgs, 10)], memory_cache=cache)

        page1 = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=3)
        page2 = await svc.get_artist_releases(ARTIST_MBID, offset=3, limit=3)
        page3 = await svc.get_artist_releases(ARTIST_MBID, offset=6, limit=3)
        page4 = await svc.get_artist_releases(ARTIST_MBID, offset=9, limit=3)

        pages = [page1, page2, page3, page4]
        assert [page.returned_count for page in pages] == [3, 3, 3, 1]
        assert [page.next_offset for page in pages] == [3, 6, 9, None]
        ids = [item.id for page in pages for item in page.albums]
        assert ids == [item["id"] for item in rgs]
        assert len(ids) == len(set(ids))

    @pytest.mark.asyncio
    async def test_disconnect_during_multi_page_fetch_raises(self):
        batch1 = [
            _make_release_group(f"rg-{i}", f"Album {i}", "Album") for i in range(5)
        ]
        batch2 = [
            _make_release_group(f"rg-{i + 5}", f"Album {i + 5}", "Album")
            for i in range(5)
        ]
        svc = _make_service(mb_release_pages=[(batch1, 200), (batch2, 200)])
        is_disconnected = AsyncMock(side_effect=[False, False, True])

        with pytest.raises(ClientDisconnectedError):
            await svc.get_artist_releases(
                ARTIST_MBID,
                offset=0,
                limit=50,
                is_disconnected=is_disconnected,
            )

        svc._cache.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_complete_fetch_cached(self):
        batch1 = [
            _make_release_group(f"rg-{i}", f"Album {i}", "Album") for i in range(100)
        ]
        batch2 = [
            _make_release_group(f"rg-{100 + i}", f"Album {100 + i}", "Album")
            for i in range(9)
        ]
        cache, store = _make_dict_cache()
        svc = _make_service(
            mb_release_pages=[(batch1, 109), (batch2, 109)],
            memory_cache=cache,
        )

        first = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)
        second = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)

        assert first.returned_count == 50
        assert first.source_total_count == 109
        assert second.returned_count == 50
        assert second.source_total_count == 109
        assert svc._mb_repo.get_artist_release_groups.await_count == 2
        svc._cache.set.assert_awaited_once()
        cached = store[mb_artist_release_groups_key(ARTIST_MBID)]
        assert len(cached) == 109

    @pytest.mark.asyncio
    async def test_partial_fetch_not_cached(self):
        batch1 = [
            _make_release_group(f"rg-{i}", f"Album {i}", "Album") for i in range(100)
        ]
        svc = _make_service(mb_release_pages=[(batch1, 200), ([], 200)])

        result = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)

        assert result.returned_count == 50
        assert result.source_total_count == 100
        svc._cache.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gid_sorted_pages_no_drop_regression(self):
        # MB's browse endpoint pages by one order but its JSON serializer
        # re-sorts each page by GID: the target RG (Negative Spaces) can land
        # anywhere in a page, and scan-position pagination dropped it.
        negative_spaces_id = "fe83cc29-01a9-4650-95ca-d3e135c07278"
        page1 = [
            _make_release_group(f"aaaaaaaa-0000-4000-8000-{i:012d}", f"Album {i}", "Album")
            for i in range(99)
        ] + [
            _make_release_group(
                negative_spaces_id, "Negative Spaces", "Album", "2024-11-15"
            )
        ]
        page2 = [
            _make_release_group(f"bbbbbbbb-0000-4000-8000-{i:012d}", f"Album B{i}", "Album")
            for i in range(9)
        ]
        cache, _ = _make_dict_cache()
        svc = _make_service(
            mb_release_pages=[(page1, 109), (page2, 109)],
            memory_cache=cache,
        )

        page_a = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)
        page_b = await svc.get_artist_releases(ARTIST_MBID, offset=50, limit=50)
        page_c = await svc.get_artist_releases(ARTIST_MBID, offset=100, limit=50)

        ids = [item.id for page in (page_a, page_b, page_c) for item in page.albums]
        assert len(ids) == 109
        assert len(ids) == len(set(ids))
        assert ids.count(negative_spaces_id) == 1
        assert page_c.has_more is False

    @pytest.mark.asyncio
    async def test_overlapping_pages_deduped(self):
        rgs = [_make_release_group(f"rg-{i}", f"Album {i}", "Album") for i in range(15)]
        page1 = rgs[:10]
        page2 = rgs[5:]  # overlaps page1 by 50%
        svc = _make_service(mb_release_pages=[(page1, 15), (page2, 15)])

        result = await svc.get_artist_releases(ARTIST_MBID, offset=0, limit=50)

        ids = [item.id for item in result.albums]
        assert len(ids) == 15
        assert len(ids) == len(set(ids))
