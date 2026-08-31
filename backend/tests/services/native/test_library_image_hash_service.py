"""Artwork blurhashes are computed while organizing, not while serving.

The regression this guards is a performance one, which is why it is worth a test at
all: computing a blurhash inline made a 100-album listing take 5.1 seconds against
Jellyfin's 0.2 for the identical request. Jellyfin computes each hash once during a
scan and stores it on the image record; these tests pin our equivalent - the
organization run fills the store, and the store is what serving reads.
"""

import io
import sqlite3
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.library_image_hash_service import LibraryImageHashService


def _png(colour, size=32) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), colour).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path: Path) -> NativeLibraryStore:
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users(id) VALUES ('admin')")
    return NativeLibraryStore(path, threading.Lock())


def _service(store, covers: dict[str, tuple[bytes, str]]) -> LibraryImageHashService:
    """A service whose albums have the given (bytes, content-hash) artwork."""
    local = AsyncMock()

    async def read(context):
        entry = covers.get(context["album_id"])
        return None if entry is None else (entry[0], "image/png", "embedded", entry[1])

    local.read = AsyncMock(side_effect=read)
    service = LibraryImageHashService(store, local)
    service._store.get_target_artwork_context = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda kind, identifier: {"album_id": identifier}
    )
    return service


# ---- storing ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_albums_cover_is_hashed_and_stored(store) -> None:
    service = _service(store, {"album-1": (_png((200, 30, 40)), "sha-1")})

    written = await service.ensure_for_albums(["album-1"])

    assert written == 1
    assert (await store.get_image_blurhashes(["sha-1"]))["sha-1"]


@pytest.mark.asyncio
async def test_the_stored_hash_carries_the_source_dimensions(store) -> None:
    """Jellyfin keeps width and height on the same record; they pick the components."""
    service = _service(store, {"album-1": (_png((10, 20, 30), size=48), "sha-1")})

    await service.ensure_for_albums(["album-1"])

    with sqlite3.connect(store.db_path) as connection:
        row = connection.execute(
            "SELECT width, height FROM library_image_blurhashes WHERE content_sha1='sha-1'"
        ).fetchone()
    assert row == (48, 48)


@pytest.mark.asyncio
async def test_already_hashed_artwork_is_not_encoded_again(store) -> None:
    """The whole point: a second organization run must not redo the CPU work."""
    service = _service(store, {"album-1": (_png((5, 90, 120)), "sha-1")})
    await service.ensure_for_albums(["album-1"])

    written = await service.ensure_for_albums(["album-1"])

    assert written == 0


@pytest.mark.asyncio
async def test_two_albums_sharing_one_cover_are_hashed_once(store) -> None:
    """The key is the artwork, not the album - a reissue shares its predecessor's art."""
    shared = _png((44, 44, 44))
    service = _service(store, {"album-1": (shared, "sha-1"), "album-2": (shared, "sha-1")})

    written = await service.ensure_for_albums(["album-1", "album-2"])

    assert written == 1


@pytest.mark.asyncio
async def test_an_album_without_artwork_is_skipped_quietly(store) -> None:
    service = _service(store, {})

    assert await service.ensure_for_albums(["album-1"]) == 0
    assert await store.count_image_blurhashes() == 0


@pytest.mark.asyncio
async def test_undecodable_artwork_yields_no_row_rather_than_an_error(store) -> None:
    """Artwork is decorative: a cover we cannot read must never fail an organization
    run that otherwise moved every file correctly."""
    service = _service(store, {"album-1": (b"not an image", "sha-1")})

    assert await service.ensure_for_albums(["album-1"]) == 0
    assert await store.count_image_blurhashes() == 0


@pytest.mark.asyncio
async def test_a_failing_artwork_read_does_not_propagate(store) -> None:
    local = AsyncMock()
    local.read = AsyncMock(side_effect=OSError("the cover file vanished"))
    service = LibraryImageHashService(store, local)
    service._store.get_target_artwork_context = AsyncMock(  # type: ignore[method-assign]
        return_value={"album_id": "album-1"}
    )

    assert await service.ensure_for_albums(["album-1"]) == 0


@pytest.mark.asyncio
async def test_no_albums_is_not_a_query(store) -> None:
    service = _service(store, {})

    assert await service.ensure_for_albums([]) == 0


