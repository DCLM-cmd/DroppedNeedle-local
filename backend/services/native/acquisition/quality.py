"""Pure acquisition-quality classification/comparison (no I/O).

Every scorer, the orchestrator's stored-snapshot recheck, Free Music ranking,
the local probe's mismatch comparison, and task/review projections consume THIS
module; nothing here reads settings files, stores, or the network. Runtime has
NO import of the API ``DownloadPolicySettings`` schema (breaking an import
cycle api.schemas <- services): ``build_snapshot`` duck-types its ``policy``
argument against ``DownloadPolicySettings``' quality fields, type-hinted under
``TYPE_CHECKING``.

Semantics governed by ``.dev-notes/Plans/Acquisition.md``:
- identity/importability/quota gates stay OUT of here (they precede quality);
- a mixed album takes the worst policy verdict of any audio file;
- proven-lossy-without-bitrate keeps the legacy canonical ``low`` projection
  (plus ``lossy_bitrate_unknown``) and is NEVER promoted through the
  family-unknown rule;
- lossless caps reject only PROVABLE violations (a known axis exceeding the
  cap); one absent axis ranks via the legacy comparator floor and stays
  labelled partial.
"""

import hashlib
import json
import re
from typing import TYPE_CHECKING

from models.acquisition_quality import (
    LOSSLESS_DETAIL_STEPS,
    NOT_IMPORTABLE_EXTENSIONS,
    SNAPSHOT_SCHEMA_VERSION,
    AudioQualityEvidence,
    AcquisitionQualitySnapshot,
    CodecFamily,
    EvidenceCertainty,
    EvidenceProvenance,
    QualityDecision,
    QualityReason,
    SnapshotOrigin,
    UnknownQualityBehavior,
    detail_comparator_axes,
    lossless_detail_step,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from api.v1.schemas.settings import DownloadPolicySettings

# Canonical five tiers, best -> worst (mirrors quality_tiers.TIER_KEYS; kept
# local so this module depends on models only, never on production ranking).
_TIER_KEYS_BEST_FIRST: tuple[str, ...] = (
    "lossless",
    "mp3_320",
    "mp3_256",
    "mp3_192",
    "low",
)

CERTAINTY_RANK: dict[EvidenceCertainty, int] = {
    EvidenceCertainty.EXACT: 3,
    EvidenceCertainty.PARTIAL: 2,
    EvidenceCertainty.INFERRED: 1,
    EvidenceCertainty.UNKNOWN: 0,
}

# Provenance trust ladder, lowest -> highest (drives evaluate_worst's merge).
_PROVENANCE_ORDER: tuple[EvidenceProvenance, ...] = (
    EvidenceProvenance.NONE,
    EvidenceProvenance.FORMAT_ONLY,
    EvidenceProvenance.CATEGORY,
    EvidenceProvenance.RELEASE_TITLE,
    EvidenceProvenance.ARCHIVE_FORMAT,
    EvidenceProvenance.SOURCE_METADATA,
    EvidenceProvenance.LOCAL_PROBE,
)

_LOSSY_BANDS: tuple[tuple[int, str], ...] = (
    (320, "mp3_320"),
    (256, "mp3_256"),
    (192, "mp3_192"),
)


def derive_default_order(quality_min: str, quality_max: str) -> list[str]:
    """Existing-install migration shape: every accepted tier HIGHEST->LOWEST."""
    lo = _TIER_KEYS_BEST_FIRST.index(quality_max)
    hi = _TIER_KEYS_BEST_FIRST.index(quality_min)
    return list(_TIER_KEYS_BEST_FIRST[lo : hi + 1])


def normalize_order(order: list[str], quality_min: str, quality_max: str) -> list[str]:
    """Validate a submitted order: exactly the accepted contiguous range, once
    each, endpoints matching ``quality_max`` first and ``quality_min`` last
    (inclusion stays the min/max range; ordering cannot add/remove tiers).
    Raises ValueError - the caller surfaces a 400; never silently clamps."""
    expected = derive_default_order(quality_min, quality_max)
    if sorted(order) != sorted(expected) or len(order) != len(expected):
        raise ValueError(
            "quality_preference_order must contain exactly the accepted tiers "
            f"{expected} (min={quality_min}, max={quality_max}), got {order}"
        )
    # Order[0] is the MOST-PREFERRED tier - any permutation of the accepted
    # set is valid (e.g. '192-first' presets start at the lowest band).
    return list(order)


def _tier_label(key: str) -> str:
    return {
        "lossless": "lossless",
        "mp3_320": "lossy 320 kbps",
        "mp3_256": "lossy 256-319 kbps",
        "mp3_192": "lossy 192-255 kbps",
        "low": "lossy below 192 kbps",
    }[key]


def compose_summary(snapshot: AcquisitionQualitySnapshot) -> str:
    """The saved product contract, returned by the backend and displayed by the
    frontend verbatim (spec UX direction block)."""
    order = snapshot.quality_preference_order
    if not order:
        return "No quality preference configured."
    sentence = f"Try {_tier_label(order[0])}"
    for tier in order[1:]:
        sentence += f", then {_tier_label(tier)}"
    sentence += "."
    pref = snapshot.lossless_preference
    if pref != "highest" and "lossless" in order and pref in LOSSLESS_DETAIL_STEPS:
        detail = {
            "cd": "CD-quality (16-bit/48 kHz)",
            "24_48": "up-to-24-bit/48 kHz",
            "24_96": "up-to-24-bit/96 kHz",
            "24_192": "up-to-24-bit/192 kHz",
        }.get(pref)
        if detail:
            sentence += f" Lossless prefers {detail} copies."
    cap_bits: list[str] = []
    if snapshot.lossless_max_bit_depth is not None:
        cap_bits.append(f"{snapshot.lossless_max_bit_depth}-bit maximum bit depth")
    if snapshot.lossless_max_sample_rate_hz is not None:
        cap_bits.append(
            f"{round(snapshot.lossless_max_sample_rate_hz / 1000)} kHz maximum sample rate"
        )
    if cap_bits:
        sentence += " Never acquire above " + " or ".join(cap_bits) + "."
    unknown = snapshot.unknown_quality_behavior
    if unknown == UnknownQualityBehavior.REVIEW.value:
        sentence += " Never acquire unknown-quality audio automatically."
    elif unknown == UnknownQualityBehavior.REJECT.value:
        sentence += " Unknown-quality copies are excluded."
    else:
        sentence += " Unknown-quality copies are a last resort."
    return sentence


def snapshot_policy_hash(snapshot: AcquisitionQualitySnapshot) -> str:
    """Stable sha256 over the normalized POLICY INPUTS only - excludes the hash
    itself, origin, and the derived summary so wording changes can't churn ids."""
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "quality_preference_order": snapshot.quality_preference_order,
        "lossy_target_kbps": snapshot.lossy_target_kbps,
        "lossy_min_bitrate_kbps": snapshot.lossy_min_bitrate_kbps,
        "lossy_max_bitrate_kbps": snapshot.lossy_max_bitrate_kbps,
        "lossless_preference": snapshot.lossless_preference,
        "lossless_max_bit_depth": snapshot.lossless_max_bit_depth,
        "lossless_max_sample_rate_hz": snapshot.lossless_max_sample_rate_hz,
        "flac_mp3_only": snapshot.flac_mp3_only,
        "unknown_quality_behavior": snapshot.unknown_quality_behavior,
        "source_selection_mode": snapshot.source_selection_mode,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_snapshot(policy: "DownloadPolicySettings") -> AcquisitionQualitySnapshot:
    """Normalize the live policy into the immutable creation-time snapshot.
    An EMPTY ``quality_preference_order`` derives the existing-install migration
    order (highest->lowest across the accepted range); a populated order was
    validated at save time via :func:`normalize_order`."""
    submitted = getattr(policy, "quality_preference_order", None)
    order = (
        list(submitted)
        if submitted
        else derive_default_order(policy.quality_min, policy.quality_max)
    )
    snapshot = AcquisitionQualitySnapshot(
        quality_preference_order=order,
        lossy_target_kbps=policy.preferred_lossy_bitrate_kbps,
        lossy_min_bitrate_kbps=policy.lossy_min_bitrate_kbps,
        lossy_max_bitrate_kbps=policy.lossy_max_bitrate_kbps,
        lossless_preference=policy.lossless_preference,
        lossless_max_bit_depth=policy.lossless_max_bit_depth,
        lossless_max_sample_rate_hz=policy.lossless_max_sample_rate_hz,
        flac_mp3_only=policy.flac_mp3_only,
        unknown_quality_behavior=policy.unknown_quality_behavior,
        source_selection_mode=policy.source_selection_mode,
    )
    snapshot.snapshot_hash = snapshot_policy_hash(snapshot)
    snapshot.summary = compose_summary(snapshot)
    return snapshot


def migration_snapshot(policy: "DownloadPolicySettings") -> AcquisitionQualitySnapshot:
    """Same normalization, tagged ``legacy_migration`` for the startup backfill."""
    snapshot = build_snapshot(policy)
    snapshot.origin = SnapshotOrigin.LEGACY_MIGRATION.value
    return snapshot


def project_canonical_tier(evidence: AudioQualityEvidence) -> str | None:
    """Canonical five-tier projection. Returns None ONLY for family-unknown
    evidence (which never becomes a fake tier). Proven lossy WITHOUT a bitrate
    keeps the legacy ``low`` projection (spec: never promoted through the
    family-unknown rule)."""
    family = evidence.codec_family
    if family is CodecFamily.LOSSLESS:
        return "lossless"
    if family is CodecFamily.LOSSY:
        rate = evidence.bitrate_kbps
        if rate is None:
            return "low"
        for floor, key in _LOSSY_BANDS:
            if rate >= floor:
                return key
        return "low"
    return None


def preference_step_from_tier(
    snapshot: AcquisitionQualitySnapshot, tier: str
) -> int | None:
    """0-based index in the snapshot order; None when outside policy."""
    try:
        return snapshot.quality_preference_order.index(tier)
    except ValueError:
        return None


def lossless_target_index(snapshot: AcquisitionQualitySnapshot) -> int | None:
    """Ladder index of the configured target resolution; None under ``highest``
    (which keeps today's descending hi-res-first order untouched)."""
    pref = snapshot.lossless_preference
    if pref == "highest":
        return None
    try:
        return LOSSLESS_DETAIL_STEPS.index(pref)
    except ValueError:
        return None


def lossless_rank_key(
    snapshot: AcquisitionQualitySnapshot, evidence: AudioQualityEvidence
) -> tuple:
    """Within-``lossless`` comparator under TARGET modes: candidates on the
    configured detail step rank first, then successively more-detailed known
    steps, then lesser-known steps, then partial/unknown - i.e. the fixed
    ladder walked upward from the target. Under ``highest`` (None target) this
    returns the legacy descending ``(min-depth, min-rate)`` axes unchanged."""
    target_idx = lossless_target_index(snapshot)
    step = lossless_detail_step(evidence.bit_depth, evidence.sample_rate_hz)
    partial_step = len(LOSSLESS_DETAIL_STEPS) - 1
    if target_idx is None:
        return (0,) + tuple(
            -axis
            for axis in detail_comparator_axes(evidence.bit_depth, evidence.sample_rate_hz)
        )
    if step >= partial_step:
        # partial/unknown resolution sorts dead last among lossless candidates
        return (1, partial_step, 0)
    upward = [i for i in range(target_idx, partial_step)] + [
        i for i in range(0, target_idx)
    ]
    return (0, upward.index(step), 0)


def lossy_target_distance(
    snapshot: AcquisitionQualitySnapshot, evidence: AudioQualityEvidence
) -> int | None:
    """|distance to the preferred lossy bitrate| for ONE file (callers fold the
    per-file max across a folder). None when not provably lossy, bitrate
    unknown, or no target configured."""
    if (
        evidence.codec_family is not CodecFamily.LOSSY
        or evidence.bitrate_kbps is None
        or snapshot.lossy_target_kbps is None
    ):
        return None
    return abs(evidence.bitrate_kbps - snapshot.lossy_target_kbps)


# Worst-first ranks used to fold folder decisions.
_NOT_IMPORTABLE_RANK = 100
_REJECTED_RANK = 90
_NEEDS_REVIEW_RANK = 80


def _certainty_floor(certainties):
    return min(certainties, key=lambda c: CERTAINTY_RANK[c])


def evaluate(
    snapshot: AcquisitionQualitySnapshot, evidence: AudioQualityEvidence
) -> QualityDecision:
    """Full eligibility + preference evaluation of ONE unit of evidence."""
    reasons: list[QualityReason] = []

    ext = (evidence.extension or "").lower()
    if ext in NOT_IMPORTABLE_EXTENSIONS:
        reasons.insert(0, QualityReason.FORMAT_NOT_IMPORTABLE)
        return QualityDecision(
            eligible=False,
            disposition="not_importable",
            evidence=evidence,
            reasons=reasons,
            summary=f"{ext.upper()} is not an importable audio format.",
        )

    family = evidence.codec_family
    if family is CodecFamily.UNKNOWN:
        rule = snapshot.unknown_quality_behavior
        if rule == UnknownQualityBehavior.REJECT.value:
            return QualityDecision(
                eligible=False,
                disposition="unknown_rejected",
                evidence=evidence,
                reasons=[QualityReason.OUTSIDE_GLOBAL_PREFERENCE],
                summary=(
                    "Quality could not be determined; the server policy "
                    "excludes it."
                ),
            )
        if rule == UnknownQualityBehavior.REVIEW.value:
            return QualityDecision(
                eligible=False,
                disposition="needs_review",
                evidence=evidence,
                summary=(
                    "Only unknown-resolution copies were found - review required."
                ),
            )
        # allow_as_fallback: strictly AFTER every known eligible step.
        return QualityDecision(
            eligible=True,
            disposition="fallback",
            preference_step=len(snapshot.quality_preference_order),
            evidence=evidence,
            reasons=[QualityReason.OUTSIDE_GLOBAL_PREFERENCE],
            summary="Unknown-quality copy held as a last-resort fallback.",
        )

    tier = project_canonical_tier(evidence)
    # Proven lossy without a reported bitrate ALWAYS carries this display
    # code - even when its legacy ``low`` projection lands outside the range.
    if (
        family is CodecFamily.LOSSY
        and evidence.bitrate_kbps is None
    ):
        reasons.append(QualityReason.LOSSY_BITRATE_UNKNOWN)

    step = preference_step_from_tier(snapshot, tier)
    if step is None:
        return QualityDecision(
            eligible=False,
            disposition="outside_policy",
            tier=tier,
            evidence=evidence,
            reasons=reasons or [QualityReason.OUTSIDE_GLOBAL_PREFERENCE],
            summary=f"{_tier_label(tier).capitalize()} is outside the accepted quality.",
        )

    detail = None
    if tier == "lossless":
        detail = lossless_detail_step(evidence.bit_depth, evidence.sample_rate_hz)
        depth_known = evidence.bit_depth is not None
        rate_known = evidence.sample_rate_hz is not None
        if depth_known != rate_known:
            reasons.append(QualityReason.LOSSLESS_RESOLUTION_PARTIAL)
        elif not depth_known and not rate_known:
            reasons.append(QualityReason.LOSSLESS_RESOLUTION_UNKNOWN)
        # Caps reject only PROVABLE violations (a KNOWN axis exceeding the cap).
        depth_violation = (
            snapshot.lossless_max_bit_depth is not None
            and depth_known
            and evidence.bit_depth > snapshot.lossless_max_bit_depth
        )
        rate_violation = (
            snapshot.lossless_max_sample_rate_hz is not None
            and rate_known
            and evidence.sample_rate_hz > snapshot.lossless_max_sample_rate_hz
        )
        if depth_violation or rate_violation:
            reasons.insert(0, QualityReason.LOSSLESS_RESOLUTION_ABOVE_MAXIMUM)
            bits: list[str] = []
            if depth_violation:
                bits.append(f"{evidence.bit_depth}-bit")
            if rate_violation:
                bits.append(f"{evidence.sample_rate_hz} Hz")
            return QualityDecision(
                eligible=False,
                disposition="outside_policy",
                tier=tier,
                lossless_detail_step=detail,
                evidence=evidence,
                reasons=reasons,
                summary=(
                    "Hi-res copy exceeds the server limit (" + " / ".join(bits) + ")."
                ),
            )
        target_idx = lossless_target_index(snapshot)
        claimed_exact = detail < len(LOSSLESS_DETAIL_STEPS) - 1 and all(
            r
            not in (
                QualityReason.LOSSLESS_RESOLUTION_PARTIAL,
                QualityReason.LOSSLESS_RESOLUTION_UNKNOWN,
            )
            for r in reasons
        )
        if target_idx is not None and claimed_exact and detail == target_idx:
            reasons.append(QualityReason.PREFERRED_LOSSLESS_RESOLUTION)

    if tier != "lossless" and evidence.bitrate_kbps is not None:
        if (
            snapshot.lossy_min_bitrate_kbps is not None
            and evidence.bitrate_kbps < snapshot.lossy_min_bitrate_kbps
        ):
            reasons.insert(0, QualityReason.LOSSY_BITRATE_BELOW_MINIMUM)
            return QualityDecision(
                eligible=False,
                disposition="outside_policy",
                tier=tier,
                evidence=evidence,
                reasons=reasons,
                summary=(
                    f"{evidence.bitrate_kbps} kbps is below the server minimum "
                    f"({snapshot.lossy_min_bitrate_kbps} kbps)."
                ),
            )
        if (
            snapshot.lossy_max_bitrate_kbps is not None
            and evidence.bitrate_kbps > snapshot.lossy_max_bitrate_kbps
        ):
            reasons.insert(0, QualityReason.LOSSY_BITRATE_ABOVE_MAXIMUM)
            return QualityDecision(
                eligible=False,
                disposition="outside_policy",
                tier=tier,
                evidence=evidence,
                reasons=reasons,
                summary=(
                    f"{evidence.bitrate_kbps} kbps is above the server maximum "
                    f"({snapshot.lossy_max_bitrate_kbps} kbps)."
                ),
            )
        if (
            snapshot.lossy_target_kbps is not None
            and evidence.bitrate_kbps == snapshot.lossy_target_kbps
        ):
            reasons.append(QualityReason.PREFERRED_LOSSY_BITRATE)

    if evidence.provenance is EvidenceProvenance.RELEASE_TITLE:
        reasons.append(QualityReason.QUALITY_INFERRED_FROM_TITLE)
    elif evidence.provenance is EvidenceProvenance.CATEGORY:
        reasons.append(QualityReason.QUALITY_INFERRED_FROM_CATEGORY)

    if step == 0:
        reasons.insert(0, QualityReason.PREFERRED_TIER)
    else:
        reasons.insert(0, QualityReason.FALLBACK_TIER)

    label_bits: list[str] = [_tier_label(tier)]
    if tier != "lossless" and evidence.bitrate_kbps is not None:
        label_bits.append(f"{evidence.bitrate_kbps} kbps")
    if tier == "lossless" and evidence.bit_depth is not None:
        rate = evidence.sample_rate_hz
        label_bits.append(
            f"{evidence.bit_depth}-bit/{round(rate / 1000)} kHz"
            if rate
            else f"{evidence.bit_depth}-bit"
        )
    return QualityDecision(
        eligible=True,
        disposition="preferred" if step == 0 else "fallback",
        tier=tier,
        preference_step=step,
        lossless_detail_step=detail,
        evidence=evidence,
        reasons=reasons,
        summary=", ".join(label_bits)
        + ("" if step == 0 else f" - fallback {step}")
        + ".",
    )


def _decision_rank(decision: QualityDecision) -> int:
    if decision.disposition == "not_importable":
        return _NOT_IMPORTABLE_RANK
    if decision.eligible:
        step = decision.preference_step
        return 10 + (step if step is not None else 9999)
    if decision.disposition == "needs_review":
        return _NEEDS_REVIEW_RANK
    return _REJECTED_RANK


def evaluate_worst(
    snapshot: AcquisitionQualitySnapshot, evidences: list[AudioQualityEvidence]
) -> QualityDecision:
    """Fold per-file decisions into ONE folder verdict: the whole folder is
    downloaded, so it takes the WORST policy outcome of any audio file
    (not-importable/rejected beat review beat higher step indices).
    Heterogeneity lands on the merged evidence's mixed_* flags; counts and byte
    totals accumulate; certainty floors to the weakest input; provenance rises
    to the strongest."""
    if not evidences:
        raise ValueError("evaluate_worst requires at least one evidence item")
    decisions = [evaluate(snapshot, ev) for ev in evidences]
    worst = max(decisions, key=_decision_rank)

    extensions = {e.extension.lower() for e in evidences if e.extension}
    families = {project_canonical_tier(e) or e.codec_family.value for e in evidences}
    depths = [e.bit_depth for e in evidences if e.bit_depth is not None]
    rates = [e.sample_rate_hz for e in evidences if e.sample_rate_hz is not None]
    merged_evidence = AudioQualityEvidence(
        extension=worst.evidence.extension,
        codec_family=worst.evidence.codec_family,
        bitrate_kbps=None
        if any(
            e.codec_family is CodecFamily.LOSSY and e.bitrate_kbps is None
            for e in evidences
        )
        else worst.evidence.bitrate_kbps,
        bit_depth=min(depths) if depths else None,
        sample_rate_hz=min(rates) if rates else None,
        total_bytes=sum((e.total_bytes or 0) for e in evidences) or None,
        audio_file_count=sum(e.audio_file_count or 1 for e in evidences),
        mixed_format=len(extensions) > 1 or any(e.mixed_format for e in evidences),
        mixed_quality=len(families) > 1 or any(e.mixed_quality for e in evidences),
        certainty=_certainty_floor([e.certainty for e in evidences]),
        provenance=max((e.provenance for e in evidences), key=_PROVENANCE_ORDER.index),
    )
    return QualityDecision(
        eligible=worst.eligible,
        disposition=worst.disposition,
        tier=worst.tier,
        preference_step=worst.preference_step,
        lossless_detail_step=worst.lossless_detail_step,
        evidence=merged_evidence,
        reasons=list(worst.reasons),
        summary=worst.summary,
    )


def preference_step(
    snapshot: AcquisitionQualitySnapshot, evidence: AudioQualityEvidence
) -> int | None:
    """Cheap stable-step lookup mirroring :func:`evaluate`. None = outside
    policy / needs-review / rejected; family-unknown fallback sits at
    ``len(order)`` - strictly after every known step."""
    return evaluate(snapshot, evidence).preference_step


def evidence_from_archive_format(format_string: str) -> AudioQualityEvidence:
    """Archive.org ``format`` strings are pre-fetch evidence (partial at best).

    Live shapes (verified against archive advancedsearch results): ``Flac``,
    ``24bit Flac`` (proves 24-bit-class depth, violating any 16-bit cap even
    with an unknown sample rate), ``MP3`` / ``VBR MP3`` (proven lossy FAMILY,
    unknown pre-fetch bitrate - keeps the legacy low projection, never promoted
    through the family-unknown rule), ``Ogg Vorbis`` (lossy, unknown bitrate).
    """
    lowered = (format_string or "").strip().lower()
    if not lowered:
        return AudioQualityEvidence()
    depth = 24 if ("24bit" in lowered or "24-bit" in lowered) else None
    if "flac" in lowered:
        return AudioQualityEvidence(
            extension="flac",
            codec_family=CodecFamily.LOSSLESS,
            bit_depth=depth,
            certainty=EvidenceCertainty.PARTIAL,
            provenance=EvidenceProvenance.ARCHIVE_FORMAT,
        )
    if "mp3" in lowered:
        return AudioQualityEvidence(
            extension="mp3",
            codec_family=CodecFamily.LOSSY,
            certainty=EvidenceCertainty.PARTIAL,
            provenance=EvidenceProvenance.ARCHIVE_FORMAT,
        )
    if "vorbis" in lowered or lowered.startswith("ogg"):
        return AudioQualityEvidence(
            extension="ogg",
            codec_family=CodecFamily.LOSSY,
            certainty=EvidenceCertainty.PARTIAL,
            provenance=EvidenceProvenance.ARCHIVE_FORMAT,
        )
    if re.match(r"^24[\s\-]?bit", lowered):
        # Bare "24bit" claim on another container: depth-proven, family unclear.
        return AudioQualityEvidence(
            codec_family=CodecFamily.UNKNOWN,
            bit_depth=depth,
            certainty=EvidenceCertainty.UNKNOWN,
            provenance=EvidenceProvenance.ARCHIVE_FORMAT,
        )
    return AudioQualityEvidence(
        extension=lowered.split()[0].strip("."),
        codec_family=CodecFamily.UNKNOWN,
        certainty=EvidenceCertainty.UNKNOWN,
        provenance=EvidenceProvenance.ARCHIVE_FORMAT,
    )


__all__ = [
    "CERTAINTY_RANK",
    "LOSSLESS_DETAIL_STEPS",
    "NOT_IMPORTABLE_EXTENSIONS",
    "SNAPSHOT_SCHEMA_VERSION",
    "AcquisitionQualitySnapshot",
    "AudioQualityEvidence",
    "CERTAINTY_RANK",
    "CodecFamily",
    "EvidenceCertainty",
    "EvidenceProvenance",
    "QualityDecision",
    "QualityReason",
    "SnapshotOrigin",
    "UnknownQualityBehavior",
    "build_snapshot",
    "compose_summary",
    "derive_default_order",
    "detail_comparator_axes",
    "evidence_from_archive_format",
    "evaluate",
    "evaluate_worst",
    "lossless_detail_step",
    "lossless_rank_key",
    "lossless_target_index",
    "lossy_target_distance",
    "migration_snapshot",
    "normalize_order",
    "preference_step",
    "preference_step_from_tier",
    "project_canonical_tier",
    "snapshot_policy_hash",
]
