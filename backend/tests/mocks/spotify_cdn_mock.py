"""Configurable Spotify CDN mock (httpx.MockTransport), shaped from the live
playlist-cover contract: a playlist's ``images[].url`` points at an
``https://*.scdn.co`` host (typically i.scdn.co) that answers directly with
``image/jpeg`` bytes - no redirects, no HTML. The mock lets tests flip status
code, content type, and body to exercise the importer's validation/degradation
paths (oversized, wrong type, 5xx, redirect) without any HTTP-mocking library.
"""

import httpx

# A tiny structurally-valid JPEG (SOI + JFIF APP0 + EOI). PlaylistService only
# validates MIME + size, so the bytes just need to round-trip identically.
JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xd9"
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nspotify-playlist-cover"

# Canonical picker URL shape as the live API returns it (640x640 playlist cover).
COVER_URL = "https://i.scdn.co/image/ab67706c0000da84abcdef0123456789abcdef01"

# A second allowlisted host, for suffix-match coverage beyond i.scdn.co.
MOSAIC_URL = "https://mosaic.scdn.co/640/ab67706c0000ffffabcdef0123456789"


class SpotifyCdnMock:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.image_bytes = JPEG_BYTES
        self.content_type = "image/jpeg"
        self.status_code = 200
        # Optional extra response headers (e.g. a lying Content-Length or a
        # Location header for redirect tests).
        self.extra_headers: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"Content-Type": self.content_type, **self.extra_headers}
        if self.status_code != 200:
            return httpx.Response(self.status_code, content=b"nope", headers=headers)
        return httpx.Response(200, content=self.image_bytes, headers=headers)

    def client(self) -> httpx.AsyncClient:
        """An httpx.AsyncClient whose transport serves this mock - bind it via
        ``cover_fetcher_for(...)`` exactly like production wires the factory
        client into SpotifyImportService."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
