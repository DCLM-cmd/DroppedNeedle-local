"""Acquisition quality evidence, immutable policy snapshots, and decisions.

Source-neutral domain structs shared by every acquisition surface (Soulseek,
Usenet, Free Music, local verification, task/review projections). Pure data +
closed enums ONLY: classification/comparison logic lives in
``services/native/acquisition/quality.py``; nothing here performs I/O or reads
settings. Persisted across our HTTP boundary, so everything subclasses
``AppStruct``.

Semantics governed by ``.dev-notes/Plans/Acquisition.md`` (owner-signed);
field names mirror the frontend hand-mirrored types in
``frontend/src/lib/types.ts`` - change both together.
"""

import enum

import msgspec

from infrastructure.msgspec_fastapi import AppStruct

# Schema version of the persisted snapshot shape. Bump ONLY on a breaking
# change to AcquisitionQualitySnapshot's wire/persisted layout.
SNAPSHOT_SCHEMA_VERSION = 1


class CodecFamily(enum.Enum):
    """The proven (or unknown) codec family of one unit of audio evidence."""
    LOSSY = "lossy"
    LOSSLESS = "lossless"
    UNKNOWN = "unknown"


class EvidenceCertainty(enum.Enum):
    """How much the evidence chain can be trusted. Ordering matters:
    ``exact > partial > inferred > unknown``."""

    EXACT = "exact"
    PARTIAL = "partial"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class EvidenceProvenance(enum.Enum):
    """WHERE the evidence came from. Drives the trust ceiling applied upstream."""

    LOCAL_PROBE = "local_probe"
    SOURCE_METADATA = "source_metadata"
    ARCHIVE_FORMAT = "archive_format"
    RELEASE_TITLE = "release_title"
    CATEGORY = "category"
    FORMAT_ONLY = "format_only"
    NONE = "none"


class UnknownQualityBehavior(enum.Enum):
    """Global rule for family-unknown evidence (codec cannot be determined)."""

    REJECT = "reject"
    REVIEW = "review"
    ALLOW_AS_FALLBACK = "allow_as_fallback"


class SourceSelectionMode(enum.Enum):
    """How the orchestrator walks sources: configured order first (default),
    or earliest global preference step across concurrently searched sources."""

    SOURCE_FIRST = "source_first"
    QUALITY_FIRST = "quality_first"


class SnapshotOrigin(enum.Enum):
    """Why this snapshot exists: captured live at creation, written once by the
    startup migration for pre-existing rows, or pinned by an intentional manual
    override flow."""

    GLOBAL_POLICY = "global_policy"
    LEGACY_MIGRATION = "legacy_migration"
    MANUAL_OVERRIDE = "manual_override"


class QualityReason(str, enum.Enum):
    """Stable reason codes (spec "Every decision has structured reasons").
    EXACTLY these seventeen; frontend maps each to consistent copy."""

    PREFERRED_TIER = "preferred_tier"
    FALLBACK_TIER = "fallback_tier"
    PREFERRED_LOSSY_BITRATE = "preferred_lossy_bitrate"
    LOSSY_BITRATE_BELOW_MINIMUM = "lossy_bitrate_below_minimum"
    LOSSY_BITRATE_ABOVE_MAXIMUM = "lossy_bitrate_above_maximum"
    LOSSY_BITRATE_UNKNOWN = "lossy_bitrate_unknown"
    PREFERRED_LOSSLESS_RESOLUTION = "preferred_lossless_resolution"
    LOSSLESS_RESOLUTION_ABOVE_MAXIMUM = "lossless_resolution_above_maximum"
    LOSSLESS_RESOLUTION_PARTIAL = "lossless_resolution_partial"
    LOSSLESS_RESOLUTION_UNKNOWN = "lossless_resolution_unknown"
    QUALITY_INFERRED_FROM_TITLE = "quality_inferred_from_title"
    QUALITY_INFERRED_FROM_CATEGORY = "quality_inferred_from_category"
    FORMAT_NOT_IMPORTABLE = "format_not_importable"
    FORMAT_OUTSIDE_POLICY = "format_outside_policy"
    OUTSIDE_GLOBAL_PREFERENCE = "outside_global_preference"
    POST_DOWNLOAD_QUALITY_MISMATCH = "post_download_quality_mismatch"
    MANUAL_QUALITY_OVERRIDE = "manual_quality_override"


# Known-unimportable audio containers (DSD families; anything else unsupported is
# caught downstream by the importer's own suffix gate). A declared DSD release must
# not spend bandwidth even when a noisy title claims "lossless".
NOT_IMPORTABLE_EXTENSIONS: frozenset[str] = frozenset({"dsf", "dff"})
# Fixed 6-step lossless detail ladder INSIDE the canonical ``lossless`` tier
# (never new canonical tiers). Steps ascend in technical detail:
#   0 "cd"      : at most 16-bit / 48 kHz
#   1 "24_48"   : up to 24-bit / 48 kHz
#   2 "24_96"   : up to 24-bit / 96 kHz
#   3 "24_192"  : up to 24-bit / 192 kHz
#   4 "hi_res"  : above 24-bit or above 192 kHz
#   5 "partial" : resolution partial/unknown
LOSSLESS_DETAIL_STEPS: tuple[str, ...] = (
    "cd",
    "24_48",
    "24_96",
    "24_192",
    "hi_res",
    "partial",
)

