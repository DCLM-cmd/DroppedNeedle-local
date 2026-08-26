"""ST1: changed type filters change ONLY the search cache key.

The response bakes the user's primary/secondary types, so two identical
queries under different prefs must be different cache entries (and the same
normalized prefs must share one entry) - closing the gap that used to force
wholesale sweeps on preference saves.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from services.search_service import SearchService


def _make_service(primary: list[str], secondary: list[str]):
    """House-style construction mirroring tests_services.test_search_service."""
    mb_repo = AsyncMock()
    mb_repo.search_grouped = AsyncMock(return_value={"artists": [], "albums": []})

    library_repo = MagicMock()
    library_repo.get_library_mbids = AsyncMock(return_value=set())

    coverart_repo = MagicMock()
    preferences_service = MagicMock()
    preferences_service.get_preferences.return_value = MagicMock(
        primary_types=list(primary), secondary_types=list(secondary)
    )

    svc = SearchService(
        mb_repo=mb_repo,
        library_repo=library_repo,
        coverart_repo=coverart_repo,
        preferences_service=preferences_service,
    )
    return svc, mb_repo


@pytest.fixture(autouse=True)
def clean_search_cache():
    SearchService.clear_cached_results()
    yield
    SearchService.clear_cached_results()


class TestTypesInCacheKey:
    @pytest.mark.asyncio
    async def test_same_query_different_types_are_distinct_entries(self):
        svc_a, repo_a = _make_service(["album", "ep"], ["studio"])
        svc_b, _repo_b = _make_service(["album"], ["live", "studio"])

        await svc_a.search("beatles", limit_artists=5, limit_albums=5)
        # Different filters -> different cache keys -> the second service
        # cannot reuse the first entry and must hit its provider again.
        svc_b._mb_repo = AsyncMock()
        svc_b._mb_repo.search_grouped = AsyncMock(
            return_value={"artists": [], "albums": []}
        )
        await svc_b.search("beatles", limit_artists=5, limit_albums=5)

        assert svc_b._mb_repo.search_grouped.await_count == 1

    @pytest.mark.asyncio
    async def test_identical_normalized_prefs_share_one_entry(self):
        svc_a, repo_a = _make_service(["Album", "EP"], ["Studio"])
        svc_b, _repo_b = _make_service(["album", "ep"], ["studio"])

        await svc_a.search("beatles", limit_artists=5, limit_albums=5)
        # Case/order drift only -> SAME normalized key -> served from cache.
        await svc_b.search("beatles", limit_artists=5, limit_albums=5)

        assert repo_a.search_grouped.await_count == 1
