from __future__ import annotations

import json
import time
from typing import Any

from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_work import LibraryWorkItem


_RECENT_FAILURE_SECONDS = 24 * 60 * 60


class LibraryAdministrativeWorkService:
    def __init__(self, store: NativeLibraryStore, *, clock=time.time) -> None:
        self._store = store
        self._clock = clock

    async def active(self) -> list[LibraryWorkItem]:
        now = self._clock()
        rows = await self._store.list_active_administrative_library_work(
            failed_after=now - _RECENT_FAILURE_SECONDS,
        )
        items = [self._operation_item(row) for row in rows]
        recovery = await self._store.library_management_recovery_diagnostics()
        attention_count = int(recovery["needs_attention_count"]) + int(
            recovery["cleanup_pending_count"]
        )
        if attention_count:
            items.append(
                LibraryWorkItem(
                    id="recovery",
                    kind="recovery",
                    state="failed",
                    phase="recovery",
                    effect="attention",
                    total=attention_count,
                    unit="items",
                    indeterminate=True,
                    remaining_count=attention_count,
                    updated_at=float(recovery["oldest_updated_at"] or now),
                    failed_count=int(recovery["needs_attention_count"]),
                    warning_count=int(recovery["cleanup_pending_count"]),
                    priority=0,
                    failure_event_id=f"recovery:{attention_count}",
                    failure_at=float(recovery["oldest_updated_at"] or now),
                )
            )
        return sorted(
            items, key=lambda item: (item.priority, -item.updated_at, item.id)
        )

    @staticmethod
    def _operation_item(row: dict[str, Any]) -> LibraryWorkItem:
        kind = str(row["kind"])
        state = str(row["state"])
        failed = state == "failed"
        common = {
            "id": str(row["id"]),
            "state": state,
            "started_at": (
                float(row["started_at"])
                if row.get("started_at") is not None
                else float(row["created_at"])
            ),
            "updated_at": float(row["updated_at"]),
            "succeeded_count": int(row["succeeded_count"]),
            "failed_count": int(row["failed_count"]),
            "skipped_count": int(row["skipped_count"]),
            "failure_event_id": str(row["id"]) if failed else None,
            "failure_at": float(row["terminal_at"]) if failed else None,
        }
        if kind == "library_management":
            summary = _json_object(row.get("management_summary_json"))
            mode = str(row.get("management_mode") or "preview")
            phase = str(row.get("management_phase") or "planning")
            file_writing = phase in {"applying", "undoing", "restoring"}
            if file_writing:
                phase = _journal_phase(row.get("journal_states_json"), phase)
            selected_count = _optional_int(summary.get("selected_item_count"))
            item_count = int(summary.get("item_count") or 0)
            return LibraryWorkItem(
                **common,
                kind="library_management",
                phase=phase,
                mode=mode,
                effect="attention"
                if failed
                else "file_writing"
                if file_writing
                else "catalog_only",
                processed=(int(row["completed_count"]) if file_writing else item_count),
                total=(
                    int(row["expected_work_count"]) if file_writing else selected_count
                ),
                unit="releases" if file_writing else "files",
                indeterminate=(
                    int(row["expected_work_count"]) <= 0
                    if file_writing
                    else selected_count is None
                ),
                subject_count=item_count or selected_count,
                origin=str(row.get("management_origin") or "manual"),
                profile_name=(
                    str(row["management_profile_name"])
                    if row.get("management_profile_name") is not None
                    else None
                ),
                warning_count=int(summary.get("warning_count") or 0),
                blocked_count=int(summary.get("blocked_count") or 0),
                priority=0 if failed else 10 if file_writing else 30,
            )
        if kind == "repair":
            purpose = str(row.get("repair_purpose") or "existing_matches")
            work_kind = (
                "identity_preparation"
                if purpose == "management_readiness"
                else "maintenance"
            )
            return LibraryWorkItem(
                **common,
                kind=work_kind,
                phase="checking_identities",
                mode=purpose,
                effect="attention" if failed else "catalog_only",
                processed=int(row["completed_count"]),
                total=int(row["expected_work_count"]) or None,
                unit="albums",
                indeterminate=int(row["expected_work_count"]) <= 0,
                priority=0
                if failed
                else 40
                if work_kind == "identity_preparation"
                else 60,
            )
        if kind == "explicit_reidentification":
            return LibraryWorkItem(
                **common,
                kind="reidentification",
                phase="checking_exact_edition",
                effect="attention" if failed else "catalog_only",
                processed=int(row["completed_count"]),
                total=int(row["expected_work_count"]) or 1,
                unit="albums",
                scope_label=(
                    str(row["album_title"])
                    if row.get("album_title") is not None
                    else None
                ),
                priority=0 if failed else 45,
            )
        if kind == "bulk_review_apply":
            return LibraryWorkItem(
                **common,
                kind="identity_review",
                phase="applying_identity_decisions",
                mode=str(row.get("bulk_action") or "review"),
                effect="attention" if failed else "catalog_only",
                processed=int(row["completed_count"]),
                total=int(row["expected_work_count"]) or None,
                unit="items",
                indeterminate=int(row["expected_work_count"]) <= 0,
                priority=0 if failed else 50,
            )
        return LibraryWorkItem(
            **common,
            kind="maintenance",
            phase="working",
            effect="attention" if failed else "catalog_only",
            processed=int(row["completed_count"]),
            total=int(row["expected_work_count"]) or None,
            indeterminate=int(row["expected_work_count"]) <= 0,
            priority=0 if failed else 60,
        )


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and value >= 0 else None


def _journal_phase(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    try:
        states = set(json.loads(value))
    except json.JSONDecodeError:
        return fallback
    if "planned" in states:
        return "preparing_snapshots"
    if "snapshot_saved" in states:
        return "writing_staged_files"
    if "staged" in states:
        return "validating_staged_files"
    if "source_backed_up" in states or "validated" in states:
        return "publishing_files"
    if "published" in states:
        return "committing_catalog"
    if "cleanup_pending" in states or "catalog_committed" in states:
        return "cleaning_up"
    return fallback
