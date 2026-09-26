"""Domain models for the drop importer (Store Sync phase 01c).

A *job* is one upload gesture (a zip, several zips, loose files). Extraction
splits it into *items*, one per top-level folder - the album-shaped unit the
identifier works on. Items are terminal at ``imported``/``skipped``/``failed``/
``discarded``; ``needs_review`` items keep their staged files on disk until the
user matches them to a release group or discards them.

Retention (F-02): staged review items pin disk, so the startup-only
``sweep_stale`` pass hard-deletes ``needs_review`` items whose parent job is
older than 90 days and unlinks their staged files. The clock is the job's
``created_at`` - items carry only ``updated_at``, which rewrites on every
touch. Free Music drops share this store and the same rule.
"""

from infrastructure.msgspec_fastapi import AppStruct


class JobStatus:
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ItemStatus:
    PROCESSING = "processing"
    IMPORTED = "imported"
    SKIPPED = "skipped"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    DISCARDED = "discarded"


class DropImportItem(AppStruct):
    id: int
    job_id: str
    folder_name: str
    status: str
    updated_at: float
    release_group_mbid: str | None = None
    album_title: str | None = None
    artist_name: str | None = None
    files_total: int = 0
    files_imported: int = 0
    detail: str | None = None
    staging_paths: list[str] = []


class DropImportJob(AppStruct):
    id: str
    user_id: str
    user_name: str
    status: str
    created_at: float
    upload_name: str
    staging_dir: str
    error: str | None = None
    items: list[DropImportItem] = []
