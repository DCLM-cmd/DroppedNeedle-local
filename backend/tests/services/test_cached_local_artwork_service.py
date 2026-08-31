from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from repositories.coverart_disk_cache import get_cache_filename
from services.home.cached_local_artwork_service import CachedLocalArtworkService


@pytest.mark.asyncio
async def test_provider_association_reads_only_existing_normal_cache_bytes(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "covers"
    cache_dir.mkdir()
    content = b"\xff\xd8\xfflocal-cover"
    path = cache_dir / f"{get_cache_filename('rg_provider-id', '500')}.bin"
    path.write_bytes(content)
    store = AsyncMock()
    store.get_cached_local_artwork_context.return_value = {
        "source": "provider",
        "source_locator": "provider-id",
        "provider_id": "provider-id",
        "version": 3,
    }
    service = CachedLocalArtworkService(store, cache_dir)

    result = await service.get("local-album-id", 3)

    assert result is not None
    assert result[:3] == (content, "image/jpeg", "provider")
    store.get_cached_local_artwork_context.assert_awaited_once_with("local-album-id", 3)


@pytest.mark.asyncio
async def test_missing_cache_claim_is_terminal_and_does_not_call_a_provider(
    tmp_path: Path,
) -> None:
    store = AsyncMock()
    store.get_cached_local_artwork_context.return_value = {
        "source": "provider",
        "source_locator": "missing",
        "provider_id": "missing",
        "version": 1,
    }
    service = CachedLocalArtworkService(store, tmp_path / "covers")

    assert await service.get("uuid-shaped-local-id", 1) is None
    assert not hasattr(service, "_provider")


@pytest.mark.asyncio
async def test_embedded_association_reads_tags_off_the_event_loop(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "album.flac"
    audio_path.write_bytes(b"audio")
    store = AsyncMock()
    store.get_cached_local_artwork_context.return_value = {
        "source": "embedded",
        "embedded_file_path": str(audio_path),
        "embedded_file_availability": "indexed",
        "version": 2,
    }
    service = CachedLocalArtworkService(store, tmp_path / "covers")
    service._tagger.read_cover_art = MagicMock(return_value=b"\x89PNG\r\n\x1a\ncover")

    result = await service.get("album", 2)

    assert result is not None
    assert result[1:3] == ("image/png", "embedded")


# ---- resolving an image's identity without reading the image -----------------------

def _cached_cover(cache_dir: Path, content: bytes, meta: dict | None) -> None:
    import json

    path = cache_dir / f"{get_cache_filename('rg_provider-id', '500')}.bin"
    path.write_bytes(content)
    if meta is not None:
        path.with_suffix(".meta.json").write_text(json.dumps(meta))


def _provider_context() -> dict:
    return {
        "source": "provider",
        "source_locator": "provider-id",
        "provider_id": "provider-id",
        "version": 1,
    }


@pytest.mark.asyncio
async def test_the_identity_is_the_hash_the_full_read_would_have_returned(
    tmp_path: Path,
) -> None:
    """The invariant that matters: this value is published as the image's ETag, so a
    cheaper way of getting it must not be a DIFFERENT way of getting it."""
    import hashlib

    cache_dir = tmp_path / "covers"
    cache_dir.mkdir()
    content = b"\xff\xd8\xffthe-cover"
    _cached_cover(
        cache_dir,
        content,
        {"content_type": "image/jpeg", "content_sha1": hashlib.sha1(content).hexdigest()},
    )
    service = CachedLocalArtworkService(AsyncMock(), cache_dir)

    identity = await service.read_identity(_provider_context())
    full = await service.read(_provider_context())

    assert identity == full[3] == hashlib.sha1(content).hexdigest()


@pytest.mark.asyncio
async def test_the_identity_comes_from_the_metadata_not_from_the_image(
    tmp_path: Path,
) -> None:
    """Proven by making the two disagree: only the metadata read can answer.

    A listing must never load the picture - one 100-album page was pulling 181 MB of
    covers off disk purely to re-derive hashes the cache had already recorded.
    """
    cache_dir = tmp_path / "covers"
    cache_dir.mkdir()
    _cached_cover(
        cache_dir,
        b"\xff\xd8\xffthe-cover",
        {"content_type": "image/jpeg", "content_sha1": "recorded-at-write-time"},
    )
    service = CachedLocalArtworkService(AsyncMock(), cache_dir)

    assert await service.read_identity(_provider_context()) == "recorded-at-write-time"


@pytest.mark.asyncio
async def test_an_entry_without_recorded_metadata_still_resolves(tmp_path: Path) -> None:
    """Covers written before the hash was recorded must not lose their ETag."""
    import hashlib

    cache_dir = tmp_path / "covers"
    cache_dir.mkdir()
    content = b"\xff\xd8\xffolder-entry"
    _cached_cover(cache_dir, content, None)
    service = CachedLocalArtworkService(AsyncMock(), cache_dir)

    assert (
        await service.read_identity(_provider_context())
        == hashlib.sha1(content).hexdigest()
    )


@pytest.mark.asyncio
async def test_a_non_image_entry_is_refused_like_the_full_read_refuses_it(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "covers"
    cache_dir.mkdir()
    _cached_cover(
        cache_dir, b"<html>an error page</html>", {"content_type": "text/html"}
    )
    service = CachedLocalArtworkService(AsyncMock(), cache_dir)

    assert await service.read_identity(_provider_context()) is None
    assert await service.read(_provider_context()) is None


@pytest.mark.asyncio
async def test_a_missing_cover_has_no_identity(tmp_path: Path) -> None:
    cache_dir = tmp_path / "covers"
    cache_dir.mkdir()
    service = CachedLocalArtworkService(AsyncMock(), cache_dir)

    assert await service.read_identity(_provider_context()) is None


@pytest.mark.asyncio
async def test_corrupt_metadata_falls_back_rather_than_failing(tmp_path: Path) -> None:
    import hashlib

    cache_dir = tmp_path / "covers"
    cache_dir.mkdir()
    content = b"\xff\xd8\xffstill-fine"
    path = cache_dir / f"{get_cache_filename('rg_provider-id', '500')}.bin"
    path.write_bytes(content)
    path.with_suffix(".meta.json").write_text("{not json")
    service = CachedLocalArtworkService(AsyncMock(), cache_dir)

    assert (
        await service.read_identity(_provider_context())
        == hashlib.sha1(content).hexdigest()
    )


@pytest.mark.asyncio
async def test_an_unknown_source_has_no_identity(tmp_path: Path) -> None:
    service = CachedLocalArtworkService(AsyncMock(), tmp_path)

    assert await service.read_identity({"source": "something-else"}) is None
