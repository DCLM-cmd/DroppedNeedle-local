"""``PluginReleaseScorer`` - release-level scoring for v1 plugin indexers.

Mirrors :class:`NewznabReleaseScorer.rank` (same snapshot/spec-context inputs,
same shared ``pipeline.run`` gates, same quality-evaluate + preference-step
ordering) but trusts the plugin's own ``score`` as the identity signal: the
plugin knows its source best, DroppedNeedle applies the policy gates.
Quarantine/ignored/quality/size/held-tier apply identically to the built-in
paths; ``usenet_min_age_minutes`` is omitted (no Usenet age concept) and the
free-space context is left ``None`` like the other scorers.
"""

import logging
import re
from collections import Counter

from infrastructure.persistence.download_store import DownloadStore
from models.download import ScoredCandidate, TargetAlbum
from repositories.protocols.indexer import PluginSearchResult
from services.native.acquisition import pipeline
from services.native.acquisition.context import build_context
from services.native.acquisition.decision import (
    Candidate,
    Reject,
    RejectCode,
    SpecPolicy,
)
from models.acquisition_quality import (
    AcquisitionQualitySnapshot,
    AudioQualityEvidence,
    CodecFamily,
    EvidenceCertainty,
    EvidenceProvenance,
)
from services.native.acquisition import quality as acq_quality

logger = logging.getLogger(__name__)

_WS = re.compile(r"\s+")

try:
    from models.download_identity import plugin_identity as _model_plugin_identity
except Exception:  # noqa: BLE001 - ModelsCheck lands the helper alongside this file
    _model_plugin_identity = None  # type: ignore[assignment]


def _plugin_key(result: PluginSearchResult) -> str:
    """Quarantine key for one plugin result: the opaque payload when set,
    else the Usenet-style normalised-title + size-MB fallback."""
    payload = (result.payload or "").strip()
    if payload:
        return payload
    norm = _WS.sub(" ", (result.title or "").strip().lower())
    size_mb = (result.size_bytes or 0) // (1024 * 1024)
    return f"{norm}\x1f{size_mb}"


def _plugin_identity(source_key: str, result: PluginSearchResult) -> str:
    if _model_plugin_identity is not None:
        return _model_plugin_identity(source_key, _plugin_key(result))
    return f"{source_key}\x1f{_plugin_key(result)}"


def _plugin_evidence(
    result: PluginSearchResult,
    tier: str,
) -> AudioQualityEvidence:
    """Project the plugin's declared tier into the shared quality evaluator."""
    size = result.size_bytes or 0
    count = len(result.files) or 1
    if tier == "lossless":
        return AudioQualityEvidence(
            extension="flac",
            codec_family=CodecFamily.LOSSLESS,
            total_bytes=size,
            audio_file_count=count,
            certainty=EvidenceCertainty.INFERRED,
            provenance=EvidenceProvenance.SOURCE_METADATA,
        )
    if tier == "mp3_320":
        return AudioQualityEvidence(
            extension="mp3",
            codec_family=CodecFamily.LOSSY,
            bitrate_kbps=320,
            total_bytes=size,
            audio_file_count=count,
            certainty=EvidenceCertainty.INFERRED,
            provenance=EvidenceProvenance.SOURCE_METADATA,
        )
    if tier == "mp3_256":
        return AudioQualityEvidence(
            extension="mp3",
            codec_family=CodecFamily.LOSSY,
            bitrate_kbps=256,
            total_bytes=size,
            audio_file_count=count,
            certainty=EvidenceCertainty.INFERRED,
            provenance=EvidenceProvenance.SOURCE_METADATA,
        )
    if tier == "mp3_192":
        return AudioQualityEvidence(
            extension="mp3",
            codec_family=CodecFamily.LOSSY,
            bitrate_kbps=192,
            total_bytes=size,
            audio_file_count=count,
            certainty=EvidenceCertainty.INFERRED,
            provenance=EvidenceProvenance.SOURCE_METADATA,
        )
    if tier == "low":
        return AudioQualityEvidence(
            extension="mp3",
            codec_family=CodecFamily.LOSSY,
            total_bytes=size,
            audio_file_count=count,
            certainty=EvidenceCertainty.INFERRED,
            provenance=EvidenceProvenance.SOURCE_METADATA,
        )
    return AudioQualityEvidence(
        extension="",
        codec_family=CodecFamily.UNKNOWN,
        total_bytes=size,
        audio_file_count=count,
        certainty=EvidenceCertainty.INFERRED,
        provenance=EvidenceProvenance.SOURCE_METADATA,
    )


