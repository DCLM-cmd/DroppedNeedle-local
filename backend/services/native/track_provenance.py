"""Per-field provenance for library display values (M-06).

A display value is only as strong as its source: file tags (``tag``),
filename/folder parses (``parsed``), index-time fallbacks (``placeholder``),
or nothing at all (``absent``). Persisted column values win when present;
otherwise producers derive the provenance from the row itself so
pre-existing rows keep a backfill path.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from models.identification import TrackProvenance
from services.native.filename_parser import ParsedNames, parse_names_from_path
from services.native.local_album_grouper import grouping_directory

UNKNOWN_ARTIST = "Unknown Artist"


def resolve_provenance(
    persisted: object, fallback: TrackProvenance
) -> TrackProvenance:
    """Precedence rule (load-bearing): a persisted non-``absent`` value wins,
    otherwise the caller-supplied derivation applies."""
    if persisted == "tag":
        return "tag"
    if persisted == "parsed":
        return "parsed"
    if persisted == "placeholder":
        return "placeholder"
    return fallback


def derive_album_provenance(
    raw_value: str | None, display_value: str | None
) -> TrackProvenance:
    """Raw-vs-substituted comparison for album fields: ``tag`` iff the raw
    tag column is non-empty, ``placeholder`` iff a substituted display value
    stands in for it, else ``absent``."""
    if (raw_value or "").strip():
        return "tag"
    if (display_value or "").strip():
        return "placeholder"
    return "absent"


def derive_title_artist_provenance(
    display_title: str | None, display_artist: str | None, stem: str | None
) -> TrackProvenance:
    """Stem/``"Unknown Artist"`` heuristic for the joint title/artist claim:
    there are no ``tag_title``/``tag_artist_name`` columns (and the engine
    never scores track ``artist_name`` on its own), so ``title_provenance``
    carries both and this is the only backfill path. Either placeholder
    signal wins (the safe direction: abstain, never veto); a
    correctly-tagged ``Title.flac`` (stem == tag) misclassifies as
    ``placeholder`` - weaker evidence, never a false veto. The final
    ``tag`` fallthrough is safe by construction: pre-1.5 rows (the only
    rows that ever derive with ``absent`` persisted) predate the indexer
    filename fallback, so their displays are genuine tags or stem
    fallbacks - never parses. Parsed displays always carry explicit
    ``parsed`` provenance from the 1.5 indexer, never ``absent``."""
    title = (display_title or "").strip()
    if not title:
        return "absent"
    if title == UNKNOWN_ARTIST or (display_artist or "").strip() == UNKNOWN_ARTIST:
        return "placeholder"
    if stem is not None and title == stem:
        return "placeholder"
    return "tag"


def track_stem(relative_path: str) -> str:
    """File stem of a posix-style library relative path."""
    return PurePosixPath(relative_path).name.rsplit(".", 1)[0]


def parse_names_for_row(relative_path: str) -> ParsedNames:
    """Filename parse against the folded parent: multi-disc subdir paths
    parse as if the disc folder were transparent, so ``Artist/Album/CD1/01
    - Title.flac`` parses like its ``Artist/Album`` siblings."""
    path = PurePosixPath(relative_path)
    folded = grouping_directory(relative_path)
    if folded not in (".", "") and str(path.parent) != folded:
        return parse_names_from_path(Path(folded) / path.name)
    return parse_names_from_path(Path(relative_path))
