import asyncio
import gzip
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from infrastructure.http.client import HttpClientFactory
from infrastructure.observability import provider_counters as counters
from repositories import musicbrainz_base as mb


class IsolatedClientFactory(HttpClientFactory):
    _clients = OrderedDict()
    _retired = []


@pytest.fixture
def isolated_accounting(monkeypatch):
    monkeypatch.setattr(counters, "_counters", counters.ProviderCounterMap())
    monkeypatch.setattr(mb, "_mb_source_mode", "official")
    monkeypatch.setattr(mb, "_mb_source_id", "accounting-fixture")
    monkeypatch.setattr(mb, "_mb_source_generation", 1)
    monkeypatch.setattr(mb, "_mb_api_base", "https://musicbrainz.org/ws/2")
    monkeypatch.setattr(mb, "mb_rate_limiter", SimpleNamespace(acquire=AsyncMock()))
    mb.mb_circuit_breaker.reset()
    yield
    mb.mb_circuit_breaker.reset()


@pytest.mark.asyncio
async def test_configured_http_counts_encoded_body_not_decoded_size(isolated_accounting, monkeypatch):
    payload = b'{"name":"' + b"a" * 4096 + b'"}'
    compressed = gzip.compress(payload)
    attempts = []

    def transport(request):
        attempts.append(request)
        encoded = request.url.params.get("encoding") == "gzip"
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"} if encoded else {},
            stream=httpx.ByteStream(compressed if encoded else payload),
        )

    client = IsolatedClientFactory.get_client(
        "accounting", http2=False, transport_factory=lambda: httpx.MockTransport(transport)
    )
    monkeypatch.setattr(mb, "get_mb_http_client", lambda: client)
    try:
        with counters.provider_workload(counters.ProviderWorkload.FOREGROUND):
            plain = await mb.mb_api_get("/artist/test", {"encoding": "identity"})
            encoded = await mb.mb_api_get("/artist/test", {"encoding": "gzip"})
        assert plain == encoded == {"name": "a" * 4096}
        rows = counters.snapshot_provider_rows()
        assert sum(row["count_total"] for row in rows) == len(attempts) == 2
        assert sum(row["downloaded_body_bytes_total"] for row in rows) == len(payload) + len(compressed)
        assert sum(row["decoded_body_bytes_total"] for row in rows) == 2 * len(payload)
        assert sum(row["unknown_body_attempts_total"] for row in rows) == 0
        assert {row["workload"] for row in rows} == {"foreground"}
    finally:
        await IsolatedClientFactory.close_all()


@pytest.mark.asyncio
async def test_mid_body_disconnect_preserves_error_and_unknown_byte_signal(isolated_accounting, monkeypatch):
    class DisconnectingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"partial":'
            raise httpx.RemoteProtocolError("fixture disconnect")

    client = IsolatedClientFactory.get_client(
        "disconnect", http2=False,
        transport_factory=lambda: httpx.MockTransport(
            lambda request: httpx.Response(200, stream=DisconnectingStream())
        ),
    )
    monkeypatch.setattr(mb, "get_mb_http_client", lambda: client)
    try:
        with pytest.raises(httpx.RemoteProtocolError, match="fixture disconnect"):
            await mb.mb_api_get("/artist/test")
        rows = counters.snapshot_provider_rows()
        assert sum(row["count_total"] for row in rows) == 1
        assert sum(row["unknown_body_attempts_total"] for row in rows) == 1
        assert {row["outcome"] for row in rows} == {"http_error"}
    finally:
        await IsolatedClientFactory.close_all()


@pytest.mark.asyncio
async def test_source_churn_overflows_without_losing_attempts_or_body_totals(isolated_accounting):
    response = httpx.Response(200, stream=httpx.ByteStream(b"{}"))
    await response.aread()
    count = counters.MAX_PROVIDER_SERIES + 75
    for generation in range(count):
        source = SimpleNamespace(source_mode="mirror", source_id=f"opaque-{generation}", generation=generation)
        counters.record_provider_call("musicbrainz", None, 200, source, response=response)
    rows = counters.snapshot_provider_rows()
    assert len(rows) == counters.MAX_PROVIDER_SERIES + 1
    assert sum(row["count_total"] for row in rows) == count
    assert sum(row["downloaded_body_bytes_total"] for row in rows) == 2 * count
    assert sum(row["decoded_body_bytes_total"] for row in rows) == 2 * count
    overflow = next(row for row in rows if row["overflow"])
    assert overflow["count_total"] == 75
    assert not any(field in overflow for field in ("source_id", "source_generation", "source_mode"))


@pytest.mark.asyncio
async def test_workload_scopes_are_task_local_and_labels_exclude_inputs(isolated_accounting):
    async def record(workload):
        with counters.provider_workload(workload):
            await asyncio.sleep(0)
            category, profile = counters.musicbrainz_request_labels(
                "/artist/secret-id", {"query": "private-query", "inc": "private-value"}
            )
            counters.record_provider_call("musicbrainz", None, 200, category=category, profile=profile)
    await asyncio.gather(record(counters.ProviderWorkload.HOME), record(counters.ProviderWorkload.FOLLOW))
    rows = counters.snapshot_provider_rows()
    assert {row["workload"] for row in rows} == {"home", "follow"}
    assert {row["request_category"] for row in rows} == {"search"}
    assert "secret-id" not in str(rows) and "private-query" not in str(rows) and "private-value" not in str(rows)
