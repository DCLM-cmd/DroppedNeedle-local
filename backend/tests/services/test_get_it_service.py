"""GetItService: relation extraction, classification, ordering, fallbacks, and caching."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.v1.schemas.settings import GetItSettings
from repositories.itunes_repository import ITunesAlbumResult
from services.get_it_service import GetItService


class FakeCache:
    def __init__(self) -> None:
        self.store: dict = {}
        self.sets = 0

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ttl_seconds=None):
        self.sets += 1
        self.store[key] = value


def _rel(rel_type: str, url: str, ended: bool = False) -> dict:
    return {"type": rel_type, "ended": ended, "url": {"resource": url}}


def _rg(relations=None, releases=None) -> dict:
    return {
        "title": "Test Album",
        "artist-credit": [{"name": "Test Artist", "joinphrase": ""}],
        "relations": relations or [],
        "releases": releases or [],
    }


def _service(rg=None, release=None, itunes=None, settings=None):
    mb = AsyncMock()
    mb.get_release_group_by_id = AsyncMock(return_value=rg)
    mb.get_release_by_id = AsyncMock(return_value=release)
    itunes_repo = AsyncMock()
    itunes_repo.find_album = AsyncMock(return_value=itunes)
    prefs = SimpleNamespace(get_get_it_settings=lambda: settings or GetItSettings())
    cache = FakeCache()
    return (
        GetItService(
            mb_repo=mb, itunes_repo=itunes_repo, preferences_service=prefs, cache=cache
        ),
        mb,
        itunes_repo,
        cache,
    )


@pytest.mark.asyncio
async def test_release_level_rels_are_consulted_and_classified():
    """Live-probed reality: purchase rels sit on releases, not release groups."""
    rg = _rg(releases=[{"id": "rel-1", "status": "Official"}])
    release = {
        "relations": [
            _rel("purchase for download", "https://artist.bandcamp.com/album/x"),
            _rel("purchase for mail-order", "https://cstrecords.com/products/x"),
            _rel("amazon asin", "https://www.amazon.co.uk/gp/product/B08XZBV5FL"),
            _rel("download for free", "https://archive.org/details/x"),
            _rel("discogs", "https://www.discogs.com/release/1"),
        ]
    }
    service, mb, itunes_repo, _ = _service(rg=rg, release=release)

    options = await service.get_purchase_options("rg-1")

    assert [l.store for l in options.digital] == ["bandcamp"]
    assert {l.store for l in options.physical} == {"amazon", "other"}
    assert [l.kind for l in options.free] == ["free"]
    itunes_repo.find_album.assert_not_awaited()  # digital exists, no fallback
    assert options.bandcamp_search_url.startswith("https://bandcamp.com/search?q=")


@pytest.mark.asyncio
async def test_ended_rels_are_ignored():
    rg = _rg(
        relations=[
            _rel("purchase for download", "https://x.bandcamp.com/a", ended=True)
        ]
    )
    service, _, itunes_repo, _ = _service(rg=rg)
    itunes_repo.find_album = AsyncMock(return_value=None)

    options = await service.get_purchase_options("rg-1")
    assert options.digital == []


@pytest.mark.asyncio
async def test_itunes_fallback_when_no_digital_link():
    rg = _rg()
    service, _, itunes_repo, _ = _service(
        rg=rg,
        itunes=ITunesAlbumResult(
            url="https://music.apple.com/gb/album/x",
            collection_name="Test Album",
            artist_name="Test Artist",
        ),
        settings=GetItSettings(store_region="GB"),
    )

    options = await service.get_purchase_options("rg-1")

    assert [l.store for l in options.digital] == ["itunes"]
    itunes_repo.find_album.assert_awaited_once()
    assert itunes_repo.find_album.await_args.kwargs["country"] == "GB"


@pytest.mark.asyncio
async def test_artist_friendly_store_ordering():
    rg = _rg(
        relations=[
            _rel("purchase for download", "https://www.amazon.co.uk/dp/X"),
            _rel("purchase for download", "https://www.qobuz.com/album/x"),
            _rel("purchase for download", "https://artist.bandcamp.com/album/x"),
            _rel("purchase for download", "https://www.beatport.com/release/x/1"),
        ]
    )
    service, _, _, _ = _service(rg=rg)

    options = await service.get_purchase_options("rg-1")
    assert [l.store for l in options.digital] == [
        "bandcamp",
        "qobuz",
        "beatport",
        "amazon",
    ]


@pytest.mark.asyncio
async def test_purchase_urls_are_returned_unchanged():
    source_url = "https://www.amazon.com/dp/X?ref_=musicbrainz"
    rg = _rg(relations=[_rel("purchase for download", source_url)])
    service, _, _, _ = _service(rg=rg)

    options = await service.get_purchase_options("rg-1")

    assert options.digital[0].url == source_url


@pytest.mark.asyncio
async def test_second_call_hits_the_cache():
    rg = _rg(
        relations=[_rel("purchase for download", "https://x.bandcamp.com/album/y")]
    )
    service, mb, _, cache = _service(rg=rg)

    first = await service.get_purchase_options("rg-1")
    second = await service.get_purchase_options("rg-1")

    assert cache.sets == 1
    assert mb.get_release_group_by_id.await_count == 1
    assert second.digital[0].url == first.digital[0].url


@pytest.mark.asyncio
async def test_unknown_release_group_yields_empty_options():
    service, _, _, _ = _service(rg=None)
    options = await service.get_purchase_options("rg-x")
    assert options.digital == [] and options.physical == [] and options.free == []


@pytest.mark.asyncio
async def test_plugin_purchase_links_merge_under_fairness_ordering():
    """Plugin purchase links join the groups without changing store ordering."""
    from unittest.mock import MagicMock

    from infrastructure.plugins.protocols import PluginPurchaseLink

    rg = _rg(
        relations=[_rel("purchase for download", "https://artist.bandcamp.com/album/x")]
    )
    plugin_host = MagicMock()
    plugin_host.purchase_providers = MagicMock(
        return_value=[SimpleNamespace(manifest=SimpleNamespace(name="shop-plugin"))]
    )
    plugin_host.gather_purchase_links = AsyncMock(
        return_value=[
            PluginPurchaseLink(
                label="Some Shop", url="https://someshop.example/a", kind="digital"
            ),
            PluginPurchaseLink(label="Bad", url="javascript:alert(1)", kind="digital"),
        ]
    )
    mb = AsyncMock()
    mb.get_release_group_by_id = AsyncMock(return_value=rg)
    mb.get_release_by_id = AsyncMock(return_value=None)
    service = GetItService(
        mb_repo=mb,
        itunes_repo=AsyncMock(find_album=AsyncMock(return_value=None)),
        preferences_service=SimpleNamespace(
            get_get_it_settings=lambda: GetItSettings()
        ),
        cache=FakeCache(),
        plugin_host=plugin_host,
    )

    options = await service.get_purchase_options("rg-1")

    assert [l.store for l in options.digital] == ["bandcamp", "other"]
    assert options.digital[1].label == "Some Shop"
    assert all(l.url.startswith("http") for l in options.digital)


# -- artist-level storefronts --


def _artist_service(relations=None, settings=None, raises=False):
    mb = AsyncMock()
    if raises:
        mb.get_artist_relations = AsyncMock(side_effect=RuntimeError("MB down"))
    else:
        mb.get_artist_relations = AsyncMock(
            return_value={"name": "Test Artist", "relations": relations or []}
        )
    cache = FakeCache()
    service = GetItService(
        mb_repo=mb,
        itunes_repo=AsyncMock(),
        preferences_service=SimpleNamespace(
            get_get_it_settings=lambda: settings or GetItSettings()
        ),
        cache=cache,
    )
    return service, mb, cache


@pytest.mark.asyncio
async def test_artist_options_surface_storefronts_in_fairness_order():
    service, _, _ = _artist_service(
        relations=[
            _rel("purchase for mail-order", "https://shop.example.com/artist"),
            _rel("bandcamp", "https://testartist.bandcamp.com"),
            _rel("discogs", "https://www.discogs.com/artist/1"),
        ]
    )

    options = await service.get_artist_purchase_options("artist-1", "Test Artist")

    assert [l.store for l in options.links] == ["bandcamp", "other"]
    assert options.bandcamp_search_url.endswith("item_type=b")


@pytest.mark.asyncio
async def test_artist_options_never_call_itunes():
    service, _, _ = _artist_service()
    await service.get_artist_purchase_options("artist-1", "Test Artist")
    service._itunes.find_album.assert_not_awaited()


@pytest.mark.asyncio
async def test_artist_options_degrade_to_search_when_mb_fails():
    service, _, _ = _artist_service(raises=True)

    options = await service.get_artist_purchase_options("artist-1", "Test Artist")

    assert options.links == []
    assert "Test+Artist" in options.bandcamp_search_url


@pytest.mark.asyncio
async def test_artist_options_are_cached():
    service, mb, cache = _artist_service(
        relations=[_rel("bandcamp", "https://x.bandcamp.com")]
    )

    await service.get_artist_purchase_options("artist-1", "Test Artist")
    await service.get_artist_purchase_options("artist-1", "Test Artist")

    assert cache.sets == 1
    assert mb.get_artist_relations.await_count == 1
