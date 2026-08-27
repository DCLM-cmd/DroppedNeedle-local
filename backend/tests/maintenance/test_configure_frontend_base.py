"""Tests for maintenance.configure_frontend_base (startup static base-path rewrite).

Contract under test: pristine template in, token-free rewritten tree out;
every failure - invalid config, staging IO, verification - leaves the
previously served tree bit-identical and fails closed.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from core.base_path import BasePathError
from maintenance import configure_frontend_base as cfb
from maintenance.configure_frontend_base import (
    BASE_PATH_TOKEN,
    StaticRewriteError,
    main,
    rewrite_static,
)

_TOKEN = BASE_PATH_TOKEN.encode("ascii")


def _build_template(tmp_path: Path) -> tuple[Path, int]:
    """Create a representative SvelteKit bundle; returns (template_root, token count)."""
    root = tmp_path / "static-template"
    immutable = root / "_app" / "immutable"
    immutable.mkdir(parents=True)
    (root / "img").mkdir()

    files: dict[str, bytes] = {
        "index.html": (
            f'<!doctype html><html><head><link rel="manifest" href="{BASE_PATH_TOKEN}/manifest.webmanifest">'
            f'</head><body><a href="{BASE_PATH_TOKEN}/login">login</a>'
            '<link rel="preload" href="/fonts/spacemono-400-latin.woff2" as="font">'
            f'<script src="{BASE_PATH_TOKEN}/_app/immutable/entry.js" type="module"></script></body></html>'
        ).encode(),
        "_app/immutable/entry.js": (
            f'const base="{BASE_PATH_TOKEN}";export const start=()=>fetch(base+"/api/v1/health");'
        ).encode(),
        "_app/immutable/entry.js.map": (
            f'{{"version":3,"sources":["entry.js","{BASE_PATH_TOKEN}"]}}'
        ).encode(),
        "app.css": b'body{background:url("../../../fonts/a.woff2")}',
        "legacy.css": f'body{{background:url("../../../{BASE_PATH_TOKEN.lstrip("/")}/fonts/b.woff2")}}'.encode(),
        "theme.css": (
            '@font-face{src:url("/fonts/hankengrotesk-latin.woff2")}'
            '.hero{background-image:url("/fonts/spacegrotesk-latin.woff2")}'
        ).encode(),
        "manifest.webmanifest": (
            f'{{"start_url":"{BASE_PATH_TOKEN}/","scope":"{BASE_PATH_TOKEN}/"}}'
        ).encode(),
        "robots.txt": f"Sitemap: {BASE_PATH_TOKEN}/sitemap.xml".encode(),
        "icon.svg": f'<svg xmlns="http://www.w3.org/2000/svg" href="{BASE_PATH_TOKEN}"/>'.encode(),
        # Binary assets must survive byte-for-byte; they never contain tokens.
        "favicon.ico": bytes(range(256)),
        "img/photo.png": b"\x89PNG\r\n\x1a\n" + os.urandom(64),
        # Precompressed variants of placeholder-era assets: deleted unconditionally.
        "index.html.br": b"\x1b/\x00stale-brotli",
        "index.html.gz": b"\x1f\x8b\x08\x00stale-gzip",
        "theme.css.br": b"stale-css-br",
        "_app/immutable/entry.js.br": b"\x1b/\x00stale-js-br",
    }
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    return root, sum(data.count(_TOKEN) for data in files.values())


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _stage_directories(parent: Path) -> list[str]:
    return [
        entry.name
        for entry in sorted(parent.iterdir())
        if entry.name.startswith(cfb._STAGE_PREFIX)
        or entry.name.startswith(cfb._PREVIOUS_PREFIX)
    ]


# --- happy paths -------------------------------------------------------------


def test_rewrite_patches_text_and_drops_compressed_variants(tmp_path: Path) -> None:
    template, expected_tokens = _build_template(tmp_path)
    static = tmp_path / "static"

    summary = rewrite_static(template, static, "/music/app")

    assert summary.base_path == "/music/app"
    assert summary.tokens_replaced == expected_tokens > 0
    assert summary.fonts_replaced == 3  # two literals in theme.css + html preload
    assert summary.compressed_variants_removed == 4

    tree = _snapshot(static)
    assert "/music/app/login" in tree["index.html"].decode()
    assert 'const base="/music/app"' in tree["_app/immutable/entry.js"].decode()
    assert 'url("../../../fonts/a.woff2")' in tree["app.css"].decode()
    assert 'url("../../../fonts/b.woff2")' in tree["legacy.css"].decode()
    theme_css = tree["theme.css"].decode()
    assert theme_css.count("/music/app/fonts/") == 2
    assert "/music/app/fonts/spacemono-400-latin.woff2" in tree["index.html"].decode()
    assert '"start_url":"/music/app/"' in tree["manifest.webmanifest"].decode()
    assert "/music/app/sitemap.xml" in tree["robots.txt"].decode()

    # Generic font URLs gain exactly one prefix each; nothing is doubled.
    joined = b"".join(tree.values())
    assert b"/music/app/music/app/fonts/" not in joined

    # Binaries untouched byte-for-byte.
    assert tree["favicon.ico"] == bytes(range(256))
    assert tree["img/photo.png"] == (template / "img/photo.png").read_bytes()

    # No stale precompressed variant survives anywhere in the served tree.
    assert not list(static.rglob("*.br"))
    assert not list(static.rglob("*.gz"))

    # The template remains pristine input; no staging leftovers remain.
    assert BASE_PATH_TOKEN.encode() in (template / "index.html").read_bytes()
    assert _stage_directories(tmp_path) == []


def test_empty_base_consumes_every_token(tmp_path: Path) -> None:
    template, expected_tokens = _build_template(tmp_path)
    static = tmp_path / "static"

    summary = rewrite_static(template, static, "")

    assert summary.base_path == ""
    assert summary.tokens_replaced == expected_tokens > 0
    assert summary.fonts_replaced == 0  # empty base leaves shipped bytes alone

    html = _snapshot(static)["index.html"]
    assert _TOKEN not in html
    assert b'href="/login"' in html
    assert b'src="/_app/immutable/entry.js"' in html
    tree = _snapshot(static)
    assert tree["theme.css"] == (template / "theme.css").read_bytes()
    assert b'url("../../../fonts/a.woff2")' in tree["app.css"]
    assert b'url("../../../fonts/b.woff2")' in tree["legacy.css"]


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    template, expected_tokens = _build_template(tmp_path)
    static = tmp_path / "static"

    first = rewrite_static(template, static, "/music")
    first_tree = _snapshot(static)

    second = rewrite_static(template, static, "/music")

    assert second.tokens_replaced == first.tokens_replaced == expected_tokens
    assert second.fonts_replaced == first.fonts_replaced == 3
    assert _snapshot(static) == first_tree
    html = first_tree["index.html"]
    assert html.count(_TOKEN) == 0
    # Exactly one prefix per URL: manifest link, login link, script src, font preload.
    assert html.count(b"/music") == 4


def test_rerun_with_new_base_proves_pristine_template_input(tmp_path: Path) -> None:
    template, _ = _build_template(tmp_path)
    template_before = _snapshot(template)
    static = tmp_path / "static"

    rewrite_static(template, static, "/a")
    rewrite_static(template, static, "/b/c")

    html = _snapshot(static)["index.html"]
    assert b"/b/c/login" in html
    assert b"/a/" not in html
    css = _snapshot(static)["theme.css"].decode()
    assert css.count("/b/c/fonts/") == 2
    assert "/a/fonts/" not in css
    assert _TOKEN not in html
    assert _snapshot(template) == template_before


@pytest.mark.parametrize("base", ["/music", "/deep/nested/base/path"])
def test_configured_bases_prefix_font_urls_exactly_once(
    tmp_path: Path, base: str
) -> None:
    template, _ = _build_template(tmp_path)

    summary = rewrite_static(template, tmp_path / "static", base)

    tree = _snapshot(tmp_path / "static")
    encoded_base = base.encode()
    # Plain literals use the dedicated font pass. Vite emits CSS font targets
    # relative to the based asset directory; removing that embedded base segment
    # keeps the browser at <base>/fonts instead of <base>/<base>/fonts.
    assert summary.fonts_replaced == 3
    assert tree["theme.css"].count(encoded_base + cfb.FONT_URL_NEEDLE) == 2
    assert tree["app.css"].count(b"../../../fonts/") == 1
    assert tree["legacy.css"].count(b"../../../fonts/") == 1
    assert encoded_base + b"/fonts/spacemono-400-latin.woff2" in tree["index.html"]
    joined = b"".join(tree.values())
    assert encoded_base * 2 + cfb.FONT_URL_NEEDLE not in joined
    assert _TOKEN not in joined


def test_local_source_css_ships_root_hosted_font_urls() -> None:
    """Dev servers resolve /fonts themselves: sources must stay unprefixed."""
    frontend_src = Path(__file__).resolve().parents[3] / "frontend" / "src"
    for name, expected in (("app.css", 8), ("auth.css", 3)):
        css = (frontend_src / name).read_text(encoding="utf-8")
        assert BASE_PATH_TOKEN not in css
        assert css.count("url('/fonts/") == expected
    assert (frontend_src.parent / "static" / "fonts").is_dir()


def test_orphan_staging_directories_are_swept(tmp_path: Path) -> None:
    template, _ = _build_template(tmp_path)
    stale_stage = tmp_path / f"{cfb._STAGE_PREFIX}123-interrupted"
    stale_stage.mkdir()
    (stale_stage / "junk.html").write_text("x")
    stale_previous = tmp_path / f"{cfb._PREVIOUS_PREFIX}456"
    stale_previous.mkdir()

    rewrite_static(template, tmp_path / "static", "/music")

    assert _stage_directories(tmp_path) == []


def test_maximum_length_base_rewrites(tmp_path: Path) -> None:
    from core.base_path import MAX_BASE_PATH_LENGTH

    template, _ = _build_template(tmp_path)
    base = "/" + ("a" * (MAX_BASE_PATH_LENGTH - 1))

    summary = rewrite_static(template, tmp_path / "static", base)

    assert summary.base_path == base
    assert _TOKEN not in _snapshot(tmp_path / "static")["index.html"]


# --- hostile configurations fail closed before any mutation ------------------

HOSTILE_BASES = [
    pytest.param("foo", id="missing-leading-slash"),
    pytest.param("//host", id="protocol-relative"),
    pytest.param("/foo/", id="trailing-slash"),
    pytest.param("/foo//bar", id="empty-segment"),
    pytest.param("/", id="root-only"),
    pytest.param("/.", id="dot-segment"),
    pytest.param("/..", id="dotdot-segment"),
    pytest.param("/./..", id="dot-dotdot-segments"),
    pytest.param("/a/../b", id="embedded-dotdot"),
    pytest.param("%41", id="bare-percent-escape"),
    pytest.param("/%41", id="percent-escaped-segment"),
    pytest.param("\\", id="lone-backslash"),
    pytest.param("/a\\b", id="embedded-backslash"),
    pytest.param("?q", id="bare-query"),
    pytest.param("/x?q", id="query-suffix"),
    pytest.param("#f", id="bare-fragment"),
    pytest.param("/x#f", id="fragment-suffix"),
    pytest.param(" ", id="space"),
    pytest.param("\t/music", id="leading-tab"),
    pytest.param("/music\n", id="trailing-newline"),
    pytest.param("\x00/music", id="nul-control"),
    pytest.param("/mu\x7fsic", id="del-control"),
    pytest.param("café", id="non-ascii-latin"),
    pytest.param("/日本語", id="non-ascii-cjk"),
    pytest.param("/" + "a" * 4096, id="overlength-segment"),
]


@pytest.mark.parametrize("raw", HOSTILE_BASES)
def test_hostile_bases_fail_before_any_mutation(tmp_path: Path, raw: str) -> None:
    template, _ = _build_template(tmp_path)
    template_before = _snapshot(template)
    static = tmp_path / "static"

    with pytest.raises(BasePathError):
        rewrite_static(template, static, raw)

    assert not static.exists()
    assert _snapshot(template) == template_before
    assert _stage_directories(tmp_path) == []


def test_missing_template_fails_closed(tmp_path: Path) -> None:
    static = tmp_path / "static"

    with pytest.raises(StaticRewriteError, match="does not exist"):
        rewrite_static(tmp_path / "absent", static, "/music")

    assert not static.exists()


def test_template_without_index_html_fails_closed(tmp_path: Path) -> None:
    template = tmp_path / "static-template"
    template.mkdir()
    (template / "assets").mkdir()

    with pytest.raises(StaticRewriteError, match="index.html"):
        rewrite_static(template, tmp_path / "static", "/music")

    assert not (tmp_path / "static").exists()


def test_template_without_token_fails_closed(tmp_path: Path) -> None:
    template = tmp_path / "static-template"
    template.mkdir()
    (template / "index.html").write_text("<!doctype html><title>dev build</title>")

    with pytest.raises(StaticRewriteError, match="DROPPEDNEEDLE_BASE_PATH_PLACEHOLDER"):
        rewrite_static(template, tmp_path / "static", "")

    assert not (tmp_path / "static").exists()


def test_token_in_binary_asset_fails_verification(tmp_path: Path) -> None:
    """A token inside a non-text file is a build misconfiguration: refuse."""
    template = tmp_path / "static-template"
    template.mkdir()
    (template / "index.html").write_bytes(f'href="{BASE_PATH_TOKEN}/login"'.encode())
    (template / "logo.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + _TOKEN + b"\x00binary-region"
    )

    with pytest.raises(StaticRewriteError, match="logo.png"):
        rewrite_static(template, tmp_path / "static", "/music")

    assert not (tmp_path / "static").exists()
    assert _stage_directories(tmp_path) == []


# --- fail-closed staging guarantees -----------------------------------------


def test_copytree_failure_keeps_previous_tree_bit_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template, _ = _build_template(tmp_path)
    static = tmp_path / "static"
    rewrite_static(template, static, "/one")
    before = _snapshot(static)

    def explode(source: Path, destination: Path, **_kwargs: object) -> None:
        raise RuntimeError("simulated staging failure")

    monkeypatch.setattr(cfb.shutil, "copytree", explode)
    with pytest.raises(RuntimeError, match="simulated staging failure"):
        rewrite_static(template, static, "/two")

    assert _snapshot(static) == before
    assert _stage_directories(tmp_path) == []


def test_final_verification_failure_keeps_previous_tree_bit_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template, _ = _build_template(tmp_path)
    static = tmp_path / "static"
    rewrite_static(template, static, "/one")
    before = _snapshot(static)

    def flaky_scan(root: Path, token: bytes) -> None:
        raise RuntimeError("simulated verification failure")

    monkeypatch.setattr(cfb, "_assert_no_tokens", flaky_scan)
    with pytest.raises(RuntimeError, match="simulated verification failure"):
        rewrite_static(template, static, "/two")

    assert _snapshot(static) == before
    assert _stage_directories(tmp_path) == []


def test_copystat_failure_keeps_previous_tree_bit_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata stamping runs pre-swap: its failure leaves the served tree intact."""
    template, _ = _build_template(tmp_path)
    static = tmp_path / "static"
    rewrite_static(template, static, "/one")
    before = _snapshot(static)

    real_copystat = shutil.copystat
    state = {"armed": False}

    def arm_on_verification(root: Path, token: bytes) -> None:
        state["armed"] = True

    def flaky_copystat(source: object, destination: object, **_kwargs: object) -> None:
        if state["armed"]:  # only the explicit pre-swap stamp, after verification
            raise OSError("simulated copystat failure")
        real_copystat(source, destination, **_kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cfb.shutil, "copystat", flaky_copystat)
    monkeypatch.setattr(cfb, "_assert_no_tokens", arm_on_verification)
    with pytest.raises(OSError, match="copystat"):
        rewrite_static(template, static, "/two")

    assert _snapshot(static) == before
    assert _stage_directories(tmp_path) == []