# Legacy comparator floor used ONLY as a ranking stand-in for an ABSENT axis
# (a known axis may prove a cap rejection but cannot prove a positive step).
_LEGACY_FLOOR_BIT_DEPTH = 16
_LEGACY_FLOOR_SAMPLE_RATE = 44100


def detail_comparator_axes(
    bit_depth: int | None, sample_rate_hz: int | None
) -> tuple[int, int]:
    """``(depth, rate)`` comparison axes with the legacy floor filled in for an
    ABSENT axis (preserves migrated Soulseek FLAC ordering; labelled partial).
    """
    return (
        bit_depth if bit_depth is not None else _LEGACY_FLOOR_BIT_DEPTH,
        sample_rate_hz if sample_rate_hz is not None else _LEGACY_FLOOR_SAMPLE_RATE,
    )




def lossless_detail_step(
    bit_depth: int | None, sample_rate_hz: int | None
) -> int:
    """Map trusted depth/rate onto the fixed ladder. Both axes present -> steps
    0-4 by ascending order match; either axis absent -> the ``partial`` step.
    Requires Hz-normalised integers (khz text handling belongs to parsing code).
    """
    if bit_depth is None or sample_rate_hz is None:
        return 5
    if bit_depth <= _LEGACY_FLOOR_BIT_DEPTH and sample_rate_hz <= 48000:
        return 0
    if bit_depth <= 24 and sample_rate_hz <= 48000:
        return 1
    if bit_depth <= 24 and sample_rate_hz <= 96000:
        return 2
    if bit_depth <= 24 and sample_rate_hz <= 192000:
        return 3
    return 4


    """``(depth, rate)`` comparison axes with the legacy floor filled in for an
    ABSENT axis (preserves migrated Soulseek FLAC ordering; labelled partial).
    """
    return (
        bit_depth if bit_depth is not None else _LEGACY_FLOOR_BIT_DEPTH,
        sample_rate_hz if sample_rate_hz is not None else _LEGACY_FLOOR_SAMPLE_RATE,
    )


class AudioQualityEvidence(AppStruct):
    """Measured/inferred quality facts about ONE downloaded-or-proposed unit of
    audio: a candidate folder (aggregate over its audio files), a track file, or
    a probed download. Aggregation policy: a folder is as good as its WORST
    audio file; ``mixed_*`` flags record heterogeneity instead of averaging."""

    # Canonical effective extension/container, lowercase, no dot ("flac", "mp3",
    # "m4a"). Empty string when nothing was determinable.
    extension: str = ""
    codec_family: CodecFamily = CodecFamily.UNKNOWN
    # kbps when meaningful for the family (None for lossless bitrates - FLAC
    # bitrate varies with compression and is NOT a fidelity axis).
    bitrate_kbps: int | None = None
    bit_depth: int | None = None
    sample_rate_hz: int | None = None
    total_bytes: int | None = None
    audio_file_count: int = 0
    mixed_format: bool = False
    mixed_quality: bool = False
    certainty: EvidenceCertainty = EvidenceCertainty.UNKNOWN
    provenance: EvidenceProvenance = EvidenceProvenance.NONE


class AcquisitionQualitySnapshot(AppStruct):
    """The immutable global-policy snapshot governing one task/search. Created
    once at task/search creation (or by the migration/backfill for existing
    rows) and persisted with the row; scorers receive it per call. LATER
    SETTINGS SAVES NEVER MUTATE A STORED SNAPSHOT - ``restart-with-current-
    policy`` is the explicit refresh path."""

    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    # Stable sha256 (hex) over the canonical JSON of the policy inputs below
    # (order/ranges/targets/caps/rules - computed in quality.build_snapshot;
    # excludes hash itself, origin, and the derived summary sentence).
    snapshot_hash: str = ""
    # Every canonical tier in [quality_min, quality_max], HIGHEST->LOWEST.
    quality_preference_order: list[str] = []
    lossy_target_kbps: int | None = None
    lossy_min_bitrate_kbps: int | None = None
    lossy_max_bitrate_kbps: int | None = None
    # One of the lossless detail step keys before "partial": cd|24_48|24_96|
    # 24_192|highest.
    lossless_preference: str = "highest"
    lossless_max_bit_depth: int | None = None
    lossless_max_sample_rate_hz: int | None = None
    flac_mp3_only: bool = True
    unknown_quality_behavior: str = UnknownQualityBehavior.ALLOW_AS_FALLBACK.value
    source_selection_mode: str = SourceSelectionMode.SOURCE_FIRST.value
    # Backend-composed human contract shown verbatim by the frontend.
    summary: str = ""
    origin: str = SnapshotOrigin.GLOBAL_POLICY.value


class QualityDecision(AppStruct):
    """The evaluator's structured verdict for one piece of evidence under one
    snapshot. ``preference_step`` indexes ``snapshot.quality_preference_order``
    (0 = preferred tier; None = outside policy / rejected-by-rule). Reasons are
    ordered most-significant first; ``summary`` is composed user-facing copy."""

    eligible: bool = False
    disposition: str = ""  # ""|reject|review|fallback|eligible
    tier: str | None = None
    preference_step: int | None = None
    lossless_detail_step: int | None = None
    evidence: AudioQualityEvidence = msgspec.field(default_factory=AudioQualityEvidence)
    reasons: list[QualityReason] = msgspec.field(default_factory=list)
    summary: str = ""
