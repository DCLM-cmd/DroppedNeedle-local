from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from repositories.audiodb_models import (
    AudioDBArtistImages,
    AudioDBArtistResponse,
    AudioDBAlbumImages,
    AudioDBAlbumResponse,
)
from services.audiodb_image_service import AudioDBImageService


SAMPLE_ARTIST_RESP = AudioDBArtistResponse(
    idArtist="111239",
    strArtist="Coldplay",
    strMusicBrainzID="cc197bad-dc9c-440d-a5b5-d52ba2e14234",
    strArtistThumb="https://example.com/thumb.jpg",
    strArtistFanart="https://example.com/fanart.jpg",
)

SAMPLE_ALBUM_RESP = AudioDBAlbumResponse(
    idAlbum="2115888",
    strAlbum="Parachutes",
    strMusicBrainzID="1dc4c347-a1db-32aa-b14f-bc9cc507b843",
    strAlbumThumb="https://example.com/album_thumb.jpg",
    strAlbumBack="https://example.com/album_back.jpg",
)

TEST_MBID = "cc197bad-dc9c-440d-a5b5-d52ba2e14234"
TEST_ALBUM_MBID = "1dc4c347-a1db-32aa-b14f-bc9cc507b843"


def _make_settings(
    enabled: bool = True,
    name_search_fallback: bool = False,
    ttl_found: int = 604800,
    ttl_not_found: int = 86400,
    ttl_library: int = 1209600,
) -> MagicMock:
    s = MagicMock()
    s.audiodb_enabled = enabled
    s.audiodb_name_search_fallback = name_search_fallback
    s.cache_ttl_audiodb_found = ttl_found
    s.cache_ttl_audiodb_not_found = ttl_not_found
    s.cache_ttl_audiodb_library = ttl_library
    return s


def _make_service(
    settings: MagicMock | None = None,
    disk_cache: AsyncMock | None = None,
    repo: AsyncMock | None = None,
    memory_cache: AsyncMock | None = None,
) -> AudioDBImageService:
    if settings is None:
        settings = _make_settings()
    prefs = MagicMock()
    prefs.get_advanced_settings.return_value = settings
    if disk_cache is None:
        disk_cache = AsyncMock()
        disk_cache.get_audiodb_artist = AsyncMock(return_value=None)
        disk_cache.get_audiodb_album = AsyncMock(return_value=None)
        disk_cache.set_audiodb_artist = AsyncMock()
        disk_cache.set_audiodb_album = AsyncMock()
    if repo is None:
        repo = AsyncMock()
    return AudioDBImageService(
        audiodb_repo=repo,
        disk_cache=disk_cache,
        preferences_service=prefs,
        memory_cache=memory_cache,
    )




class TestGetCachedArtistImages:
    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self):
        svc = _make_service(settings=_make_settings(enabled=False))
        result = await svc.get_cached_artist_images(TEST_MBID)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_mbid(self):
        svc = _make_service()
        assert await svc.get_cached_artist_images("") is None
        assert await svc.get_cached_artist_images("  ") is None

    @pytest.mark.asyncio
    async def test_returns_none_on_cache_miss(self):
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=None)
        svc = _make_service(disk_cache=disk)
        result = await svc.get_cached_artist_images(TEST_MBID)
        assert result is None
        disk.get_audiodb_artist.assert_awaited_once_with(TEST_MBID)

    @pytest.mark.asyncio
    async def test_returns_images_on_cache_hit(self):
        images = AudioDBArtistImages.from_response(SAMPLE_ARTIST_RESP, lookup_source="mbid")
        raw = msgspec.structs.asdict(images)
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=raw)
        svc = _make_service(disk_cache=disk)
        result = await svc.get_cached_artist_images(TEST_MBID)
        assert result is not None
        assert result.thumb_url == "https://example.com/thumb.jpg"
        assert result.is_negative is False

    @pytest.mark.asyncio
    async def test_returns_none_on_corrupt_cache_data(self):
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value={"lookup_source": 999})
        disk.delete_entity = AsyncMock()
        svc = _make_service(disk_cache=disk)
        result = await svc.get_cached_artist_images(TEST_MBID)
        assert result is None
        disk.delete_entity.assert_awaited_once_with("audiodb_artist", TEST_MBID)




