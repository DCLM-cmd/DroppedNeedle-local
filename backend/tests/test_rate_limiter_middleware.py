"""Tests for TokenBucketRateLimiter and RateLimitMiddleware."""

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
import httpx

from infrastructure.resilience.rate_limiter import TokenBucketRateLimiter
from middleware import RateLimitMiddleware


# TokenBucketRateLimiter unit tests


@pytest.mark.asyncio
async def test_remaining_full_at_start():
    limiter = TokenBucketRateLimiter(rate=10.0, capacity=20)
    assert limiter.remaining == 20


@pytest.mark.asyncio
async def test_remaining_decreases_after_acquire():
    limiter = TokenBucketRateLimiter(rate=10.0, capacity=20)
    await limiter.try_acquire()
    assert limiter.remaining == 19


@pytest.mark.asyncio
async def test_retry_after_zero_when_tokens_available():
    limiter = TokenBucketRateLimiter(rate=10.0, capacity=20)
    assert limiter.retry_after() == 0.0


@pytest.mark.asyncio
async def test_retry_after_positive_when_exhausted():
    limiter = TokenBucketRateLimiter(rate=1.0, capacity=1)
    await limiter.try_acquire()
    assert limiter.retry_after() > 0


# Middleware helpers


def _build_app(
    default_rate: float = 100.0,
    default_capacity: int = 200,
    overrides: dict | None = None,
) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/test")
    async def api_test():
        return PlainTextResponse("ok")

    @app.get("/health")
    async def health():
        return PlainTextResponse("healthy")

    @app.get("/api/v1/special")
    async def api_special():
        return PlainTextResponse("special")

    app.add_middleware(
        RateLimitMiddleware,
        default_rate=default_rate,
        default_capacity=default_capacity,
        overrides=overrides,
    )
    return app


# RateLimitMiddleware integration tests


@pytest.mark.asyncio
async def test_middleware_allows_request():
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/test")

    assert resp.status_code == 200
    assert "X-RateLimit-Limit" in resp.headers
    assert "X-RateLimit-Remaining" in resp.headers


@pytest.mark.asyncio
async def test_middleware_returns_429_when_exhausted():
    app = _build_app(default_rate=1.0, default_capacity=1)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/api/v1/test")  # consumes the single token
        resp = await client.get("/api/v1/test")

    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


@pytest.mark.asyncio
async def test_middleware_skips_non_api_paths():
    app = _build_app(default_rate=1.0, default_capacity=1)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Exhaust the limiter via an API path
        await client.get("/api/v1/test")
        await client.get("/api/v1/test")
        # /health should still work — not rate-limited
        resp = await client.get("/health")

    assert resp.status_code == 200
    assert "X-RateLimit-Limit" not in resp.headers


@pytest.mark.asyncio
async def test_one_clients_burst_does_not_starve_another():
    """The reported bug: a runaway tab 429'd every OTHER page in the app.

    With one shared bucket, whoever spent the tokens first took them from
    everyone. The budget is per client now, so exhausting one caller's
    allowance must leave a different caller untouched.
    """
    app = _build_app(default_rate=1.0, default_capacity=1)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        greedy = {"Authorization": "Bearer runaway-tab"}
        await client.get("/api/v1/test", headers=greedy)
        exhausted = await client.get("/api/v1/test", headers=greedy)

        bystander = await client.get(
            "/api/v1/test", headers={"Authorization": "Bearer someone-else"}
        )

    assert exhausted.status_code == 429
    assert bystander.status_code == 200


@pytest.mark.asyncio
async def test_same_client_credential_shares_one_budget():
    app = _build_app(default_rate=1.0, default_capacity=1)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer one-client"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/api/v1/test", headers=headers)
        second = await client.get("/api/v1/test", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 429


@pytest.mark.asyncio
async def test_setup_status_is_not_captured_by_the_setup_budget():
    """'/api/v1/auth/setup' is a PREFIX, so it also matches the bootstrap call
    every cold page load makes. Registered ahead of it, the status path keeps
    its own budget instead of inheriting the deliberately tiny setup one."""
    app = FastAPI()

    @app.get("/api/v1/auth/setup/status")
    async def setup_status():
        return PlainTextResponse("ok")

    app.add_middleware(
        RateLimitMiddleware,
        default_rate=100.0,
        default_capacity=200,
        overrides={
            "/api/v1/auth/setup/status": (20.0, 60),
            "/api/v1/auth/setup": (1.0, 3),
        },
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        statuses = [
            (await client.get("/api/v1/auth/setup/status")).status_code
            for _ in range(6)
        ]

    assert statuses == [200] * 6


@pytest.mark.asyncio
async def test_middleware_per_route_override():
    overrides = {"/api/v1/special": (100.0, 500)}
    app = _build_app(default_rate=1.0, default_capacity=1, overrides=overrides)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Default limiter has capacity=1, override has capacity=500
        await client.get("/api/v1/test")  # consumes default token
        resp_default = await client.get("/api/v1/test")  # should be 429

        resp_override = await client.get("/api/v1/special")  # uses override, plenty of tokens

    assert resp_default.status_code == 429
    assert resp_override.status_code == 200
    assert resp_override.headers["X-RateLimit-Limit"] == "500"