# ---- the backfill an organization run triggers -------------------------------------

@pytest.mark.asyncio
async def test_the_backfill_walks_the_library_and_stops_at_the_end(store) -> None:
    covers = {f"album-{i}": (_png((i, i, i)), f"sha-{i}") for i in range(3)}
    service = _service(store, covers)
    pages = [[{"release_group_mbid": album_id} for album_id in covers], []]
    service._store.list_target_albums = AsyncMock(  # type: ignore[method-assign]
        side_effect=[(page, len(page)) for page in pages]
    )

    assert await service.backfill() == 3
    assert await store.count_image_blurhashes() == 3


@pytest.mark.asyncio
async def test_a_failing_listing_ends_the_backfill_without_raising(store) -> None:
    service = _service(store, {})
    service._store.list_target_albums = AsyncMock(  # type: ignore[method-assign]
        side_effect=sqlite3.OperationalError("database is locked")
    )

    assert await service.backfill() == 0


@pytest.mark.asyncio
async def test_a_listing_without_the_expected_id_column_is_reported(store, caplog):
    """The bug this catches actually happened: the code read a column the listing does
    not have, so the backfill reported success and wrote nothing, and every album went
    on serving no blurhash. Silence is the dangerous outcome here, not the error."""
    service = _service(store, {})
    service._store.list_target_albums = AsyncMock(  # type: ignore[method-assign]
        return_value=([{"album_title": "No id here"}], 1)
    )

    with caplog.at_level("WARNING"):
        assert await service.backfill() == 0

    assert "No album ids" in caplog.text


# ---- artist pictures ---------------------------------------------------------------

def _covers(images: dict[str, bytes], cached: set[str]) -> AsyncMock:
    """A cover service where ``cached`` artists already have their picture on disk."""
    covers = AsyncMock()

    async def etag(artist_id, size=None):
        return f"sha-{artist_id}" if artist_id in cached else None

    async def image(artist_id, size=None, **kwargs):
        data = images.get(artist_id)
        if data is None:
            return None
        cached.add(artist_id)  # fetching is what puts it in the cache
        return (data, "image/png", "provider")

    covers.get_artist_image_etag = AsyncMock(side_effect=etag)
    covers.get_artist_image = AsyncMock(side_effect=image)
    return covers


def _artist_service(store, covers) -> LibraryImageHashService:
    service = LibraryImageHashService(store, AsyncMock(), covers=covers)
    service._store.list_target_albums = AsyncMock(return_value=([], 0))  # type: ignore[method-assign]
    return service


@pytest.mark.asyncio
async def test_an_uncached_artist_picture_is_fetched_then_hashed(store) -> None:
    """The bug: an artist advertised a picture that did not exist, so the image route
    answered 404 and no hash was stored - which Finamp reports as a misconfigured
    server. Jellyfin downloads remote art while scanning; this is the equivalent."""
    covers = _covers({"artist-1": _png((90, 20, 20))}, cached=set())
    service = _artist_service(store, covers)
    service._store.list_target_artists = AsyncMock(  # type: ignore[method-assign]
        side_effect=[([{"artist_mbid": "artist-1"}], 1), ([], 0)]
    )

    assert await service.backfill() == 1
    assert (await store.get_image_blurhashes(["sha-artist-1"]))["sha-artist-1"]
    covers.get_artist_image.assert_awaited()


@pytest.mark.asyncio
async def test_an_artist_without_any_picture_is_skipped(store) -> None:
    covers = _covers({}, cached=set())
    service = _artist_service(store, covers)
    service._store.list_target_artists = AsyncMock(  # type: ignore[method-assign]
        side_effect=[([{"artist_mbid": "artist-1"}], 1), ([], 0)]
    )

    assert await service.backfill() == 0
    assert await store.count_image_blurhashes() == 0


@pytest.mark.asyncio
async def test_an_already_hashed_artist_is_not_fetched_again(store) -> None:
    covers = _covers({"artist-1": _png((1, 2, 3))}, cached={"artist-1"})
    service = _artist_service(store, covers)
    service._store.list_target_artists = AsyncMock(  # type: ignore[method-assign]
        side_effect=[([{"artist_mbid": "artist-1"}], 1), ([], 0),
                     ([{"artist_mbid": "artist-1"}], 1), ([], 0)]
    )
    await service.backfill()
    covers.get_artist_image.reset_mock()

    assert await service.backfill() == 0
    covers.get_artist_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_artists_are_skipped_when_no_cover_service_is_wired(store) -> None:
    service = LibraryImageHashService(store, AsyncMock())
    service._store.list_target_albums = AsyncMock(return_value=([], 0))  # type: ignore[method-assign]

    assert await service.backfill() == 0


