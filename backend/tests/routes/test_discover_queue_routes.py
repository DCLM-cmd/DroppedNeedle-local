from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from api.v1.routes.discover import router
from api.v1.schemas.discover import DiscoverQueueItemLight, DiscoverQueueResponse
from core.dependencies import get_discover_queue_manager
from tests.helpers import build_test_client, override_user_auth


def queue(items, queue_id):
    return DiscoverQueueResponse(items=[
        DiscoverQueueItemLight(
            release_group_mbid=f"rg-{index}", album_name=f"Album {index}",
            artist_name="Artist", artist_mbid="artist", recommendation_reason="Similar artists", in_library=False,
        ) for index in items
    ], queue_id=queue_id)


@pytest.fixture
def route_state():
    cached = {}
    manager = AsyncMock()
    builds = []

    async def consume(user):
        return cached.pop(user, None)

    async def build(user, count):
        builds.append(user)
        return queue(range(30)[:count if count is not None else 10], f"new-{user}")

    manager.consume_queue.side_effect = consume
    manager.build_lightweight_queue.side_effect = build
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_discover_queue_manager] = lambda: manager
    override_user_auth(app, user_id="listener")
    return app, cached, builds


def test_cold_queue_is_lightweight_and_bounds_oversized_requests(route_state):
    app, _, _ = route_state
    response = build_test_client(app).get("/discover/queue?count=50")
    assert response.status_code == 200
    assert [item["album_name"] for item in response.json()["items"]] == [f"Album {index}" for index in range(20)]


def test_existing_queue_is_returned_before_replacement_and_consumed_once(route_state):
    app, cached, builds = route_state
    cached["listener"] = queue([90], "saved")
    client = build_test_client(app)
    first = client.get("/discover/queue")
    assert first.json()["queue_id"] == "saved"
    assert first.json()["items"][0]["album_name"] == "Album 90"
    assert builds == []
    replacement = client.get("/discover/queue")
    assert replacement.json()["queue_id"] == "new-listener"
    assert replacement.json()["items"][0]["album_name"] == "Album 0"


def test_queue_get_cannot_consume_another_users_snapshot(route_state):
    app, cached, _ = route_state
    cached["victim"] = queue([99], "private-victim")
    response = build_test_client(app).get("/discover/queue?user_id=victim")
    assert response.status_code == 200
    assert response.json()["queue_id"] == "new-listener"
    assert cached["victim"].queue_id == "private-victim"