class TestGetCachedAlbumImages:
    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self):
        svc = _make_service(settings=_make_settings(enabled=False))
        result = await svc.get_cached_album_images(TEST_ALBUM_MBID)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_cache_miss(self):
        disk = AsyncMock()
        disk.get_audiodb_album = AsyncMock(return_value=None)
        svc = _make_service(disk_cache=disk)
        result = await svc.get_cached_album_images(TEST_ALBUM_MBID)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_images_on_cache_hit(self):
        images = AudioDBAlbumImages.from_response(SAMPLE_ALBUM_RESP, lookup_source="mbid")
        raw = msgspec.structs.asdict(images)
        disk = AsyncMock()
        disk.get_audiodb_album = AsyncMock(return_value=raw)
        svc = _make_service(disk_cache=disk)
        result = await svc.get_cached_album_images(TEST_ALBUM_MBID)
        assert result is not None
        assert result.album_thumb_url == "https://example.com/album_thumb.jpg"
        assert result.is_negative is False




class TestFetchAndCacheArtistImages:
    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self):
        svc = _make_service(settings=_make_settings(enabled=False))
        result = await svc.fetch_and_cache_artist_images(TEST_MBID)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_mbid(self):
        svc = _make_service()
        result = await svc.fetch_and_cache_artist_images("")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_cached_positive_without_fetching(self):
        images = AudioDBArtistImages.from_response(SAMPLE_ARTIST_RESP, lookup_source="mbid")
        raw = msgspec.structs.asdict(images)
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=raw)
        disk.set_audiodb_artist = AsyncMock()
        repo = AsyncMock()
        svc = _make_service(disk_cache=disk, repo=repo)

        result = await svc.fetch_and_cache_artist_images(TEST_MBID)
        assert result is not None
        assert result.thumb_url == "https://example.com/thumb.jpg"
        repo.get_artist_by_mbid.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetches_by_mbid_and_caches_on_hit(self):
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=None)
        disk.set_audiodb_artist = AsyncMock()
        repo = AsyncMock()
        repo.get_artist_by_mbid = AsyncMock(return_value=SAMPLE_ARTIST_RESP)
        svc = _make_service(disk_cache=disk, repo=repo)

        result = await svc.fetch_and_cache_artist_images(TEST_MBID, name="Coldplay")
        assert result is not None
        assert result.thumb_url == "https://example.com/thumb.jpg"
        assert result.lookup_source == "mbid"
        disk.set_audiodb_artist.assert_awaited_once()
        call_args = disk.set_audiodb_artist.call_args
        assert call_args[1]["ttl_seconds"] == 604800

    @pytest.mark.asyncio
    async def test_monitored_uses_library_ttl(self):
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=None)
        disk.set_audiodb_artist = AsyncMock()
        repo = AsyncMock()
        repo.get_artist_by_mbid = AsyncMock(return_value=SAMPLE_ARTIST_RESP)
        svc = _make_service(disk_cache=disk, repo=repo)

        result = await svc.fetch_and_cache_artist_images(TEST_MBID, is_monitored=True)
        assert result is not None
        call_args = disk.set_audiodb_artist.call_args
        assert call_args[1]["ttl_seconds"] == 1209600
        assert call_args[1]["is_monitored"] is True

    @pytest.mark.asyncio
    async def test_caches_negative_on_mbid_miss(self):
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=None)
        disk.set_audiodb_artist = AsyncMock()
        repo = AsyncMock()
        repo.get_artist_by_mbid = AsyncMock(return_value=None)
        svc = _make_service(disk_cache=disk, repo=repo)

        result = await svc.fetch_and_cache_artist_images(TEST_MBID)
        assert result is not None
        assert result.is_negative is True
        assert result.lookup_source == "mbid"
        disk.set_audiodb_artist.assert_awaited_once()
        call_args = disk.set_audiodb_artist.call_args
        assert call_args[1]["ttl_seconds"] == 86400

    @pytest.mark.asyncio
    async def test_falls_back_to_name_search_on_mbid_miss(self):
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=None)
        disk.set_audiodb_artist = AsyncMock()
        repo = AsyncMock()
        repo.get_artist_by_mbid = AsyncMock(return_value=None)
        repo.search_artist_by_name = AsyncMock(return_value=SAMPLE_ARTIST_RESP)
        svc = _make_service(
            settings=_make_settings(name_search_fallback=True),
            disk_cache=disk, repo=repo,
        )

        result = await svc.fetch_and_cache_artist_images(TEST_MBID, name="Coldplay")
        assert result is not None
        assert result.is_negative is False
        assert result.lookup_source == "name"
        assert disk.set_audiodb_artist.await_count == 2

    @pytest.mark.asyncio
    async def test_no_name_search_when_fallback_disabled(self):
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=None)
        disk.set_audiodb_artist = AsyncMock()
        repo = AsyncMock()
        repo.get_artist_by_mbid = AsyncMock(return_value=None)
        svc = _make_service(
            settings=_make_settings(name_search_fallback=False),
            disk_cache=disk, repo=repo,
        )

        result = await svc.fetch_and_cache_artist_images(TEST_MBID, name="Coldplay")
        assert result is not None
        assert result.is_negative is True
        repo.search_artist_by_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_none_on_repo_exception(self):
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=None)
        repo = AsyncMock()
        repo.get_artist_by_mbid = AsyncMock(side_effect=Exception("API error"))
        svc = _make_service(disk_cache=disk, repo=repo)

        result = await svc.fetch_and_cache_artist_images(TEST_MBID)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_negative_on_name_search_exception(self):
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=None)
        disk.set_audiodb_artist = AsyncMock()
        repo = AsyncMock()
        repo.get_artist_by_mbid = AsyncMock(return_value=None)
        repo.search_artist_by_name = AsyncMock(side_effect=Exception("API error"))
        svc = _make_service(
            settings=_make_settings(name_search_fallback=True),
            disk_cache=disk, repo=repo,
        )

        result = await svc.fetch_and_cache_artist_images(TEST_MBID, name="Coldplay")
        assert result is not None
        assert result.is_negative is True
        assert result.lookup_source == "mbid"

    @pytest.mark.asyncio
    async def test_skips_refetch_on_cached_negative_with_name_source(self):
        negative_name = AudioDBArtistImages.negative(lookup_source="name")
        raw = msgspec.structs.asdict(negative_name)
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=raw)
        repo = AsyncMock()
        svc = _make_service(
            settings=_make_settings(name_search_fallback=True),
            disk_cache=disk, repo=repo,
        )

        result = await svc.fetch_and_cache_artist_images(TEST_MBID, name="Coldplay")
        assert result is not None
        assert result.is_negative is True
        repo.get_artist_by_mbid.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cached_negative_mbid_skips_mbid_call_and_tries_name(self):
        negative_mbid = AudioDBArtistImages.negative(lookup_source="mbid")
        raw = msgspec.structs.asdict(negative_mbid)
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=raw)
        disk.set_audiodb_artist = AsyncMock()
        repo = AsyncMock()
        repo.search_artist_by_name = AsyncMock(return_value=SAMPLE_ARTIST_RESP)
        svc = _make_service(
            settings=_make_settings(name_search_fallback=True),
            disk_cache=disk, repo=repo,
        )

        result = await svc.fetch_and_cache_artist_images(TEST_MBID, name="Coldplay")
        assert result is not None
        assert result.is_negative is False
        assert result.lookup_source == "name"
        repo.get_artist_by_mbid.assert_not_awaited()
        repo.search_artist_by_name.assert_awaited_once_with("Coldplay")

    @pytest.mark.asyncio
    async def test_cached_negative_mbid_returns_cached_when_no_name(self):
        negative_mbid = AudioDBArtistImages.negative(lookup_source="mbid")
        raw = msgspec.structs.asdict(negative_mbid)
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=raw)
        repo = AsyncMock()
        svc = _make_service(
            settings=_make_settings(name_search_fallback=True),
            disk_cache=disk, repo=repo,
        )

        result = await svc.fetch_and_cache_artist_images(TEST_MBID)
        assert result is not None
        assert result.is_negative is True
        repo.get_artist_by_mbid.assert_not_awaited()
        repo.search_artist_by_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cached_negative_mbid_returns_cached_when_fallback_disabled(self):
        negative_mbid = AudioDBArtistImages.negative(lookup_source="mbid")
        raw = msgspec.structs.asdict(negative_mbid)
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=raw)
        repo = AsyncMock()
        svc = _make_service(
            settings=_make_settings(name_search_fallback=False),
            disk_cache=disk, repo=repo,
        )

        result = await svc.fetch_and_cache_artist_images(TEST_MBID, name="Coldplay")
        assert result is not None
        assert result.is_negative is True
        repo.get_artist_by_mbid.assert_not_awaited()
        repo.search_artist_by_name.assert_not_awaited()


