"""OIDC handler-level tests: the SPA hand-off redirect must stay a RELATIVE,
same-site Location prefixed exactly once by the deployment base path, immune
to forwarded host/prefix headers, while state handling and no-store survive."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.routes.auth import router
from core.base_path import BasePathMiddleware
from core.dependencies.auth_providers import get_oidc_user_auth_service
from core.exceptions import AuthenticationError, ExternalServiceError
from services.oidc_user_auth_service import OIDCUserAuthService
from tests.helpers import add_production_exception_handlers, mock_user

VALID_STATE = "oidc-state-ok-0123456789"
EXCHANGE_CODE = "one-time-exchange-0123456789"
FAILING_PROVIDER_CODE = "provider-code-exchange-fails"


class StubOIDCService(OIDCUserAuthService):
    """The exact surface the production handlers consume; provider HTTP stubbed."""

    def __init__(self) -> None:
        super().__init__(auth_store=None, preferences_service=None, cache=None)
        self.seen_states: list[str] = []

    async def build_authorize_url(self) -> str:
        return "https://issuer.example.com/authorize?client_id=test-client"

    async def handle_callback(
        self, *, code: str, state: str, user_agent: str | None = None
    ) -> str:
        self.seen_states.append(state)
        if state != VALID_STATE:
            raise AuthenticationError("Invalid or expired OIDC state")
        if code == FAILING_PROVIDER_CODE:
            raise ExternalServiceError("OIDC provider returned an unexpected response")
        return EXCHANGE_CODE

    async def exchange_code(self, exchange_code: str):
        if exchange_code != EXCHANGE_CODE:
            raise AuthenticationError("Invalid or expired exchange code")
        return mock_user(), "raw-session-token"


@pytest.fixture()
def service() -> StubOIDCService:
    return StubOIDCService()


def _app(service: StubOIDCService) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_oidc_user_auth_service] = lambda: service
    add_production_exception_handlers(app)
    return app


def test_callback_location_is_relative_and_no_store_at_domain_root(service):
    resp = TestClient(_app(service), follow_redirects=False).get(
        f"/auth/oidc/callback?code=provider-code&state={VALID_STATE}"
    )
    assert resp.status_code == 307
    # Byte-stable hand-off relative to wherever the SPA is hosted.
    assert resp.headers["location"] == f"/auth/callback?code={EXCHANGE_CODE}"
    assert resp.headers["cache-control"] == "no-store"
    assert service.seen_states == [VALID_STATE]


def test_callback_location_prefixed_exactly_once_under_base(service):
    app = _app(service)
    resp = TestClient(
        BasePathMiddleware(app, "/music"), follow_redirects=False
    ).get(f"/music/auth/oidc/callback?code=provider-code&state={VALID_STATE}")
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location == f"/music/auth/callback?code={EXCHANGE_CODE}"
    assert not location.startswith("/music/music")
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("base", ["", "/music"])
def test_callback_location_survives_host_and_forward_header_spoofing(
    service, base
):
    app = _app(service)
    client = TestClient(
        BasePathMiddleware(app, base) if base else app,
        follow_redirects=False,
    )
    headers = {
        "Host": "evil.example.com",
        "X-Forwarded-Host": "evil.example.com",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Prefix": "/evil-surface",
    }
    resp = client.get(
        f"{base}/auth/oidc/callback?code=provider-code&state={VALID_STATE}",
        headers=headers,
    )
    expected_base = f"{base}/auth/callback?code={EXCHANGE_CODE}"
    assert resp.headers["location"] == expected_base
    assert "evil.example.com" not in resp.headers["location"]
    assert not resp.headers["location"].startswith("http")


@pytest.mark.parametrize("base", ["", "/music"])
def test_exchange_sets_session_cookie_at_scoped_api_path(service, base):
    app = _app(service)
    client = TestClient(BasePathMiddleware(app, base) if base else app, follow_redirects=False)

    resp = client.post(f"{base}/auth/oidc/exchange", json={"code": EXCHANGE_CODE})
    assert resp.status_code == 200
    assert resp.json()["token"] == "raw-session-token"

    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("droppedneedle_session=raw-session-token;")
    assert f"Path={base}/api;" in cookie
    if base:
        assert "Path=/api;" not in cookie


def test_callback_rejects_state_with_envelope_error(service):
    resp = TestClient(_app(service), follow_redirects=False).get(
        "/auth/oidc/callback?code=provider-code&state=forged-state"
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "OIDC authentication failed"
    assert service.seen_states == ["forged-state"]


def test_callback_maps_provider_outage_to_service_unavailable(service):
    resp = TestClient(_app(service), follow_redirects=False).get(
        f"/auth/oidc/callback?code={FAILING_PROVIDER_CODE}&state={VALID_STATE}"
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == "OIDC provider unavailable"
