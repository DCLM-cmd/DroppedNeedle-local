"""Source-scoped quarantine/identity keys (D8).

The generalised ``download_quarantine`` table keys a blocklisted release by
``(source, identity, release_group_mbid)`` where ``identity`` is a single opaque
string whose encoding is source-specific. Centralised here so the store, the
scorers, and the orchestrator agree byte-for-byte on what a row's key means.

- **soulseek** identity = ``username`` + ``filename`` - preserves exactly the
  old ``(username, filename)`` semantics, so Phase 0 is behaviour-preserving.
- **usenet** identity = normalised ``title`` + size-rounded-to-MB (D8/m4): the
  cross-indexer release identity, NOT the per-indexer ``guid``.
"""

import re
import unicodedata

SOURCE_SOULSEEK = "soulseek"
SOURCE_USENET = "usenet"

SOULSEEK_ID_SEPARATOR = "\x1f"
_UNIT = SOULSEEK_ID_SEPARATOR
_WS = re.compile(r"\s+")


def _canonical_soulseek_part(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\\", "/"))


def soulseek_identity(username: str, filename: str) -> str:
    """Identity of a Soulseek per-file pick, canonicalized for persistence."""
    return (
        f"{_canonical_soulseek_part(username)}{SOULSEEK_ID_SEPARATOR}"
        f"{_canonical_soulseek_part(filename)}"
    )


def canonical_soulseek_identity(identity: str) -> str:
    """Canonicalize an encoded Soulseek identity, including legacy rows."""
    username, separator, filename = identity.partition(SOULSEEK_ID_SEPARATOR)
    if not separator:
        return _canonical_soulseek_part(identity)
    return soulseek_identity(username, filename)


def usenet_identity(title: str, size_bytes: int) -> str:
    """Identity of a Usenet release: normalised title + size-rounded-to-MB.

    Title is lower-cased with whitespace collapsed so trivial spacing/case
    differences across indexers dedup together; size is bucketed to the MB so a
    byte or two of metadata jitter between indexers doesn't split the identity
    (mirrors the cross-indexer dedup key, ``02-…`` §Aggregation)."""
    norm = _WS.sub(" ", title.strip().lower())
    size_mb = size_bytes // (1024 * 1024)
    return f"{norm}{_UNIT}{size_mb}"
