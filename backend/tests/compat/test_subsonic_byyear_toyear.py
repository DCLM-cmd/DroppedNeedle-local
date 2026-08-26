"""GH-294: toYear=0 is an open upper bound in byYear album lists.

Feishin sends toYear=0 for an open-ended range; Navidrome-compatible servers
accept it. Malformed and out-of-range years still fail with Subsonic error 10,
and getRandomSongs keeps its existing permissive parsing."""

import pytest

from tests.compat.test_subsonic_search_cover import _get, _sub


@pytest.mark.asyncio
async def test_byyear_toyear_zero_is_an_open_upper_bound(compat_env):
    body = _sub(
        _get(
            compat_env, "getAlbumList2",
            type="byYear", fromYear=2026, toYear=0, offset=0, size=20,
        )
    )
    assert "error" not in body, body.get("error")
    assert body["status"] == "ok"
    assert "album" in body["albumList2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["abc", "10000"])
async def test_malformed_and_out_of_range_years_still_fail_code_10(
    compat_env, bad: str
):
    body = _sub(
        _get(compat_env, "getAlbumList2", type="byYear", fromYear=2026, toYear=bad)
    )
    assert body["error"]["code"] == 10


@pytest.mark.asyncio
async def test_getrandomsongs_contrast_unaffected(compat_env):
    body = _sub(_get(compat_env, "getRandomSongs", size=5, fromYear=2026, toYear=0))
    assert "error" not in body or body["error"].get("code") != 10
