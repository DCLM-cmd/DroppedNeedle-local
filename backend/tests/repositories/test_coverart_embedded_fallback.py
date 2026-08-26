from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import repositories.coverart_repository as coverart_repository_module
from repositories.coverart_repository import (
    CoverArtRepository,
    _sniff_image_content_type,
)


RELEASE_GROUP_MBID = "11111111-1111-1111-1111-111111111111"
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def _miss_external(monkeypatch):
    async def dedupe_return_none(_key, _factory):
        return None

    monkeypatch.setattr(
        coverart_repository_module._deduplicator, "dedupe", dedupe_return_none
    )


@pytest.mark.parametrize(
    "data,expected",
    [
        (_JPEG, "image/jpeg"),
        (_PNG, "image/png"),
        (b"GIF89a" + b"\x00" * 8, "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', None),
        (b"not an image", None),
        (b"\xff\xd8", None),  # too short
    ],
)
def test_sniff_image_content_type(data, expected):
    assert _sniff_image_content_type(data) == expected


@pytest.mark.asyncio
async def test_embedded_cover_served_when_every_external_source_misses(
    tmp_path, monkeypatch
):
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")

    library_db = MagicMock()
    library_db.get_library_files_for_album = AsyncMock(
        return_value=[{"file_path": str(track)}]
    )

    async with httpx.AsyncClient() as http_client:
        repo = CoverArtRepository(
            http_client=http_client,
            cache=MagicMock(),
            cache_dir=tmp_path,
            library_db=library_db,
        )
        repo._disk_cache.read = AsyncMock(return_value=None)
        repo._disk_cache.is_negative = AsyncMock(return_value=False)
        repo._disk_cache.write_negative = AsyncMock()
        repo._disk_cache.write = AsyncMock()
        repo._tagger.read_cover_art = MagicMock(return_value=_JPEG)
        _miss_external(monkeypatch)

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result == (_JPEG, "image/jpeg", "embedded")
        repo._disk_cache.write_negative.assert_not_awaited()
        repo._disk_cache.write.assert_awaited_once()
        assert repo._disk_cache.write.await_args.args[3] == {"source": "embedded"}


@pytest.mark.asyncio
async def test_no_library_db_falls_through_to_negative_cache(tmp_path, monkeypatch):
    async with httpx.AsyncClient() as http_client:
        repo = CoverArtRepository(
            http_client=http_client, cache=MagicMock(), cache_dir=tmp_path
        )
        repo._disk_cache.read = AsyncMock(return_value=None)
        repo._disk_cache.is_negative = AsyncMock(return_value=False)
        repo._disk_cache.write_negative = AsyncMock()
        _miss_external(monkeypatch)

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result is None
        repo._disk_cache.write_negative.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_raster_embedded_art_is_skipped(tmp_path, monkeypatch):
    track = tmp_path / "track.mp3"
    track.write_bytes(b"fake mp3")

    library_db = MagicMock()
    library_db.get_library_files_for_album = AsyncMock(
        return_value=[{"file_path": str(track)}]
    )

    async with httpx.AsyncClient() as http_client:
        repo = CoverArtRepository(
            http_client=http_client,
            cache=MagicMock(),
            cache_dir=tmp_path,
            library_db=library_db,
        )
        repo._disk_cache.read = AsyncMock(return_value=None)
        repo._disk_cache.is_negative = AsyncMock(return_value=False)
        repo._disk_cache.write_negative = AsyncMock()
        repo._tagger.read_cover_art = MagicMock(return_value=b"<svg></svg>")
        _miss_external(monkeypatch)

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result is None
        repo._disk_cache.write_negative.assert_awaited_once()


def _repo_with_library(tmp_path, http_client, library_db, *, prefer_local: bool):
    repo = CoverArtRepository(
        http_client=http_client,
        cache=MagicMock(),
        cache_dir=tmp_path,
        library_db=library_db,
        local_cover_priority=lambda: prefer_local,
    )
    repo._disk_cache.read = AsyncMock(return_value=None)
    repo._disk_cache.is_negative = AsyncMock(return_value=False)
    repo._disk_cache.write_negative = AsyncMock()
    repo._disk_cache.write = AsyncMock()
    repo._album_fetcher.fetch_release_group_cover = AsyncMock(return_value=None)
    repo._album_fetcher.fetch_cached_audiodb_cover = AsyncMock(return_value=None)
    return repo


def _library_db_with(track):
    library_db = MagicMock()
    library_db.get_library_files_for_album = AsyncMock(
        return_value=[{"file_path": str(track)}]
    )
    return library_db


@pytest.mark.asyncio
async def test_folder_cover_served_before_network_when_preferred(tmp_path):
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")
    (tmp_path / "cover.jpg").write_bytes(_JPEG)

    async with httpx.AsyncClient() as http_client:
        repo = _repo_with_library(
            tmp_path, http_client, _library_db_with(track), prefer_local=True
        )

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result == (_JPEG, "image/jpeg", "folder")
        repo._album_fetcher.fetch_release_group_cover.assert_not_awaited()
        repo._disk_cache.write_negative.assert_not_awaited()
        assert repo._disk_cache.write.await_args.args[3] == {"source": "folder"}


@pytest.mark.asyncio
async def test_embedded_cover_served_before_network_when_preferred(tmp_path):
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")

    async with httpx.AsyncClient() as http_client:
        repo = _repo_with_library(
            tmp_path, http_client, _library_db_with(track), prefer_local=True
        )
        repo._tagger.read_cover_art = MagicMock(return_value=_JPEG)

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result == (_JPEG, "image/jpeg", "embedded")
        repo._album_fetcher.fetch_release_group_cover.assert_not_awaited()
        repo._disk_cache.write_negative.assert_not_awaited()


@pytest.mark.asyncio
async def test_folder_art_wins_over_embedded_art(tmp_path):
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")
    (tmp_path / "Front.PNG").write_bytes(_PNG)

    async with httpx.AsyncClient() as http_client:
        repo = _repo_with_library(
            tmp_path, http_client, _library_db_with(track), prefer_local=True
        )
        repo._tagger.read_cover_art = MagicMock(return_value=_JPEG)

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result == (_PNG, "image/png", "folder")
        repo._tagger.read_cover_art.assert_not_called()


@pytest.mark.asyncio
async def test_network_sources_win_before_local_when_preference_off(
    tmp_path, monkeypatch
):
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")
    (tmp_path / "cover.jpg").write_bytes(_JPEG)
    caa = (b"caa-bytes", "image/jpeg", "cover-art-archive")

    async def fake_dedupe(_key, _factory):
        return caa

    monkeypatch.setattr(coverart_repository_module._deduplicator, "dedupe", fake_dedupe)

    async with httpx.AsyncClient() as http_client:
        repo = _repo_with_library(
            tmp_path, http_client, _library_db_with(track), prefer_local=False
        )
        repo._tagger.read_cover_art = MagicMock(return_value=_JPEG)

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result == caa
        repo._tagger.read_cover_art.assert_not_called()


@pytest.mark.asyncio
async def test_cached_local_cover_is_not_displaced_by_audiodb_when_preferred(tmp_path):
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")

    async with httpx.AsyncClient() as http_client:
        repo = _repo_with_library(
            tmp_path, http_client, _library_db_with(track), prefer_local=True
        )
        repo._disk_cache.read = AsyncMock(
            return_value=(_JPEG, "image/jpeg", {"source": "folder"})
        )
        repo._album_fetcher.fetch_cached_audiodb_cover = AsyncMock(
            return_value=(b"audiodb-bytes", "image/jpeg", "audiodb")
        )

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result == (_JPEG, "image/jpeg", "folder")
        repo._album_fetcher.fetch_cached_audiodb_cover.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_local_cover_is_displaced_by_audiodb_when_preference_off(tmp_path):
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")

    async with httpx.AsyncClient() as http_client:
        repo = _repo_with_library(
            tmp_path, http_client, _library_db_with(track), prefer_local=False
        )
        repo._disk_cache.read = AsyncMock(
            return_value=(_JPEG, "image/jpeg", {"source": "folder"})
        )
        repo._album_fetcher.fetch_cached_audiodb_cover = AsyncMock(
            return_value=(b"audiodb-bytes", "image/jpeg", "audiodb")
        )

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result == (b"audiodb-bytes", "image/jpeg", "audiodb")


@pytest.mark.asyncio
async def test_local_cover_beats_banked_negative_when_preferred(tmp_path):
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")
    (tmp_path / "cover.jpg").write_bytes(_JPEG)

    async with httpx.AsyncClient() as http_client:
        repo = _repo_with_library(
            tmp_path, http_client, _library_db_with(track), prefer_local=True
        )
        repo._disk_cache.is_negative = AsyncMock(return_value=True)

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result == (_JPEG, "image/jpeg", "folder")
        repo._album_fetcher.fetch_release_group_cover.assert_not_awaited()
        repo._disk_cache.write_negative.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_paths_win_over_stale_legacy_rows(tmp_path):
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")
    (tmp_path / "cover.jpg").write_bytes(_JPEG)

    library_db = MagicMock()
    library_db.get_library_files_for_album = AsyncMock(
        return_value=[
            {"file_path": str(tmp_path / "gone-before-organize" / "old.flac")}
        ]
    )
    native_store = MagicMock()
    native_store.get_indexed_track_paths_for_release_group = AsyncMock(
        return_value=[str(track)]
    )

    async with httpx.AsyncClient() as http_client:
        repo = CoverArtRepository(
            http_client=http_client,
            cache=MagicMock(),
            cache_dir=tmp_path,
            library_db=library_db,
            native_library_store=native_store,
            local_cover_priority=lambda: True,
        )
        repo._disk_cache.read = AsyncMock(return_value=None)
        repo._disk_cache.is_negative = AsyncMock(return_value=False)
        repo._disk_cache.write_negative = AsyncMock()
        repo._disk_cache.write = AsyncMock()
        repo._album_fetcher.fetch_release_group_cover = AsyncMock(return_value=None)
        repo._album_fetcher.fetch_cached_audiodb_cover = AsyncMock(return_value=None)

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result == (_JPEG, "image/jpeg", "folder")
        library_db.get_library_files_for_album.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_disc_cover_at_album_root(tmp_path):
    (tmp_path / "CD1").mkdir()
    (tmp_path / "CD2").mkdir()
    (tmp_path / "CD1" / "track.flac").write_bytes(b"fake flac")
    (tmp_path / "CD2" / "track.flac").write_bytes(b"fake flac")
    (tmp_path / "cover.jpg").write_bytes(_JPEG)

    library_db = MagicMock()
    library_db.get_library_files_for_album = AsyncMock(
        return_value=[
            {"file_path": str(tmp_path / "CD1" / "track.flac")},
            {"file_path": str(tmp_path / "CD2" / "track.flac")},
        ]
    )

    async with httpx.AsyncClient() as http_client:
        repo = _repo_with_library(tmp_path, http_client, library_db, prefer_local=True)

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result == (_JPEG, "image/jpeg", "folder")


@pytest.mark.asyncio
async def test_tracks_spanning_library_roots_never_probe_root(tmp_path):
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "zone" / "root_b"
    root_a.mkdir()
    root_b.mkdir(parents=True)
    (root_a / "track.flac").write_bytes(b"fake flac")
    (root_b / "track.flac").write_bytes(b"fake flac")
    (tmp_path / "cover.jpg").write_bytes(_JPEG)

    library_db = MagicMock()
    library_db.get_library_files_for_album = AsyncMock(
        return_value=[
            {"file_path": str(root_a / "track.flac")},
            {"file_path": str(root_b / "track.flac")},
        ]
    )

    async with httpx.AsyncClient() as http_client:
        repo = _repo_with_library(tmp_path, http_client, library_db, prefer_local=True)

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        # Distinct parents -> no ancestor probe: the cover.jpg above both roots is
        # not art for this album.
        assert result is None
        repo._disk_cache.write_negative.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_raster_folder_art_is_skipped(tmp_path):
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")
    (tmp_path / "cover.svg").write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    )

    async with httpx.AsyncClient() as http_client:
        repo = _repo_with_library(
            tmp_path, http_client, _library_db_with(track), prefer_local=True
        )

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result is None
        repo._disk_cache.write_negative.assert_awaited_once()


@pytest.mark.asyncio
async def test_oversized_folder_art_is_skipped(tmp_path):
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")
    oversized = bytearray(_JPEG)
    oversized.extend(b"\x00" * (25 * 1024 * 1024))
    (tmp_path / "cover.jpg").write_bytes(bytes(oversized))

    async with httpx.AsyncClient() as http_client:
        repo = _repo_with_library(
            tmp_path, http_client, _library_db_with(track), prefer_local=True
        )

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result is None
        repo._disk_cache.write_negative.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_art_still_last_resort_when_preference_off(tmp_path, monkeypatch):
    track = tmp_path / "track.flac"
    track.write_bytes(b"fake flac")
    _miss_external(monkeypatch)

    async with httpx.AsyncClient() as http_client:
        repo = _repo_with_library(
            tmp_path, http_client, _library_db_with(track), prefer_local=False
        )
        repo._tagger.read_cover_art = MagicMock(return_value=_JPEG)

        result = await repo.get_release_group_cover(RELEASE_GROUP_MBID, size="500")

        assert result == (_JPEG, "image/jpeg", "embedded")
