"""The target composition's cover adapter must expose everything the builders call.

The compat builders talk to whatever cover repository they were handed. In the target
composition that is not ``CoverArtRepository`` but ``TargetCoverArtService``, a thin
adapter that maps local ids onto provider ids - and when a method was added to the
repository and not to the adapter, the builders' ``except Exception`` swallowed the
AttributeError. Every album then reported no blurhash, which Finamp shows the user as
"the server seems to be misconfigured", with no trace anywhere on the server.

This test is about the SET of methods rather than the one that was missing, so the
next addition cannot drift the same way.
"""

import inspect

import pytest

from repositories.coverart_repository import CoverArtRepository
from services.compat.target_cover_art_service import TargetCoverArtService

# What a compat builder or route may ask a cover repository for.
_USED_BY_COMPAT = [
    "get_release_group_cover",
    "get_release_group_cover_etag",
    "get_release_group_cover_blurhash",
    "get_artist_image",
    "get_artist_image_etag",
    "get_artist_image_blurhash",
    "get_release_cover",
    "get_release_cover_etag",
]


@pytest.mark.parametrize("method", _USED_BY_COMPAT)
def test_the_adapter_implements_every_method_the_builders_use(method) -> None:
    assert hasattr(TargetCoverArtService, method), (
        f"TargetCoverArtService is missing {method}; the builders would silently "
        f"publish nothing for it"
    )


@pytest.mark.parametrize("method", _USED_BY_COMPAT)
def test_the_repository_itself_implements_them_too(method) -> None:
    assert hasattr(CoverArtRepository, method)


@pytest.mark.parametrize("method", _USED_BY_COMPAT)
def test_both_are_awaitable(method) -> None:
    """A builder awaits these; a sync method would raise at the await, not here."""
    assert inspect.iscoroutinefunction(getattr(TargetCoverArtService, method))
    assert inspect.iscoroutinefunction(getattr(CoverArtRepository, method))


@pytest.mark.parametrize(
    "method,expected",
    [
        ("get_release_group_cover_blurhash", ["album_id", "size"]),
        ("get_artist_image_blurhash", ["artist_id", "size"]),
    ],
)
def test_the_adapter_keeps_the_repository_call_shape(method, expected) -> None:
    """The builder passes positionally, so the adapter must accept the same order."""
    params = [
        p
        for p in inspect.signature(getattr(TargetCoverArtService, method)).parameters
        if p != "self"
    ]

    assert params == expected