class TestFetchAndCacheAlbumImages:
    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self):
        svc = _make_service(settings=_make_settings(enabled=False))
        result = await svc.fetch_and_cache_album_images(TEST_ALBUM_MBID)
        assert result is None

    @pytest.mark.asyncio
    async def test_fetches_by_mbid_and_caches(self):
        disk = AsyncMock()
        disk.get_audiodb_album = AsyncMock(return_value=None)
        disk.set_audiodb_album = AsyncMock()
        repo = AsyncMock()
        repo.get_album_by_mbid = AsyncMock(return_value=SAMPLE_ALBUM_RESP)
        svc = _make_service(disk_cache=disk, repo=repo)

        result = await svc.fetch_and_cache_album_images(
            TEST_ALBUM_MBID, artist_name="Coldplay", album_name="Parachutes",
        )
        assert result is not None
        assert result.album_thumb_url == "https://example.com/album_thumb.jpg"
        assert result.lookup_source == "mbid"

    @pytest.mark.asyncio
    async def test_caches_negative_on_mbid_miss(self):
        disk = AsyncMock()
        disk.get_audiodb_album = AsyncMock(return_value=None)
        disk.set_audiodb_album = AsyncMock()
        repo = AsyncMock()
        repo.get_album_by_mbid = AsyncMock(return_value=None)
        svc = _make_service(disk_cache=disk, repo=repo)

        result = await svc.fetch_and_cache_album_images(TEST_ALBUM_MBID)
        assert result is not None
        assert result.is_negative is True

    @pytest.mark.asyncio
    async def test_falls_back_to_name_search_for_album(self):
        disk = AsyncMock()
        disk.get_audiodb_album = AsyncMock(return_value=None)
        disk.set_audiodb_album = AsyncMock()
        repo = AsyncMock()
        repo.get_album_by_mbid = AsyncMock(return_value=None)
        repo.search_album_by_name = AsyncMock(return_value=SAMPLE_ALBUM_RESP)
        svc = _make_service(
            settings=_make_settings(name_search_fallback=True),
            disk_cache=disk, repo=repo,
        )

        result = await svc.fetch_and_cache_album_images(
            TEST_ALBUM_MBID, artist_name="Coldplay", album_name="Parachutes",
        )
        assert result is not None
        assert result.is_negative is False
        assert result.lookup_source == "name"

    @pytest.mark.asyncio
    async def test_no_name_fallback_without_both_names(self):
        disk = AsyncMock()
        disk.get_audiodb_album = AsyncMock(return_value=None)
        disk.set_audiodb_album = AsyncMock()
        repo = AsyncMock()
        repo.get_album_by_mbid = AsyncMock(return_value=None)
        svc = _make_service(
            settings=_make_settings(name_search_fallback=True),
            disk_cache=disk, repo=repo,
        )

        result = await svc.fetch_and_cache_album_images(
            TEST_ALBUM_MBID, artist_name="Coldplay", album_name=None,
        )
        assert result is not None
        assert result.is_negative is True
        repo.search_album_by_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cached_negative_mbid_skips_mbid_call_and_tries_name_album(self):
        negative_mbid = AudioDBAlbumImages.negative(lookup_source="mbid")
        raw = msgspec.structs.asdict(negative_mbid)
        disk = AsyncMock()
        disk.get_audiodb_album = AsyncMock(return_value=raw)
        disk.set_audiodb_album = AsyncMock()
        repo = AsyncMock()
        repo.search_album_by_name = AsyncMock(return_value=SAMPLE_ALBUM_RESP)
        svc = _make_service(
            settings=_make_settings(name_search_fallback=True),
            disk_cache=disk, repo=repo,
        )

        result = await svc.fetch_and_cache_album_images(
            TEST_ALBUM_MBID, artist_name="Coldplay", album_name="Parachutes",
        )
        assert result is not None
        assert result.is_negative is False
        assert result.lookup_source == "name"
        repo.get_album_by_mbid.assert_not_awaited()
        repo.search_album_by_name.assert_awaited_once_with("Coldplay", "Parachutes")

    @pytest.mark.asyncio
    async def test_cached_negative_mbid_returns_cached_when_fallback_disabled_album(self):
        negative_mbid = AudioDBAlbumImages.negative(lookup_source="mbid")
        raw = msgspec.structs.asdict(negative_mbid)
        disk = AsyncMock()
        disk.get_audiodb_album = AsyncMock(return_value=raw)
        repo = AsyncMock()
        svc = _make_service(
            settings=_make_settings(name_search_fallback=False),
            disk_cache=disk, repo=repo,
        )

        result = await svc.fetch_and_cache_album_images(
            TEST_ALBUM_MBID, artist_name="Coldplay", album_name="Parachutes",
        )
        assert result is not None
        assert result.is_negative is True
        repo.get_album_by_mbid.assert_not_awaited()
        repo.search_album_by_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_none_on_repo_exception(self):
        disk = AsyncMock()
        disk.get_audiodb_album = AsyncMock(return_value=None)
        repo = AsyncMock()
        repo.get_album_by_mbid = AsyncMock(side_effect=Exception("fail"))
        svc = _make_service(disk_cache=disk, repo=repo)

        result = await svc.fetch_and_cache_album_images(TEST_ALBUM_MBID)
        assert result is None

    @pytest.mark.asyncio
    async def test_monitored_album_uses_library_ttl(self):
        disk = AsyncMock()
        disk.get_audiodb_album = AsyncMock(return_value=None)
        disk.set_audiodb_album = AsyncMock()
        repo = AsyncMock()
        repo.get_album_by_mbid = AsyncMock(return_value=SAMPLE_ALBUM_RESP)
        svc = _make_service(disk_cache=disk, repo=repo)

        result = await svc.fetch_and_cache_album_images(TEST_ALBUM_MBID, is_monitored=True)
        assert result is not None
        call_args = disk.set_audiodb_album.call_args
        assert call_args[1]["ttl_seconds"] == 1209600




