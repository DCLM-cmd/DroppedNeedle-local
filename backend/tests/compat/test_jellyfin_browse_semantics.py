"""Issue #241 - Jellyfin browse semantics: SortBy/SortOrder, contributingArtistIds, Latest."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from models.audio import AudioInfo, AudioTag
from services.native.library_manager import _synth_artist_mbid

pytestmark = pytest.mark.asyncio

_OK_RG = "b1392450-e666-3926-a536-22c65f834433"
_ALPHA_RG = "rg-alpha-0000000000000000000000"
_ZULU_RG = "rg-zulu-00000000000000000000000"
_COLLAB_RG = "rg-collab-00000000000000000000"


def _h(env):
    return {"Authorization": f'MediaBrowser Token="{env.secret}", Client="pytest"'}


def _jget(env, path, **params):
    r = env.client.get(f"/jellyfin{path}", params=params, headers=_h(env))
    assert r.status_code == 200, r.content
    return json.loads(r.content)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _names(res):
    return [i["Name"] for i in res["Items"]]


async def _seed_extra(env):
    async def add(path, *, title, artist, album, album_artist, year, rg, rec):
        await env.lm.upsert_file(
            Path(path),
            AudioTag(
                title=title, artist=artist, album=album, track_number=1,
                album_artist=album_artist, year=year,
            ),
            AudioInfo(
                duration_seconds=180.0, bitrate=900, sample_rate=44100,
                channels=2, file_format="flac", file_size_bytes=1000,
                bit_depth=16,
            ),
            release_group_mbid=rg, recording_mbid=rec, file_mtime=1.0,
        )

    await add("/music/alpha.flac", title="Alpha Song", artist="Alpha Band",
              album="Alpha", album_artist="Alpha Band", year=1970,
              rg=_ALPHA_RG, rec="rec-alpha")
    await add("/music/zulu.flac", title="Zulu Song", artist="Zulu Band",
              album="Zulu", album_artist="Zulu Band", year=2020,
              rg=_ZULU_RG, rec="rec-zulu")
    await add("/music/feat.flac", title="Feat Track", artist="Featured Guy",
              album="Collab", album_artist="Main Band", year=2021,
              rg=_COLLAB_RG, rec="rec-feat")


async def _seed_plays(env):
    base = 1_700_000_000
    for n in range(3):  # OK Computer: most plays, but older
        await env.phs.insert(
            "user-alice", track_name="Airbag", artist_name="Radiohead",
            played_at=_iso(base + n), recording_mbid="rec-1",
            release_group_mbid=_OK_RG,
        )
    await env.phs.insert(  # Collab: one play, most recent
        "user-alice", track_name="Feat Track", artist_name="Featured Guy",
        played_at=_iso(base + 100), recording_mbid="rec-feat",
        release_group_mbid=_COLLAB_RG,
    )


async def _artist_jf(env, name):
    return await env.id_map.to_jf("artist", _synth_artist_mbid(name))


async def test_sortby_dateplayed_playcount_album_order_matches_history(compat_env):
    await _seed_extra(compat_env)
    await _seed_plays(compat_env)
    base = {"IncludeItemTypes": "MusicAlbum"}
    default = _names(_jget(compat_env, "/Items", **base))
    by_played = _names(_jget(compat_env, "/Items", SortBy="DatePlayed", **base))
    by_count = _names(_jget(compat_env, "/Items", SortBy="PlayCount", **base))
    assert by_played[:2] == ["Collab", "OK Computer"]  # most recently played first
    assert by_count[:2] == ["OK Computer", "Collab"]  # most played first
    assert by_played != default or by_count != default  # history differs from default


async def test_sortby_dateplayed_playcount_audio_order_matches_history(compat_env):
    await _seed_extra(compat_env)
    await _seed_plays(compat_env)
    base = {"IncludeItemTypes": "Audio"}
    by_played = _names(_jget(compat_env, "/Items", SortBy="DatePlayed", **base))
    by_count = _names(_jget(compat_env, "/Items", SortBy="PlayCount", **base))
    assert by_played == ["Feat Track", "Airbag"]
    assert by_count == ["Airbag", "Feat Track"]


async def test_sortname_respected_both_directions(compat_env):
    await _seed_extra(compat_env)
    albums = {"IncludeItemTypes": "MusicAlbum"}
    asc = _names(_jget(compat_env, "/Items", SortBy="SortName", **albums))
    assert asc == sorted(asc, key=str.casefold)
    assert asc[0] == "Alpha" and asc[-1] == "Zulu"
    desc = _names(
        _jget(compat_env, "/Items", SortBy="SortName", SortOrder="Descending",
              **albums)
    )
    assert desc == list(reversed(asc))

    tracks = {"IncludeItemTypes": "Audio"}
    t_asc = _names(_jget(compat_env, "/Items", SortBy="SortName", **tracks))
    assert t_asc == sorted(t_asc, key=str.casefold)
    t_desc = _names(
        _jget(compat_env, "/Items", SortBy="SortName", SortOrder="Descending",
              **tracks)
    )
    assert t_desc == list(reversed(t_asc))


async def test_sortby_random_shuffles_instead_of_default_order(compat_env):
    await _seed_extra(compat_env)
    base = {"IncludeItemTypes": "MusicAlbum", "Limit": 100}
    legacy = tuple(_names(_jget(compat_env, "/Items", **base)))
    seen = set()
    for _ in range(5):
        res = _jget(compat_env, "/Items", SortBy="Random", **base)
        assert res["TotalRecordCount"] == 4
        assert sorted(_names(res)) == sorted(legacy)  # same set, any order
        seen.add(tuple(_names(res)))
    assert len(seen) > 1  # shuffled: never the default order five times running


async def test_productionyear_respected_both_directions(compat_env):
    await _seed_extra(compat_env)
    base = {"IncludeItemTypes": "MusicAlbum"}
    asc = _names(_jget(compat_env, "/Items", SortBy="ProductionYear", **base))
    assert asc == ["Alpha", "OK Computer", "Zulu", "Collab"]
    desc = _names(
        _jget(compat_env, "/Items", SortBy="ProductionYear",
              SortOrder="Descending", **base)
    )
    assert desc == ["Collab", "Zulu", "OK Computer", "Alpha"]
    # PremiereDate aliases the year sort (Jellify sends it first), newest first.
    assert _names(_jget(compat_env, "/Items", SortBy="PremiereDate", **base)) == desc


async def test_sort_parsing_is_lenient_and_case_insensitive(compat_env):
    await _seed_extra(compat_env)
    base = {"IncludeItemTypes": "MusicAlbum"}
    legacy = _names(_jget(compat_env, "/Items", **base))
    assert _names(_jget(compat_env, "/Items", SortBy="Bogus", **base)) == legacy
    # First known wins: unknown first value is skipped, not fatal.
    by_name = _names(_jget(compat_env, "/Items", SortBy="SortName", **base))
    assert by_name == sorted(by_name, key=str.casefold)  # really sorted, not legacy
    assert _names(
        _jget(compat_env, "/Items", SortBy="Bogus,SortName", **base)
    ) == by_name
    # Lowercase param names/values bind like the real case-insensitive server.
    assert _names(_jget(compat_env, "/Items", sortby="sortname", **base)) == by_name
    by_year_desc = _names(
        _jget(compat_env, "/Items", SortBy="ProductionYear",
              SortOrder="Descending", **base)
    )
    assert _names(
        _jget(compat_env, "/Items", sortby="premieredate",
              sortorder="descending", **base)
    ) == by_year_desc
    # DateCreated defaults to the legacy newest-first order.
    assert _names(
        _jget(compat_env, "/Items", SortBy="DateCreated", **base)
    ) == legacy


async def test_contributing_artist_ids_returns_only_appearances(compat_env):
    await _seed_extra(compat_env)
    feat = await _artist_jf(compat_env, "Featured Guy")
    res = _jget(
        compat_env, "/Items", IncludeItemTypes="MusicAlbum",
        ContributingArtistIds=feat,
    )
    assert _names(res) == ["Collab"]
    # Lowercase param spelling binds the same filter.
    res_lower = _jget(
        compat_env, "/Items", IncludeItemTypes="MusicAlbum",
        contributingartistids=feat,
    )
    assert _names(res_lower) == ["Collab"]


async def test_contributing_artist_ids_empty_never_falls_through(compat_env):
    await _seed_extra(compat_env)
    base = {"IncludeItemTypes": "MusicAlbum"}
    # Album-artist-only acts have no appearances: empty, not the full catalog.
    main = await _artist_jf(compat_env, "Main Band")
    res = _jget(compat_env, "/Items", ContributingArtistIds=main, **base)
    assert res["Items"] == [] and res["TotalRecordCount"] == 0
    radiohead = await _artist_jf(compat_env, "Radiohead")
    res = _jget(compat_env, "/Items", ContributingArtistIds=radiohead, **base)
    assert res["Items"] == []
    # Undecodable ids filter to nothing instead of everything.
    res = _jget(compat_env, "/Items", ContributingArtistIds="no-such-id", **base)
    assert res["Items"] == []


async def test_latest_returns_recently_added_album_array(compat_env):
    await _seed_extra(compat_env)
    latest = _jget(compat_env, "/UserItems/Latest")
    assert isinstance(latest, list)
    assert len(latest) == 4
    assert all(i["Type"] == "MusicAlbum" for i in latest)
    # Same recency definition as the default browse list.
    browse = _jget(compat_env, "/Items", IncludeItemTypes="MusicAlbum")
    assert [i["Id"] for i in latest] == [
        i["Id"] for i in browse["Items"][:4]
    ]
    # Legacy dialect matches the modern one.
    legacy = _jget(compat_env, "/Users/user-alice/Items/Latest")
    assert legacy == latest


async def test_latest_limit_and_parent(compat_env):
    await _seed_extra(compat_env)
    latest = _jget(compat_env, "/UserItems/Latest", Limit=2)
    assert len(latest) == 2
    full = _jget(compat_env, "/UserItems/Latest")
    assert latest == full[:2]
    library_id = _jget(compat_env, "/UserViews")["Items"][0]["Id"]
    assert _jget(compat_env, "/UserItems/Latest", ParentId=library_id) == full
    # Unknown parents (and non-library items) yield an empty array, like _browse.
    assert _jget(compat_env, "/UserItems/Latest", ParentId="no-such-id") == []
    album_id = _jget(compat_env, "/Items", IncludeItemTypes="MusicAlbum")[
        "Items"
    ][0]["Id"]
    assert _jget(compat_env, "/UserItems/Latest", ParentId=album_id) == []


async def test_latest_requires_auth(compat_env):
    r = compat_env.client.get("/jellyfin/UserItems/Latest")
    assert r.status_code == 401
    r = compat_env.client.get("/jellyfin/Users/user-alice/Items/Latest")
    assert r.status_code == 401
