"""Blacklisting the source that delivered an album.

The case no automatic check can catch: the files verified, the import was clean, and
the album is simply the wrong VERSION - a clean edit where the explicit cut was
wanted. Nothing in the pipeline can detect that, so nothing else will ever stop the
same release being picked again.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.exceptions import ValidationError
from models.download import DownloadTask, ScoredCandidate
from models.download_identity import (
    SOURCE_SOULSEEK,
    SOURCE_USENET,
    delivered_identities,
    soulseek_identity,
    usenet_identity,
)
from repositories.protocols.download_client import DownloadSearchResult
from services.native.download_service import ORIGIN_REPLACEMENT

from tests.services.test_download_service import _make_service


def _task(**over) -> DownloadTask:
    base = dict(
        id="task-1",
        user_id="u1",
        release_group_mbid="rg-1",
        status="completed",
        source="soulseek",
        search_job_id="job-1",
        candidate_index=0,
        artist_name="Kanye West",
        album_title="The Life of Pablo",
        year=2016,
    )
    base.update(over)
    return DownloadTask(**base)


def _file(name: str) -> DownloadSearchResult:
    return DownloadSearchResult(
        username="peer",
        filename=name,
        parent_directory="Pablo",
        size=1000,
        extension="flac",
    )


@pytest.mark.asyncio
async def test_soulseek_is_blocked_per_delivered_file():
    """The scorers match Soulseek per file, so a peer-wide entry would never fire."""
    service, store, *_ = _make_service()
    store.list_tasks.return_value = [_task()]
    store.get_search_job_candidates.return_value = [
        ScoredCandidate(
            source="soulseek",
            username="peer",
            files=[_file("Pablo/01.flac"), _file("Pablo/02.flac")],
        )
    ]

    result = await service.blacklist_album_source("rg-1", "u1", "admin")

    assert result["blocked"] == 2
    recorded = {c.kwargs["identity"] for c in store.record_quarantine.await_args_list}
    assert recorded == {
        soulseek_identity("peer", "Pablo/01.flac"),
        soulseek_identity("peer", "Pablo/02.flac"),
    }
    assert all(
        c.kwargs["reason"] == "manual"
        for c in store.record_quarantine.await_args_list
    )


@pytest.mark.asyncio
async def test_usenet_is_blocked_by_its_release_identity():
    service, store, *_ = _make_service()
    store.list_tasks.return_value = [_task(source="usenet")]
    store.get_search_job_candidates.return_value = [
        ScoredCandidate(
            source="usenet",
            usenet_release=SimpleNamespace(title="Pablo-2016-WEB-FLAC", size_bytes=500),
        )
    ]

    result = await service.blacklist_album_source("rg-1", "u1", "admin")

    assert result["sources"] == ["usenet"]
    assert store.record_quarantine.await_args_list[0].kwargs["identity"] == (
        usenet_identity("Pablo-2016-WEB-FLAC", 500)
    )


@pytest.mark.asyncio
async def test_nothing_delivered_is_reported_not_silently_accepted():
    service, store, *_ = _make_service()
    store.list_tasks.return_value = []

    with pytest.raises(ValidationError, match="No completed download"):
        await service.blacklist_album_source("rg-1", "u1", "admin")


@pytest.mark.asyncio
async def test_pinned_identity_blocks_even_after_the_search_job_is_pruned():
    """The bug this fixes: an album removed weeks after it landed has, by then, lost
    its search job to the 7-day prune, so re-deriving the delivered identity yielded
    nothing and the block silently did nothing. The identity pinned on the task at
    completion must block on its own, without ever touching the job."""
    service, store, *_ = _make_service()
    pinned = usenet_identity("JID-The Forever Story (Extended Version)-WEB-FLAC", 499)
    store.list_tasks.return_value = [
        _task(
            source="usenet",
            delivered_blocklist_json=json.dumps([[SOURCE_USENET, pinned]]),
        )
    ]
    store.get_search_job_candidates.return_value = []  # pruned - would block nothing

    result = await service.blacklist_album_source("rg-1", "u1", "admin")

    assert result["blocked"] == 1
    assert store.record_quarantine.await_args_list[0].kwargs["identity"] == pinned
    store.get_search_job_candidates.assert_not_awaited()


def test_delivered_identities_helper_usenet_and_soulseek():
    usenet = ScoredCandidate(
        source="usenet",
        usenet_release=SimpleNamespace(title="Album-WEB-FLAC", size_bytes=500),
    )
    assert delivered_identities(usenet) == [
        (SOURCE_USENET, usenet_identity("Album-WEB-FLAC", 500))
    ]

    soulseek = ScoredCandidate(
        source="soulseek",
        username="peer",
        files=[_file("A/01.flac"), _file("")],  # blank filename dropped
    )
    assert delivered_identities(soulseek) == [
        (SOURCE_SOULSEEK, soulseek_identity("peer", "A/01.flac"))
    ]
    # a usenet candidate with no release yields nothing to block, not a crash
    assert delivered_identities(ScoredCandidate(source="usenet")) == []


@pytest.mark.asyncio
async def test_a_pruned_search_job_is_reported_rather_than_half_blocked():
    """Without the candidate there is nothing to match on; claiming success would
    leave the user believing a block is in place that is not."""
    service, store, *_ = _make_service()
    store.list_tasks.return_value = [_task()]
    store.get_search_job_candidates.return_value = []

    with pytest.raises(ValidationError, match="could not be identified"):
        await service.blacklist_album_source("rg-1", "u1", "admin")


@pytest.mark.asyncio
async def test_redownload_bypasses_the_already_in_library_gate():
    """The album IS in the library - that is exactly what the user rejected. A normal
    re-request answers 'already satisfied' and would do nothing at all."""
    service, store, *_ = _make_service(in_library=True)
    store.list_tasks.return_value = [_task()]
    store.get_search_job_candidates.return_value = [
        ScoredCandidate(source="soulseek", username="peer", files=[_file("a.flac")])
    ]
    service.request_album = AsyncMock(return_value="task-new")

    result = await service.blacklist_album_source(
        "rg-1", "u1", "admin", redownload=True
    )

    assert result["task_id"] == "task-new"
    assert service.request_album.await_args.kwargs["origin"] == ORIGIN_REPLACEMENT


@pytest.mark.asyncio
async def test_blacklisting_alone_does_not_re_request():
    service, store, *_ = _make_service()
    store.list_tasks.return_value = [_task()]
    store.get_search_job_candidates.return_value = [
        ScoredCandidate(source="soulseek", username="peer", files=[_file("a.flac")])
    ]
    service.request_album = AsyncMock()

    result = await service.blacklist_album_source("rg-1", "u1", "admin")

    assert result["task_id"] is None
    service.request_album.assert_not_awaited()
