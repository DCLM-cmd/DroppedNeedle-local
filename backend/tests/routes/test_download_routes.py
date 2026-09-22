"""Route coverage for local file/album downloads (permission-gated)."""

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from api.v1.routes.download import router
from core.config import Settings
from core.dependencies import get_local_files_service, get_preferences_service
from core.exceptions import ResourceNotFoundError
from services.preferences_service import PreferencesService
from tests.helpers import build_test_client, override_user_auth


def _prefs(tmp_path: Path, access: str) -> PreferencesService:
    settings = Settings()
    settings.config_file_path = tmp_path / "config.json"
    prefs = PreferencesService(settings)
    stored = prefs.get_security_settings()
    stored.library_download_access = access
    prefs.save_security_settings(stored)
    return prefs


def _app(prefs: PreferencesService, service: AsyncMock, *, role: str | None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_local_files_service] = lambda: service
    app.dependency_overrides[get_preferences_service] = lambda: prefs
    if role is not None:
        override_user_auth(app, role=role)
    return app


def _zip_file(tmp_path: Path) -> Path:
    path = tmp_path / "album.zip"
    path.write_bytes(b"PK\x03\x04fake-zip-bytes")
    return path


def test_album_zip_200_for_string_id_and_cleans_up_tmpfile(tmp_path: Path) -> None:
    zip_path = _zip_file(tmp_path)
    service = AsyncMock()
    service.create_album_zip.return_value = (zip_path, "Album.zip")
    client = build_test_client(_app(_prefs(tmp_path, "everyone"), service, role="user"))

    response = client.get("/download/local/album/album-1")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.content == b"PK\x03\x04fake-zip-bytes"
    service.create_album_zip.assert_awaited_once_with("album-1")
    deadline = time.monotonic() + 5
    while zip_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not zip_path.exists()


def test_album_zip_unknown_ids_404_with_envelope(tmp_path: Path) -> None:
    service = AsyncMock()
    service.create_album_zip.side_effect = ResourceNotFoundError("nope")
    service.create_album_zip_by_mbid.side_effect = ResourceNotFoundError("nope")
    client = build_test_client(_app(_prefs(tmp_path, "everyone"), service, role="user"))

    for path in (
        "/download/local/album/unknown-id",
        "/download/local/album/mbid/mbid-9",
    ):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


def test_track_without_file_404s(tmp_path: Path) -> None:
    service = AsyncMock()
    service.get_download_track.side_effect = ResourceNotFoundError("nope")
    client = build_test_client(_app(_prefs(tmp_path, "everyone"), service, role="user"))

    assert client.get("/download/local/track/file-9").status_code == 404


def test_path_escape_403s(tmp_path: Path) -> None:
    service = AsyncMock()
    service.get_download_track.side_effect = PermissionError("outside root")
    client = build_test_client(_app(_prefs(tmp_path, "everyone"), service, role="user"))

    assert client.get("/download/local/track/file-1").status_code == 403


@pytest.mark.parametrize(
    ("access", "role", "expected"),
    [
        ("everyone", "user", 200),
        ("everyone", "trusted", 200),
        ("everyone", "admin", 200),
        ("trusted", "user", 403),
        ("trusted", "trusted", 200),
        ("trusted", "admin", 200),
        ("admin", "user", 403),
        ("admin", "trusted", 403),
        ("admin", "admin", 200),
    ],
)
def test_album_download_permission_matrix(
    tmp_path: Path, access: str, role: str, expected: int
) -> None:
    zip_path = _zip_file(tmp_path)
    service = AsyncMock()
    service.create_album_zip.return_value = (zip_path, "Album.zip")
    client = build_test_client(_app(_prefs(tmp_path, access), service, role=role))

    response = client.get("/download/local/album/album-1")

    assert response.status_code == expected
    if expected == 403:
        assert response.json()["error"]["code"] == "FORBIDDEN"
        service.create_album_zip.assert_not_awaited()
    else:
        service.create_album_zip.assert_awaited_once_with("album-1")


@pytest.mark.parametrize(
    "path",
    ["/download/local/track/file-1", "/download/local/album/mbid/mbid-1"],
)
def test_track_and_mbid_endpoints_share_the_permission_gate(
    tmp_path: Path, path: str
) -> None:
    service = AsyncMock()
    client = build_test_client(_app(_prefs(tmp_path, "admin"), service, role="user"))

    response = client.get(path)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    service.get_download_track.assert_not_awaited()
    service.create_album_zip_by_mbid.assert_not_awaited()


def test_download_requires_authentication(tmp_path: Path) -> None:
    service = AsyncMock()
    client = build_test_client(_app(_prefs(tmp_path, "everyone"), service, role=None))

    assert client.get("/download/local/album/album-1").status_code == 401
    assert client.get("/download/access").status_code == 401
    service.create_album_zip.assert_not_awaited()


@pytest.mark.parametrize(
    ("access", "role", "expected"),
    [
        ("everyone", "user", True),
        ("everyone", "trusted", True),
        ("everyone", "admin", True),
        ("trusted", "user", False),
        ("trusted", "trusted", True),
        ("trusted", "admin", True),
        ("admin", "user", False),
        ("admin", "trusted", False),
        ("admin", "admin", True),
    ],
)
def test_download_access_reports_viewer_capability(
    tmp_path: Path, access: str, role: str, expected: bool
) -> None:
    client = build_test_client(_app(_prefs(tmp_path, access), AsyncMock(), role=role))

    response = client.get("/download/access")

    assert response.status_code == 200
    assert response.json() == {"allowed": expected}
