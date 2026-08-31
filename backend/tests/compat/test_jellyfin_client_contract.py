"""Shapes and budgets a real Jellyfin client depends on.

Every defect here produced the same user-visible symptom - "Finamp shows nothing" -
while the server log was full of 200s, because each one breaks the CLIENT after a
successful HTTP call:

* ``/Users/Public`` had no route at all, so it fell into ``/Users/{user_id}`` and was
  read as "the user whose id is Public": 401 without a token, and a single UserDto
  OBJECT with one. Jellyfin returns an ARRAY, so the client's parser threw.
* ``MediaSourceInfo`` emitted a handful of fields. Finamp's generated parser
  hard-casts the non-nullable ones, so every track's metadata fetch threw inside
  ``_$MediaSourceInfoFromJson``.
* Playback progress reports were charged to the small mutation budget, so ordinary
  listening earned 33 x 429 in one session.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.compat.jellyfin.models as jm
from api.compat.common.deps import CompatServices, get_compat_services
from api.compat.common.ratelimit import is_mutation_request
from api.compat.jellyfin.router import router


def _client():
    services = MagicMock(spec=CompatServices)
    services.preferences = MagicMock()
    services.preferences.get_connect_apps_settings = MagicMock(
        return_value=SimpleNamespace(jellyfin_enabled=True)
    )
    services.app_passwords = MagicMock()
    services.app_passwords.verify_token = AsyncMock(
        return_value=SimpleNamespace(
            id="u1", username="nils", username_display="Nils",
            display_name="Nils", role="admin",
        )
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_compat_services] = lambda: services
    return TestClient(app)


# ---- /Users/Public --------------------------------------------------------------

def test_public_users_is_a_list_not_an_object() -> None:
    """The regression: a client iterates this, and got a UserDto object instead."""
    body = json.loads(_client().get("/jellyfin/Users/Public").content)

    assert isinstance(body, list)


def test_public_users_needs_no_token() -> None:
    """It is what a client reads BEFORE logging in; 401 breaks the login screen."""
    assert _client().get("/jellyfin/Users/Public").status_code == 200


def test_public_users_discloses_no_usernames() -> None:
    """Anyone who can reach the port can call this, so it must not list people."""
    assert json.loads(_client().get("/jellyfin/Users/Public").content) == []


def test_a_real_user_id_still_resolves() -> None:
    """The literal route must not shadow the parameterised one."""
    auth = {"Authorization": 'MediaBrowser Token="t", Client="c", DeviceId="d"'}
    body = json.loads(_client().get("/jellyfin/Users/u1", headers=auth).content)

    assert body["Id"]
    assert body["Name"]


# ---- MediaSourceInfo ------------------------------------------------------------

# Non-nullable in Jellyfin's schema, so a generated client hard-casts them.
_REQUIRED_SOURCE_FIELDS = {
    "Id": str, "Protocol": str, "Type": str, "Name": str, "ETag": str,
    "IsRemote": bool, "ReadAtNativeFramerate": bool, "IgnoreDts": bool,
    "IgnoreIndex": bool, "GenPtsInput": bool, "SupportsTranscoding": bool,
    "SupportsDirectStream": bool, "SupportsDirectPlay": bool,
    "IsInfiniteStream": bool, "RequiresOpening": bool, "RequiresClosing": bool,
    "RequiresLooping": bool, "SupportsProbing": bool,
    "MediaStreams": list, "MediaAttachments": list, "Formats": list,
    "RequiredHttpHeaders": dict, "DefaultAudioStreamIndex": int,
}
_REQUIRED_STREAM_FIELDS = {
    "Type": str, "Index": int, "IsDefault": bool, "IsInterlaced": bool,
    "IsForced": bool, "IsExternal": bool, "IsTextSubtitleStream": bool,
    "SupportsExternalStream": bool,
}


def _encoded(struct):
    return json.loads(msgspec.json.encode(struct))


@pytest.mark.parametrize("field,typ", sorted(_REQUIRED_SOURCE_FIELDS.items()))
def test_a_media_source_carries_every_hard_cast_field(field, typ) -> None:
    encoded = _encoded(jm.MediaSourceInfo(Id="item-1"))

    assert field in encoded, f"{field} is dropped from the response"
    assert isinstance(encoded[field], typ), field


@pytest.mark.parametrize("field,typ", sorted(_REQUIRED_STREAM_FIELDS.items()))
def test_a_media_stream_carries_every_hard_cast_field(field, typ) -> None:
    encoded = _encoded(jm.MediaStream())

    assert field in encoded, f"{field} is dropped from the response"
    assert isinstance(encoded[field], typ), field


def test_no_required_field_is_emitted_as_null() -> None:
    """A null is exactly as fatal as a missing key to a hard cast."""
    encoded = _encoded(jm.MediaSourceInfo(Id="item-1"))

    assert not [f for f in _REQUIRED_SOURCE_FIELDS if encoded.get(f) is None]


def test_the_playable_details_still_come_through() -> None:
    """Filling the shape must not displace what actually plays the track."""
    encoded = _encoded(
        jm.MediaSourceInfo(
            Id="item-1", Container="flac", Size=123, Bitrate=609000,
            RunTimeTicks=8365956, DirectStreamUrl="http://host/stream.flac",
            MediaStreams=[jm.MediaStream(Codec="flac", SampleRate=44100)],
        )
    )

    assert encoded["Container"] == "flac"
    assert encoded["DirectStreamUrl"].endswith("stream.flac")
    assert encoded["MediaStreams"][0]["Codec"] == "flac"
    assert encoded["RunTimeTicks"] == 8365956


# ---- rate limiting --------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/jellyfin/Sessions/Playing",
        "/jellyfin/Sessions/Playing/Progress",
        "/jellyfin/Sessions/Playing/Stopped",
        "/jellyfin/Sessions/Playing/Ping",
        "/jellyfin/Sessions/Capabilities",
        "/jellyfin/Sessions/Capabilities/Full",
        "/jellyfin/Sessions/Logout",
    ],
)
def test_playback_telemetry_is_not_charged_to_the_mutation_budget(path) -> None:
    """The regression: a player reports progress on a cadence, and 33 of those
    reports came back 429 during one listening session."""
    assert not is_mutation_request("POST", path)


@pytest.mark.parametrize(
    "path",
    ["/jellyfin/Playlists", "/jellyfin/Playlists/p1/Items", "/jellyfin/UserFavoriteItems/i1"],
)
def test_real_mutations_are_still_budgeted(path) -> None:
    assert is_mutation_request("POST", path)


def test_deletes_are_still_mutations() -> None:
    assert is_mutation_request("DELETE", "/jellyfin/Sessions/Playing")


# ---- the untouched-file endpoints ----------------------------------------------

def _routes():
    return {
        (route.path, method)
        for route in router.routes
        if hasattr(route, "methods")
        for method in route.methods
    }


@pytest.mark.parametrize(
    "path", ["/jellyfin/Items/{item_id}/File", "/jellyfin/Items/{item_id}/Download"]
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_the_untouched_file_endpoints_are_routed(path, method) -> None:
    """The regression, and the reason it was invisible: neither path was routed, so
    the request fell through to the SPA catch-all and the PLAYER got index.html -
    3 KB of text/html answered with a cheerful 206. iOS reported only
    "AVError -11828 / cannot open", and a server-side check of the status code alone
    looked healthy.

    Finamp opens these rather than the DirectStreamUrl it was handed, so nothing
    played at all.
    """
    assert (path, method) in _routes()


def test_the_file_endpoint_is_not_shadowed_by_the_item_lookup() -> None:
    """/Items/{item_id} must not swallow /Items/{item_id}/File."""
    paths = {path for path, _ in _routes()}

    assert "/jellyfin/Items/{item_id}" in paths
    assert "/jellyfin/Items/{item_id}/File" in paths


# ---- blurhashes -----------------------------------------------------------------

def test_the_blur_hash_shape_matches_jellyfin() -> None:
    """Jellyfin publishes ``{"Primary": {<image tag>: <hash>}}``. Finamp keys the hash
    by the tag to tell that an image it already holds is the same one - without it, it
    warns that "the server seems to be misconfigured" and re-downloads artwork."""
    from api.compat.jellyfin.builders import JellyfinBuilder

    assert JellyfinBuilder._blur_hashes("tag-1", "U~Lqe9%M") == {
        "Primary": {"tag-1": "U~Lqe9%M"}
    }


def test_only_real_strings_reach_the_payload() -> None:
    """The field is typed ``dict[str, dict[str, str]]``, and the cover repository is
    reached through an adapter - so what arrives is not always what the type hints
    here promise. Anything else only surfaces at the very end of the request, in the
    encoder, as a 500 on the whole listing."""
    from unittest.mock import AsyncMock

    from api.compat.jellyfin.builders import JellyfinBuilder

    for tag, blurhash in (
        (AsyncMock(), "U~Lqe9%M"),
        ("tag-1", AsyncMock()),
        (object(), object()),
        (b"tag-1", b"hash"),
        (5, 7),
    ):
        assert JellyfinBuilder._blur_hashes(tag, blurhash) == {}


@pytest.mark.parametrize(
    "tag,blurhash", [(None, "U~Lqe9%M"), ("tag-1", None), (None, None), ("", "")]
)
def test_a_missing_half_publishes_nothing_rather_than_a_broken_pair(tag, blurhash) -> None:
    """A hash with no tag cannot be matched to an image, and a tag with no hash is
    what a client reads as "this server computes none"."""
    from api.compat.jellyfin.builders import JellyfinBuilder

    assert JellyfinBuilder._blur_hashes(tag, blurhash) == {}


def test_the_dto_carries_blur_hashes_as_a_dict_of_dicts() -> None:
    encoded = json.loads(
        msgspec.json.encode(
            jm.BaseItemDto(
                Id="a", Name="A", ServerId="s", Type="MusicAlbum",
                ImageTags={"Primary": "tag-1"},
                ImageBlurHashes={"Primary": {"tag-1": "U~Lqe9%M"}},
            )
        )
    )

    assert encoded["ImageBlurHashes"]["Primary"]["tag-1"] == "U~Lqe9%M"


# ---- the fields clients name in Fields= -----------------------------------------

# Finamp sends this exact list on every listing. A field a client ASKED for and did
# not get is not a neutral omission - it silently disables the feature behind it.
_FINAMP_REQUESTS = [
    "ChildCount", "DateCreated", "DateLastMediaAdded", "Etag", "Genres",
    "ParentId", "ProviderIds", "Tags", "SortName", "People",
]


@pytest.mark.parametrize("field", _FINAMP_REQUESTS)
def test_every_field_finamp_asks_for_exists_on_the_item_dto(field) -> None:
    assert field in jm.BaseItemDto.__struct_fields__, (
        f"clients request Fields={field} and we have no such field to answer with"
    )


@pytest.mark.parametrize(
    "field",
    [
        # music metadata we hold
        "NormalizationGain", "AlbumNormalizationGain", "GenreItems", "PremiereDate",
        "AlbumCount", "SongCount", "RecursiveItemCount", "CumulativeRunTimeTicks",
        "MediaSources", "MediaStreams", "MediaSourceCount",
        # capability flags a client gates its UI on
        "CanDownload", "CanDelete", "PlayAccess",
    ],
)
def test_the_music_relevant_jellyfin_fields_exist(field) -> None:
    assert field in jm.BaseItemDto.__struct_fields__


def test_capability_flags_are_real_values_not_null() -> None:
    """CanDownload decides whether a client offers offline downloads at all, so a
    null there reads as "no" - or crashes a hard cast."""
    encoded = json.loads(
        msgspec.json.encode(jm.BaseItemDto(Id="a", Name="A", Type="Audio"))
    )

    assert encoded["CanDownload"] is True
    assert encoded["CanDelete"] is False
    assert encoded["PlayAccess"] == "Full"
    assert encoded["Tags"] == []
    assert encoded["People"] == []


def test_a_person_carries_what_a_client_renders() -> None:
    encoded = json.loads(
        msgspec.json.encode(jm.BaseItemPerson(Name="Quadeca", Id="artist-1"))
    )

    assert encoded["Name"] == "Quadeca"
    assert encoded["Id"] == "artist-1"
    assert encoded["Type"] == "Artist"
