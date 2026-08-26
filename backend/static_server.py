import mimetypes
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.staticfiles import NotModifiedResponse
from starlette.types import Scope


# slim base images omit woff2 from mime.types; register it so fonts serve as font/woff2
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")

_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}
_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
_STATIC_CACHE_CONTROL = "public, max-age=604800"


def _encoding_quality(header: str, encoding: str) -> float:
    explicit: dict[str, float] = {}
    wildcard = 0.0
    for item in header.split(","):
        token, *parameters = item.strip().casefold().split(";")
        if not token:
            continue
        quality = 1.0
        for parameter in parameters:
            name, separator, value = parameter.strip().partition("=")
            if name == "q" and separator:
                try:
                    quality = min(1.0, max(0.0, float(value)))
                except ValueError:
                    quality = 0.0
        if token == "*":
            wildcard = quality
        else:
            explicit[token] = quality
    return explicit.get(encoding, wildcard)


def _precompressed_variant(
    full_path: Path, stat_result: os.stat_result, scope: Scope
) -> tuple[Path, os.stat_result, str | None, bool]:
    variants: list[tuple[str, Path, os.stat_result]] = []
    for encoding, suffix in (("br", ".br"), ("gzip", ".gz")):
        candidate = Path(f"{full_path}{suffix}")
        try:
            candidate_stat = candidate.stat()
        except OSError:
            continue
        if candidate_stat.st_size < stat_result.st_size:
            variants.append((encoding, candidate, candidate_stat))

    if not variants or "range" in Headers(scope=scope):
        return full_path, stat_result, None, bool(variants)

    accept_encoding = Headers(scope=scope).get("accept-encoding", "")
    accepted = [
        (encoding, path, candidate_stat, _encoding_quality(accept_encoding, encoding))
        for encoding, path, candidate_stat in variants
    ]
    eligible = [candidate for candidate in accepted if candidate[3] > 0]
    if not eligible:
        return full_path, stat_result, None, True
    encoding, path, candidate_stat, _quality = max(
        eligible,
        key=lambda candidate: (candidate[3], candidate[0] == "br"),
    )
    return path, candidate_stat, encoding, True


class FrontendStaticFiles(StaticFiles):
    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        original_path = Path(full_path)
        selected_path, selected_stat, encoding, has_variants = _precompressed_variant(
            original_path, stat_result, scope
        )
        media_type = mimetypes.guess_type(original_path.name)[0]
        response = FileResponse(
            selected_path,
            status_code=status_code,
            stat_result=selected_stat,
            media_type=media_type or "application/octet-stream",
        )
        if has_variants:
            response.headers.add_vary_header("Accept-Encoding")
        if encoding is not None:
            response.headers["Content-Encoding"] = encoding
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if path.startswith("immutable/") and response.status_code in (200, 304):
            response.headers["Cache-Control"] = _IMMUTABLE_CACHE_CONTROL
        elif response.status_code in (200, 304):
            response.headers.update(_NO_CACHE_HEADERS)
        return response


class CacheControlledStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = _STATIC_CACHE_CONTROL
        return response


def mount_frontend(app: FastAPI) -> None:
    backend_static = Path(__file__).parent / "static"
    frontend_root = Path(__file__).resolve().parents[1] / "frontend"
    build_candidates = [backend_static, frontend_root / "build"]

    def first_existing_build() -> Path:
        for candidate in build_candidates:
            if (candidate / "index.html").exists():
                return candidate
        return backend_static

    build_dir = first_existing_build()
    index_html = build_dir / "index.html"
    asset_dirs = [build_dir, frontend_root / "static"]

    def resolve_asset(filename: str) -> Path | None:
        for directory in asset_dirs:
            candidate = directory / filename
            if candidate.exists():
                return candidate
        return None

    if (build_dir / "_app").exists():
        app.mount(
            "/_app",
            FrontendStaticFiles(directory=build_dir / "_app", html=False),
            name="_app",
        )

    if (img_dir := build_dir / "img").exists():
        app.mount(
            "/img",
            CacheControlledStaticFiles(directory=img_dir, html=False),
            name="img",
        )

    if (fonts_dir := build_dir / "fonts").exists():
        app.mount(
            "/fonts",
            CacheControlledStaticFiles(directory=fonts_dir, html=False),
            name="fonts",
        )

    @app.get("/robots.txt")
    async def serve_robots():
        if robots := resolve_asset("robots.txt"):
            return FileResponse(
                robots,
                media_type="text/plain",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        raise HTTPException(status_code=404, detail="Not found")

    @app.get("/logo.png")
    async def serve_logo():
        if logo := resolve_asset("logo.png"):
            return FileResponse(logo, headers={"Cache-Control": _STATIC_CACHE_CONTROL})
        raise HTTPException(status_code=404, detail="Not found")

    @app.get("/logo_wide.png")
    async def serve_logo_wide():
        if logo := resolve_asset("logo_wide.png"):
            return FileResponse(logo, headers={"Cache-Control": _STATIC_CACHE_CONTROL})
        raise HTTPException(status_code=404, detail="Not found")

    @app.get("/logo_wide_white.png")
    async def serve_logo_wide_white():
        if logo := resolve_asset("logo_wide_white.png"):
            return FileResponse(logo, headers={"Cache-Control": _STATIC_CACHE_CONTROL})
        raise HTTPException(status_code=404, detail="Not found")

    @app.get("/logo_icon.png")
    async def serve_logo_icon():
        if logo := resolve_asset("logo_icon.png"):
            return FileResponse(logo, headers={"Cache-Control": _STATIC_CACHE_CONTROL})
        raise HTTPException(status_code=404, detail="Not found")

    @app.get("/favicon.ico")
    async def serve_favicon_ico():
        if icon := resolve_asset("favicon.ico"):
            return FileResponse(
                icon,
                media_type="image/x-icon",
                headers={"Cache-Control": "public, max-age=604800"},
            )
        raise HTTPException(status_code=404, detail="Not found")

    @app.get("/favicon-{size}.png")
    async def serve_favicon_png(size: str):
        if icon := resolve_asset(f"favicon-{size}.png"):
            return FileResponse(
                icon,
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=604800"},
            )
        raise HTTPException(status_code=404, detail="Not found")

    @app.get("/apple-touch-icon.png")
    async def serve_apple_touch_icon():
        if icon := resolve_asset("apple-touch-icon.png"):
            return FileResponse(
                icon,
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=604800"},
            )
        raise HTTPException(status_code=404, detail="Not found")

    @app.get("/android-chrome-{size}.png")
    async def serve_android_chrome(size: str):
        if icon := resolve_asset(f"android-chrome-{size}.png"):
            return FileResponse(
                icon,
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=604800"},
            )
        raise HTTPException(status_code=404, detail="Not found")

    @app.get("/site.webmanifest")
    async def serve_webmanifest():
        if manifest := resolve_asset("site.webmanifest"):
            return FileResponse(
                manifest,
                media_type="application/manifest+json",
                headers={"Cache-Control": "public, max-age=604800"},
            )
        raise HTTPException(status_code=404, detail="Not found")

    @app.get("/")
    async def serve_root():
        if index_html.exists():
            return FileResponse(index_html, headers=_NO_CACHE_HEADERS)
        raise HTTPException(status_code=404, detail="Frontend not built yet")

    @app.get("/{full_path:path}")
    async def serve_spa_routes(full_path: str):
        if full_path.startswith("api"):
            raise HTTPException(status_code=404, detail="API route not found")
        if index_html.exists():
            return FileResponse(index_html, headers=_NO_CACHE_HEADERS)
        raise HTTPException(status_code=404, detail="Frontend not built yet")