# ---- album covers are fetched when the catalog only points at a provider ------------

@pytest.mark.asyncio
async def test_an_album_cover_that_was_never_fetched_is_materialised(store) -> None:
    """The same deadlock the artist path had: the catalog says the cover comes from a
    provider, nothing has fetched it, and nothing would - the download only happens
    when a client asks, and a client only asks once the listing advertises an image.
    Jellyfin pulls remote art down while scanning; the organization run does it here.
    """
    fetched: list[str] = []
    data = _png((60, 90, 120))
    local = AsyncMock()

    async def read(context):
        # Empty until the cover has actually been fetched into the cache.
        return (data, "image/png", "provider", "sha-1") if fetched else None

    local.read = AsyncMock(side_effect=read)
    covers = AsyncMock()

    async def fetch(album_id, size=None, **kwargs):
        fetched.append(album_id)
        return (data, "image/png", "provider")

    covers.get_release_group_cover = AsyncMock(side_effect=fetch)
    service = LibraryImageHashService(store, local, covers=covers)
    service._store.get_target_artwork_context = AsyncMock(  # type: ignore[method-assign]
        return_value={"source": "provider", "provider_id": "p-1"}
    )

    assert await service.ensure_for_albums(["album-1"]) == 1
    assert fetched == ["album-1"]
    assert (await store.get_image_blurhashes(["sha-1"]))["sha-1"]


@pytest.mark.asyncio
async def test_an_album_whose_cover_cannot_be_fetched_is_skipped(store) -> None:
    local = AsyncMock()
    local.read = AsyncMock(return_value=None)
    covers = AsyncMock()
    covers.get_release_group_cover = AsyncMock(return_value=None)
    service = LibraryImageHashService(store, local, covers=covers)
    service._store.get_target_artwork_context = AsyncMock(  # type: ignore[method-assign]
        return_value={"source": "provider", "provider_id": "p-1"}
    )

    assert await service.ensure_for_albums(["album-1"]) == 0


@pytest.mark.asyncio
async def test_a_cached_cover_is_not_fetched_again(store) -> None:
    covers = AsyncMock()
    service = _service(store, {"album-1": (_png((7, 7, 7)), "sha-1")})
    service._covers = covers

    assert await service.ensure_for_albums(["album-1"]) == 1
    covers.get_release_group_cover.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetching_artwork_is_paced(store, monkeypatch) -> None:
    """Materialising a cover reaches the Cover Art Archive, which answers on the same
    address as MusicBrainz and ListenBrainz. A burst here is felt by all three, and
    this installation has already been refused at that edge. Background work has no
    deadline, so it waits between fetches - and only between FETCHES, never between
    covers it already holds."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(
        "services.native.library_image_hash_service.asyncio.sleep", fake_sleep
    )
    data = _png((30, 60, 90))
    fetched: list[str] = []
    local = AsyncMock()
    local.read = AsyncMock(
        side_effect=lambda ctx: (data, "image/png", "provider", "sha-1")
        if fetched
        else None
    )
    covers = AsyncMock()

    async def fetch(album_id, size=None, **kwargs):
        fetched.append(album_id)
        return (data, "image/png", "provider")

    covers.get_release_group_cover = AsyncMock(side_effect=fetch)
    service = LibraryImageHashService(store, local, covers=covers)
    service._store.get_target_artwork_context = AsyncMock(  # type: ignore[method-assign]
        return_value={"source": "provider", "provider_id": "p-1"}
    )

    await service.ensure_for_albums(["album-1"])

    assert slept, "a fetch went out with no pause before it"


@pytest.mark.asyncio
async def test_a_cover_already_held_is_not_paced_for(store, monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(
        "services.native.library_image_hash_service.asyncio.sleep", fake_sleep
    )
    service = _service(store, {"album-1": (_png((9, 9, 9)), "sha-1")})
    service._covers = AsyncMock()

    await service.ensure_for_albums(["album-1"])

    assert slept == []
