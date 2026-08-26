"""Tests that the basic artist info path returns correctly and skips Wikidata enrichment."""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from models.artist import ArtistInfo, ReleaseItem
from services.artist_service import ArtistService


ARTIST_MBID = "f4a31f0a-51dd-4fa7-986d-3095c40c5ed9"


def _make_mb_artist() -> dict:
    return {
        "id": ARTIST_MBID,
        "name": "Test Artist",
        "type": "Group",
        "country": "GB",
        "disambiguation": "",
        "life-span": {"begin": "2000", "end": None, "ended": "false"},
        "tag-list": [{"name": "rock", "count": 5}],
        "alias-list": [],
        "url-relation-list": [],
        "release-group-list": [
            {
                "id": "rg-001",
                "title": "First Album",
                "type": "Album",
                "primary-type": "Album",
                "secondary-type-list": [],
                "first-release-date": "2020-01-01",
            }
        ],
        "release-group-count": 1,
    }


def _make_service(
    *, cached_artist: ArtistInfo | None = None
) -> tuple[ArtistService, AsyncMock]:
    mb_repo = AsyncMock()
    mb_repo.get_artist_by_id = AsyncMock(return_value=_make_mb_artist())

    library_repo = MagicMock()
    library_repo.is_configured.return_value = False
    library_repo.get_library_mbids = AsyncMock(return_value=set())
    library_repo.get_requested_mbids = AsyncMock(return_value=set())
    library_repo.get_artist_mbids = AsyncMock(return_value=set())

    wikidata_repo = AsyncMock()
    wikidata_repo.get_wikidata_info = AsyncMock(
        side_effect=AssertionError("Wikidata should NOT be called in basic path")
    )

    prefs = MagicMock()
    prefs.get_preferences.return_value = MagicMock(
        primary_types=["Album", "Single", "EP"],
        secondary_types=[],
    )
    prefs.get_advanced_settings.return_value = MagicMock(
        cache_ttl_artist_library=21600,
        cache_ttl_artist_non_library=3600,
    )

    memory_cache = AsyncMock()
    memory_cache.get = AsyncMock(return_value=cached_artist)
    memory_cache.set = AsyncMock()

    disk_cache = AsyncMock()
    disk_cache.get_artist = AsyncMock(return_value=None)
    disk_cache.set_artist = AsyncMock()

    svc = ArtistService(
        mb_repo=mb_repo,
        library_repo=library_repo,
        wikidata_repo=wikidata_repo,
        preferences_service=prefs,
        memory_cache=memory_cache,
        disk_cache=disk_cache,
    )
    svc.test_memory_cache = memory_cache
    svc.test_disk_cache = disk_cache
    return svc, wikidata_repo


class TestGetArtistInfoBasic:
    @pytest.mark.asyncio
    async def test_cold_cache_skips_wikidata(self):
        svc, wikidata_repo = _make_service()

        result = await svc.get_artist_info_basic(ARTIST_MBID)

        assert result.name == "Test Artist"
        assert result.musicbrainz_id == ARTIST_MBID
        assert result.description is None
        assert result.image is None
        wikidata_repo.get_wikidata_info.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cold_cache_sets_release_group_count(self):
        svc, _ = _make_service()

        result = await svc.get_artist_info_basic(ARTIST_MBID)

        assert result.release_group_count == 1

    @pytest.mark.asyncio
    async def test_cached_artist_returned_directly(self):
        cached = ArtistInfo(
            name="Cached Artist",
            musicbrainz_id=ARTIST_MBID,
            description="Cached description",
            image="https://example.com/img.jpg",
            albums=[ReleaseItem(id="rg-cached", title="Cached Album", type="Album")],
        )
        svc, wikidata_repo = _make_service(cached_artist=cached)

        result = await svc.get_artist_info_basic(ARTIST_MBID)

        assert result.name == "Cached Artist"
        assert result.description == "Cached description"
        assert result.image == "https://example.com/img.jpg"
        wikidata_repo.get_wikidata_info.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_mbid_raises_value_error(self):
        svc, _ = _make_service()

        with pytest.raises(ValueError):
            await svc.get_artist_info_basic("not-a-uuid")


class TestBasicInfoDeferralEquivalence:
    """B3.1: moving the disk mirror to a deferred task must leave the returned
    payload byte-identical; the memory write stays inline (readable on return)."""

    @pytest.mark.asyncio
    async def test_payload_byte_identical_and_memory_inline(self):
        import msgspec

        svc, _wikidata = _make_service()
        result = await svc.get_artist_info_basic(ARTIST_MBID)

        # Memory cache holds the exact object returned, inline on return.
        # (A3 adds a second, separate write: the seeded mb:artist_rgs page-1
        # entry from the embedded detail payload.)
        artist_info_writes = [
            call
            for call in svc.test_memory_cache.set.await_args_list
            if call.args[0].startswith("artist_info:")
        ]
        assert len(artist_info_writes) == 1
        cached_value = artist_info_writes[0].args[1]
        assert msgspec.json.encode(cached_value) == msgspec.json.encode(result)
        rgs_writes = [
            call
            for call in svc.test_memory_cache.set.await_args_list
            if call.args[0].startswith("mb:artist_rgs:")
        ]
        assert len(rgs_writes) == 1  # A3 seeding wrote the embedded page-1

        # Disk mirror is deferred but completes with the same payload.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        svc.test_disk_cache.set_artist.assert_awaited_once()
        disk_args = svc.test_disk_cache.set_artist.await_args
        assert disk_args.args[0] == ARTIST_MBID
        assert msgspec.json.encode(disk_args.args[1]) == msgspec.json.encode(result)

    @pytest.mark.asyncio
    async def test_deferred_disk_failure_does_not_break_response(self):
        svc, _wikidata = _make_service()
        svc.test_disk_cache.set_artist = AsyncMock(
            side_effect=RuntimeError("disk gone")
        )

        result = await svc.get_artist_info_basic(ARTIST_MBID)

        assert result.name == "Test Artist"
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # let the deferred task fail; response unaffected