# ---- the adapter must return values, not coroutines ------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,identifier",
    [
        ("get_release_group_cover_blurhash", "album-1"),
        ("get_artist_image_blurhash", "artist-1"),
    ],
)
async def test_the_adapter_returns_a_value_not_a_coroutine(method, identifier) -> None:
    """A forgotten ``await`` inside the adapter returns a coroutine, which sails
    through every type hint and only fails at the very end of the request, in
    msgspec: "Encoding objects of type coroutine is unsupported" - a 500 on every
    listing, from a field that is meant to be decorative.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (12, 34, 56)).save(buffer, format="PNG")

    store = AsyncMock()
    store.get_target_artwork_context = AsyncMock(return_value={"provider_id": "p-1"})
    # The blurhash is looked up by the artwork's content hash, never computed here:
    # the organization run stores it, exactly as Jellyfin stores it while scanning.
    store.get_image_blurhashes = AsyncMock(return_value={"etag-1": "U~Lqe9%M"})
    local = AsyncMock()
    # Listings resolve the artwork's IDENTITY, never its bytes: reading the picture to
    # derive a hash cost 181 MB of image I/O for a single 100-album page.
    local.read_identity = AsyncMock(return_value="etag-1")
    local.read = AsyncMock(return_value=(buffer.getvalue(), "image/png", "", "etag-1"))
    # The artist path resolves its etag through the provider, then looks the hash up
    # in the store - a tag without a matching hash is what Finamp calls a broken server.
    provider = SimpleNamespace(
        get_artist_image_etag=AsyncMock(return_value="etag-1"),
        get_artist_image_blurhash=AsyncMock(return_value="U~Lqe9%M"),
        get_release_group_cover_blurhash=AsyncMock(return_value="U~Lqe9%M"),
    )

    adapter = TargetCoverArtService(store, provider, local)
    result = await getattr(adapter, method)(identifier)

    assert not inspect.iscoroutine(result), f"{method} returned a coroutine"
    if result is not None:
        assert isinstance(result, str) and result


# ---- artist images are stored at 250 --------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_artist_image_etag", "get_artist_image_blurhash"])
async def test_an_unsized_artist_lookup_finds_the_250_entry(method, tmp_path) -> None:
    """Artist images land under ``artist_<id>_250`` unless a size was asked for, and
    the DTO builders ask UNSIZED - so both lookups missed the only entry that exists.
    Every artist then reported no image tag, no client requested the picture, and
    nothing ever populated the cache to break the loop.
    """
    import io
    from unittest.mock import AsyncMock, MagicMock

    from PIL import Image

    from repositories.coverart_repository import CoverArtRepository

    artist = "ef326b3d-61f0-4379-aca0-21237d696d63"
    sized = tmp_path / "sized.bin"
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (90, 20, 140)).save(buffer, format="PNG")
    sized.write_bytes(buffer.getvalue())

    repo = CoverArtRepository.__new__(CoverArtRepository)
    repo._disk_cache = MagicMock()
    repo._disk_cache.get_file_path = MagicMock(
        side_effect=lambda ident, suffix: (
            str(sized) if ident.endswith("_250") else str(tmp_path / "missing.bin")
        )
    )
    repo._disk_cache.get_content_hash = AsyncMock(
        side_effect=lambda path: "etag-250" if str(path) == str(sized) else None
    )
    repo._memory_get_hash = AsyncMock(return_value=None)

    result = await getattr(repo, method)(artist)

    assert result, f"{method} found nothing for an artist whose image is cached at 250"


@pytest.mark.asyncio
async def test_a_sized_request_is_unaffected() -> None:
    """The fallback must not fire when the caller asked for a specific size."""
    from unittest.mock import AsyncMock, MagicMock

    from repositories.coverart_repository import CoverArtRepository

    repo = CoverArtRepository.__new__(CoverArtRepository)
    repo._disk_cache = MagicMock()
    repo._disk_cache.get_file_path = MagicMock(return_value="/nowhere.bin")
    repo._disk_cache.get_content_hash = AsyncMock(return_value=None)
    repo._memory_get_hash = AsyncMock(return_value=None)

    assert await repo.get_artist_image_blurhash(
        "ef326b3d-61f0-4379-aca0-21237d696d63", 500
    ) is None


# ---- listings resolve identity, not pixels ----------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["get_release_group_cover_etag", "get_release_group_cover_blurhash"]
)
async def test_a_listing_never_loads_the_cover_bytes(method) -> None:
    """Both were deriving a short string by reading and re-hashing the whole image.

    Measured on the real library: one 100-album page pulled 181 MB of covers off disk,
    twice over, because the ETag and the blurhash each asked independently. Jellyfin
    serves the same listing without opening a single image file.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    store = AsyncMock()
    store.get_target_artwork_context = AsyncMock(return_value={"provider_id": "p-1"})
    store.get_image_blurhashes = AsyncMock(return_value={"sha-1": "U~Lqe9%M"})
    local = AsyncMock()
    local.read_identity = AsyncMock(return_value="sha-1")
    provider = SimpleNamespace(
        get_release_group_cover_etag=AsyncMock(return_value=None),
        get_release_group_cover_blurhash=AsyncMock(return_value=None),
    )

    adapter = TargetCoverArtService(store, provider, local)
    result = await getattr(adapter, method)("album-1")

    assert result
    local.read.assert_not_awaited()


# ---- one image record, both halves -------------------------------------------------

@pytest.mark.asyncio
async def test_the_pair_is_resolved_from_a_single_lookup() -> None:
    """Asking for the tag and the hash separately resolved the same picture twice.

    Two catalog lookups and two identity reads per listed album came to 358 ms of a
    378 ms hundred-album page. Jellyfin reads its image record once and takes both
    values off it.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    store = AsyncMock()
    store.get_target_artwork_context = AsyncMock(return_value={"provider_id": "p-1"})
    store.get_image_blurhashes = AsyncMock(return_value={"sha-1": "U~Lqe9%M"})
    local = AsyncMock()
    local.read_identity = AsyncMock(return_value="sha-1")

    adapter = TargetCoverArtService(store, SimpleNamespace(), local)
    tag, blurhash = await adapter.get_release_group_cover_image_info("album-1")

    assert (tag, blurhash) == ("sha-1", "U~Lqe9%M")
    assert local.read_identity.await_count == 1
    assert store.get_target_artwork_context.await_count == 1


@pytest.mark.asyncio
async def test_an_album_without_local_art_still_falls_back_to_the_provider() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    store = AsyncMock()
    store.get_target_artwork_context = AsyncMock(return_value={"provider_id": "p-1"})
    local = AsyncMock()
    local.read_identity = AsyncMock(return_value=None)
    provider = SimpleNamespace(
        get_release_group_cover_etag=AsyncMock(return_value="provider-tag"),
        get_release_group_cover_blurhash=AsyncMock(return_value="provider-hash"),
    )

    adapter = TargetCoverArtService(store, provider, local)

    assert await adapter.get_release_group_cover_image_info("album-1") == (
        "provider-tag",
        "provider-hash",
    )


@pytest.mark.asyncio
async def test_an_artist_without_a_picture_reports_neither_half() -> None:
    """A tag without its hash is what makes Finamp call the server misconfigured."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    store = AsyncMock()
    store.get_target_artwork_context = AsyncMock(return_value={"provider_id": "p-1"})
    provider = SimpleNamespace(get_artist_image_etag=AsyncMock(return_value=None))

    adapter = TargetCoverArtService(store, provider, AsyncMock())

    assert await adapter.get_artist_image_info("artist-1") == (None, None)