def test_readonly_parent_fails_closed(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    template, _ = _build_template(tmp_path)
    parent = tmp_path / "serve"
    parent.mkdir()
    parent.chmod(0o555)
    static = parent / "static"
    try:
        with pytest.raises(OSError):
            rewrite_static(template, static, "/music")
    finally:
        parent.chmod(0o755)

    assert not static.exists()


def test_boundary_spanning_token_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chunked proof scan must catch tokens split across read chunks."""
    chunk = 4096
    monkeypatch.setattr(cfb, "_SCAN_CHUNK_BYTES", chunk)
    template = tmp_path / "static-template"
    template.mkdir()
    # Token starts 10 bytes before a chunk edge and runs past it.
    spanning = b"x" * (chunk - 10) + _TOKEN + b"tail"
    blob = template / "blob.bin"
    blob.write_bytes(spanning)

    with pytest.raises(StaticRewriteError, match="blob.bin"):
        cfb._assert_no_tokens(template, _TOKEN)

    # Sanity: the same scan passes once the token is genuinely replaced.
    blob.write_bytes(b"x" * (chunk - 10) + b"/music" + b"tail")
    cfb._assert_no_tokens(template, _TOKEN)


# --- swap atomicity ----------------------------------------------------------


def test_swap_restores_previous_tree_if_final_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template, _ = _build_template(tmp_path)
    static = tmp_path / "static"
    rewrite_static(template, static, "/one")
    before = _snapshot(static)

    real_replace = os.replace
    calls = {"count": 0}

    def failing_replace(src: object, dst: object) -> None:
        calls["count"] += 1
        if calls["count"] == 2:  # stage -> static rename after old was moved aside
            raise OSError("simulated final rename failure")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(cfb.os, "replace", failing_replace)
    with pytest.raises(OSError, match="final rename"):
        rewrite_static(template, static, "/two")

    assert _snapshot(static) == before
    assert _stage_directories(tmp_path) == []


# --- CLI surface -------------------------------------------------------------


def test_main_invalid_env_config_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    template, _ = _build_template(tmp_path)
    static = tmp_path / "static"
    monkeypatch.setenv("BASE_PATH", "bad value")

    rc = main(["--template-root", str(template), "--static-root", str(static)])

    assert rc == 1
    captured = capsys.readouterr()
    assert "FATAL" in captured.err
    assert not static.exists()


def test_main_rewrite_failure_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template = tmp_path / "static-template"
    template.mkdir()  # no index.html -> StaticRewriteError branch

    rc = main(
        [
            "--template-root",
            str(template),
            "--static-root",
            str(tmp_path / "static"),
            "--base-path",
            "/music",
        ]
    )

    assert rc == 2
    assert "FATAL" in capsys.readouterr().err


def test_main_success_prints_init_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    template, _ = _build_template(tmp_path)
    monkeypatch.delenv("BASE_PATH", raising=False)

    rc = main(
        ["--template-root", str(template), "--static-root", str(tmp_path / "static")]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("[init]")
    assert _TOKEN not in (tmp_path / "static" / "index.html").read_bytes()
