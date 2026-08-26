"""Snapshot-based existing-identity audit and explicit safe-detach Apply."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable

import msgspec.json

from api.v1.schemas.library_operations import (
    IdentityPreparationCreateRequest,
    IdentityPreparationEstimateResponse,
    OperationListResponse,
    OperationResponse,
    RepairCreateRequest,
    RepairEstimateResponse,
    RepairFindingListResponse,
    RepairFindingResponse,
    SuggestedEditionSummary,
)
from core.exceptions import ExternalServiceError, ResourceNotFoundError, ValidationError
from infrastructure.queue.priority_queue import RequestPriority
from infrastructure.resilience.retry import CircuitOpenError
from infrastructure.persistence.native_library_store import (
    AUTOMATIC_SAFE_EVIDENCE_REASONS,
    NativeLibraryStore,
    _complete_track_identity_mapping,
)
from models.identification import (
    AlbumCandidate,
    CandidateEvidence,
    CandidateTrack,
    GroupingTrack,
    IdentificationAttempt,
    IdentificationEvidenceRecord,
    TrackEvidence,
)
from models.library_work import OperationJob, RepairFinding
from repositories.protocols.identification import IdentificationProviderProtocol
from repositories.protocols.musicbrainz_management import (
    CanonicalMusicBrainzRepositoryProtocol,
    MbManagementRelease,
)
from services.native.album_evidence_engine import (
    DURATION_GRACE_SECONDS,
    MATCHER_VERSION,
    AlbumEvidenceEngine,
    _fold,
)
from services.native.album_identification_service import (
    _candidate_key,
    _to_grouping_track,
)
from services.native.conditional_fingerprint_service import FINGERPRINTER_VERSION
from services.native.identification_revisions import (
    album_identity_revision,
    album_input_revisions,
)
from services.native.library_operation_service import (
    LEASE_SECONDS,
    LibraryOperationService,
)

MANAGEMENT_READINESS_PURPOSE = "management_readiness"
MANAGEMENT_MAPPING_VERSION = "management-edition-readiness-v4"

# MusicBrainz breaker timeout is 60 s; this 2x window (matching the artist
# reconciliation service) gives the breaker a recovery window between attempts.
_PROVIDER_DEFERRED_RETRY_SECONDS = 120.0


class _ProviderUnavailable(Exception):
    """Control-flow: the identity provider is unavailable, so the audit defers
    the whole job instead of writing 'unverifiable' findings during the outage."""


class IdentityRepairService:
    def __init__(
        self,
        store: NativeLibraryStore,
        provider: IdentificationProviderProtocol | None = None,
        evidence: AlbumEvidenceEngine | None = None,
        canonical_provider: CanonicalMusicBrainzRepositoryProtocol | None = None,
        provider_available: Callable[[], bool] | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._evidence = evidence or AlbumEvidenceEngine()
        self._canonical_provider = canonical_provider
        self._provider_available = provider_available
        self._operations = LibraryOperationService(store)

    async def create(
        self,
        request: RepairCreateRequest,
        actor_user_id: str,
        *,
        now: float | None = None,
    ) -> OperationResponse:
        timestamp = time.time() if now is None else now
        job = OperationJob(
            id=str(uuid.uuid4()),
            kind="repair",
            requested_by_user_id=actor_user_id,
            input_catalog_revision=await self._store.get_catalog_revision(),
            idempotency_key=request.idempotency_key,
            created_at=timestamp,
        )
        row = await self._store.create_repair_operation(
            job,
            scope={
                "root_ids": request.root_ids,
                "legacy_only": request.source_matcher_version is None,
            },
            source_matcher_version=request.source_matcher_version,
            target_matcher_version=request.target_matcher_version,
        )
        return self._operations._response(row)

    async def create_management_preparation(
        self,
        request: IdentityPreparationCreateRequest,
        actor_user_id: str,
        *,
        now: float | None = None,
    ) -> OperationResponse:
        timestamp = time.time() if now is None else now
        job = OperationJob(
            id=str(uuid.uuid4()),
            kind="repair",
            requested_by_user_id=actor_user_id,
            input_catalog_revision=await self._store.get_catalog_revision(),
            idempotency_key=request.idempotency_key,
            created_at=timestamp,
        )
        row = await self._store.create_repair_operation(
            job,
            scope={
                "root_ids": request.root_ids,
                "legacy_only": False,
                "purpose": MANAGEMENT_READINESS_PURPOSE,
            },
            source_matcher_version=None,
            target_matcher_version=MANAGEMENT_MAPPING_VERSION,
        )
        return self._operations._response(row)

    async def estimate_management_preparation(
        self, root_ids: list[str]
    ) -> IdentityPreparationEstimateResponse:
        unique_root_ids = list(dict.fromkeys(root_ids))
        result = await self._store.estimate_management_identity_preparation(
            unique_root_ids
        )
        return IdentityPreparationEstimateResponse(
            album_count=result["album_count"],
            ready_album_count=result["ready_album_count"],
            mapping_required_count=result["mapping_required_count"],
            exact_release_required_count=result["exact_release_required_count"],
            selected_root_count=len(unique_root_ids),
            queued_preparation_count=result["queued_preparation_count"],
        )

    async def estimate(self, root_ids: list[str]) -> RepairEstimateResponse:
        unique_root_ids = list(dict.fromkeys(root_ids))
        result = await self._store.estimate_repair_operation(unique_root_ids)
        return RepairEstimateResponse(
            identity_count=result["identity_count"],
            selected_root_count=len(unique_root_ids),
            queued_repair_count=result["queued_repair_count"],
        )

    async def history(
        self,
        *,
        purpose: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> OperationListResponse:
        if limit < 1 or limit > 50:
            raise ValidationError(
                "Operation history page size must be between 1 and 50."
            )
        before_created_at: float | None = None
        before_id: str | None = None
        if cursor is not None:
            try:
                created, before_id = cursor.split(":", 1)
                before_created_at = float(created)
            except (TypeError, ValueError) as error:
                raise ValidationError(
                    "The operation history cursor is invalid."
                ) from error
        rows = await self._store.list_repair_operation_jobs(
            purpose=purpose,
            limit=limit + 1,
            before_created_at=before_created_at,
            before_id=before_id,
        )
        page = rows[:limit]
        return OperationListResponse(
            items=[await self._operations.get(str(row["id"])) for row in page],
            next_cursor=(
                f"{page[-1]['created_at']}:{page[-1]['id']}"
                if len(rows) > limit and page
                else None
            ),
        )

    async def get_for_purpose(self, job_id: str, purpose: str) -> OperationResponse:
        snapshot = await self._store.get_operation_snapshot(job_id)
        if snapshot is None or snapshot["snapshot"] is None:
            raise ResourceNotFoundError("Identity operation not found.")
        scope = json.loads(str(snapshot["snapshot"]["scope_json"]))
        actual = str(scope.get("purpose", "existing_matches"))
        if actual != purpose:
            raise ResourceNotFoundError("Identity operation not found.")
        return await self._operations.get(job_id)

    async def run_claimed_audit(
        self,
        job: dict,
        worker_id: str,
        *,
        now: float | None = None,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
        provider_available: Callable[[], bool] | None = None,
    ) -> OperationResponse:
        snapshot = await self._store.get_operation_snapshot(str(job["id"]))
        scope = (
            snapshot["snapshot"].get("scope_json")
            if snapshot is not None and snapshot["snapshot"] is not None
            else None
        )
        purpose = (
            str(json.loads(str(scope)).get("purpose", "existing_matches"))
            if scope
            else "existing_matches"
        )
        availability = (
            self._provider_available if provider_available is None else provider_available
        )
        while True:
            timestamp = time.time() if now is None else now
            if availability is not None and not availability():
                return await self._defer_audit(str(job["id"]), worker_id, timestamp)
            controlled = await self._store.checkpoint_operation_control(
                str(job["id"]), worker_id, now=timestamp
            )
            if controlled is not None and controlled["state"] != "running":
                return self._operations._response(controlled)
            work = await self._store.claim_operation_work(
                str(job["id"]), worker_id, now=timestamp
            )
            if work is None:
                await self._store.mark_repair_ready(
                    str(job["id"]), worker_id, now=timestamp
                )
                return await self._operations.get(str(job["id"]))
            context = await self._store.get_album_identification_context(
                str(work["local_album_id"])
            )
            renewed = await self._store.heartbeat_operation_job(
                str(job["id"]),
                worker_id,
                now=timestamp,
                lease_seconds=LEASE_SECONDS,
            )
            if not renewed:
                raise ResourceNotFoundError("The identity check lease changed.")
            try:
                if purpose == MANAGEMENT_READINESS_PURPOSE:
                    finding, attempt, evidence = await self._classify_management_readiness(
                        str(job["id"]), work, context, timestamp
                    )
                else:
                    finding, attempt, evidence = await self._classify(
                        str(job["id"]), work, context
                    )
            except _ProviderUnavailable:
                return await self._defer_audit(
                    str(job["id"]),
                    worker_id,
                    timestamp,
                    ordinal=int(work["ordinal"]),
                )
            await self._store.save_repair_finding_for_work(
                str(job["id"]),
                int(work["ordinal"]),
                worker_id=worker_id,
                expected_work_revision=int(work["row_revision"]),
                finding=finding,
                attempt=attempt,
                evidence=evidence,
                now=timestamp,
            )
            if checkpoint is not None:
                await checkpoint()

    async def _defer_audit(
        self,
        job_id: str,
        worker_id: str,
        timestamp: float,
        *,
        ordinal: int | None = None,
    ) -> OperationResponse:
        deferred = await self._store.defer_repair_audit_work(
            job_id=job_id,
            ordinal=ordinal,
            worker_id=worker_id,
            reason_code="PROVIDER_DEFERRED",
            now=timestamp,
            retry_not_before=timestamp + _PROVIDER_DEFERRED_RETRY_SECONDS,
        )
        return self._operations._response(deferred)

    async def _classify_management_readiness(
        self,
        job_id: str,
        work: dict,
        context: dict | None,
        timestamp: float,
    ) -> tuple[
        RepairFinding,
        IdentificationAttempt | None,
        list[IdentificationEvidenceRecord],
    ]:
        album_id = str(work["local_album_id"])
        if context is None:
            return (
                self._finding(job_id, work, "stale", "IDENTITY_CHANGED", False),
                None,
                [],
            )
        identity = context["identity"]
        custom = await self._store.get_custom_edition_state(album_id)
        if custom is not None:
            return (
                self._finding(
                    job_id,
                    work,
                    "needs_review" if custom.stale else "ready",
                    "CUSTOM_MANIFEST_STALE"
                    if custom.stale
                    else "CUSTOM_EDITION_MANIFEST_VERIFIED",
                    False,
                    identity_revision=(
                        int(identity["row_revision"]) if identity is not None else None
                    ),
                ),
                None,
                [],
            )
        if (
            identity is None
            or not identity["release_group_mbid"]
            or not identity["release_mbid"]
        ):
            return await self._classify_exact_release_suggestion(
                job_id, work, context, identity
            )
        tracks = [row for row in context["tracks"] if row["availability"] == "indexed"]
        release_track_ids = [
            str(row["release_track_mbid"])
            for row in tracks
            if row["release_track_mbid"]
        ]
        complete = (
            bool(tracks)
            and all(
                row["recording_mbid"]
                and row["release_track_mbid"]
                and row["identity_release_mbid"] == identity["release_mbid"]
                and row["medium_position"] is not None
                and row["release_track_position"] is not None
                for row in tracks
            )
            and len(set(release_track_ids)) == len(tracks)
        )
        if self._canonical_provider is None:
            return (
                self._finding(
                    job_id,
                    work,
                    "unverifiable",
                    "PROVIDER_DEFERRED",
                    False,
                    identity_revision=int(identity["row_revision"]),
                ),
                None,
                [],
            )
        try:
            release = await self._canonical_provider.get_canonical_release(
                str(identity["release_mbid"]),
                includes=("artist-credits", "recordings", "release-groups"),
                priority=RequestPriority.BACKGROUND_SYNC,
            )
        except (ExternalServiceError, CircuitOpenError) as error:
            raise _ProviderUnavailable(
                "MusicBrainz is unavailable; deferring the identity audit."
            ) from error
        if release is None:
            return (
                self._finding(
                    job_id,
                    work,
                    "needs_review",
                    "SELECTED_RELEASE_UNAVAILABLE",
                    False,
                    identity_revision=int(identity["row_revision"]),
                ),
                None,
                [],
            )
        if (
            release.id != identity["release_mbid"]
            or release.release_group.id != identity["release_group_mbid"]
        ):
            return (
                self._finding(
                    job_id,
                    work,
                    "needs_review",
                    "SELECTED_RELEASE_CONFLICT",
                    False,
                    identity_revision=int(identity["row_revision"]),
                ),
                None,
                [],
            )
        local_tracks = [_to_grouping_track(row) for row in tracks]
        for local, row in zip(local_tracks, tracks, strict=True):
            local.recording_mbid = (
                row["recording_mbid"] or row["embedded_recording_mbid"]
            )
            local.release_mbid = str(identity["release_mbid"])
            local.release_group_mbid = str(identity["release_group_mbid"])
        candidate = self._management_candidate(release)
        try:
            recording_redirects = await self._normalize_recording_redirects(
                local_tracks,
                tracks,
                candidate,
            )
        except (ExternalServiceError, CircuitOpenError) as error:
            raise _ProviderUnavailable(
                "MusicBrainz is unavailable; deferring the identity audit."
            ) from error
        evaluated = self._evidence.evaluate_candidate(local_tracks, candidate)
        self._disambiguate_duplicate_recordings(
            local_tracks,
            tracks,
            candidate,
            evaluated,
        )
        for item in evaluated.track_evidence:
            redirects = recording_redirects.get(item.local_track_id, [])
            if redirects and item.classification == "supported":
                item.recording_mbid_redirects = redirects
                item.evidence_kinds.append("recording_mbid_redirect")
        proposed = [
            item
            for item in evaluated.track_evidence
            if item.classification == "supported"
        ]
        by_id = {item.local_track_id: item for item in proposed}
        release_tracks = [item.release_track_mbid for item in proposed]
        safe = (
            bool(tracks)
            and bool(proposed)
            and all(
                (
                    item.recording_mbid
                    and item.release_track_mbid
                    and item.candidate_disc_number is not None
                    and item.candidate_track_position is not None
                )
                for item in proposed
            )
        )
        release_type_confirmed = bool(
            evaluated.reason_code == "RELEASE_TYPE_REQUIRES_CONFIRMATION"
            and identity["decision_source"] == "manual"
            and complete
        )
        safe = bool(
            safe
            and (evaluated.reason_code == "SUPPORTED" or release_type_confirmed)
            and len(proposed) == len(tracks)
            and len(set(release_tracks)) == len(release_tracks)
        )
        if safe:
            for row in tracks:
                item = by_id[str(row["id"])]
                if any(
                    (
                        not self._recording_identity_matches(
                            row["recording_mbid"], item
                        ),
                        row["identity_release_mbid"]
                        and row["identity_release_mbid"] != release.id,
                        row["release_track_mbid"]
                        and row["release_track_mbid"] != item.release_track_mbid,
                        row["embedded_release_group_mbid"]
                        and row["embedded_release_group_mbid"]
                        != release.release_group.id,
                        row["embedded_release_mbid"]
                        and row["embedded_release_mbid"] != release.id,
                        not self._recording_identity_matches(
                            row["embedded_recording_mbid"], item
                        ),
                        row["embedded_release_track_mbid"]
                        and row["embedded_release_track_mbid"]
                        != item.release_track_mbid,
                    )
                ):
                    safe = False
                    evaluated.reason_code = "CONFLICTING_TRACK_EVIDENCE"
                    break
        verified_unchanged = bool(
            safe
            and complete
            and all(
                row["recording_mbid"] == by_id[str(row["id"])].recording_mbid
                and row["identity_release_mbid"] == release.id
                and row["release_track_mbid"]
                == by_id[str(row["id"])].release_track_mbid
                and int(row["medium_position"])
                == by_id[str(row["id"])].candidate_disc_number
                and int(row["release_track_position"])
                == by_id[str(row["id"])].candidate_track_position
                for row in tracks
            )
        )
        result_reason = (
            "EXACT_RELEASE_MAPPINGS_VERIFIED"
            if verified_unchanged
            else ("EXACT_RELEASE_MAPPING_SUPPORTED" if safe else evaluated.reason_code)
        )
        revisions = album_input_revisions(tracks)
        attempt_id = str(uuid.uuid4())
        evidence_id = str(uuid.uuid4())
        attempt = IdentificationAttempt(
            id=attempt_id,
            local_album_id=album_id,
            trigger="management_identity_preparation",
            input_tag_revision=revisions[0],
            input_file_revision=revisions[1],
            input_policy_revision=revisions[2],
            matcher_version=MANAGEMENT_MAPPING_VERSION,
            state="identified" if safe else "contradictory",
            terminal_reason_code=result_reason,
            selected_candidate_key=_candidate_key(evaluated) if safe else None,
            candidate_count=1,
            started_at=timestamp,
            completed_at=timestamp,
        )
        record = IdentificationEvidenceRecord(
            id=evidence_id,
            attempt_id=attempt_id,
            candidate_key=_candidate_key(evaluated),
            evidence=evaluated,
            created_at=timestamp,
        )
        return (
            self._finding(
                job_id,
                work,
                (
                    "ready"
                    if verified_unchanged
                    else ("mapping_ready" if safe else "needs_review")
                ),
                result_reason,
                safe and not verified_unchanged,
                evidence_id=evidence_id,
                identity_revision=int(identity["row_revision"]),
            ),
            attempt,
            [record],
        )

    async def _classify_exact_release_suggestion(
        self,
        job_id: str,
        work: dict,
        context: dict,
        identity: dict | None,
    ) -> tuple[
        RepairFinding,
        IdentificationAttempt | None,
        list[IdentificationEvidenceRecord],
    ]:
        """Suggest one sealable exact edition from stored identification evidence."""
        album_id = str(work["local_album_id"])
        identity_revision = (
            int(identity["row_revision"]) if identity is not None else None
        )

        def bare() -> tuple[
            RepairFinding,
            IdentificationAttempt | None,
            list[IdentificationEvidenceRecord],
        ]:
            return (
                self._finding(
                    job_id,
                    work,
                    "exact_release_required",
                    "EXACT_EDITION_NOT_ACCEPTED",
                    False,
                    identity_revision=identity_revision,
                ),
                None,
                [],
            )

        tracks = [row for row in context["tracks"] if row["availability"] == "indexed"]
        if not tracks:
            return bare()
        stored = await self._store.get_latest_album_identification_evidence(album_id)
        if stored is None:
            return bare()
        attempt, evidence_rows = stored
        if album_input_revisions(tracks) != (
            str(attempt["input_tag_revision"]),
            str(attempt["input_file_revision"]),
            str(attempt["input_policy_revision"]),
        ):
            return bare()
        suggestible: list[tuple[dict, CandidateEvidence]] = []
        for row in evidence_rows:
            candidate_evidence = msgspec.json.decode(
                bytes(row["evidence_json"]), type=CandidateEvidence
            )
            if (
                candidate_evidence.reason_code in AUTOMATIC_SAFE_EVIDENCE_REASONS
                and candidate_evidence.release_mbid
                and _complete_track_identity_mapping(tracks, candidate_evidence)
                is not None
            ):
                suggestible.append((row, candidate_evidence))
        if not suggestible:
            return bare()
        competing_count = len(suggestible)
        if competing_count == 1:
            winner_row, winner = suggestible[0]
            summary: dict[str, object] = {
                "title": winner.album_title,
                "date": winner.release_date,
                "country": None,
                "status": None,
                "track_count": len(winner.track_evidence)
                + len(winner.unmatched_expected_tracks),
                "competing_count": 1,
            }
        else:
            ranked: list[
                tuple[tuple[int, str, int, str], dict, CandidateEvidence, dict]
            ] = []
            for row, candidate_evidence in suggestible:
                release: MbManagementRelease | None = None
                if self._canonical_provider is not None:
                    try:
                        release = await self._canonical_provider.get_canonical_release(
                            str(candidate_evidence.release_mbid),
                            includes=("media",),
                            priority=RequestPriority.BACKGROUND_SYNC,
                        )
                    except (ExternalServiceError, CircuitOpenError) as error:
                        raise _ProviderUnavailable(
                            "MusicBrainz is unavailable; deferring the identity audit."
                        ) from error
                    if release is None:
                        continue
                summary = {
                    "title": (
                        release.title
                        if release is not None
                        else candidate_evidence.album_title
                    ),
                    "date": (release.date if release is not None else None)
                    or candidate_evidence.release_date,
                    "country": release.country if release is not None else None,
                    "status": release.status if release is not None else None,
                    "track_count": (
                        sum(medium.track_count for medium in release.media)
                        if release is not None
                        else len(candidate_evidence.track_evidence)
                        + len(candidate_evidence.unmatched_expected_tracks)
                    ),
                    "competing_count": competing_count,
                }
                key = (
                    0 if release is not None and release.status == "Official" else 1,
                    str(summary["date"] or "9999"),
                    0 if release is not None and release.country == "XW" else 1,
                    str(candidate_evidence.release_mbid),
                )
                ranked.append((key, row, candidate_evidence, summary))
            if not ranked:
                return bare()
            _, winner_row, winner, summary = min(ranked, key=lambda item: item[0])
        finding = self._finding(
            job_id,
            work,
            "exact_release_suggested",
            "EXACT_EDITION_SUGGESTED",
            True,
            evidence_id=str(winner_row["id"]),
            identity_revision=identity_revision,
        )
        finding.suggested_release_mbid = str(winner.release_mbid)
        finding.suggested_release_group_mbid = winner.release_group_mbid
        finding.suggested_edition_json = json.dumps(summary, sort_keys=True)
        return finding, None, []

    async def _normalize_recording_redirects(
        self,
        local_tracks: list[GroupingTrack],
        rows: list[dict],
        candidate: AlbumCandidate,
    ) -> dict[str, list[str]]:
        if self._canonical_provider is None:
            return {}
        candidate_ids = {
            track.recording_mbid.casefold()
            for track in candidate.tracks
            if track.recording_mbid
        }
        resolved: dict[str, str | None] = {}
        redirects_by_track: dict[str, list[str]] = {}
        for local, row in zip(local_tracks, rows, strict=True):
            recording_ids = {
                str(value).casefold()
                for value in (
                    row["recording_mbid"],
                    row["embedded_recording_mbid"],
                )
                if value
            }
            canonical_ids: set[str] = set()
            redirects: list[str] = []
            for recording_id in recording_ids:
                canonical_id = recording_id
                if recording_id not in candidate_ids:
                    if recording_id not in resolved:
                        resolved[
                            recording_id
                        ] = await self._canonical_provider.resolve_recording_mbid(
                            recording_id,
                            priority=RequestPriority.BACKGROUND_SYNC,
                        )
                    provider_id = resolved[recording_id]
                    if (
                        provider_id
                        and provider_id.casefold() in candidate_ids
                        and provider_id.casefold() != recording_id
                    ):
                        canonical_id = provider_id.casefold()
                        redirects.append(recording_id)
                canonical_ids.add(canonical_id)
            if len(canonical_ids) == 1:
                canonical_id = next(iter(canonical_ids))
                if canonical_id in candidate_ids:
                    local.recording_mbid = canonical_id
                    if redirects:
                        redirects_by_track[local.local_track_id] = sorted(
                            set(redirects)
                        )
        return redirects_by_track

    @staticmethod
    def _disambiguate_duplicate_recordings(
        local_tracks: list[GroupingTrack],
        rows: list[dict],
        candidate: AlbumCandidate,
        evidence: CandidateEvidence,
    ) -> None:
        candidates_by_recording: dict[str, list[CandidateTrack]] = {}
        for track in candidate.tracks:
            if track.recording_mbid:
                candidates_by_recording.setdefault(
                    track.recording_mbid.casefold(), []
                ).append(track)
        local_by_id = {
            local.local_track_id: (local, row)
            for local, row in zip(local_tracks, rows, strict=True)
        }
        ambiguous = False
        for item in evidence.track_evidence:
            if item.classification != "supported" or not item.recording_mbid:
                continue
            duplicates = candidates_by_recording.get(item.recording_mbid.casefold(), [])
            if len(duplicates) < 2:
                continue
            local, row = local_by_id[item.local_track_id]
            selected = IdentityRepairService._select_duplicate_recording_track(
                local, row, duplicates
            )
            if selected is None:
                item.classification = "contradictory"
                item.evidence_kinds.append("ambiguous_release_track_identity")
                item.release_track_mbid = None
                item.candidate_disc_number = None
                item.candidate_track_position = None
                ambiguous = True
                continue
            item.candidate_track_title = selected.title
            item.candidate_disc_number = selected.disc_number
            item.candidate_track_position = selected.position
            item.recording_mbid = selected.recording_mbid
            item.release_track_mbid = selected.release_track_mbid
            item.evidence_kinds.append("duplicate_recording_disambiguated")
        if ambiguous:
            evidence.reason_code = "CONFLICTING_TRACK_EVIDENCE"

    @staticmethod
    def _select_duplicate_recording_track(
        local: GroupingTrack,
        row: dict,
        candidates: list[CandidateTrack],
    ) -> CandidateTrack | None:
        explicit_ids = {
            str(value).casefold()
            for value in (
                row["release_track_mbid"],
                row["embedded_release_track_mbid"],
            )
            if value
        }
        if explicit_ids:
            matching = [
                track
                for track in candidates
                if track.release_track_mbid
                and track.release_track_mbid.casefold() in explicit_ids
            ]
            return matching[0] if len(explicit_ids) == len(matching) == 1 else None

        signals: list[set[int]] = []
        if local.track_number > 0:
            position_matches = {
                index
                for index, track in enumerate(candidates)
                if local.disc_number == track.disc_number
                and local.track_number in {track.position, track.absolute_position}
            }
            signals.append(position_matches)
        if local.title.strip():
            title_matches = {
                index
                for index, track in enumerate(candidates)
                if _fold(local.title) == _fold(track.title)
            }
            signals.append(title_matches)
        if local.duration_seconds is not None and any(
            track.duration_seconds is not None for track in candidates
        ):
            duration_matches = {
                index
                for index, track in enumerate(candidates)
                if track.duration_seconds is not None
                and abs(local.duration_seconds - track.duration_seconds)
                <= DURATION_GRACE_SECONDS
            }
            signals.append(duration_matches)
        if not signals:
            return None
        matching_indexes = set.intersection(*signals)
        if len(matching_indexes) != 1:
            return None
        return candidates[next(iter(matching_indexes))]

    @staticmethod
    def _recording_identity_matches(value: str | None, item: TrackEvidence) -> bool:
        if not value:
            return True
        normalized = value.casefold()
        return normalized == (item.recording_mbid or "").casefold() or normalized in {
            alias.casefold() for alias in item.recording_mbid_redirects
        }

    @staticmethod
    def _management_candidate(release: MbManagementRelease) -> AlbumCandidate:
        absolute = 0
        tracks: list[CandidateTrack] = []
        for medium in release.media:
            for track in medium.tracks:
                absolute += 1
                duration = track.length or track.recording.length
                tracks.append(
                    CandidateTrack(
                        title=track.title or track.recording.title,
                        position=track.position,
                        disc_number=medium.position,
                        absolute_position=absolute,
                        duration_seconds=(duration / 1000.0 if duration else None),
                        recording_mbid=track.recording.id or None,
                        release_track_mbid=track.id or None,
                    )
                )
        album_artist = "".join(
            f"{credit.name or credit.artist.name}{credit.joinphrase}"
            for credit in release.artist_credit
        ).strip()
        first_artist = (
            release.artist_credit[0].artist if release.artist_credit else None
        )
        return AlbumCandidate(
            release_group_mbid=release.release_group.id,
            release_mbid=release.id,
            album_title=release.title or release.release_group.title,
            album_artist_name=album_artist,
            artist_mbid=first_artist.id if first_artist is not None else None,
            tracks=tracks,
            release_type=(
                release.release_group.primary_type.casefold()
                if release.release_group.primary_type
                else None
            ),
            secondary_types=[
                value.casefold() for value in release.release_group.secondary_types
            ],
            release_date=release.date or release.release_group.first_release_date,
            source_kinds=["accepted_exact_release"],
        )

    async def begin_apply(
        self,
        job_id: str,
        *,
        expected_row_revision: int,
        confirmation: bool,
        now: float | None = None,
    ) -> OperationResponse:
        if not confirmation:
            raise ValidationError(
                "Confirm the repair report before applying safe detachments."
            )
        snapshot = await self._store.get_operation_snapshot(job_id)
        if snapshot is None or snapshot["snapshot"] is None:
            raise ResourceNotFoundError("Repair job not found.")
        scope = json.loads(str(snapshot["snapshot"]["scope_json"]))
        if scope.get("purpose") == MANAGEMENT_READINESS_PURPOSE:
            raise ResourceNotFoundError("Repair job not found.")
        row = await self._store.start_repair_apply(
            job_id,
            expected_row_revision=expected_row_revision,
            now=time.time() if now is None else now,
        )
        return self._operations._response(row)

    async def begin_management_preparation_apply(
        self,
        job_id: str,
        *,
        expected_row_revision: int,
        confirmation: bool,
        now: float | None = None,
    ) -> OperationResponse:
        if not confirmation:
            raise ValidationError(
                "Confirm the exact-release mapping report before accepting it."
            )
        snapshot = await self._store.get_operation_snapshot(job_id)
        if snapshot is None or snapshot["snapshot"] is None:
            raise ResourceNotFoundError("Identity preparation job not found.")
        scope = json.loads(str(snapshot["snapshot"]["scope_json"]))
        if scope.get("purpose") != MANAGEMENT_READINESS_PURPOSE:
            raise ResourceNotFoundError("Identity preparation job not found.")
        if snapshot["snapshot"]["target_matcher_version"] != MANAGEMENT_MAPPING_VERSION:
            raise ValidationError(
                "These identity checks used older rules. Run a fresh identity check."
            )
        row = await self._store.start_repair_apply(
            job_id,
            expected_row_revision=expected_row_revision,
            now=time.time() if now is None else now,
        )
        return self._operations._response(row)

    async def discard_management_preparation(
        self,
        job_id: str,
        *,
        expected_row_revision: int,
        now: float | None = None,
    ) -> OperationResponse:
        row = await self._store.discard_management_identity_preparation(
            job_id,
            expected_row_revision=expected_row_revision,
            now=time.time() if now is None else now,
        )
        return self._operations._response(row)

    async def run_claimed_apply(
        self,
        job: dict,
        worker_id: str,
        actor_user_id: str,
        *,
        now: float | None = None,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
    ) -> OperationResponse:
        while True:
            timestamp = time.time() if now is None else now
            controlled = await self._store.checkpoint_operation_control(
                str(job["id"]), worker_id, now=timestamp
            )
            if controlled is not None and controlled["state"] != "running":
                return self._operations._response(controlled)
            work = await self._store.claim_operation_work(
                str(job["id"]), worker_id, now=timestamp
            )
            if work is None:
                done = await self._store.finish_operation_job(
                    str(job["id"]),
                    worker_id,
                    state="succeeded",
                    terminal_code="APPLY_COMPLETED",
                    now=timestamp,
                )
                return self._operations._response(done)
            renewed = await self._store.heartbeat_operation_job(
                str(job["id"]),
                worker_id,
                now=timestamp,
                lease_seconds=LEASE_SECONDS,
            )
            if not renewed:
                raise ResourceNotFoundError("The identity check lease changed.")
            await self._store.apply_repair_work(
                str(job["id"]),
                int(work["ordinal"]),
                worker_id=worker_id,
                expected_work_revision=int(work["row_revision"]),
                actor_user_id=actor_user_id,
                now=timestamp,
            )
            if checkpoint is not None:
                await checkpoint()

    async def findings(
        self,
        job_id: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
        finding_category: str | None = None,
    ) -> RepairFindingListResponse:
        if limit < 1 or limit > 200:
            raise ValidationError("Repair finding page size must be between 1 and 200.")
        snapshot = await self._store.get_operation_snapshot(job_id)
        if snapshot is None or snapshot["snapshot"] is None:
            raise ResourceNotFoundError("Repair job not found.")
        scope = json.loads(str(snapshot["snapshot"]["scope_json"]))
        management_readiness = scope.get("purpose") == MANAGEMENT_READINESS_PURPOSE
        if management_readiness:
            categories = {
                "ready": ["ready"],
                "mapping_ready": ["mapping_ready"],
                "exact_release_required": [
                    "exact_release_required",
                    "exact_release_suggested",
                ],
                "needs_review": ["needs_review"],
                "unverifiable": ["unverifiable", "stale"],
            }
        else:
            categories = {
                "valid": ["valid"],
                "safe_detach": ["safe_detach"],
                "needs_review": ["needs_review"],
                "unverifiable": ["unverifiable", "stale"],
                "manual_identity": ["manual_identity"],
            }
        if finding_category is not None and finding_category not in categories:
            raise ValidationError("The repair finding category is invalid.")
        cursor_updated_at: float | None = None
        cursor_id: str | None = None
        if cursor is not None:
            try:
                updated, cursor_id = cursor.split(":", 1)
                cursor_updated_at = float(updated)
            except (TypeError, ValueError) as error:
                raise ValidationError(
                    "The repair finding cursor is invalid."
                ) from error
        result = await self._store.list_repair_findings(
            job_id,
            limit=limit,
            finding_codes=categories.get(finding_category),
            cursor_updated_at=cursor_updated_at,
            cursor_id=cursor_id,
            current_only=management_readiness,
        )
        rows = result["rows"]
        next_cursor = None
        if result["has_more"] and rows:
            next_cursor = f"{rows[-1]['updated_at']}:{rows[-1]['id']}"

        def _suggested_edition(row: dict) -> SuggestedEditionSummary | None:
            if not row["suggested_release_mbid"]:
                return None
            payload = json.loads(str(row["suggested_edition_json"]))
            return SuggestedEditionSummary(
                release_mbid=str(row["suggested_release_mbid"]),
                release_group_mbid=str(row["suggested_release_group_mbid"]),
                title=str(payload.get("title") or ""),
                track_count=int(payload.get("track_count") or 0),
                competing_count=int(payload.get("competing_count") or 1),
                date=payload.get("date"),
                country=payload.get("country"),
                status=payload.get("status"),
            )

        return RepairFindingListResponse(
            items=[
                RepairFindingResponse(
                    id=str(row["id"]),
                    local_album_id=str(row["local_album_id"]),
                    album_title=str(row["album_title"]),
                    album_artist_name=row["album_artist_name"],
                    album_year=row["album_year"],
                    cover_available=bool(row["cover_available"]),
                    evidence_id=row["evidence_id"],
                    review_id=row["review_id"],
                    finding_code=str(row["finding_code"]),
                    reason_code=str(row["reason_code"]),
                    confidence=str(row["confidence"]),
                    apply_eligible=bool(row["apply_eligible"]),
                    state=str(row["state"]),
                    apply_result=row["apply_result"],
                    suggested_edition=_suggested_edition(row),
                    updated_at=float(row["updated_at"]),
                    row_revision=int(row["row_revision"]),
                )
                for row in rows
            ],
            next_cursor=next_cursor,
            has_more=bool(result["has_more"]),
            current_counts_by_finding=result["current_counts_by_finding"],
            refresh_required=bool(
                management_readiness
                and result["target_matcher_version"] != MANAGEMENT_MAPPING_VERSION
            ),
        )

    async def _classify(
        self, job_id: str, work: dict, context: dict | None
    ) -> tuple[
        RepairFinding,
        IdentificationAttempt | None,
        list[IdentificationEvidenceRecord],
    ]:
        album_id = str(work["local_album_id"])
        if context is None or context["identity"] is None:
            return (
                self._finding(job_id, work, "stale", "IDENTITY_CHANGED", False),
                None,
                [],
            )
        identity = context["identity"]
        tracks = [row for row in context["tracks"] if row["availability"] == "indexed"]
        if not tracks:
            return (
                self._finding(job_id, work, "stale", "IDENTITY_CHANGED", False),
                None,
                [],
            )
        if identity["decision_source"] == "manual":
            return (
                self._finding(
                    job_id,
                    work,
                    "manual_identity",
                    "MANUAL_IDENTITY_REPORT_ONLY",
                    False,
                    identity_revision=int(identity["row_revision"]),
                ),
                None,
                [],
            )
        attempt: IdentificationAttempt | None = None
        records: list[IdentificationEvidenceRecord] = []
        stored: IdentificationEvidenceRecord | None = None
        candidate: AlbumCandidate | None = None
        fingerprint_filled = False
        if self._provider is not None and identity["release_mbid"]:
            try:
                candidate = await self._provider.get_exact_release_candidate(
                    str(identity["release_mbid"]),
                    RequestPriority.BACKGROUND_SYNC,
                )
            except (ExternalServiceError, CircuitOpenError) as error:
                raise _ProviderUnavailable(
                    "MusicBrainz is unavailable; deferring the identity audit."
                ) from error
            if candidate is not None:
                grouping_tracks = [_to_grouping_track(row) for row in tracks]
                for track, row in zip(grouping_tracks, tracks, strict=True):
                    cached = await self._store.get_fingerprint_outcome(
                        track.local_track_id,
                        str(row["stat_revision"]),
                        FINGERPRINTER_VERSION,
                    )
                    if (
                        not track.recording_mbid
                        and cached is not None
                        and cached.state == "matched"
                        and cached.recording_mbid
                    ):
                        track.recording_mbid = cached.recording_mbid
                        fingerprint_filled = True
                evaluated = self._evidence.evaluate_candidate(
                    grouping_tracks, candidate
                )
                if (
                    candidate.release_mbid != identity["release_mbid"]
                    or candidate.release_group_mbid != identity["release_group_mbid"]
                ):
                    evaluated.reason_code = "CONFLICTING_TRACK_EVIDENCE"
                attempt_id = str(uuid.uuid4())
                evidence_id = str(uuid.uuid4())
                revisions = album_input_revisions(tracks)
                attempt = IdentificationAttempt(
                    id=attempt_id,
                    local_album_id=album_id,
                    trigger="repair_audit",
                    input_tag_revision=revisions[0],
                    input_file_revision=revisions[1],
                    input_policy_revision=revisions[2],
                    input_identity_revision=album_identity_revision(identity, tracks),
                    matcher_version=MATCHER_VERSION,
                    state=(
                        "identified"
                        if evaluated.reason_code == "SUPPORTED"
                        else "contradictory"
                    ),
                    terminal_reason_code=evaluated.reason_code,
                    selected_candidate_key=_candidate_key(evaluated),
                    candidate_count=1,
                    started_at=float(work["updated_at"]),
                    completed_at=float(work["updated_at"]),
                )
                stored = IdentificationEvidenceRecord(
                    id=evidence_id,
                    attempt_id=attempt_id,
                    candidate_key=_candidate_key(evaluated),
                    evidence=evaluated,
                    created_at=float(work["updated_at"]),
                )
                records = [stored]
        if stored is None:
            return (
                self._finding(
                    job_id,
                    work,
                    "unverifiable",
                    "EVIDENCE_UNAVAILABLE",
                    False,
                    identity_revision=int(identity["row_revision"]),
                ),
                None,
                [],
            )
        supported = sum(
            item.classification == "supported"
            for item in stored.evidence.track_evidence
        )
        contradictory = sum(
            item.classification == "contradictory"
            for item in stored.evidence.track_evidence
        )
        complete = bool(
            candidate is not None
            and len(candidate.tracks) == len(tracks)
            and len(stored.evidence.track_evidence) == len(tracks)
        )
        safe = (
            complete
            and not fingerprint_filled
            and (supported == 0 or contradictory > 0)
        )
        if safe:
            finding_code = "safe_detach"
            reason = "ZERO_SUPPORT" if supported == 0 else "HARD_CONTRADICTION"
        elif stored.evidence.reason_code in {
            "ACCEPTED",
            "SUPPORTED",
            "SUPPORTED_EMBEDDED_IDS",
        }:
            finding_code = "valid"
            reason = "CURRENT_IDENTITY_PASSES"
        else:
            finding_code = "needs_review"
            reason = "NON_TERMINAL_SAFETY_CONCERN"
        return (
            self._finding(
                job_id,
                work,
                finding_code,
                reason,
                safe,
                evidence_id=stored.id,
                identity_revision=int(identity["row_revision"]),
            ),
            attempt,
            records,
        )

    @staticmethod
    def _finding(
        job_id: str,
        work: dict,
        finding_code: str,
        reason_code: str,
        apply_eligible: bool,
        *,
        evidence_id: str | None = None,
        identity_revision: int | None = None,
    ) -> RepairFinding:
        return RepairFinding(
            id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{job_id}:{work['local_album_id']}:{finding_code}",
                )
            ),
            local_album_id=str(work["local_album_id"]),
            expected_album_revision=int(work["expected_subject_revision"]),
            expected_identity_revision=identity_revision,
            finding_code=finding_code,
            reason_code=reason_code,
            confidence="complete" if apply_eligible else "bounded",
            apply_eligible=apply_eligible,
            evidence_id=evidence_id,
        )
