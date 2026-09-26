"""ProwlarrClient: header auth, tolerant decode, error mapping, no secret leaks."""

import httpx
import pytest

from core.exceptions import ProwlarrApiError, ProwlarrAuthError, RateLimitedError
from repositories.prowlarr.prowlarr_client import ProwlarrClient
from tests.mocks.prowlarr_mock import client_for


def _client(host: str, key: str = "secret") -> ProwlarrClient:
    return ProwlarrClient(client_for(), f"http://{host}", key)


@pytest.mark.asyncio
async def test_sends_api_key_header():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("X-Api-Key")
        return httpx.Response(200, content=b"[]")

    client = ProwlarrClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        "http://prowlarr",
        "secret",
    )
    await client.list_indexers()
    assert seen["key"] == "secret"


@pytest.mark.asyncio
async def test_search_maps_usenet_and_skips_torrent_and_url_less():
    releases = await _client("prowlarr").search("radiohead in rainbows", [3000])
    assert len(releases) == 1  # torrent hit + url-less hit skipped
    hit = releases[0]
    assert hit.title == "Radiohead - In Rainbows (2007) [FLAC]"
    assert hit.indexer_id == "prowlarr:7"
    assert hit.indexer_name == "NZBGeek"
    assert hit.nzb_url == "https://prowlarr.test/9/download?apikey=MOCKKEY&link=ezQxYw"
    assert hit.size_bytes == 2315726631
    assert hit.category_ids == [3040]
    assert hit.grabs == 205
    assert hit.files == 113
    assert hit.usenet_date is not None and hit.usenet_date > 0
    assert hit.password == 0


@pytest.mark.asyncio
async def test_search_torrent_only_returns_empty():
    assert await _client("prowlarr-torrents").search("x", [3000]) == []


@pytest.mark.asyncio
async def test_list_indexers_and_system_status():
    client = _client("prowlarr")
    indexers = await client.list_indexers()
    assert [(i.id, i.name, i.protocol, i.enable) for i in indexers] == [
        (7, "NZBGeek", "usenet", True),
        (11, "DrunkenSlug", "usenet", True),
        (12, "DisabledTracker", "torrent", False),
    ]
    status = await client.system_status()
    assert status is not None and status.version == "1.32.2.4987"


@pytest.mark.asyncio
async def test_system_status_404_degrades_to_none():
    assert await _client("prowlarr-nostatus").system_status() is None


@pytest.mark.asyncio
async def test_401_maps_to_auth_error_without_retry_signal():
    with pytest.raises(ProwlarrAuthError) as exc_info:
        await _client("prowlarr-auth").search("x", [3000])
    assert exc_info.value.auth is True
    assert exc_info.value.code == 401


@pytest.mark.asyncio
async def test_429_maps_to_rate_limited_with_retry_after():
    with pytest.raises(RateLimitedError) as exc_info:
        await _client("prowlarr-ratelimit").search("x", [3000])
    assert exc_info.value.retry_after_seconds == 5.0


@pytest.mark.asyncio
async def test_500_maps_to_api_error_with_code():
    with pytest.raises(ProwlarrApiError) as exc_info:
        await _client("prowlarr-error").search("x", [3000])
    assert exc_info.value.code == 500


@pytest.mark.asyncio
async def test_html_body_maps_to_decode_error():
    with pytest.raises(ProwlarrApiError, match="decode failed"):
        await _client("prowlarr-html").search("x", [3000])


@pytest.mark.asyncio
async def test_malformed_url_maps_to_api_error_not_raw_httpx():
    client = ProwlarrClient(client_for(), "http://:bad-port", "secret")
    with pytest.raises(ProwlarrApiError):
        await client.list_indexers()


@pytest.mark.asyncio
async def test_key_absent_from_logs(caplog):
    with caplog.at_level("WARNING"):
        try:
            await _client("prowlarr-error", key="supersecretkey").search("x", [3000])
        except ProwlarrApiError:
            pass
    assert "supersecretkey" not in caplog.text
