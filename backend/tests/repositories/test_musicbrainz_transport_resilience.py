from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import infrastructure.resilience.retry as retry_module
import repositories.musicbrainz_base as mb_base
from core.exceptions import ExternalServiceError
from infrastructure.queue.priority_queue import RequestPriority


@pytest.fixture(autouse=True)
def reset_musicbrainz_transport(monkeypatch):
    limiter = SimpleNamespace(acquire=AsyncMock())
    monkeypatch.setattr(mb_base, "mb_rate_limiter", limiter)
    monkeypatch.setattr(retry_module, "asyncio", SimpleNamespace(sleep=AsyncMock()))
    mb_base.mb_circuit_breaker.reset()
    yield limiter
    mb_base.mb_circuit_breaker.reset()


class _RaisingClient:
    def __init__(self, error: httpx.HTTPError) -> None:
        self.error = error
        self.calls = 0

    async def get(self, _url: str, params=None):
        self.calls += 1
        raise self.error


class _StatusClient:
    def __init__(self, status: int) -> None:
        self.status = status
        self.calls = 0

    async def get(self, _url: str, params=None):
        self.calls += 1
        return httpx.Response(self.status, content=b"{}")


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.RemoteProtocolError])
async def test_connect_and_protocol_errors_fail_after_one_attempt(
    reset_musicbrainz_transport, monkeypatch, error_type
) -> None:
    request = httpx.Request("GET", "https://musicbrainz.org/ws/2/artist")
    client = _RaisingClient(error_type("blocked", request=request))
    monkeypatch.setattr(mb_base, "_http_client", client)

    with pytest.raises(error_type):
        await mb_base.mb_api_get("/artist")

    assert client.calls == 1
    assert mb_base.mb_circuit_breaker.failure_count == 1
    reset_musicbrainz_transport.acquire.assert_awaited_once_with(
        priority=int(RequestPriority.USER_INITIATED)
    )


@pytest.mark.asyncio
async def test_503_remains_retryable_within_budget(
    reset_musicbrainz_transport, monkeypatch
) -> None:
    client = _StatusClient(503)
    monkeypatch.setattr(mb_base, "_http_client", client)

    with pytest.raises(ExternalServiceError, match="rate limited"):
        await mb_base.mb_api_get("/artist")

    assert client.calls > 1
    assert client.calls <= 3
    assert mb_base.mb_circuit_breaker.failure_count == 1
