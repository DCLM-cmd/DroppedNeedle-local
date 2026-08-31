"""T4.1 - Jellyfin BaseItemDto builder: PascalCase, ticks, UserData, ImageTags."""

import msgspec
import pytest

from api.compat.jellyfin.builders import JellyfinBuilder, ticks
from api.compat.jellyfin.models import SERVER_ID
from services.compat.view_models import ViewAlbum, ViewArtist, ViewTrack

_aio = pytest.mark.asyncio


class _Cover:
    def __init__(self, album_tag=None, artist_tag=None, artist_blurhash="LEHV6n"):
        self._album_tag = album_tag
        self._artist_tag = artist_tag
        self._artist_blurhash = artist_blurhash

    async def get_release_group_cover_etag(self, rg, size="500"):
        return self._album_tag

    async def get_artist_image_etag(self, aid, size=None):
        return self._artist_tag

    async def get_artist_image_blurhash(self, aid, size=None):
        return self._artist_blurhash

    # The builders ask for tag and hash in ONE call so a listing resolves each picture
    # once rather than twice; both real cover compositions provide this.
    async def get_release_group_cover_image_info(self, rg, size="500"):
        return self._album_tag, "LEHV6nWB"

    async def get_artist_image_info(self, aid, size=None):
        return self._artist_tag, self._artist_blurhash


def _track(**over) -> ViewTrack:
    base = dict(
        file_id="f1", title="Airbag", album_title="OK Computer",
        rg_mbid="rg-1", artist_name="Radiohead", artist_mbid="ar-mb",
        album_artist_name="Radiohead", album_artist_mbid="ar-mb",
        track_number=1, disc_number=1, year=1997, genre="Alt Rock",
        duration_seconds=234.56, file_format="flac", sample_rate=44100,
        channels=2, bit_depth=16, recording_mbid="rec-1",
    )
    base.update(over)
    return ViewTrack(**base)


@_aio
async def test_audio_pascalcase_and_ticks(compat_id_map_service):
    b = JellyfinBuilder(compat_id_map_service, _Cover(album_tag="tagX"), SERVER_ID)
    item = await b.audio(_track())
    raw = msgspec.to_builtins(item)
    for key in ("Id", "Name", "Type", "RunTimeTicks", "IndexNumber",
                "ParentIndexNumber", "AlbumId", "ImageTags", "UserData", "Container"):
        assert key in raw
    assert not any(k[:1].islower() for k in raw)  # no snake_case leakage
    assert raw["Type"] == "Audio"
    assert raw["RunTimeTicks"] == round(234.56 * 10_000_000)
    assert raw["IndexNumber"] == 1 and raw["ParentIndexNumber"] == 1
    assert raw["Container"] == "flac"
    assert raw["ServerId"] == SERVER_ID


@_aio
async def test_audio_userdata_block(compat_id_map_service):
    b = JellyfinBuilder(compat_id_map_service, _Cover(album_tag="t"), SERVER_ID)
    item = await b.audio(_track(starred_at=123.0, play_count=4))
    ud = msgspec.to_builtins(item)["UserData"]
    assert ud["IsFavorite"] is True
    assert ud["PlayCount"] == 4 and ud["Played"] is True
    assert ud["ItemId"] == item.Id and ud["Key"] == item.Id


@_aio
async def test_imagetags_present_when_art_else_absent(compat_id_map_service):
    with_art = JellyfinBuilder(compat_id_map_service, _Cover(album_tag="abc"), SERVER_ID)
    audio = await with_art.audio(_track())
    assert audio.ImageTags == {"Primary": "abc"}
    assert audio.AlbumPrimaryImageTag == "abc"

    no_art = JellyfinBuilder(compat_id_map_service, _Cover(album_tag=None), SERVER_ID)
    audio2 = await no_art.audio(_track())
    assert audio2.ImageTags == {}
    assert audio2.AlbumPrimaryImageTag is None


@_aio
async def test_album_and_artist_items(compat_id_map_service):
    b = JellyfinBuilder(compat_id_map_service, _Cover(album_tag="t", artist_tag="art"), SERVER_ID)
    album = await b.album(ViewAlbum(
        rg_mbid="rg-1", title="OK Computer", artist_name="Radiohead",
        artist_mbid="ar-mb", year=1997, track_count=12,
        total_duration_seconds=3000.0, genre="Alt Rock",
    ))
    assert album.Type == "MusicAlbum" and album.IsFolder is True
    assert album.RunTimeTicks == ticks(3000.0)
    assert album.ChildCount == 12
    assert album.AlbumArtists[0].Name == "Radiohead"
    assert album.ProviderIds["MusicBrainzReleaseGroup"] == "rg-1"

    artist = await b.artist(ViewArtist(artist_mbid="ar-mb", name="Radiohead", album_count=3))
    assert artist.Type == "MusicArtist" and artist.IsFolder is True
    assert artist.ImageTags == {"Primary": "art"}
    assert artist.ProviderIds["MusicBrainzArtist"] == "ar-mb"


@_aio
async def test_ids_round_trip_through_id_map(compat_id_map_service):
    b = JellyfinBuilder(compat_id_map_service, _Cover(), SERVER_ID)
    item = await b.audio(_track())
    assert await compat_id_map_service.from_jf(item.Id) == ("track", "f1")
    assert await compat_id_map_service.from_jf(item.AlbumId) == ("album", "rg-1")