class TestMemoryCacheReadThrough:
    @pytest.mark.asyncio
    async def test_disk_hit_promotes_to_memory_artist(self):
        images = AudioDBArtistImages.from_response(SAMPLE_ARTIST_RESP, lookup_source="mbid")
        raw = msgspec.structs.asdict(images)
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=raw)
        mem = AsyncMock()
        mem.get = AsyncMock(return_value=None)
        mem.set = AsyncMock()
        svc = _make_service(disk_cache=disk, memory_cache=mem)

        result = await svc.get_cached_artist_images(TEST_MBID)
        assert result is not None
        assert result.thumb_url == "https://example.com/thumb.jpg"
        mem.set.assert_awaited_once()
        set_key = mem.set.call_args[0][0]
        assert set_key == f"audiodb_artist:{TEST_MBID}"

    @pytest.mark.asyncio
    async def test_memory_hit_skips_disk_artist(self):
        images = AudioDBArtistImages.from_response(SAMPLE_ARTIST_RESP, lookup_source="mbid")
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=None)
        mem = AsyncMock()
        mem.get = AsyncMock(return_value=images)
        mem.set = AsyncMock()
        svc = _make_service(disk_cache=disk, memory_cache=mem)

        result = await svc.get_cached_artist_images(TEST_MBID)
        assert result is not None
        assert result.thumb_url == "https://example.com/thumb.jpg"
        disk.get_audiodb_artist.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disk_hit_promotes_to_memory_album(self):
        images = AudioDBAlbumImages.from_response(SAMPLE_ALBUM_RESP, lookup_source="mbid")
        raw = msgspec.structs.asdict(images)
        disk = AsyncMock()
        disk.get_audiodb_album = AsyncMock(return_value=raw)
        mem = AsyncMock()
        mem.get = AsyncMock(return_value=None)
        mem.set = AsyncMock()
        svc = _make_service(disk_cache=disk, memory_cache=mem)

        result = await svc.get_cached_album_images(TEST_ALBUM_MBID)
        assert result is not None
        assert result.album_thumb_url == "https://example.com/album_thumb.jpg"
        mem.set.assert_awaited_once()
        set_key = mem.set.call_args[0][0]
        assert set_key == f"audiodb_album:{TEST_ALBUM_MBID}"

    @pytest.mark.asyncio
    async def test_fetch_and_cache_promotes_to_memory(self):
        disk = AsyncMock()
        disk.get_audiodb_artist = AsyncMock(return_value=None)
        disk.set_audiodb_artist = AsyncMock()
        repo = AsyncMock()
        repo.get_artist_by_mbid = AsyncMock(return_value=SAMPLE_ARTIST_RESP)
        mem = AsyncMock()
        mem.get = AsyncMock(return_value=None)
        mem.set = AsyncMock()
        svc = _make_service(disk_cache=disk, repo=repo, memory_cache=mem)

        result = await svc.fetch_and_cache_artist_images(TEST_MBID)
        assert result is not None
        assert mem.set.await_count >= 1
        set_key = mem.set.call_args[0][0]
        assert set_key == f"audiodb_artist:{TEST_MBID}"




