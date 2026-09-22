from typing import Annotated, Literal

import msgspec

from infrastructure.msgspec_fastapi import AppStruct


class CachePrefixStat(AppStruct):
    prefix: str
    hits: int
    misses: int
    sets: int
    hit_rate_percent: float
    window_seconds: int


class CacheStats(AppStruct):
    memory_entries: int
    memory_size_bytes: int
    memory_size_mb: float
    disk_metadata_count: int
    disk_metadata_albums: int
    disk_metadata_artists: int
    disk_cover_count: int
    disk_cover_size_bytes: int
    disk_cover_size_mb: float
    library_db_artist_count: int
    library_db_album_count: int
    library_db_size_bytes: int
    library_db_size_mb: float
    total_size_bytes: int
    total_size_mb: float
    memory_accounting: Literal["shallow"] = "shallow"
    response_entries: int = 0
    response_logical_bytes: int = 0
    response_hits: Annotated[int, msgspec.Meta(ge=0)] = 0
    response_evictions: Annotated[int, msgspec.Meta(ge=0)] = 0
    response_speculative_used: Annotated[int, msgspec.Meta(ge=0)] = 0
    database_allocated_bytes: int = 0
    database_wal_bytes: int = 0
    library_db_last_sync: int | None = None
    disk_audiodb_artist_count: int = 0
    disk_audiodb_album_count: int = 0
    memory_hits: int = 0
    memory_misses: int = 0
    memory_hit_rate_percent: float = 0.0
    per_prefix: list[CachePrefixStat] = []
    counters_since: int | None = None


class CacheClearResponse(AppStruct):
    success: bool
    message: str
    cleared_memory_entries: int = 0
    cleared_disk_files: int = 0
    cleared_response_entries: int = 0
    cleared_library_artists: int = 0
    cleared_library_albums: int = 0
    # Cover files are a subset of cleared_disk_files; response rows are separate.
    cover_files_cleared: int = 0
