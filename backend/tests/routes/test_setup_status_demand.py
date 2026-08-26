"""GH-293 setup-status route: coalesced demand signal release and telemetry.

The route stays public and read-only; the signal must be released on success,
exception, and cancellation paths so background identity work can never be
wedged by a stale demand hold.
"""

from __future__ import annotations

from fastapi import FastAPI

from api.v1.routes.auth import router
from core.dependencies import service_providers
from core.dependencies.auth_providers import get_auth_service
from services.auth_service import AuthService
from services.native.bootstrap_demand_signal import BootstrapDemandSignal
from tests.helpers import build_test_client


class _SetupRequiredService:
    def __init__(self, required: bool) -> None:
        self._required = required
        self.calls = 0

    async def is_setup_required(self) -> bool:
        self.calls += 1
        return self._required


class _FailingService:
    async def is_setup_required(self) -> bool:
        raise RuntimeError("injected setup-status failure")


def _app(service: AuthService) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_auth_service] = lambda: service
    return app


def test_setup_status_records_demand_and_releases_on_success(monkeypatch) -> None:
    signal = BootstrapDemandSignal()
    monkeypatch.setattr(
        service_providers, "get_bootstrap_demand_signal", lambda: signal
    )
    service = _SetupRequiredService(required=True)
    client = build_test_client(_app(service))  # type: ignore[arg-type]

    first = client.get("/auth/setup/status")
    second = client.get("/auth/setup/status")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {"required": True}
    assert second.json() == {"required": True}
    assert service.calls == 2
    assert signal.active is False
    snapshot = signal.latency_snapshot()
    assert snapshot["count"] == 2
    assert snapshot["errors"] == 0
    assert snapshot["samples"] == 2


def test_setup_status_releases_signal_on_exception(monkeypatch) -> None:
    signal = BootstrapDemandSignal()
    monkeypatch.setattr(
        service_providers, "get_bootstrap_demand_signal", lambda: signal
    )
    client = build_test_client(_app(_FailingService()))  # type: ignore[arg-type]

    assert client.get("/auth/setup/status").status_code == 500
    assert signal.active is False
    snapshot = signal.latency_snapshot()
    assert snapshot["count"] >= 1
    assert snapshot["errors"] >= 1


def test_setup_status_releases_signal_and_records_error_on_cancellation(
    monkeypatch,
) -> None:
    """A cancelled setup-status request must release the coalesced demand
    signal and record the cancellation in the bounded error telemetry."""
    import asyncio

    import httpx
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from starlette.types import ASGIApp

    signal = BootstrapDemandSignal()
    monkeypatch.setattr(
        service_providers, "get_bootstrap_demand_signal", lambda: signal
    )

    class _SlowService:
        async def is_setup_required(self) -> bool:
            await asyncio.sleep(0.5)
            return True

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_auth_service] = lambda: _SlowService()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            request_task = asyncio.create_task(client.get("/auth/setup/status"))
            await asyncio.sleep(0.05)  # request is inside the handler
            request_task.cancel()
            try:
                await request_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    asyncio.run(exercise())
    assert signal.active is False  # released on cancellation
    snapshot = signal.latency_snapshot()
    assert snapshot["count"] >= 1
    assert snapshot["errors"] >= 1


def test_setup_status_public_and_unauthenticated(monkeypatch) -> None:
    signal = BootstrapDemandSignal()
    monkeypatch.setattr(
        service_providers, "get_bootstrap_demand_signal", lambda: signal
    )
    required = _SetupRequiredService(required=False)
    client = build_test_client(_app(required))  # type: ignore[arg-type]

    response = client.get("/auth/setup/status")
    assert response.status_code == 200
    assert response.json() == {"required": False}
    assert signal.active is False
