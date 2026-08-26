"""GH-294: year 0 is the unbounded lower endpoint for byYear album lists.

OpenSubsonic uses parameter order for direction. Feishin sends the current year
followed by 0 for an unrestricted newest-first list.
"""

import pytest

from tests.compat.test_subsonic_search_cover import _get, _sub
from tests.compat.test_subsonic_browsing import _add_album


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint,response_key",
    [("getAlbumList2", "albumList2"), ("getAlbumList", "albumList")],
)
async def test_byyear_zero_preserves_unbounded_range_and_direction(
    compat_env, endpoint: str, response_key: str
):
    for suffix, title, year in (
        ("201", "Sentinel Old", 1980),
        ("202", "Sentinel Current", 2026),
        ("203", "Sentinel Future", 2030),
    ):
        await _add_album(
            compat_env,
            rg=f"00000000-0000-0000-0000-000000000{suffix}",
            title=title,
            year=year,
            genre="Rock",
        )

    def sentinel_titles(first: int, last: int) -> list[str]:
        body = _sub(
            _get(
                compat_env,
                endpoint,
                type="byYear",
                fromYear=first,
                toYear=last,
                offset=0,
                size=50,
            )
        )
        assert "error" not in body, body.get("error")
        titles = [
            album.get("name") or album.get("title", "")
            for album in body[response_key]["album"]
        ]
        return [title for title in titles if title.startswith("Sentinel ")]

    assert sentinel_titles(2026, 0) == ["Sentinel Current", "Sentinel Old"]
    assert sentinel_titles(0, 2026) == ["Sentinel Old", "Sentinel Current"]
    assert sentinel_titles(0, 0) == [
        "Sentinel Old",
        "Sentinel Current",
        "Sentinel Future",
    ]


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
