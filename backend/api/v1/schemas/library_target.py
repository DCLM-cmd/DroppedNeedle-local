from typing import Literal

import msgspec

from infrastructure.msgspec_fastapi import AppStruct


class TargetNativeArtist(AppStruct):
    id: str
    name: str
    musicbrainz_artist_id: str | None = None
    artist_identity_state: Literal["local_only", "musicbrainz_linked"] = "local_only"
    album_count: int = 0
    track_count: int = 0
    appearance_release_count: int = 0
    appearance_track_count: int = 0
    library_relationship: Literal["album_artist", "contributor", "both"] = (
        "album_artist"
    )
    date_added: float | None = None
    row_revision: int = 1


class TargetNativeAlbum(AppStruct):
    id: str
    title: str
    artist_name: str
    artist_id: str
    musicbrainz_release_group_id: str | None = None
    musicbrainz_release_id: str | None = None
    musicbrainz_artist_id: str | None = None
    album_identity_state: Literal[
        "local_only", "release_group_linked", "release_linked", "custom_edition"
    ] = "local_only"
    track_count: int = 0
    total_duration_seconds: float = 0
    total_size_bytes: int = 0
    format: str | None = None
    year: int | None = None
    is_compilation: bool = False
    cover_available: bool = False
    date_added: float | None = None
    sort_name: str | None = None
    original_release_date: str | None = None
    contribution_id: str | None = None
    contribution_state: str | None = None


class ActiveEditionConversionSummary(AppStruct):
    job_id: str
    release_mbid: str
    state: Literal["preflight", "acquiring", "ready", "needs_recheck"]
    kept_count: int
    acquire_count: int
    staged_count: int
    failed_count: int
    recycle_count: int
    row_revision: int
    final_preview_job_id: str | None = None


class TargetNativeAlbumDetail(TargetNativeAlbum):
    row_revision: int = 1
    input_revision: str = ""
    identification_status: Literal[
        "identified",
        "needs_review",
        "keep_tagged",
        "local_metadata",
        "manual_identity_needs_review",
    ] = "local_metadata"
    review_id: str | None = None
    review_revision: int | None = None
    management_identity_readiness: Literal[
        "not_applicable",
        "exact_release_required",
        "track_mapping_required",
        "custom_manifest_stale",
        "ready",
    ] = "not_applicable"
    mapped_track_count: int = 0
    management_identity_kind: Literal["exact_release", "custom_edition"] | None = None
    custom_manifest_id: str | None = None
    custom_manifest_version: int | None = None
    custom_manifest_track_count: int = 0
    custom_manifest_recognized_track_count: int = 0
    custom_manifest_stale: bool = False
    management_excluded: bool = False
    management_exclusion_revision: int | None = None
    management_excluded_at: float | None = None
    active_edition_conversion: ActiveEditionConversionSummary | None = None


class ManagementReenableRequest(AppStruct):
    expected_exclusion_revision: int


class ManagementReenableResponse(AppStruct):
    reenabled: bool


class ReleaseEditionResult(AppStruct):
    release_mbid: str
    release_group_mbid: str
    artist_name: str
    title: str
    musicbrainz_url: str
    date: str | None = None
    country: str | None = None
    status: str | None = None
    packaging: str | None = None
    media_formats: list[str] = msgspec.field(default_factory=list)
    disc_count: int = 0
    track_count: int = 0
    label: str | None = None
    catalogue_number: str | None = None
    barcode: str | None = None
    disambiguation: str | None = None
    score: int = 0
    belongs_to_current_release_group: bool = False
    is_current_release: bool = False


class ReleaseEditionSearchResponse(AppStruct):
    title_query: str
    artist_query: str
    items: list[ReleaseEditionResult] = msgspec.field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 12


class TargetNativeTrack(AppStruct):
    id: str
    title: str
    album_id: str
    album_title: str
    artist_id: str
    artist_name: str
    album_artist_id: str
    album_artist_name: str
    musicbrainz_recording_id: str | None = None
    musicbrainz_release_group_id: str | None = None
    musicbrainz_artist_id: str | None = None
    musicbrainz_album_artist_id: str | None = None
    disc_number: int = 1
    track_number: int = 0
    year: int | None = None
    genre: str | None = None
    duration_seconds: float = 0
    format: str = ""
    bit_rate: int | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    channels: int | None = None
    file_size_bytes: int = 0
    date_added: float | None = None
    cover_available: bool = False
    current_tier: str | None = None
    below_cutoff: bool = False


class TargetNativeAlbumsResponse(AppStruct):
    items: list[TargetNativeAlbum] = []
    total: int = 0


class TargetNativeArtistsResponse(AppStruct):
    items: list[TargetNativeArtist] = []
    total: int = 0
    album_artist_total: int = 0
    contributor_total: int = 0


class TargetNativeArtistAppearance(AppStruct):
    album: TargetNativeAlbum
    tracks: list[TargetNativeTrack] = []


class TargetNativeArtistAppearancesResponse(AppStruct):
    items: list[TargetNativeArtistAppearance] = []
    total: int = 0
    total_tracks: int = 0
    offset: int = 0
    limit: int = 0


class TargetNativeTracksResponse(AppStruct):
    items: list[TargetNativeTrack] = []
    total: int = 0
    offset: int = 0
    limit: int = 0


class TargetNativeStatsResponse(AppStruct):
    total_albums: int = 0
    total_artists: int = 0
    total_tracks: int = 0
    total_size_bytes: int = 0
    format_breakdown: dict[str, int] = {}
    review_count: int = 0
    local_only_count: int = 0
    last_scan_at: float | None = None


class TargetNativeAlbumStatusResponse(AppStruct):
    in_library: bool
    album_id: str
    track_count: int = 0
    tracks: list[TargetNativeTrack] = []


class TargetNativeProviderIdsResponse(AppStruct):
    musicbrainz_release_group_ids: list[str] = []


class TargetCatalogRemovalResponse(AppStruct):
    success: bool
    id: str
    removed_track_ids: list[str] = []
