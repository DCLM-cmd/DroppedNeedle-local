"""Rewrite the shipped static frontend for the deployed BASE_PATH.

The Docker image stores the pristine SvelteKit build under ``/app/static-template``
with the literal placeholder ``/__DROPPEDNEEDLE_BASE__`` baked into every internal
absolute URL (see ``frontend/svelte.config.js``). On each container start this
script runs as the unprivileged runtime user and rebuilds the served tree under
the writable cache directory configured by ``DROPPEDNEEDLE_STATIC_DIR``:

1. validate ``BASE_PATH`` through the shared core normalizer - the single strict
   parser used by Settings and the ASGI layer; invalid input never touches disk,
2. delete stale precompressed variants (SvelteKit ships ``.br``/``.gz`` copies of
   the placeholder-era bytes, which cannot be byte-patched reliably),
3. replace every placeholder occurrence with the validated base path via literal
   ``bytes.replace`` - no shell language, no interpolation into command text,
4. prefix every literal ``/fonts/`` URL with that same validated base so locally
   root-hosted source CSS (dev servers load /fonts without any rewriter) becomes
   ``<base>/fonts/`` under a configured deployment - font URLs sitting directly
   behind the placeholder are already fully prefixed by the token substitution,
   so they are never visited twice; an empty base leaves them root-hosted,
5. verify the rewrite happened (at least one placeholder consumed) and that no
   placeholder remains anywhere in the output tree.

All output is assembled in a staging directory next to the target and swapped in
only after verification succeeds, so a failed run leaves the previous tree intact
or nothing behind - it never presents a partial rewrite as complete. Any problem
exits nonzero; ``entrypoint.sh`` fails closed before uvicorn execs.

Token contract: the placeholder string must match ``frontend/svelte.config.js``
byte-for-byte.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# One shared validator with Settings and the ASGI layer.
from core.base_path import BasePathError, normalize_base_path

BASE_PATH_TOKEN = "/__DROPPEDNEEDLE_BASE__"
COMPRESSED_SUFFIXES = (".br", ".gz")
# Source CSS stays root-hosted for local development. Vite emits those URLs
# relative to the built stylesheet, while unbundled text assets retain absolute
# /fonts/ URLs. Startup preserves relative targets and prefixes absolute ones.
FONT_URL_NEEDLE = b"/fonts/"
_FONT_URL_MARKER = b"__DROPPEDNEEDLE_FONT_URL_SLOT__"
_FONT_URL_PAIR_MARKER = b"__DROPPEDNEEDLE_FONT_URL_PAIR__"
_RELATIVE_FONT_URL_MARKER = b"__DROPPEDNEEDLE_RELATIVE_FONT_URL_PAIR__"
# Replacement is confined to known-text assets produced by the SvelteKit build.
# A placeholder found in any other asset means a build misconfiguration and fails
# closed instead of patching binary blobs heuristically.
TEXT_SUFFIXES = frozenset(
    {
        ".html",
        ".js",
        ".css",
        ".mjs",
        ".json",
        ".map",
        ".txt",
        ".xml",
        ".svg",
        ".webmanifest",
    }
)
DEFAULT_TEMPLATE_ROOT = Path("/app/static-template")
DEFAULT_STATIC_ROOT = Path(os.getenv("DROPPEDNEEDLE_STATIC_DIR", "/app/static"))
_STAGE_PREFIX = ".droppedneedle-static-stage-"
_PREVIOUS_PREFIX = ".droppedneedle-static-previous-"
_SCAN_CHUNK_BYTES = 1024 * 1024


class StaticRewriteError(RuntimeError):
    """Raised when the shipped static tree cannot be rewritten safely."""


@dataclass(frozen=True)
class RewriteSummary:
    """Result of one ``rewrite_static`` run."""

    base_path: str
    files_patched: int
    tokens_replaced: int
    fonts_replaced: int
    compressed_variants_removed: int
    orphans_removed: int


def _copy_template(template_root: Path, stage_root: Path) -> None:
    if not template_root.is_dir():
        raise StaticRewriteError(
            f"Pristine static template does not exist: {template_root}. "
            "Do not bind-mount over /app."
        )
    if not (template_root / "index.html").is_file():
        raise StaticRewriteError(
            f"{template_root} does not look like an adapter-static build "
            "(no index.html fallback); refusing to serve it."
        )
    shutil.copytree(template_root, stage_root, dirs_exist_ok=True)


def _remove_precompressed(root: Path) -> int:
    removed = 0
    for directory, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if filename.endswith(COMPRESSED_SUFFIXES):
                (Path(directory) / filename).unlink()
                removed += 1
    return removed


def _iter_files(root: Path):
    for directory, _dirnames, filenames in os.walk(root):
        for filename in sorted(filenames):
            yield Path(directory) / filename


def _read_file(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise StaticRewriteError(f"Cannot read asset {path}: {exc}") from exc


def _rewrite_text_assets(root: Path, base_path: str) -> tuple[int, int, int]:
    """Stamp ``base_path`` onto known text assets.

    Placeholder tokens become ``base_path``. Absolute ``/fonts/`` references
    gain the same prefix, while Vite's ``../fonts/`` targets stay relative to
    the already-based stylesheet location. A token embedded in that relative
    Vite form is removed rather than replaced to prevent ``base/base/fonts``.
    Tokens in non-text assets fail closed.

    Returns ``(files_patched, tokens_replaced, fonts_replaced)``; the last value
    counts absolute font references changed by the dedicated font pass.
    """

    token = BASE_PATH_TOKEN.encode("ascii")
    replacement = base_path.encode("ascii")
    files_patched = 0
    tokens_replaced = 0
    fonts_replaced = 0
    offenders: list[str] = []
    for path in _iter_files(root):
        data = _read_file(path)
        token_count = data.count(token)
        relative_token_font = b"../" + token.removeprefix(b"/") + FONT_URL_NEEDLE
        relative_plain_font = b"../" + FONT_URL_NEEDLE.removeprefix(b"/")
        relative_font_count = data.count(relative_token_font)
        staged = data.replace(relative_token_font, _RELATIVE_FONT_URL_MARKER)
        relative_font_count += staged.count(relative_plain_font)
        staged = staged.replace(relative_plain_font, _RELATIVE_FONT_URL_MARKER)
        if base_path:
            token_font_count = staged.count(token + FONT_URL_NEEDLE)
            font_count = staged.count(FONT_URL_NEEDLE)
            plain_font_count = font_count - token_font_count
        else:
            token_font_count = 0
            font_count = 0
            plain_font_count = 0
        if token_count == 0 and font_count == 0 and relative_font_count == 0:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            offenders.append(str(path.relative_to(root)))
            continue
        if base_path and token_font_count:
            staged = staged.replace(token + FONT_URL_NEEDLE, _FONT_URL_PAIR_MARKER)
        if plain_font_count:
            staged = staged.replace(FONT_URL_NEEDLE, _FONT_URL_MARKER)
            staged = staged.replace(_FONT_URL_MARKER, replacement + FONT_URL_NEEDLE)
        if token_count:
            staged = staged.replace(token, replacement)
        if token_font_count:
            staged = staged.replace(
                _FONT_URL_PAIR_MARKER, replacement + FONT_URL_NEEDLE
            )
        if relative_font_count:
            staged = staged.replace(_RELATIVE_FONT_URL_MARKER, b"../fonts/")
        if (
            _FONT_URL_MARKER in staged
            or _FONT_URL_PAIR_MARKER in staged
            or _RELATIVE_FONT_URL_MARKER in staged
        ):
            raise StaticRewriteError(f"Internal marker survived rewrite of {path}")
        try:
            path.write_bytes(staged)
        except OSError as exc:
            raise StaticRewriteError(f"Cannot rewrite asset {path}: {exc}") from exc
        files_patched += 1
        tokens_replaced += token_count
        fonts_replaced += plain_font_count
    if offenders:
        raise StaticRewriteError(
            f"{BASE_PATH_TOKEN!r} leaked into non-text assets; refusing a heuristic "
            f"patch: {', '.join(sorted(offenders))}"
        )
    return files_patched, tokens_replaced, fonts_replaced


def _assert_no_tokens(root: Path, token: bytes) -> None:
    """Stream every output file proving ``token`` no longer occurs anywhere."""

    overlap = len(token) - 1
    offenders: list[str] = []
    for path in _iter_files(root):
        tail = b""
        found = False
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(_SCAN_CHUNK_BYTES)
                    if not chunk:
                        break
                    haystack = tail + chunk
                    if token in haystack:
                        found = True
                        break
                    tail = haystack[-overlap:]
        except OSError as exc:
            raise StaticRewriteError(
                f"Cannot inspect rewritten asset {path}: {exc}"
            ) from exc
        if found:
            offenders.append(str(path.relative_to(root)))
    if offenders:
        raise StaticRewriteError(
            f"Placeholder survived the rewrite in: {', '.join(offenders[:10])}"
        )


def _clear_orphan_stages(parent: Path) -> int:
    """Drop staging directories left by crashed earlier runs (single writer)."""

    if not parent.is_dir():
        return 0
    removed = 0
    for child in sorted(parent.iterdir()):
        if child.name.startswith(_STAGE_PREFIX) or child.name.startswith(
            _PREVIOUS_PREFIX
        ):
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def rewrite_static(
    template_root: Path, static_root: Path, base_path: str | None
) -> RewriteSummary:
    """Rebuild ``static_root`` from the pristine ``template_root`` for ``base_path``.

    Raises before mutating anything when the base path is invalid or the template
    is unusable; raises (after cleaning the staging tree and restoring any
    previous tree moved aside) when the rewrite cannot be completed or verified.
    The previously served tree survives every pre-swap and swap failure untouched.
    """

    normalized = normalize_base_path(base_path)
    if template_root == static_root:
        raise StaticRewriteError(
            f"Static root and template root must differ ({static_root}); the "
            "template is the immutable input for every restart."
        )
    orphans_removed = _clear_orphan_stages(static_root.parent)
    static_root.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX, dir=static_root.parent))
    previous_root: Path | None = None
    try:
        _copy_template(template_root, stage_root)
        compressed_variants_removed = _remove_precompressed(stage_root)
        files_patched, tokens_replaced, fonts_replaced = _rewrite_text_assets(
            stage_root, normalized
        )
        if tokens_replaced == 0:
            raise StaticRewriteError(
                f"No {BASE_PATH_TOKEN!r} placeholder found in {template_root}; the "
                "image was built without DROPPEDNEEDLE_BASE_PATH_PLACEHOLDER=1 "
                "or from a stale frontend build."
            )
        _assert_no_tokens(stage_root, BASE_PATH_TOKEN.encode("ascii"))
        # Metadata stamping is the last fallible step and must precede the swap:
        # once the served tree is replaced, only best-effort deletion may remain.
        shutil.copystat(template_root, stage_root, follow_symlinks=False)
        if static_root.exists():
            previous_root = Path(
                tempfile.mkdtemp(prefix=_PREVIOUS_PREFIX, dir=static_root.parent)
            )
            # Free the reserved name so the rename below has a nonexistent target.
            previous_root.rmdir()
            os.replace(static_root, previous_root)
        try:
            os.replace(stage_root, static_root)
        except BaseException:
            if previous_root is not None:
                os.replace(previous_root, static_root)
                previous_root = None
            raise
    except BaseException:
        shutil.rmtree(stage_root, ignore_errors=True)
        if previous_root is not None:
            shutil.rmtree(previous_root, ignore_errors=True)
        raise
    if previous_root is not None:
        shutil.rmtree(previous_root, ignore_errors=True)
    return RewriteSummary(
        base_path=normalized,
        files_patched=files_patched,
        tokens_replaced=tokens_replaced,
        fonts_replaced=fonts_replaced,
        compressed_variants_removed=compressed_variants_removed,
        orphans_removed=orphans_removed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild the runtime static frontend from its pristine template, "
            "substituting the BASE_PATH placeholder with the deployment base path."
        )
    )
    parser.add_argument(
        "--template-root",
        type=Path,
        default=DEFAULT_TEMPLATE_ROOT,
        help="Pristine SvelteKit build shipped in the image (default %(default)s).",
    )
    parser.add_argument(
        "--static-root",
        type=Path,
        default=DEFAULT_STATIC_ROOT,
        help="Runtime static tree served by uvicorn (default %(default)s).",
    )
    parser.add_argument(
        "--base-path",
        default=os.environ.get("BASE_PATH", ""),
        help="Deployment base path (default: $BASE_PATH, empty means domain root).",
    )
    args = parser.parse_args(argv)
    try:
        summary = rewrite_static(
            template_root=args.template_root,
            static_root=args.static_root,
            base_path=args.base_path,
        )
    except BasePathError as exc:
        print(f"[init] FATAL: Invalid BASE_PATH: {exc}", file=sys.stderr)
        return 1
    except (StaticRewriteError, OSError) as exc:
        print(f"[init] FATAL: Static BASE_PATH rewrite failed: {exc}", file=sys.stderr)
        return 2
    served = summary.base_path or "/ (domain root)"
    print(
        f"[init] Static rewrite complete: base={served}, "
        f"files_patched={summary.files_patched}, "
        f"tokens_replaced={summary.tokens_replaced}, "
        f"fonts_replaced={summary.fonts_replaced}, "
        f"precompressed_removed={summary.compressed_variants_removed}, "
        f"orphans_removed={summary.orphans_removed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
