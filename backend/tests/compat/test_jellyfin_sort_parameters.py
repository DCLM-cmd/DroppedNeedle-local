"""SortBy must mean what Jellyfin's ItemSortBy means.

An unrecognised SortBy does not error - it silently falls through to the endpoint's
default. So a client asking for disc-then-track order got "newest imported first"
and had no way to tell, which is how a two-disc album came back interleaved.
"""

import json

import pytest

from api.compat.jellyfin.router import _ALBUM_SORTS, _TRACK_SORTS

pytestmark = pytest.mark.asyncio


def _h(env):
    return {"Authorization": f'MediaBrowser Token="{env.secret}", Client="pytest"'}


def _jget(env, path, **params):
    r = env.client.get(f"/jellyfin{path}", params=params, headers=_h(env))
    assert r.status_code == 200, (r.status_code, r.content[:200])
    return json.loads(r.content)


async def test_disc_and_track_order_are_understood():
    """Jellyfin names them IndexNumber (track) and ParentIndexNumber (disc)."""
    assert "indexnumber" in _TRACK_SORTS
    assert "parentindexnumber" in _TRACK_SORTS


async def test_every_mapped_sort_has_both_directions():
    """SortOrder=Descending must actually reverse; a single-entry mapping silently
    ignored the direction."""
    for table in (_ALBUM_SORTS, _TRACK_SORTS):
        for name, pair in table.items():
            assert len(pair) == 2, name
            if name != "random":
                assert pair[0] != pair[1], f"{name} does not distinguish direction"


async def test_tracks_sorted_by_index_number_ascend_and_descend(compat_env):
    ascending = _jget(
        compat_env,
        "/Users/user-alice/Items",
        IncludeItemTypes="Audio",
        SortBy="IndexNumber",
        SortOrder="Ascending",
    )["Items"]
    descending = _jget(
        compat_env,
        "/Users/user-alice/Items",
        IncludeItemTypes="Audio",
        SortBy="IndexNumber",
        SortOrder="Descending",
    )["Items"]

    numbers = [i.get("IndexNumber") for i in ascending if i.get("IndexNumber")]
    assert numbers == sorted(numbers), numbers
    if len(numbers) > 1:
        reverse = [i.get("IndexNumber") for i in descending if i.get("IndexNumber")]
        assert reverse == sorted(reverse, reverse=True), reverse


async def test_an_album_lists_its_tracks_in_disc_then_track_order(compat_env):
    albums = _jget(
        compat_env, "/Users/user-alice/Items", IncludeItemTypes="MusicAlbum"
    )["Items"]
    for album in albums:
        tracks = _jget(compat_env, "/Users/user-alice/Items", ParentId=album["Id"])[
            "Items"
        ]
        keys = [
            (t.get("ParentIndexNumber") or 1, t.get("IndexNumber") or 0)
            for t in tracks
        ]
        assert keys == sorted(keys), (album.get("Name"), keys)


async def test_an_unknown_sort_still_answers(compat_env):
    """Jellyfin ignores a sort it does not know rather than failing the request."""
    body = _jget(
        compat_env,
        "/Users/user-alice/Items",
        IncludeItemTypes="Audio",
        SortBy="SomethingJellyfinNeverDefined",
    )
    assert "Items" in body


async def test_artists_honour_their_sort_key():
    """The target view discarded sort_by outright, so every artist list came back
    alphabetical however it was asked for."""
    from unittest.mock import AsyncMock

    from services.compat.target_library_view_service import TargetLibraryViewService

    view = TargetLibraryViewService.__new__(TargetLibraryViewService)
    view._store = AsyncMock()
    view._store.list_target_artists.return_value = ([], 0)
    view._artist = lambda row: row
    view._overlay_favorites = AsyncMock()
    view._overlay_plays = AsyncMock()

    await view.get_artists(sort_by="album_count", sort_order="desc")

    passed = view._store.list_target_artists.await_args.kwargs
    assert passed["sort_by"] == "album_count"
    assert passed["sort_order"] == "desc"


async def test_name_starts_with_narrows_the_album_list(compat_env):
    """Jellyfin's A-Z jump bar. Ignored, a letter tap returned the whole library."""
    everything = _jget(
        compat_env, "/Users/user-alice/Items", IncludeItemTypes="MusicAlbum"
    )["Items"]
    assert everything, "fixture has no albums to filter"
    letter = everything[0]["Name"][0]

    narrowed = _jget(
        compat_env,
        "/Users/user-alice/Items",
        IncludeItemTypes="MusicAlbum",
        NameStartsWith=letter,
    )["Items"]

    assert narrowed, f"nothing matched {letter!r}"
    assert all(i["Name"].upper().startswith(letter.upper()) for i in narrowed), [
        i["Name"] for i in narrowed
    ]
    assert len(narrowed) <= len(everything)


async def test_name_starts_with_treats_wildcards_literally(compat_env):
    """An unescaped LIKE pattern would make "%" match the entire catalog."""
    body = _jget(
        compat_env,
        "/Users/user-alice/Items",
        IncludeItemTypes="MusicAlbum",
        NameStartsWith="%",
    )
    assert body["Items"] == []


async def test_years_is_a_list_not_a_range(compat_env):
    """Asking for two years must not also return everything between them."""
    albums = _jget(
        compat_env, "/Users/user-alice/Items", IncludeItemTypes="MusicAlbum"
    )["Items"]
    years = sorted({a.get("ProductionYear") for a in albums if a.get("ProductionYear")})
    if not years:
        pytest.skip("fixture albums carry no production year")

    filtered = _jget(
        compat_env,
        "/Users/user-alice/Items",
        IncludeItemTypes="MusicAlbum",
        Years=str(years[0]),
    )["Items"]

    assert filtered, "the year filter matched nothing"
    assert {a.get("ProductionYear") for a in filtered} == {years[0]}


async def test_an_unmatched_year_returns_nothing_rather_than_everything(compat_env):
    body = _jget(
        compat_env,
        "/Users/user-alice/Items",
        IncludeItemTypes="MusicAlbum",
        Years="1066",
    )
    assert body["Items"] == []
