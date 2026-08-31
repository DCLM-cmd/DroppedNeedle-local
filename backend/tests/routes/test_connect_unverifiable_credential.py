"""A credential the provider never saw must still be storable.

Connecting ListenBrainz required a successful live validation before it would save
anything. When MetaBrainz edge-blocked this installation's address, validate-token
answered 429 for days - so the account could not be connected at all, however
correct the token was, and the dialog repeated the same failure every time.

A REJECTED credential is different and still refused: that is the provider giving a
verdict, not withholding one.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api.v1.routes.me_connections import connect_listenbrainz
from services.settings_service import ListenBrainzVerifyResult

pytestmark = pytest.mark.asyncio


def _body():
    return SimpleNamespace(username="taktiler_dachs", user_token="a-token")


def _service(result: ListenBrainzVerifyResult):
    return SimpleNamespace(
        verify_listenbrainz=AsyncMock(return_value=result),
        on_listenbrainz_connection_changed=AsyncMock(),
    )


async def test_an_unreachable_provider_stores_the_credential_unverified():
    store = AsyncMock()
    service = _service(
        ListenBrainzVerifyResult(
            valid=False, message="ListenBrainz is rate limiting us.", reachable=False
        )
    )

    status = await connect_listenbrainz(
        SimpleNamespace(id="u1"), _body(), service, store
    )

    store.upsert.assert_awaited_once()
    assert status.enabled is True
    assert status.verified is False
    # the user is told WHY it is unconfirmed rather than being blocked
    assert "rate limiting" in (status.message or "")


async def test_a_rejected_credential_is_still_refused():
    store = AsyncMock()
    service = _service(
        ListenBrainzVerifyResult(
            valid=False, message="Token invalid or expired", reachable=True
        )
    )

    with pytest.raises(HTTPException) as raised:
        await connect_listenbrainz(SimpleNamespace(id="u1"), _body(), service, store)

    assert raised.value.status_code == 400
    store.upsert.assert_not_awaited()


async def test_a_verified_credential_reports_verified():
    store = AsyncMock()
    service = _service(
        ListenBrainzVerifyResult(valid=True, message="Connected", reachable=True)
    )

    status = await connect_listenbrainz(
        SimpleNamespace(id="u1"), _body(), service, store
    )

    assert status.verified is True and status.message is None
    store.upsert.assert_awaited_once()


async def test_a_missing_username_is_still_rejected():
    store = AsyncMock()
    service = _service(
        ListenBrainzVerifyResult(valid=False, message="x", reachable=False)
    )

    with pytest.raises(HTTPException):
        await connect_listenbrainz(
            SimpleNamespace(id="u1"),
            SimpleNamespace(username="   ", user_token="t"),
            service,
            store,
        )