class TestClearAudioDBRoute:
    @pytest.mark.asyncio
    async def test_clear_audiodb_endpoint_pattern(self):
        from api.v1.routes.cache import router

        paths = [r.path for r in router.routes]
        assert "/cache/clear/audiodb" in paths


class TestClearAudioDBService:
    @pytest.mark.asyncio
    async def test_clear_audiodb_clears_disk_and_memory(self):
        from services.cache_service import CacheService

        disk_cache = AsyncMock()
        disk_cache.get_stats = MagicMock(return_value={
            "audiodb_artist_count": 5,
            "audiodb_album_count": 3,
        })
        disk_cache.clear_audiodb = AsyncMock()
        mem_cache = AsyncMock()
        mem_cache.clear_prefix = AsyncMock(return_value=4)
        library_db = MagicMock()
        svc = CacheService(
            cache=mem_cache, library_db=library_db, disk_cache=disk_cache,
            mb_response_store=MagicMock(),
        )

        result = await svc.clear_audiodb()
        assert result.success is True
        assert result.cleared_disk_files == 8
        assert result.cleared_memory_entries == 4
        disk_cache.clear_audiodb.assert_awaited_once()
        mem_cache.clear_prefix.assert_awaited_once_with("audiodb_")

    @pytest.mark.asyncio
    async def test_clear_audiodb_invalidates_cached_stats(self):
        from services.cache_service import CacheService

        disk_cache = AsyncMock()
        disk_cache.get_stats = MagicMock(return_value={
            "audiodb_artist_count": 0,
            "audiodb_album_count": 0,
        })
        disk_cache.clear_audiodb = AsyncMock()
        mem_cache = AsyncMock()
        mem_cache.clear_prefix = AsyncMock(return_value=0)
        library_db = MagicMock()
        svc = CacheService(
            cache=mem_cache, library_db=library_db, disk_cache=disk_cache,
            mb_response_store=MagicMock(),
        )
        svc._cached_stats = {"some": "stats"}

        await svc.clear_audiodb()
        assert svc._cached_stats is None
