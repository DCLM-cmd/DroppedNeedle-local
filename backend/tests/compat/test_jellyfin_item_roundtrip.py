"""Every id we hand a client must be fetchable back as an item.

Finamp's first call after listing the views is
``GET /Users/{user}/Items/{view id}`` - it fetches the view it was just given. The
resolver only knew track/album/artist/playlist, so the ``library`` id from
/Users/{id}/Views answered 404 and no content ever loaded. ``genre`` ids are handed
out by browsing and had the same hole.

The test is written against the SET of id kinds rather than the two that were
missing, so handing out a new kind without resolving it fails here.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.compat.jellyfin.router import _single_item

# Every kind the compat layer mints an id for (grep: to_jf("...")).
ID_KINDS = {"album", "artist", "genre", "library", "playlist", "track"}


def _services(*, genres=()):
    services = SimpleNamespace()
    services.id_map = AsyncMock()
    services.id_map.to_jf = AsyncMock(side_effect=lambda kind, key: f"jf-{kind}-{key}")
    services.view = AsyncMock()
    services.view.get_genres = AsyncMock(return_value=list(genres))
    services.view.get_track = AsyncMock(return_value=None)
    services.view.get_album = AsyncMock(return_value=None)
    services.view.get_artist_with_albums = AsyncMock(return_value=None)
    services.playlists = AsyncMock()
    services.playlists.get_all_playlists = AsyncMock(return_value=[])
    return services


@pytest.mark.asyncio
async def test_the_music_view_can_be_fetched_back() -> None:
    """The regression: this is Finamp's first request after /Views."""
    item = await _single_item(_services(), MagicMock(), "library", "music", None)

    assert item is not None
    assert item.Type == "CollectionFolder"
    assert item.CollectionType == "music"
    assert item.IsFolder is True


@pytest.mark.asyncio
async def test_a_genre_can_be_fetched_back() -> None:
    builder = MagicMock()
    builder.genre = AsyncMock(return_value="genre-dto")
    services = _services(genres=[SimpleNamespace(name="Hip Hop")])

    item = await _single_item(services, builder, "genre", "hip-hop", None)

    assert item == "genre-dto"


@pytest.mark.asyncio
async def test_an_unknown_genre_is_still_not_found() -> None:
    """Resolving the kind must not invent items that do not exist."""
    services = _services(genres=[SimpleNamespace(name="Hip Hop")])

    assert await _single_item(services, MagicMock(), "genre", "jazz", None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(ID_KINDS))
async def test_every_minted_id_kind_is_handled(kind) -> None:
    """Not that it resolves to something - the fixtures are empty - but that the
    resolver KNOWS the kind. An unknown kind falls through to None for every id of
    that kind, which is the 404 this test exists to prevent."""
    builder = MagicMock()
    builder.genre = AsyncMock(return_value="dto")
    builder.audio = AsyncMock(return_value="dto")
    builder.album = AsyncMock(return_value="dto")
    builder.artist = AsyncMock(return_value="dto")
    services = _services(genres=[SimpleNamespace(name="Known")])

    # A kind the resolver handles either returns an item or consults a source for it.
    await _single_item(services, builder, kind, "known", None)

    consulted = (
        services.view.get_track.await_count
        + services.view.get_album.await_count
        + services.view.get_artist_with_albums.await_count
        + services.view.get_genres.await_count
        + services.playlists.get_all_playlists.await_count
        + services.id_map.to_jf.await_count
    )
    assert consulted > 0, f"{kind} ids are handed out but nothing resolves them"
