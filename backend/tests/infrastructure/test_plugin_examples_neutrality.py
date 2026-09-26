"""Example plugins stay neutral: no real indexer/tracker/Soulseek addresses.

Mirrors backend/tests/repositories/test_no_bundled_sources.py (which scans
shipped backend + frontend source but not examples/): the same forbidden
domains must not appear anywhere under examples/plugins/, and every http(s)
URL the examples mention must point at a fictional host (.test / .example /
localhost) so copying an example can never reach a real third party.
"""

import re
from pathlib import Path

from tests.repositories.test_no_bundled_sources import _FORBIDDEN_DOMAINS

EXAMPLES = Path(__file__).parent.parent.parent.parent / "examples" / "plugins"

_URL_RE = re.compile(r"https?://([^/\s'\"<>]+)", re.IGNORECASE)
_SKIP_DIRS = {"__pycache__", ".venv", "node_modules"}
_SKIP_SUFFIXES = {".pyc"}
_BINARY_SNIFF_BYTES = 8192


def _example_files() -> list[Path]:
    return [
        path
        for path in sorted(EXAMPLES.rglob("*"))
        if path.is_file()
        and not any(part in _SKIP_DIRS for part in path.parts)
        and path.suffix not in _SKIP_SUFFIXES
    ]


def _text_of(path: Path) -> str | None:
    raw = path.read_bytes()
    if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
        return None
    return raw.decode("utf-8", errors="ignore")


def _host_allowed(host: str) -> bool:
    candidate = host.strip().lower().split("@")[-1].split(":")[0].strip("[]")
    if candidate in ("localhost", "::1") or candidate.startswith("127."):
        return True
    return candidate.endswith(".test") or candidate.endswith(".example")


def test_examples_carry_no_forbidden_indexer_or_tracker_domain():
    offenders: list[str] = []
    for path in _example_files():
        text = _text_of(path)
        if text is None:
            continue
        haystack = text.lower()
        for domain in _FORBIDDEN_DOMAINS:
            if domain in haystack:
                offenders.append(f"{path.relative_to(EXAMPLES)}: {domain!r}")
    assert offenders == [], (
        "Example plugins must not reference real indexers, trackers, or Soulseek servers.\n  "
        + "\n  ".join(offenders)
    )


def test_examples_use_only_fictional_http_hosts():
    offenders: list[str] = []
    for path in _example_files():
        text = _text_of(path)
        if text is None:
            continue
        for match in _URL_RE.finditer(text):
            if not _host_allowed(match.group(1)):
                offenders.append(f"{path.relative_to(EXAMPLES)}: {match.group(0)!r}")
    assert offenders == [], (
        "Example plugins may only link fictional hosts (.test / .example / localhost).\n  "
        + "\n  ".join(offenders)
    )