def test_server_id_stable_32hex():
    import re

    assert re.fullmatch(r"[0-9a-f]{32}", SERVER_ID)
    # deterministic across imports
    from api.compat.jellyfin.models import SERVER_ID as again
    assert SERVER_ID == again


@_aio
async def test_an_artist_picture_is_only_advertised_with_its_blurhash(
    compat_id_map_service,
):
    """Finamp reads a tag whose hash is missing as a broken server and says so.

    Artist pictures are fetched on demand, so one can enter the cache - and gain a
    tag - between two organization runs, before its hash has been computed. Publishing
    the tag alone put the user in front of a warning about server misconfiguration.
    """
    builder = JellyfinBuilder(
        compat_id_map_service,
        _Cover(artist_tag="art", artist_blurhash=None),
        SERVER_ID,
    )

    artist = await builder.artist(
        ViewArtist(artist_mbid="ar-mb", name="Radiohead", album_count=9)
    )

    assert artist.ImageTags == {}
    assert artist.ImageBlurHashes == {}


# ---- sorting ---------------------------------------------------------------------

def _request(params: dict):
    """A stand-in carrying real QueryParams, which is what _params() reads."""
    from types import SimpleNamespace

    from starlette.datastructures import QueryParams

    return SimpleNamespace(query_params=QueryParams(params))


@pytest.mark.parametrize(
    "sort_by,order,expected",
    [
        ("SortName", "Ascending", "name"),
        ("SortName", "Descending", "name_desc"),
        ("Album", "Ascending", "name"),
        ("AlbumArtist", "Ascending", "artist"),
        ("AlbumArtist", "Descending", "artist_desc"),
        ("DateCreated", "Descending", "recent"),
        ("DateCreated", "Ascending", "recent_asc"),
        ("ProductionYear", "Descending", "newest"),
        ("ProductionYear", "Ascending", "oldest"),
        ("Random", "Ascending", "random"),
    ],
)
def test_album_sorts_translate_from_jellyfins_vocabulary(sort_by, order, expected):
    """Clients send SortBy with SortOrder on every list request. None of it was read,
    so however the user set the control the answer came back in the default order -
    which in Finamp looks like sorting being stuck on newest-first."""
    from api.compat.jellyfin.router import _ALBUM_SORTS, _sort_key

    request = _request({"SortBy": sort_by, "SortOrder": order})

    assert _sort_key(request, _ALBUM_SORTS, "recent") == expected


@pytest.mark.parametrize(
    "sort_by,order,expected",
    [
        ("SortName", "Ascending", "title"),
        ("SortName", "Descending", "title_desc"),
        ("Album", "Ascending", "album"),
        ("Artist", "Descending", "artist_desc"),
    ],
)
def test_track_sorts_translate_too(sort_by, order, expected):
    from api.compat.jellyfin.router import _TRACK_SORTS, _sort_key

    request = _request({"SortBy": sort_by, "SortOrder": order})

    assert _sort_key(request, _TRACK_SORTS, "recent") == expected


def test_a_sort_we_cannot_express_keeps_the_default_rather_than_guessing():
    """PlayCount and friends have no catalog equivalent. Sorting by something else
    entirely would be worse than not sorting."""
    from api.compat.jellyfin.router import _ALBUM_SORTS, _sort_key

    request = _request({"SortBy": "PlayCount"})

    assert _sort_key(request, _ALBUM_SORTS, "recent") == "recent"


def test_the_first_understood_key_in_a_list_wins():
    """Clients send comma-separated lists like "PlayCount,SortName"."""
    from api.compat.jellyfin.router import _ALBUM_SORTS, _sort_key

    request = _request({"SortBy": "PlayCount,SortName"})

    assert _sort_key(request, _ALBUM_SORTS, "recent") == "name"


def test_no_sort_at_all_is_the_default():
    from api.compat.jellyfin.router import _ALBUM_SORTS, _sort_key

    assert _sort_key(_request({}), _ALBUM_SORTS, "recent") == "recent"


# ---- the library view is an item too, and clients read it first --------------------

def test_the_library_cover_is_a_readable_image() -> None:
    """It was a 1x1 PNG that no decoder would open - Pillow reports "broken PNG file".
    Clients were served a corrupt image from the very first thing they fetch."""
    import io

    from PIL import Image

    from api.compat.jellyfin.router import _LIBRARY_COVER_PNG

    with Image.open(io.BytesIO(_LIBRARY_COVER_PNG)) as image:
        image.load()
        assert min(image.size) >= 16


def test_the_library_view_carries_a_blurhash_beside_its_tag() -> None:
    """A tag whose hash is missing is what makes Finamp announce that the server
    computes no blurhashes - and the library view is the FIRST item it asks for, so
    the warning survived every album and artist getting one.
    """
    from api.compat.jellyfin.router import _music_view

    view = _music_view("library-1")

    assert view.ImageTags == {"Primary": "library-1"}
    assert view.ImageBlurHashes == {
        "Primary": {"library-1": view.ImageBlurHashes["Primary"]["library-1"]}
    }
    assert view.ImageBlurHashes["Primary"]["library-1"]
