from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException

from api.v1.routes import cache as cache_routes
from api.v1.schemas.cache import CacheClearResponse, CacheStats
from core.dependencies import get_cache_service
from middleware import _get_current_admin
from tests.helpers import build_test_client, mock_admin_user


def _deny_admin() -> None:
    raise HTTPException(status_code=403, detail="admin only")


def _app(service: AsyncMock, admin_override) -> FastAPI:
    app = FastAPI()
    app.include_router(cache_routes.router)
    app.dependency_overrides[get_cache_service] = lambda: service
    app.dependency_overrides[_get_current_admin] = admin_override
    return app


def test_library_cache_clear_requires_admin() -> None:
    service = AsyncMock()
    response = build_test_client(_app(service, _deny_admin)).post(
        "/cache/clear/library"
    )

    assert response.status_code == 403
    service.clear_library_cache.assert_not_awaited()


def test_admin_library_cache_clear_uses_injected_target_safe_service() -> None:
    service = AsyncMock()
    service.clear_library_cache.return_value = CacheClearResponse(
        success=True,
        message="The native catalog and rollback data were preserved.",
    )
    response = build_test_client(_app(service, mock_admin_user)).post(
        "/cache/clear/library"
    )

    assert response.status_code == 200
    assert "preserved" in response.json()["message"]
    service.clear_library_cache.assert_awaited_once()


class TestClearMetadataRoute:
    def test_metadata_clear_route_registered(self):
        paths = [route.path for route in cache_routes.router.routes]
        assert "/cache/clear/metadata" in paths

    def test_metadata_clear_requires_admin(self):
        service = AsyncMock()
        response = build_test_client(_app(service, _deny_admin)).post(
            "/cache/clear/metadata"
        )

        assert response.status_code == 403
        service.clear_metadata_cache.assert_not_awaited()

    def test_admin_metadata_clear_returns_non_destructive_receipt(self):
        service = AsyncMock()
        service.clear_metadata_cache.return_value = CacheClearResponse(
            success=True,
            message="Successfully cleared 3 memory entries and 12 metadata files (covers preserved)",
            cleared_memory_entries=3,
            cleared_disk_files=12,
            cover_files_cleared=0,
        )
        response = build_test_client(_app(service, mock_admin_user)).post(
            "/cache/clear/metadata"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert "covers preserved" in body["message"]
        assert body["cover_files_cleared"] == 0
        assert body["cleared_memory_entries"] == 3
        assert body["cleared_disk_files"] == 12
        service.clear_metadata_cache.assert_awaited_once()


class TestCacheStatsRoute:
    """GET /cache/stats is admin-gated like every other internals endpoint."""

    @staticmethod
    def _stats_payload() -> dict:
        return {
            "memory_entries": 4,
            "memory_size_bytes": 4096,
            "memory_size_mb": 0.004,
            "disk_metadata_count": 12,
            "disk_metadata_albums": 3,
            "disk_metadata_artists": 2,
            "disk_cover_count": 1550,
            "disk_cover_size_bytes": 162529280,
            "disk_cover_size_mb": 155.0,
            "library_db_artist_count": 5,
            "library_db_album_count": 8,
            "library_db_size_bytes": 8192,
            "library_db_size_mb": 0.008,
            "total_size_bytes": 162541568,
            "total_size_mb": 155.01,
        }

    def test_stats_unauthenticated_returns_401(self):
        service = AsyncMock()
        service.get_stats.return_value = CacheStats(**self._stats_payload())
        # No auth override: the real admin dependency runs and rejects.
        app = FastAPI()
        app.include_router(cache_routes.router)
        app.dependency_overrides[get_cache_service] = lambda: service
        response = build_test_client(app).get("/cache/stats")

        assert response.status_code == 401
        service.get_stats.assert_not_awaited()

    def test_stats_non_admin_forbidden(self):
        service = AsyncMock()
        response = build_test_client(_app(service, _deny_admin)).get("/cache/stats")

        assert response.status_code == 403
        service.get_stats.assert_not_awaited()

    def test_admin_stats_returns_payload(self):
        service = AsyncMock()
        service.get_stats.return_value = CacheStats(**self._stats_payload())
        response = build_test_client(_app(service, mock_admin_user)).get("/cache/stats")

        assert response.status_code == 200
        body = response.json()
        assert body["memory_entries"] == 4
        assert body["disk_cover_count"] == 1550
        assert body["disk_cover_size_mb"] == 155.0
        service.get_stats.assert_awaited_once()
