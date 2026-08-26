"""Durable custom-edition and exact-edition conversion contracts."""

from __future__ import annotations

from typing import Literal

import msgspec


ManagementIdentityKind = Literal["exact_release", "custom_edition"]
EditionConversionState = Literal[
    "preflight",
    "acquiring",
    "ready",
    "needs_recheck",
    "cancelled",
    "failed",
    "applied",
]
EditionConversionTargetState = Literal[
    "kept", "pending", "downloading", "staged", "failed"
]
EditionConversionLocalAction = Literal[
    "keep", "recycle_conflict", "recycle_duplicate", "recycle_extra"
]


class ImmutableEditionStruct(msgspec.Struct, frozen=True, kw_only=True):
    pass


class CustomEditionTrack(ImmutableEditionStruct):
    manifest_id: str
    ordinal: int
    local_track_id: str
    source_track_revision: int
    source_identity_revision: int | None
    stat_revision: str
    tag_revision: str
    title: str
    artist_name: str
    album_title: str
    album_artist_name: str
    disc_number: int
    track_number: int
    recording_mbid: str | None = None
    artist_mbid: str | None = None
    album_artist_mbid: str | None = None
    metadata_json: str = "{}"
    file_format: str = ""
    duration_seconds: float | None = None


class CustomEditionManifest(ImmutableEditionStruct):
    id: str
    local_album_id: str
    version: int
    release_group_mbid: str
    album_title: str
    album_artist_name: str
    artist_mbid: str | None
    album_metadata_json: str
    source_album_revision: int
    source_identity_revision: int | None
    input_revision: str
    content_hash: str
    selected_candidate_key: str | None
    sealed_by_user_id: str
    sealed_at: float
    tracks: tuple[CustomEditionTrack, ...] = ()


class CustomEditionState(ImmutableEditionStruct):
    manifest: CustomEditionManifest
    stale: bool
    recognized_track_count: int


class ManagementExclusion(ImmutableEditionStruct):
    local_album_id: str
    reason: str
    excluded_by_user_id: str
    excluded_at: float
    row_revision: int = 1


class EditionConversionLocalFile(ImmutableEditionStruct):
    job_id: str
    local_track_id: str
    action: EditionConversionLocalAction
    target_ordinal: int | None
    evidence_kind: str
    expected_track_revision: int
    expected_identity_revision: int | None
    expected_stat_revision: str


class EditionConversionTarget(ImmutableEditionStruct):
    job_id: str
    ordinal: int
    disc_number: int
    track_number: int
    release_track_mbid: str
    recording_mbid: str
    title: str
    duration_seconds: float | None
    state: EditionConversionTargetState
    kept_local_track_id: str | None = None
    staged_artifact_id: str | None = None
    failure_code: str | None = None
    row_revision: int = 1


class EditionConversionArtifact(ImmutableEditionStruct):
    id: str
    job_id: str
    target_ordinal: int
    held_path: str
    file_sha256: str
    fingerprint: str | None
    release_track_mbid: str
    recording_mbid: str
    source_kind: Literal["download", "free_music", "retained_copy"]
    source_task_id: str | None
    file_size_bytes: int
    created_at: float


class EditionConversionJob(ImmutableEditionStruct):
    id: str
    local_album_id: str
    target_release_group_mbid: str
    target_release_mbid: str
    target_album_title: str
    target_artist_name: str
    state: EditionConversionState
    expected_album_revision: int
    expected_input_revision: str
    expected_identity_revision: str
    preflight_token_hash: str
    download_source_ready: bool
    required_temporary_bytes: int
    kept_count: int
    acquire_count: int
    recycle_count: int
    staged_count: int
    failed_count: int
    final_preview_job_id: str | None
    final_preview_token_hash: str | None
    final_bundle_json: str | None
    final_bundle_hash: str | None
    requested_by_user_id: str
    error_code: str | None
    created_at: float
    updated_at: float
    row_revision: int = 1
    targets: tuple[EditionConversionTarget, ...] = ()
    local_files: tuple[EditionConversionLocalFile, ...] = ()
    artifacts: tuple[EditionConversionArtifact, ...] = ()
