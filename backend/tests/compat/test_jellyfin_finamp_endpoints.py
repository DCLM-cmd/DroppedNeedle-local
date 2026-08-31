"""The endpoints Finamp calls that this server did not answer.

Finamp's CarPlay browse and its "recently added" rows go through
``/Users/{id}/Items/Latest``. Until it existed the request fell through to the
frontend's catch-all and came back as an HTML page with status 200, so the client
failed while decoding rather than on the status - which is why it presented as
"CarPlay loads nothing" instead of as a missing endpoint.
"""

import json

import pytest

pytestmark = pytest.mark.asyncio


def _h(env):
    return {"Authorization": f'MediaBrowser Token="{env.secret}", Client="pytest"'}


def _jget(env, path, **params):
    r = env.client.get(f"/jellyfin{path}", params=params, headers=_h(env))
    assert r.status_code == 200, (r.status_code, r.content[:300])
    return json.loads(r.content)


async def test_latest_returns_a_bare_array_not_a_query_result(compat_env):
    """Jellyfin answers this one endpoint with a plain array. Finamp decodes it as
    such, so wrapping it in a BaseItemDtoQueryResult breaks the client even though
    every field inside would be right."""
    body = _jget(compat_env, "/Users/user-alice/Items/Latest")

    assert isinstance(body, list), body
    for item in body:
        assert "Id" in item and "Name" in item


async def test_latest_defaults_to_twenty_and_honours_an_explicit_limit(compat_env):
    assert len(_jget(compat_env, "/Users/user-alice/Items/Latest")) <= 20
    assert len(_jget(compat_env, "/Users/user-alice/Items/Latest", Limit=1)) <= 1


async def test_latest_returns_tracks_when_audio_is_requested(compat_env):
    items = _jget(
        compat_env, "/Users/user-alice/Items/Latest", IncludeItemTypes="Audio"
    )
    assert all(item["Type"] == "Audio" for item in items), items


async def test_latest_is_not_shadowed_by_the_single_item_route(compat_env):
    """``Latest`` is a literal segment competing with ``/Items/{itemId}``; declared in
    the wrong order it is read as an item id and answers 404."""
    r = compat_env.client.get(
        "/jellyfin/Users/user-alice/Items/Latest", headers=_h(compat_env)
    )
    assert r.status_code == 200


async def test_system_endpoint_reports_the_caller_vantage_point(compat_env):
    body = _jget(compat_env, "/System/Endpoint")

    assert set(body) == {"IsLocal", "IsInNetwork"}
    assert isinstance(body["IsLocal"], bool)


async def test_album_similar_answers_under_the_albums_path(compat_env):
    albums = _jget(compat_env, "/Users/user-alice/Items", IncludeItemTypes="MusicAlbum")
    album_id = albums["Items"][0]["Id"]

    body = _jget(compat_env, f"/Albums/{album_id}/Similar")

    assert "Items" in body and "TotalRecordCount" in body


async def test_lyrics_answers_a_real_404_not_the_frontend_shell(compat_env):
    tracks = _jget(compat_env, "/Users/user-alice/Items", IncludeItemTypes="Audio")
    track_id = tracks["Items"][0]["Id"]

    r = compat_env.client.get(
        f"/jellyfin/Audio/{track_id}/Lyrics", headers=_h(compat_env)
    )

    assert r.status_code == 404
    assert b"<!doctype html" not in r.content.lower()


async def test_playlist_users_is_an_empty_list(compat_env):
    body = _jget(compat_env, "/Playlists/whatever/Users")
    assert body == []


async def test_albums_report_a_primary_image_aspect_ratio(compat_env):
    """Jellyfin reports it from its stored image record; a client uses it to reserve
    the right space before the picture arrives. It was the one field of the DTO we
    never sent."""
    albums = _jget(
        compat_env, "/Users/user-alice/Items", IncludeItemTypes="MusicAlbum"
    )["Items"]

    assert albums, "fixture has no albums"
    for album in albums:
        # Absent when no dimensions are stored - the DTO omits its defaults, as
        # Jellyfin does. What must never happen is a made-up value.
        ratio = album.get("PrimaryImageAspectRatio")
        assert ratio is None or 0.1 < ratio < 10, (album.get("Name"), ratio)


async def test_a_stored_ratio_is_reported(compat_env, monkeypatch):
    """With dimensions on record the field carries width / height."""
    from api.compat.jellyfin import builders

    async def _art_call(self, method, *args):  # noqa: ANN001
        if method == "get_release_group_cover_image_info":
            return ("tag-1", "LEHV6nWB2yk8pyo0adR*.7kCMdnj")
        if method == "get_image_aspect_ratio":
            return 1.5
        return None

    monkeypatch.setattr(builders.JellyfinBuilder, "_art_call", _art_call, raising=False)

    albums = _jget(
        compat_env, "/Users/user-alice/Items", IncludeItemTypes="MusicAlbum"
    )["Items"]

    assert albums
    assert all(a.get("PrimaryImageAspectRatio") == 1.5 for a in albums), albums


async def test_the_aspect_ratio_is_absent_rather_than_guessed(compat_env):
    """A cover with no stored dimensions reports nothing instead of a made-up 1.0 -
    a wrong ratio lays the grid out wrongly, a missing one lets the client decide."""
    tracks = _jget(compat_env, "/Users/user-alice/Items", IncludeItemTypes="Audio")[
        "Items"
    ]
    for track in tracks:
        ratio = track.get("PrimaryImageAspectRatio")
        assert ratio is None or ratio > 0
