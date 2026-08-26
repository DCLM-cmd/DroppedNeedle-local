import gzip

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from infrastructure.http.compression import CompressibleGZipMiddleware


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(CompressibleGZipMiddleware, minimum_size=20, compresslevel=6)

    @app.get("/{media_type:path}")
    async def content(media_type: str) -> Response:
        resolved_media_type = media_type.replace("--", "/")
        return Response(
            b"compressible response body " * 20, media_type=resolved_media_type
        )

    return TestClient(app)


@pytest.mark.parametrize(
    "media_type",
    [
        "text--plain",
        "application--json",
        "application--problem+json",
        "application--javascript",
        "image--svg+xml",
    ],
)
def test_compresses_textual_response_types(client: TestClient, media_type: str) -> None:
    response = client.get(f"/{media_type}", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.content == b"compressible response body " * 20
    assert "accept-encoding" in response.headers["vary"].casefold()


@pytest.mark.parametrize(
    "media_type",
    [
        "font--woff2",
        "image--png",
        "audio--flac",
        "video--mp4",
        "application--octet-stream",
        "text--event-stream",
    ],
)
def test_does_not_recompress_media_fonts_or_event_streams(
    client: TestClient, media_type: str
) -> None:
    response = client.get(f"/{media_type}", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.content == b"compressible response body " * 20


def test_honors_gzip_quality_zero(client: TestClient) -> None:
    response = client.get(
        "/text--plain", headers={"Accept-Encoding": "gzip;q=0, *;q=1"}
    )

    assert "content-encoding" not in response.headers


def test_declared_small_streaming_response_respects_minimum_size() -> None:
    app = FastAPI()
    app.add_middleware(CompressibleGZipMiddleware, minimum_size=20, compresslevel=6)

    @app.get("/")
    async def content() -> Response:
        return Response(b"small", media_type="application/javascript")

    response = TestClient(app).get("/", headers={"Accept-Encoding": "gzip"})

    assert response.content == b"small"
    assert "content-encoding" not in response.headers
    assert response.headers["content-length"] == "5"


def test_preserves_existing_content_encoding_without_double_compression() -> None:
    original = b"already encoded response " * 20
    encoded = gzip.compress(original)
    app = FastAPI()
    app.add_middleware(CompressibleGZipMiddleware, minimum_size=20, compresslevel=6)

    @app.get("/")
    async def content() -> Response:
        return Response(
            encoded,
            media_type="application/javascript",
            headers={"Content-Encoding": "gzip"},
        )

    response = TestClient(app).get("/", headers={"Accept-Encoding": "gzip"})

    assert response.headers["content-encoding"] == "gzip"
    assert response.content == original
    assert response.headers["content-length"] == str(len(encoded))
