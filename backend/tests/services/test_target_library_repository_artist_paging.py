"""F-TARGETCATALOG-04: artist MBID paging via the store keyset query.

The repository delegates each page to
``NativeLibraryStore.target_provider_artist_ids_page`` - one bounded keyset
SQL read per page, never the full-set ``target_provider_artist_ids``. The
mocked-store tests below pin that delegation contract (casefolded ordering,
strict cursor advancement, blank-ID skip handled by SQL, empty termination,
and the limit floor); the real-SQL behaviour is covered by the store
integration test in test_target_cutoff_unmet.py's module family.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.native.target_library_repository import TargetLibraryRepository


def _repo(pages: dict[str, list[str]]) -> TargetLibraryRepository:
    store = MagicMock()
    async def page(after_mbid, *, limit):
        # Mirror the real store: the limit is floored at one.
        return pages.get(after_mbid, [])[: max(1, limit)]

    store.target_provider_artist_ids_page = AsyncMock(side_effect=page)
    store.target_provider_artist_ids = AsyncMock()
    return TargetLibraryRepository(store)


@pytest.mark.asyncio
async def test_page_is_sorted_casefolded_and_starts_after_the_cursor() -> None:
    repo = _repo(
        {
            "": [
                "0a1f7f1e-2a3b-4c5d-8e9f-000000000001",
                "0b1f7f1e-2a3b-4c5d-8e9f-000000000002",
            ],
            "0b1f7f1e-2a3b-4c5d-8e9f-000000000002": [
                "0c1f7f1e-2a3b-4c5d-8e9f-000000000003"
            ],
            "0c1f7f1e-2a3b-4c5d-8e9f-000000000003": [],
        }
    )

    first = await repo.get_artist_mbid_page(after_mbid="", limit=2)
    assert first == [
        "0a1f7f1e-2a3b-4c5d-8e9f-000000000001",
        "0b1f7f1e-2a3b-4c5d-8e9f-000000000002",
    ]

    second = await repo.get_artist_mbid_page(after_mbid=first[-1], limit=2)
    assert second == ["0c1f7f1e-2a3b-4c5d-8e9f-000000000003"]
    assert await repo.get_artist_mbid_page(after_mbid=second[-1], limit=2) == []


@pytest.mark.asyncio
async def test_empty_library_terminates_immediately() -> None:
    assert await _repo({"": []}).get_artist_mbid_page(after_mbid="", limit=500) == []


@pytest.mark.asyncio
async def test_blank_mbids_are_skipped_and_limit_is_floored() -> None:
    # Blank IDs are filtered by the keyset SQL (TRIM != ''); the mocked page
    # returns only non-blank normalized IDs.
    repo = _repo({"": ["0a1f7f1e-2a3b-4c5d-8e9f-000000000001"]})
    assert await repo.get_artist_mbid_page(after_mbid="", limit=500) == [
        "0a1f7f1e-2a3b-4c5d-8e9f-000000000001"
    ]
    assert len(await repo.get_artist_mbid_page(after_mbid="", limit=0)) == 1


@pytest.mark.asyncio
async def test_paging_never_reads_the_full_artist_set() -> None:
    """F-TARGETCATALOG-04: page requests must not call the full-set method."""
    repo = _repo({"": ["0a1f7f1e-2a3b-4c5d-8e9f-000000000001"]})
    await repo.get_artist_mbid_page(after_mbid="", limit=500)
    repo._store.target_provider_artist_ids.assert_not_called()
