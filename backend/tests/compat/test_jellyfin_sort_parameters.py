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
