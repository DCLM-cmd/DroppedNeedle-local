"""CompositeIndexer with Prowlarr pooled: health aggregation (no primary
short-circuit), cross-member pooling/dedup, unconfigured-tree-unchanged."""

import pytest

from models.common import ServiceStatus
from repositories.protocols.indexer import IndexerResult, UsenetRelease
from repositories.prowlarr.prowlarr_client import ProwlarrClient
from repositories.prowlarr.prowlarr_indexer import ProwlarrIndexer
from services.native.acquisition.composite_indexer import CompositeIndexer
from tests.mocks.fake_download_client import FakeIndexer
from tests.mocks.prowlarr_mock import client_for


def _prowlarr(host: str = "prowlarr") -> ProwlarrIndexer:
    return ProwlarrIndexer(
        ProwlarrClient(client_for(), f"http://{host}", "secret"),
        categories=[3000],
    )


class _ErrorMember:
    """A configured-but-unreachable member (e.g. empty Newznab primary)."""

    @property
    def indexer_name(self) -> str:
        return "usenet"

    def is_configured(self) -> bool:
        return True

    async def health_check(self) -> ServiceStatus:
        return ServiceStatus(status="error", message="No indexer reachable")

    async def search_album(self, *a, **k):
        return []

    async def search_track(self, *a, **k):
        return []


class _RaisingMember(_ErrorMember):
    async def health_check(self) -> ServiceStatus:
        raise RuntimeError("boom")


def _release(title: str, size: int, tag: str) -> IndexerResult:
    return IndexerResult(
        source="usenet",
        usenet=UsenetRelease(
            indexer_id=tag,
            indexer_name=tag,
            guid=f"{tag}-g",
            title=title,
            nzb_url=f"https://idx.example/{tag}",
            size_bytes=size,
        ),
    )


class _FixedMember(_ErrorMember):
    def __init__(self, releases: list[IndexerResult]):
        self._releases = releases

    async def health_check(self) -> ServiceStatus:
        return ServiceStatus(status="ok", version=None, message="fixed")

    async def search_album(self, *a, **k):
        return list(self._releases)

    async def search_track(self, *a, **k):
        return list(self._releases)


@pytest.mark.asyncio
async def test_health_aggregates_primary_and_extras():
    composite = CompositeIndexer(_ErrorMember(), [_prowlarr()])
    status = await composite.health_check()
    assert status.status == "ok"  # healthy Prowlarr visible despite error primary
    assert "1/2" in status.message
    assert status.version == "1.32.2.4987"


@pytest.mark.asyncio
async def test_health_all_error_and_empty():
    composite = CompositeIndexer(_ErrorMember(), [_ErrorMember()])
    status = await composite.health_check()
    assert status.status == "error"
    status = await CompositeIndexer(None, []).health_check()
    assert status.status == "error"
    assert "No indexers configured" in status.message


@pytest.mark.asyncio
async def test_health_member_exception_never_raises():
    composite = CompositeIndexer(_RaisingMember(), [_RaisingMember()])
    status = await composite.health_check()
    assert status.status == "error"


@pytest.mark.asyncio
async def test_pools_primary_with_extras_and_dedups_first_seen_wins():
    # Composite mechanics (production pools the SELECTED backend as primary plus
    # plugin extras; either/or means Prowlarr-vs-Newznab dupes can't occur).
    shared = _release("Radiohead - In Rainbows", 100, "primary-copy")
    primary = _FixedMember([shared, _release("Other Album", 50, "primary-only")])
    composite = CompositeIndexer(primary, [_prowlarr()])
    results = await composite.search_album("Radiohead", "In Rainbows")
    titles = [r.usenet.title for r in results]
    assert "Other Album" in titles
    assert "Radiohead - In Rainbows (2007) [FLAC]" in titles  # extra pooled
    # First-seen-wins: the primary copy of a dup identity wins over extras'.
    dup = _FixedMember([_release("Radiohead - In Rainbows (2007) [FLAC]", 2315726631, "nb")])
    composite2 = CompositeIndexer(dup, [_prowlarr()])
    results2 = await composite2.search_album("Radiohead", "In Rainbows")
    assert [r.usenet.indexer_id for r in results2] == ["nb"]


@pytest.mark.asyncio
async def test_unconfigured_prowlarr_changes_nothing():
    composite = CompositeIndexer(FakeIndexer(), [ProwlarrIndexer(None)])
    results = await composite.search_album("Radiohead", "In Rainbows")
    assert len(results) == 1  # only the FakeIndexer hit
    assert results[0].usenet.indexer_id == "fake"


@pytest.mark.asyncio
async def test_failing_member_never_fails_fanout():
    composite = CompositeIndexer(_RaisingMember(), [_prowlarr("prowlarr-error")])
    assert await composite.search_album("Radiohead", "In Rainbows") == []


def test_select_usenet_primary_either_or():
    from core.dependencies.service_providers import _select_usenet_primary

    newznab, prowlarr = object(), object()
    assert _select_usenet_primary("indexers", newznab, prowlarr) is newznab
    assert _select_usenet_primary("prowlarr", newznab, prowlarr) is prowlarr
    # Unknown selectors collapse to indexers (pre-existing setups unaffected).
    assert _select_usenet_primary("lidarr", newznab, prowlarr) is newznab
    assert _select_usenet_primary("", newznab, prowlarr) is newznab
