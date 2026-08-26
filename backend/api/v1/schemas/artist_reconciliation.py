"""Administrator artist-identity reconciliation API contracts."""

from __future__ import annotations

from typing import Literal

import msgspec

from infrastructure.msgspec_fastapi import AppStruct

ArtistReconciliationGroupState = Literal[
    "waiting_for_identity",
    "provider_conflict",
    "ambiguous_credit_structure",
    "same_name_only",
    "resolved_automatically",
]


class ArtistReconciliationProgress(AppStruct):
    state: str
    completed_count: int = 0
    expected_count: int = 0
    automatically_resolved_count: int = 0
    waiting_for_identity_count: int = 0
    genuine_review_count: int = 0
    provider_conflict_count: int = 0
    ambiguous_credit_structure_count: int = 0
    same_name_only_count: int = 0
    operation_job_id: str | None = None


class ArtistReconciliationMember(AppStruct):
    id: str
    name: str
    sort_name: str | None
    row_revision: int
    provider_mbid: str | None
    album_credit_count: int
    track_credit_count: int
    primary_album_count: int
    favorite_count: int = 0
    playlist_count: int = 0
    history_count: int = 0
    compatibility_id_count: int = 0
    proven_credit_count: int = 0
    active_credit_count: int = 0


class ArtistDuplicateGroupSummary(AppStruct):
    id: str
    display_name: str
    state: ArtistReconciliationGroupState
    member_count: int
    members: list[ArtistReconciliationMember]
    provider_mbids: list[str] = msgspec.field(default_factory=list)
    recommended_survivor_id: str | None = None
    affected_reference_count: int = 0
    reason_code: str = ""
    resolved_at: float | None = None


class ArtistDuplicateGroupListResponse(AppStruct):
    items: list[ArtistDuplicateGroupSummary]
    next_cursor: str | None = None
    has_more: bool = False
    total: int = 0
    counts: dict[str, int] = msgspec.field(default_factory=dict)


class ArtistCreditEvidence(AppStruct):
    subject_kind: Literal["album", "track"]
    subject_id: str
    subject_name: str
    source_local_artist_id: str | None
    local_artist_id: str
    artist_mbid: str
    canonical_name: str
    credited_name: str
    join_phrase: str
    release_mbid: str
    release_track_mbid: str | None
    album_identity_revision: int
    track_identity_revision: int | None
    evidence_hash: str


class ArtistOwnedReference(AppStruct):
    id: str
    name: str
    row_revision: int
    identity_ready: bool
    exact_track_mapping_ready: bool


class ArtistDuplicateGroupDetail(ArtistDuplicateGroupSummary):
    evidence: list[ArtistCreditEvidence] = msgspec.field(default_factory=list)
    releases: list[ArtistOwnedReference] = msgspec.field(default_factory=list)
    tracks: list[ArtistOwnedReference] = msgspec.field(default_factory=list)
    reference_counts: dict[str, int] = msgspec.field(default_factory=dict)
    member_revisions: dict[str, int] = msgspec.field(default_factory=dict)


class ArtistDuplicateGroupDismissRequest(AppStruct):
    expected_member_revisions: dict[str, int]


class ArtistDuplicateGroupDismissResponse(AppStruct):
    group_id: str
    dismissed_pairs: int
