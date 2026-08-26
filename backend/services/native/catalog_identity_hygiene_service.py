"""Durable, catalog-only repair for proven legacy album identity defects."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable

from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_work import OperationJob
from services.native.background_workload_gate import BackgroundWorkloadGate

CATALOG_IDENTITY_HYGIENE_PURPOSE = "catalog_identity_hygiene"
CATALOG_IDENTITY_HYGIENE_VERSION = "catalog-identity-hygiene-v1"
_BACKFILL_IDEMPOTENCY_KEY = "catalog-identity-hygiene:v1:backfill"


def _input_revision(context: dict) -> str:
    identity = context.get("identity")
    payload = {
        "version": CATALOG_IDENTITY_HYGIENE_VERSION,
        "album_id": context["album"]["id"],
        "album_revision": context["album"]["row_revision"],
        "identity": (
            {
                "release_group_mbid": identity.get("release_group_mbid"),
                "release_mbid": identity.get("release_mbid"),
                "decision_source": identity.get("decision_source"),
                "row_revision": identity.get("row_revision"),
            }
            if identity is not None
            else None
        ),
        "tracks": [
            {
                "id": track["id"],
                "row_revision": track["row_revision"],
                "availability": track["availability"],
                "tag_album_title": track.get("tag_album_title"),
                "tag_album_artist_name": track.get("tag_album_artist_name"),
                "embedded_release_group_mbid": track.get("embedded_release_group_mbid"),
            }
            for track in context["tracks"]
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


class CatalogIdentityHygieneService:
    def __init__(
        self,
        store: NativeLibraryStore,
        workload_gate: BackgroundWorkloadGate | None = None,
        on_catalog_changed: Callable[[], Awaitable[None]] | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._workload_gate = workload_gate
        self._on_catalog_changed = on_catalog_changed
        self._clock = clock

    async def enqueue_backfill(self) -> dict:
        now = self._clock()
        job = OperationJob(
            id=str(uuid.uuid4()),
            kind="repair",
            requested_by_user_id=None,
            input_catalog_revision=await self._store.get_catalog_revision(),
            idempotency_key=_BACKFILL_IDEMPOTENCY_KEY,
            created_at=now,
        )
        return await self._store.create_repair_operation(
            job,
            scope={"purpose": CATALOG_IDENTITY_HYGIENE_PURPOSE, "album_ids": []},
            source_matcher_version=None,
            target_matcher_version=CATALOG_IDENTITY_HYGIENE_VERSION,
        )

    async def enqueue_album(self, local_album_id: str) -> dict | None:
        context = await self._store.get_catalog_identity_hygiene_context(local_album_id)
        if context is None:
            return None
        revision = _input_revision(context)
        now = self._clock()
        job = OperationJob(
            id=str(uuid.uuid4()),
            kind="repair",
            requested_by_user_id=None,
            input_catalog_revision=await self._store.get_catalog_revision(),
            idempotency_key=(
                f"catalog-identity-hygiene:v1:{local_album_id}:{revision}"
            ),
            created_at=now,
        )
        return await self._store.create_repair_operation(
            job,
            scope={
                "purpose": CATALOG_IDENTITY_HYGIENE_PURPOSE,
                "album_ids": [local_album_id],
            },
            source_matcher_version=None,
            target_matcher_version=CATALOG_IDENTITY_HYGIENE_VERSION,
        )

    async def run_claimed(self, job: dict, worker_id: str) -> dict:
        job_id = str(job["id"])
        while True:
            now = self._clock()
            controlled = await self._store.checkpoint_operation_control(
                job_id, worker_id, now=now
            )
            if controlled is not None and controlled["state"] != "running":
                return controlled
            work = await self._store.claim_operation_work(job_id, worker_id, now=now)
            if work is None:
                return await self._store.finish_operation_job(
                    job_id,
                    worker_id,
                    state="succeeded",
                    terminal_code="CATALOG_IDENTITY_HYGIENE_COMPLETED",
                    now=now,
                )
            if self._workload_gate is not None and self._workload_gate.scan_active:
                return await self._store.defer_catalog_identity_hygiene_work(
                    job_id=job_id,
                    ordinal=int(work["ordinal"]),
                    worker_id=worker_id,
                    reason_code="FILESYSTEM_SCAN_ACTIVE",
                    now=now,
                )
            result = await self._store.apply_catalog_identity_hygiene_work(
                job_id=job_id,
                ordinal=int(work["ordinal"]),
                worker_id=worker_id,
                local_album_id=str(work["local_album_id"]),
                now=now,
            )
            if result["changed"] and self._on_catalog_changed is not None:
                await self._on_catalog_changed()
