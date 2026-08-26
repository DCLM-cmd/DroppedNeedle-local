"""HTTP contracts for safe exact-edition conversion."""

from typing import Literal

import msgspec

from infrastructure.msgspec_fastapi import AppStruct


class EditionConversionPreflightRequest(AppStruct):
    release_group_mbid: str
    release_mbid: str


class EditionConversionStartRequest(AppStruct):
    preflight_token: str
    expected_row_revision: int
    confirmation: bool = False


class EditionConversionRetryRequest(AppStruct):
    expected_row_revision: int
    target_ordinals: list[int] = msgspec.field(default_factory=list)


class EditionConversionRecheckRequest(AppStruct):
    expected_row_revision: int


class EditionConversionCancelRequest(AppStruct):
    expected_row_revision: int
    confirmation: bool = False


class EditionConversionPreviewRequest(AppStruct):
    expected_row_revision: int


class EditionConversionTargetResponse(AppStruct):
    ordinal: int
    disc_number: int
    track_number: int
    release_track_mbid: str
    recording_mbid: str
    title: str
    duration_seconds: float | None
    state: Literal["kept", "pending", "downloading", "staged", "failed"]
    kept_local_track_id: str | None = None
    failure_code: str | None = None


class EditionConversionLocalFileResponse(AppStruct):
    local_track_id: str
    action: Literal["keep", "recycle_conflict", "recycle_duplicate", "recycle_extra"]
    target_ordinal: int | None
    evidence_kind: str


class EditionConversionStatusResponse(AppStruct):
    job_id: str
    local_album_id: str
    release_group_mbid: str
    release_mbid: str
    album_title: str
    artist_name: str
    state: Literal[
        "preflight",
        "acquiring",
        "ready",
        "needs_recheck",
        "cancelled",
        "failed",
        "applied",
    ]
    download_source_ready: bool
    required_temporary_bytes: int
    kept_count: int
    acquire_count: int
    recycle_count: int
    staged_count: int
    failed_count: int
    row_revision: int
    created_at: float
    updated_at: float
    targets: list[EditionConversionTargetResponse] = msgspec.field(default_factory=list)
    local_files: list[EditionConversionLocalFileResponse] = msgspec.field(
        default_factory=list
    )
    final_preview_job_id: str | None = None
    preflight_token: str | None = None
    error_code: str | None = None


class EditionConversionPreviewResponse(AppStruct):
    status: EditionConversionStatusResponse
    preview_token: str
