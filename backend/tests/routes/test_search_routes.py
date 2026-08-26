import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI

from api.v1.schemas.search import (
    EnrichmentResponse,
    SearchResponse,
    SearchResult,
    SuggestResponse,
    SuggestResult,
)
from api.v1.routes.search import router
from core.dependencies import (
    get_search_service,
    get_coverart_repository,
    get_search_enrichment_service,
)
from tests.helpers import build_test_client


@pytest.fixture
def mock_search_service():
    mock_svc = MagicMock()
    mock_svc.suggest = AsyncMock(return_value=SuggestResponse(results=[]))
    return mock_svc


@pytest.fixture
def client(mock_search_service):
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[get_search_service] = lambda: mock_search_service
    return build_test_client(test_app)


def test_suggest_rejects_single_char_query(client, mock_search_service):
    response = client.get("/search/suggest?q=a")

    assert response.status_code == 422
    mock_search_service.suggest.assert_not_called()


def test_suggest_rejects_empty_query(client, mock_search_service):
    response = client.get("/search/suggest?q=")

    assert response.status_code == 422
    mock_search_service.suggest.assert_not_called()


def test_suggest_rejects_missing_query(client, mock_search_service):
    response = client.get("/search/suggest")

    assert response.status_code == 422
    mock_search_service.suggest.assert_not_called()


def test_suggest_accepts_two_char_query(client, mock_search_service):
    response = client.get("/search/suggest?q=ab")

    assert response.status_code == 200
    assert response.json() == {"results": [], "remote_status": "ok"}


def test_suggest_exposes_degraded_remote_status(client, mock_search_service):
    mock_search_service.suggest.return_value = SuggestResponse(remote_status="timeout")

    response = client.get("/search/suggest?q=unfamiliar")

    assert response.status_code == 200
    assert response.json() == {"results": [], "remote_status": "timeout"}


def test_bucket_exposes_terminal_status(client, mock_search_service):
    mock_search_service.search_bucket = AsyncMock(return_value=([], None, "timeout"))

    response = client.get("/search/artists?q=unfamiliar&limit=6")

    assert response.status_code == 200
    assert response.json() == {
        "bucket": "artists",
        "limit": 6,
        "offset": 0,
        "results": [],
        "top_result": None,
        "status": "timeout",
    }


def test_suggest_limit_lower_bound(client, mock_search_service):
    response = client.get("/search/suggest?q=test&limit=0")

    assert response.status_code == 422


def test_suggest_limit_upper_bound(client, mock_search_service):
    response = client.get("/search/suggest?q=test&limit=11")

    assert response.status_code == 422


def test_suggest_limit_defaults_to_five(client, mock_search_service):
    response = client.get("/search/suggest?q=test")

    assert response.status_code == 200
    mock_search_service.suggest.assert_called_once_with(query="test", limit=5)


def test_suggest_custom_limit(client, mock_search_service):
    response = client.get("/search/suggest?q=test&limit=3")

    assert response.status_code == 200
    mock_search_service.suggest.assert_called_once_with(query="test", limit=3)


def test_suggest_whitespace_padded_short_input_returns_empty(
    client, mock_search_service
):
    """Whitespace-padded query that is < 2 chars after strip returns empty at route level."""
    response = client.get("/search/suggest?q=%20%20a%20%20")

    assert response.status_code == 200
    assert response.json() == {"results": [], "remote_status": "ok"}
    mock_search_service.suggest.assert_not_called()


def test_suggest_whitespace_padded_valid_input_strips(client, mock_search_service):
    """Whitespace-padded query that is >= 2 chars after strip passes stripped value to service."""
    response = client.get("/search/suggest?q=%20%20ab%20%20")

    assert response.status_code == 200
    mock_search_service.suggest.assert_called_once_with(query="ab", limit=5)


def test_search_response_tolerates_additive_score_field():
    """Existing /api/search consumers tolerate the additive score field on SearchResult."""
    mock_search_service = MagicMock()
    mock_search_service.search = AsyncMock(
        return_value=SearchResponse(
            artists=[
                SearchResult(
                    type="artist",
                    title="Muse",
                    musicbrainz_id="mb-1",
                    in_library=False,
                    requested=False,
                    score=90,
                )
            ],
            albums=[
                SearchResult(
                    type="album",
                    title="Absolution",
                    musicbrainz_id="mb-2",
                    artist="Muse",
                    in_library=True,
                    requested=False,
                    score=85,
                )
            ],
        )
    )
    mock_search_service.schedule_cover_prefetch = MagicMock(return_value=[])

    mock_coverart = MagicMock()
    mock_enrichment = MagicMock()

    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[get_search_service] = lambda: mock_search_service
    test_app.dependency_overrides[get_coverart_repository] = lambda: mock_coverart
    test_app.dependency_overrides[get_search_enrichment_service] = (
        lambda: mock_enrichment
    )
    search_client = build_test_client(test_app)

    response = search_client.get("/search?q=muse")

    assert response.status_code == 200
    data = response.json()
    assert "artists" in data
    assert "albums" in data
    assert data["artists"][0]["score"] == 90
    assert data["albums"][0]["score"] == 85
    assert data["artists"][0]["title"] == "Muse"


def test_enrichment_route_forwards_request_disconnect_callback():
    enrichment_service = MagicMock()
    enrichment_service.enrich_batch = AsyncMock(return_value=EnrichmentResponse())
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[get_search_enrichment_service] = (
        lambda: enrichment_service
    )
    search_client = build_test_client(test_app)

    response = search_client.post(
        "/search/enrich/batch",
        json={"artists": [], "albums": []},
    )

    assert response.status_code == 200
    assert enrichment_service.enrich_batch.await_count == 1
    disconnect_callback = enrichment_service.enrich_batch.call_args.kwargs[
        "is_disconnected"
    ]
    assert callable(disconnect_callback)
