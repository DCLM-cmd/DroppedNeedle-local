"""``get_artist_aliases`` - the download search fallback's alias source (cache-aside,
primary-name excluded, best-effort on failure)."""

from unittest.mock import AsyncMock

import pytest

import repositories.musicbrainz_artist as mod
from repositories.musicbrainz_artist import MusicBrainzArtistMixin


class _Repo(MusicBrainzArtistMixin):
    def __init__(self):
        self._cache = AsyncMock()
        self._cache.get.return_value = None


_MB_ARTIST = {
    "name": "宇多田ヒカル",
    "aliases": [
        {"name": "Utada Hikaru", "type": "Artist name"},
        {"name": "宇多田ヒカル", "type": "Artist name"},   # = primary, must be excluded
        {"name": "utada hikaru", "type": "Search hint"},  # case-dupe, must be excluded
        {"name": "Utada", "type": "Artist name"},
        {"name": "", "type": "Artist name"},              # empty, must be excluded
    ],
}


@pytest.mark.asyncio
async def test_aliases_exclude_primary_and_dupes(monkeypatch):
    repo = _Repo()
    monkeypatch.setattr(mod, "mb_api_get", AsyncMock(return_value=_MB_ARTIST))

    aliases = await repo.get_artist_aliases("mbid-1")

    assert aliases == ["Utada Hikaru", "Utada"]
    repo._cache.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_aliases_limit(monkeypatch):
    repo = _Repo()
    many = {"name": "X", "aliases": [{"name": f"Alias {n}"} for n in range(10)]}
    monkeypatch.setattr(mod, "mb_api_get", AsyncMock(return_value=many))

    assert len(await repo.get_artist_aliases("mbid-1", limit=3)) == 3


@pytest.mark.asyncio
async def test_aliases_cached_value_short_circuits(monkeypatch):
    repo = _Repo()
    repo._cache.get.return_value = ["Cached Alias"]
    fetch = AsyncMock()
    monkeypatch.setattr(mod, "mb_api_get", fetch)

    assert await repo.get_artist_aliases("mbid-1") == ["Cached Alias"]
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_aliases_failure_returns_empty(monkeypatch):
    repo = _Repo()
    monkeypatch.setattr(mod, "mb_api_get", AsyncMock(side_effect=RuntimeError("503")))

    assert await repo.get_artist_aliases("mbid-1") == []
    repo._cache.set.assert_not_awaited()  # a transient failure is never cached


@pytest.mark.asyncio
async def test_aliases_empty_mbid_is_noop():
    repo = _Repo()
    assert await repo.get_artist_aliases("") == []
    repo._cache.get.assert_not_awaited()
