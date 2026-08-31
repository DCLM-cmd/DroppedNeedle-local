"""The session socket exists, authenticates, and answers keep-alives.

Jellyfin clients open ``/socket`` right after signing in. There was no route, so
Starlette refused the upgrade with 403 and Finamp reconnected in a loop for as long
as it was running - visible in the server log as a steady stream of 403s.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.compat.common.deps import CompatServices, get_compat_services
from api.compat.jellyfin.router import router


def _client(*, token_user=SimpleNamespace(id="u1"), enabled=True):
    services = MagicMock(spec=CompatServices)
    services.preferences = MagicMock()
    services.preferences.get_connect_apps_settings = MagicMock(
        return_value=SimpleNamespace(jellyfin_enabled=enabled)
    )
    services.app_passwords = MagicMock()
    services.app_passwords.verify_token = AsyncMock(return_value=token_user)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_compat_services] = lambda: services
    return TestClient(app), services


def _decode(raw):
    return msgspec.json.decode(raw)


def test_a_client_with_a_token_is_upgraded_and_greeted() -> None:
    client, _ = _client()

    with client.websocket_connect("/jellyfin/socket?ApiKey=tok&deviceId=d1") as ws:
        greeting = _decode(ws.receive_text())

    assert greeting["MessageType"] == "ForceKeepAlive"
    assert greeting["Data"] > 0
    assert greeting["MessageId"]


def test_the_token_may_also_be_spelled_api_key() -> None:
    """Clients differ on the spelling; both are already accepted over HTTP."""
    client, services = _client()

    with client.websocket_connect("/jellyfin/socket?api_key=tok") as ws:
        ws.receive_text()

    services.app_passwords.verify_token.assert_awaited_with("tok")


def test_a_keep_alive_is_answered() -> None:
    """The client stops trusting the connection if this goes unanswered."""
    client, _ = _client()

    with client.websocket_connect("/jellyfin/socket?ApiKey=tok") as ws:
        ws.receive_text()  # ForceKeepAlive
        ws.send_text(msgspec.json.encode({"MessageType": "KeepAlive"}).decode())
        reply = _decode(ws.receive_text())

    assert reply["MessageType"] == "KeepAlive"


def test_a_subscription_is_accepted_without_closing() -> None:
    """SessionsStart & friends have no acknowledgement - the socket must stay up."""
    client, _ = _client()

    with client.websocket_connect("/jellyfin/socket?ApiKey=tok") as ws:
        ws.receive_text()
        ws.send_text(
            msgspec.json.encode(
                {"MessageType": "SessionsStart", "Data": "0,1500"}
            ).decode()
        )
        ws.send_text(msgspec.json.encode({"MessageType": "KeepAlive"}).decode())
        assert _decode(ws.receive_text())["MessageType"] == "KeepAlive"


def test_garbage_does_not_kill_the_connection() -> None:
    client, _ = _client()

    with client.websocket_connect("/jellyfin/socket?ApiKey=tok") as ws:
        ws.receive_text()
        ws.send_text("not json")
        ws.send_text(msgspec.json.encode({"MessageType": "KeepAlive"}).decode())
        assert _decode(ws.receive_text())["MessageType"] == "KeepAlive"


def test_an_unauthenticated_client_is_refused() -> None:
    """Verified before accept, so the upgrade itself never succeeds."""
    from starlette.websockets import WebSocketDisconnect

    client, _ = _client(token_user=None)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/jellyfin/socket?ApiKey=bad") as ws:
            ws.receive_text()


def test_a_client_with_no_token_at_all_is_refused() -> None:
    from starlette.websockets import WebSocketDisconnect

    client, services = _client()

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/jellyfin/socket") as ws:
            ws.receive_text()
    services.app_passwords.verify_token.assert_not_called()


def test_the_socket_is_closed_when_the_jellyfin_api_is_disabled() -> None:
    from starlette.websockets import WebSocketDisconnect

    client, _ = _client(enabled=False)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/jellyfin/socket?ApiKey=tok") as ws:
            ws.receive_text()


def test_both_capabilities_forms_are_accepted() -> None:
    """Jellyfin exposes the bare path (query parameters) and /Full (JSON body).

    Only /Full existed, so Finamp's bare POST got 405 - and it does not tolerate
    that: updateCapabilities() throws and takes the rest of startup with it, so the
    app never reached the point of requesting any items. Every request in the server
    log was a 200 while the user saw an empty library.
    """
    client, _ = _client()
    auth = {"Authorization": 'MediaBrowser Token="tok", Client="Finamp", DeviceId="d1"'}

    bare = client.post(
        "/jellyfin/Sessions/Capabilities",
        headers=auth,
        params={
            "playableMediaTypes": "Audio",
            "supportedCommands": "Play,PlayState,SetVolume",
            "supportsMediaControl": "true",
            "supportsPersistentIdentifier": "true",
        },
    )
    full = client.post(
        "/jellyfin/Sessions/Capabilities/Full",
        headers=auth,
        json={"PlayableMediaTypes": ["Audio"], "SupportedCommands": ["Play"]},
    )

    assert bare.status_code == 204, bare.text
    assert full.status_code == 204, full.text
