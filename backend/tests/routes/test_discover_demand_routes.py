from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from api.v1.routes.discover import router
from api.v1.schemas.discover import DiscoverQueuePreview
from core.dependencies import get_discover_service, get_youtube_repo
from core.dependencies.service_providers import get_discovery_demand_service
from core.exceptions import ExternalServiceError
from repositories.musicbrainz_base import capture_mb_source_context
from services.discover_service import DiscoverService
from tests.helpers import build_test_client, override_user_auth

RG = "074aa5b0-712e-4d6c-8d14-8aedc43e84fd"


@pytest.fixture
def route_app():
    store = AsyncMock()
    activity = []

    async def record(user, feature, source, last_active, artist, section, provider):
        activity.append((user, feature, artist, section, provider))

    store.record_activity.side_effect = record
    service = DiscoverService(
        listenbrainz_repo=AsyncMock(), jellyfin_repo=AsyncMock(), library_repo=AsyncMock(),
        musicbrainz_repo=AsyncMock(), preferences_service=MagicMock(), discovery_snapshot_store=store,
    )
    service.preview_queue_item = AsyncMock(return_value=DiscoverQueuePreview(status="unavailable", youtube_search_url="https://www.youtube.com/results?search_query=Artist+Album"))
    demand = MagicMock()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_discover_service] = lambda: service
    app.dependency_overrides[get_youtube_repo] = lambda: MagicMock()
    app.dependency_overrides[get_discovery_demand_service] = lambda: demand
    return app, service, activity, demand


@pytest.mark.parametrize("path,body", [("/activity", {"feature": "queue"}), (f"/queue/preview/{RG}", None)])
def test_demand_routes_reject_unauthenticated_before_work(route_app, path, body):
    app, service, activity, demand = route_app
    response = build_test_client(app).post(f"/api/v1/discover{path}", json=body)
    assert response.status_code == 401
    assert activity == []
    service.preview_queue_item.assert_not_awaited()
    demand.trigger_user.assert_not_called()


@pytest.mark.parametrize("role", ["user", "admin"])
def test_activity_is_scoped_to_authenticated_user_and_returns_opaque_source(route_app, role):
    app, _, activity, _ = route_app
    override_user_auth(app, role=role, user_id="owner")
    source = capture_mb_source_context()
    response = build_test_client(app).post("/api/v1/discover/activity", json={"feature": "queue"})
    assert response.status_code == 200
    assert activity == [("owner", "queue", "", "", "")]
    assert response.json() == {"source_mode": source.source_mode, "source_id": source.source_id, "generation": source.generation}
    assert "source_url" not in response.json()


def test_caller_cannot_record_activity_for_another_user(route_app):
    app, _, activity, _ = route_app
    override_user_auth(app, user_id="owner")
    response = build_test_client(app).post("/api/v1/discover/activity", json={"feature": "home", "user_id": "victim"})
    assert response.status_code == 200
    assert activity == [("owner", "home", "", "", "")]


@pytest.mark.parametrize("body", [
    {"feature": "invalid"},
    {"feature": "artist"},
    {"feature": "artist", "artist_mbid": "invalid", "section": "similar", "provider": "lastfm"},
    {"feature": "home", "artist_mbid": RG, "section": "similar", "provider": "lastfm"},
])
def test_invalid_activity_does_not_schedule_or_persist(route_app, body):
    app, _, activity, demand = route_app
    override_user_auth(app)
    response = build_test_client(app).post("/api/v1/discover/activity", json=body)
    assert response.status_code in (400, 422)
    assert "error" in response.json()
    assert activity == []
    demand.trigger_user.assert_not_called()


def test_artist_activity_records_only_requested_section_and_provider(route_app):
    app, _, activity, _ = route_app
    override_user_auth(app, user_id="listener")
    response = build_test_client(app).post("/api/v1/discover/activity", json={"feature": "artist", "artist_mbid": RG, "section": "top_songs", "provider": "lastfm"})
    assert response.status_code == 200
    assert activity == [("listener", "artist", RG, "top_songs", "lastfm")]


@pytest.mark.parametrize("role", ["user", "admin"])
def test_public_preview_requires_no_queue_membership(route_app, role):
    app, _, activity, _ = route_app
    override_user_auth(app, role=role)
    response = build_test_client(app).post(f"/api/v1/discover/queue/preview/{RG}")
    assert response.status_code == 200
    assert response.json() == {"status": "unavailable", "youtube_url": None, "youtube_search_url": "https://www.youtube.com/results?search_query=Artist+Album"}
    assert activity == []


def test_preview_provider_failure_uses_error_envelope_not_success_absence(route_app):
    app, service, _, _ = route_app
    override_user_auth(app)
    service.preview_queue_item.side_effect = ExternalServiceError("provider failed")
    response = build_test_client(app).post(f"/api/v1/discover/queue/preview/{RG}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "EXTERNAL_SERVICE_UNAVAILABLE"
    assert "status" not in response.json()
