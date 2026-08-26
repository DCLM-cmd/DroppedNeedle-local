"""Auth matrix + wiring for the D-EDITION-AUTO undo route (S-2)."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from api.v1.routes.library_operations_target import router
from api.v1.schemas.library_operations import AutomaticEditionUndoResponse
from core.dependencies import get_target_identity_repair_service
from middleware import _get_current_admin, _get_current_user
from tests.helpers import build_test_client, override_admin_auth


def _app() -> FastAPI:
    application = FastAPI()
    application.include_router(router)
    return application


def test_undo_route_forwards_admin_request() -> None:
    service = AsyncMock()
    service.undo_automatic_edition.return_value = AutomaticEditionUndoResponse(
        local_album_id="album-1",
        outcome="restored",
        review_id=None,
    )
    app = _app()
    app.dependency_overrides[get_target_identity_repair_service] = lambda: service

    override_admin_auth(app)
    client = build_test_client(app)
    response = client.post(
        "/library/albums/album-1/undo-automatic-edition",
        json={
            "expected_album_revision": 4,
            "expected_identity_revision": 2,
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "local_album_id": "album-1",
        "outcome": "restored",
        "review_id": None,
    }
    call = service.undo_automatic_edition.await_args
    assert call is not None
    assert call.args[0] == "album-1"
    assert call.args[1].expected_album_revision == 4
    assert call.args[1].expected_identity_revision == 2
    assert call.args[2] == "test-admin-id"


@pytest.mark.asyncio
async def test_undo_route_is_admin_only() -> None:
    app = _app()

    def reject_admin() -> None:
        raise HTTPException(status_code=403, detail="Admin access required")

    app.dependency_overrides[_get_current_admin] = reject_admin
    app.dependency_overrides[_get_current_user] = lambda: type(
        "U", (), {"id": "user-1", "role": "user"}
    )()
    client = build_test_client(app)
    response = client.post(
        "/library/albums/album-1/undo-automatic-edition",
        json={
            "expected_album_revision": 4,
            "expected_identity_revision": 2,
        },
    )
    assert response.status_code == 403

    unauthenticated = FastAPI()
    unauthenticated.include_router(router)
    plain = build_test_client(unauthenticated)
    assert (
        plain.post(
            "/library/albums/album-1/undo-automatic-edition",
            json={
                "expected_album_revision": 4,
                "expected_identity_revision": 2,
            },
        ).status_code
        == 401
    )