class PluginReleaseScorer:
    """Scores one plugin source's releases under the task's quality snapshot."""

    def __init__(self, download_store: DownloadStore, *, source_key: str = "") -> None:
        self._store = download_store
        self._source_key = source_key

    async def rank(
        self,
        target: TargetAlbum,
        releases: list[PluginSearchResult],
        *,
        snapshot: AcquisitionQualitySnapshot,
        spec_extras: "SpecPolicy | None" = None,
        auto_accept_threshold: float = 0.70,
        manual_threshold: float = 0.50,
        track_count: int | None = None,  # noqa: ARG002 - shape parity with Newznab
        held_tier: str | None = None,
        source_key: str | None = None,
    ) -> list[ScoredCandidate]:
        key = source_key or self._source_key
        context = await build_context(self._store, held_tier=held_tier)
        import msgspec as _ms

        order = snapshot.quality_preference_order
        recipe_mode = acq_quality.is_recipe_snapshot(snapshot)
        policy = SpecPolicy(
            quality_min="low" if recipe_mode else order[-1] if order else "low",
            quality_max="lossless"
            if recipe_mode
            else order[0]
            if order
            else "lossless",
        )
        if spec_extras is not None:
            policy = _ms.structs.replace(
                policy,
                max_size_mb=spec_extras.max_size_mb,
                ignored_terms=tuple(spec_extras.ignored_terms),
                required_terms=tuple(spec_extras.required_terms),
                usenet_retention_days=spec_extras.usenet_retention_days,
            )
        scored: list[ScoredCandidate] = []
        pipeline_drops: Counter[RejectCode] = Counter()

        for release in releases:
            tier = release.quality_tier or "unknown"
            decision = pipeline.run(
                Candidate(
                    source=key,
                    identity=_plugin_identity(key, release),
                    match_text=release.title,
                    tier=tier,
                    size_bytes=release.size_bytes,
                ),
                target,
                context,
                policy,
            )
            if isinstance(decision, Reject):
                pipeline_drops[decision.code] += 1
                continue
            try:
                final = float(release.score)
            except (TypeError, ValueError):
                final = 0.0
            final = min(1.0, max(0.0, final))
            band = (
                "auto"
                if final >= auto_accept_threshold
                else "manual"
                if final >= manual_threshold
                else "rejected"
            )
            evidence = _plugin_evidence(release, tier)
            quality_decision = acq_quality.evaluate(snapshot, evidence)
            if (
                recipe_mode
                and not quality_decision.eligible
                and acq_quality.is_hard_quality_rejection(quality_decision)
            ):
                pipeline_drops[RejectCode.QUALITY_REJECTED] += 1
                continue
            if recipe_mode and not quality_decision.eligible and band == "auto":
                band = "manual"
            scored.append(
                ScoredCandidate(
                    source=key,
                    plugin_release=release,
                    files=list(release.files),
                    coherence=round(final, 4),
                    file_confidence=round(final, 4),
                    final_score=round(final, 4),
                    tier=band,
                    quality_evidence=evidence,
                    quality_decision=quality_decision,
                )
            )

        band_rank = {"auto": 2, "manual": 1, "rejected": 0}
        worst_step = len(order) + 2

        # Deterministic title order first; the stable main sort below keeps it
        # for full ties while plugin input order survives beyond that.
        scored.sort(key=lambda cand: (cand.plugin_release.title or "" if cand.plugin_release else ""))

        def _sort_key(cand: ScoredCandidate):
            decision_ = cand.quality_decision
            evidence_ = cand.quality_evidence
            step = decision_.preference_step if decision_ else None
            if recipe_mode:
                step = step if step is not None else len(snapshot.quality_recipe) + 1
                refinement = acq_quality.recipe_refinement_key(
                    snapshot, evidence_ or AudioQualityEvidence()
                )
                return (
                    band_rank.get(cand.tier, 0),
                    -step,
                    *(-value for value in refinement),
                    cand.final_score,
                )
            certainty = acq_quality.CERTAINTY_RANK[
                evidence_.certainty if evidence_ else EvidenceCertainty.PARTIAL
            ]
            return (
                band_rank.get(cand.tier, 0),
                -(step if step is not None else worst_step),
                -certainty,
                cand.final_score,
            )

        scored.sort(key=_sort_key, reverse=True)
        if pipeline_drops:
            logger.info(
                "plugin.scored",
                extra={
                    "source": key,
                    "releases": len(releases),
                    "scored": len(scored),
                    **{
                        f"dropped_{code.value}": n for code, n in pipeline_drops.items()
                    },
                },
            )
        return scored[:50]
