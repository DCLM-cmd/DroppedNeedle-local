from unittest.mock import AsyncMock, MagicMock

import pytest

from services.native.target_library_repository import TargetLibraryRepository


MBIDS = {
    "0c1f7f1e-2a3b-4c5d-8e9f-000000000003",
    "0A1F7F1E-2A3B-4C5D-8E9F-000000000001",
    "0b1f7f1e-2a3b-4c5d-8e9f-000000000002",
}


def _repo(mbids: set[str]) -> TargetLibraryRepository:
    store = MagicMock()
    store.target_provider_artist_ids = AsyncMock(return_value=mbids)
    return TargetLibraryRepository(store)


@pytest.mark.asyncio
async def test_page_is_sorted_casefolded_and_starts_after_the_cursor() -> None:
    repo = _repo(MBIDS)

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
    assert await _repo(set()).get_artist_mbid_page(after_mbid="", limit=500) == []


@pytest.mark.asyncio
async def test_blank_mbids_are_skipped_and_limit_is_floored() -> None:
    repo = _repo({"", "0a1f7f1e-2a3b-4c5d-8e9f-000000000001"})
    assert await repo.get_artist_mbid_page(after_mbid="", limit=500) == [
        "0a1f7f1e-2a3b-4c5d-8e9f-000000000001"
    ]
    assert len(await repo.get_artist_mbid_page(after_mbid="", limit=0)) == 1
