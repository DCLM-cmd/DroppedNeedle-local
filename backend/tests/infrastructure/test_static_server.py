import gzip

from fastapi import FastAPI
from fastapi.testclient import TestClient

import static_server
from static_server import (
    CacheControlledStaticFiles,
    FrontendStaticFiles,
    mount_frontend,
)


def test_hashed_frontend_assets_are_immutable(tmp_path):
    immutable = tmp_path / "immutable"
    immutable.mkdir()
    (immutable / "entry.abc123.js").write_text("export {};", encoding="utf-8")
    app = FastAPI()
    app.mount("/_app", FrontendStaticFiles(directory=tmp_path))

    response = TestClient(app).get("/_app/immutable/entry.abc123.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_unhashed_frontend_metadata_is_not_marked_immutable(tmp_path):
    (tmp_path / "version.json").write_text('{"version":"1"}', encoding="utf-8")
    app = FastAPI()
    app.mount("/_app", FrontendStaticFiles(directory=tmp_path))

    response = TestClient(app).get("/_app/version.json")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_frontend_assets_negotiate_smaller_precompressed_variants(tmp_path):
    immutable = tmp_path / "immutable"
    immutable.mkdir()
    original = b"const value = 'compressible';\n" * 100
    asset = immutable / "entry.abc123.js"
    asset.write_bytes(original)
    gzip_payload = gzip.compress(original, compresslevel=9)
    asset.with_name(f"{asset.name}.gz").write_bytes(gzip_payload)
    brotli_placeholder = b"precompressed-brotli"
    asset.with_name(f"{asset.name}.br").write_bytes(brotli_placeholder)
    app = FastAPI()
    app.mount("/_app", FrontendStaticFiles(directory=tmp_path))
    client = TestClient(app)

    brotli_response = client.head(
        "/_app/immutable/entry.abc123.js",
        headers={"Accept-Encoding": "gzip;q=0.5, br"},
    )
    gzip_response = client.get(
        "/_app/immutable/entry.abc123.js",
        headers={"Accept-Encoding": "gzip, br;q=0.5"},
    )
    identity_response = client.get(
        "/_app/immutable/entry.abc123.js",
        headers={"Accept-Encoding": "identity"},
    )

    assert brotli_response.headers["content-encoding"] == "br"
    assert brotli_response.headers["content-length"] == str(len(brotli_placeholder))
    assert gzip_response.headers["content-encoding"] == "gzip"
    assert gzip_response.headers["content-length"] == str(len(gzip_payload))
    assert gzip_response.content == original
    assert "content-encoding" not in identity_response.headers
    assert identity_response.content == original
    assert all(
        "accept-encoding" in response.headers["vary"].casefold()
        for response in (brotli_response, gzip_response, identity_response)
    )
    assert (
        len(
            {
                brotli_response.headers["etag"],
                gzip_response.headers["etag"],
                identity_response.headers["etag"],
            }
        )
        == 3
    )


def test_precompressed_assets_preserve_identity_for_ranges_and_larger_sidecars(
    tmp_path,
):
    immutable = tmp_path / "immutable"
    immutable.mkdir()
    ranged_asset = immutable / "range.js"
    ranged_asset.write_bytes(b"abcdefghij" * 100)
    ranged_asset.with_name(f"{ranged_asset.name}.br").write_bytes(b"small")
    tiny_asset = immutable / "tiny.js"
    tiny_asset.write_bytes(b"ok")
    tiny_asset.with_name(f"{tiny_asset.name}.gz").write_bytes(b"larger-sidecar")
    app = FastAPI()
    app.mount("/_app", FrontendStaticFiles(directory=tmp_path))
    client = TestClient(app)

    range_response = client.get(
        "/_app/immutable/range.js",
        headers={"Accept-Encoding": "br", "Range": "bytes=0-3"},
    )
    tiny_response = client.get(
        "/_app/immutable/tiny.js", headers={"Accept-Encoding": "gzip"}
    )

    assert range_response.status_code == 206
    assert range_response.content == b"abcd"
    assert "content-encoding" not in range_response.headers
    assert tiny_response.content == b"ok"
    assert "content-encoding" not in tiny_response.headers


def test_static_fonts_and_images_receive_bounded_shared_cache(tmp_path):
    (tmp_path / "font.woff2").write_bytes(b"font")
    app = FastAPI()
    app.mount("/fonts", CacheControlledStaticFiles(directory=tmp_path))

    response = TestClient(app).get("/fonts/font.woff2")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=604800"
    assert response.headers["content-type"] == "font/woff2"


def test_frontend_entry_and_named_assets_keep_intended_cache_policy(
    tmp_path, monkeypatch
):
    frontend_build = tmp_path / "frontend" / "build"
    app_dir = frontend_build / "_app"
    app_dir.mkdir(parents=True)
    (frontend_build / "index.html").write_text("<main>test</main>", encoding="utf-8")
    (frontend_build / "logo.png").write_bytes(b"png")
    (app_dir / "env.js").write_text("window.env = {};", encoding="utf-8")
    monkeypatch.setattr(
        static_server, "__file__", str(tmp_path / "backend" / "static_server.py")
    )
    app = FastAPI()
    mount_frontend(app)
    client = TestClient(app)

    root_response = client.get("/")
    env_response = client.get("/_app/env.js")
    logo_response = client.get("/logo.png")

    assert root_response.headers["cache-control"] == "no-cache"
    assert env_response.headers["cache-control"] == "no-cache"
    assert logo_response.headers["cache-control"] == "public, max-age=604800"
