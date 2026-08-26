from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send


_COMPRESSIBLE_APPLICATION_TYPES = frozenset(
    {
        "application/javascript",
        "application/json",
        "application/manifest+json",
        "application/wasm",
        "application/x-javascript",
        "application/x-ndjson",
        "application/xml",
        "image/svg+xml",
    }
)


def _is_compressible_content_type(content_type: str) -> bool:
    media_type = content_type.partition(";")[0].strip().casefold()
    if media_type == "text/event-stream":
        return False
    return (
        media_type.startswith("text/")
        or media_type in _COMPRESSIBLE_APPLICATION_TYPES
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    )


def _accepts_gzip(header: str) -> bool:
    wildcard_quality: float | None = None
    gzip_quality: float | None = None
    for item in header.split(","):
        token, *parameters = item.strip().casefold().split(";")
        quality = 1.0
        for parameter in parameters:
            name, separator, value = parameter.strip().partition("=")
            if name == "q" and separator:
                try:
                    quality = min(1.0, max(0.0, float(value)))
                except ValueError:
                    quality = 0.0
        if token == "gzip":
            gzip_quality = quality
        elif token == "*":
            wildcard_quality = quality
    return (gzip_quality if gzip_quality is not None else wildcard_quality or 0.0) > 0


class _CompressibleGZipResponder(GZipResponder):
    async def send_with_compression(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.initial_message = message
            headers = Headers(raw=message["headers"])
            self.content_encoding_set = "content-encoding" in headers
            declared_length = headers.get("content-length")
            below_minimum = bool(
                declared_length is not None
                and declared_length.isdecimal()
                and int(declared_length) < self.minimum_size
            )
            self.content_type_is_excluded = (
                below_minimum
                or not _is_compressible_content_type(headers.get("content-type", ""))
            )
            return
        await super().send_with_compression(message)


class CompressibleGZipMiddleware:
    """Compress dynamic text responses without recompressing media or fonts."""

    def __init__(
        self, app: ASGIApp, minimum_size: int = 500, compresslevel: int = 9
    ) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not _accepts_gzip(Headers(scope=scope).get("accept-encoding", "")):
            await self.app(scope, receive, send)
            return
        responder = _CompressibleGZipResponder(
            self.app,
            self.minimum_size,
            compresslevel=self.compresslevel,
        )
        await responder(scope, receive, send)
