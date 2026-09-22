"""Revision-keyed, conditional fingerprinting outside filesystem scans."""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import logging

from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.audio import FingerprintResult
from models.identification import FingerprintOutcome

logger = logging.getLogger(__name__)
FINGERPRINTER_VERSION = "fpcalc-acoustid-v1"
MAX_CONCURRENT_FINGERPRINTS = 2
TRANSIENT_RETRY_SECONDS = 60.0


class FingerprinterProtocol(Protocol):
    def is_enabled(self) -> bool: ...

    async def generate_fingerprint(self, path: Path) -> tuple[str, int]: ...

    async def generate_tracked(self, path: Path) -> tuple[str, int, bool]: ...

    async def lookup_fingerprint(
        self, fingerprint: str, duration: int
    ) -> FingerprintResult: ...


class ConditionalFingerprintService:
    def __init__(
        self,
        store: NativeLibraryStore,
        fingerprinter: FingerprinterProtocol,
    ) -> None:
        self._store = store
        self._fingerprinter = fingerprinter
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_FINGERPRINTS)

    async def fingerprint_if_needed(
        self,
        *,
        local_track_id: str,
        path: Path,
        stat_revision: str,
        needed: bool,
        now: float | None = None,
        checkpoint: Callable[[], Awaitable[bool]] | None = None,
    ) -> tuple[FingerprintOutcome | None, bool]:
        """Return ``(outcome, did_work)`` - ``did_work`` is True only when
        this call performed fresh fingerprint work. A reused cached row
        (including a cached ``failed`` row, whose ``cache_hit``-style signal
        reads False) returns False so callers never charge budget or defer
        on work they did not do."""
        if not needed:
            # 4.9: attempted-unnecessary writes a skipped row so the three
            # terminal guards (conditional cache reuse, identify-lane
            # cache_hit, re-id cache_hit) see honest terminal state instead
            # of never-attempted None. Terminal-free: did_work stays False
            # so F-042/F-07 budget accounting never charges skipped, and the
            # re-id worker consumes the same tuple identically. Write once
            # per stat_revision: an identical skipped row is reused without
            # another upsert (no per-attempt attempt_count noise - the store
            # also treats skipped as terminal).
            cached_skipped = await self._store.get_fingerprint_outcome(
                local_track_id, stat_revision, FINGERPRINTER_VERSION
            )
            if cached_skipped is not None:
                return cached_skipped, False
            timestamp_skipped = time.time() if now is None else now
            skipped = FingerprintOutcome(
                id=str(uuid.uuid4()),
                local_track_id=local_track_id,
                stat_revision=stat_revision,
                fingerprinter_version=FINGERPRINTER_VERSION,
                state="skipped",
                first_attempt_at=timestamp_skipped,
                last_attempt_at=timestamp_skipped,
            )
            await self._store.record_fingerprint_outcome(skipped)
            logger.debug(
                "fingerprint fresh: local_track_id=%s state=skipped",
                local_track_id,
            )
            return (
                await self._store.get_fingerprint_outcome(
                    local_track_id, stat_revision, FINGERPRINTER_VERSION
                ),
                False,
            )
        if checkpoint is not None and not await checkpoint():
            return None, False
        cached = await self._store.get_fingerprint_outcome(
            local_track_id, stat_revision, FINGERPRINTER_VERSION
        )
        timestamp = time.time() if now is None else now
        if cached is not None and cached.state == "disabled":
            # F-041: reuse a disabled row ONLY while AcoustID stays disabled;
            # once a key exists, fall through so generation proceeds instead
            # of freezing this stat_revision forever. An F-06
            # FPCALC_BINARY_ABSENT row always falls through here (the key is
            # present), so a later-installed binary is picked up - the
            # absence is recorded for evidence but never reused.
            if not self._fingerprinter.is_enabled():
                logger.debug(
                    "fingerprint cached: local_track_id=%s state=disabled",
                    local_track_id,
                )
                return cached, False
        elif cached is not None and (
            cached.state not in ("failed", "deferred")
            or cached.retry_after is None
            or cached.retry_after > timestamp
        ):
            logger.debug(
                "fingerprint cached: local_track_id=%s state=%s",
                local_track_id,
                cached.state,
            )
            return cached, False
        if not self._fingerprinter.is_enabled():
            disabled = FingerprintOutcome(
                id=cached.id if cached is not None else str(uuid.uuid4()),
                local_track_id=local_track_id,
                stat_revision=stat_revision,
                fingerprinter_version=FINGERPRINTER_VERSION,
                state="disabled",
                failure_code="ACOUSTID_KEY_ABSENT",
                first_attempt_at=(cached.first_attempt_at if cached else timestamp),
                last_attempt_at=timestamp,
            )
            await self._store.record_fingerprint_outcome(disabled)
            logger.debug(
                "fingerprint fresh: local_track_id=%s state=disabled",
                local_track_id,
            )
            return (
                await self._store.get_fingerprint_outcome(
                    local_track_id, stat_revision, FINGERPRINTER_VERSION
                ),
                True,
            )
        if cached is not None and cached.fingerprint and cached.duration_seconds:
            fingerprint = cached.fingerprint
            duration = int(cached.duration_seconds)
            # 4.10a: a resumed fingerprint keeps the generation's partial flag
            # so the final outcome below preserves it.
            partial_decode = bool(getattr(cached, "partial_decode", False))
        else:
            partial_decode = False
            try:
                async with self._semaphore:
                    # 4.10a: the tracked generation carries the fpcalc
                    # tolerance flag (:374 via :238-256) instead of discarding
                    # it like generate_fingerprint() (:234-236). Older
                    # fakes only speak generate_fingerprint(); they stay
                    # full (False) - today's behavior.
                    tracked = getattr(
                        self._fingerprinter, "generate_tracked", None
                    )
                    if tracked is None:
                        tracked = getattr(
                            self._fingerprinter, "_generate_tracked", None
                        )
                    if tracked is not None:
                        fingerprint, duration, partial_decode = await tracked(
                            path
                        )
                    else:
                        generated = await self._fingerprinter.generate_fingerprint(
                            path
                        )
                        if isinstance(generated, tuple) and len(generated) == 3:
                            fingerprint, duration, partial_decode = generated  # type: ignore[misc]
                        else:
                            fingerprint, duration = generated  # type: ignore[misc]
                            partial_decode = False
            except FileNotFoundError:
                # F-06: the fpcalc binary is absent - a permanent host
                # condition, not a transient fault. Map to disabled (existing
                # state, new cause) and proceed tag-only exactly like
                # ACOUSTID_KEY_ABSENT. This handler MUST precede the OSError
                # handler below (FileNotFoundError subclasses OSError);
                # non-FileNotFoundError OSErrors keep the failure path.
                # Genuine mid-run crashes (CalledProcessError, TimeoutError,
                # parse ValueError) stay `failed` -> defer.
                binary_absent = FingerprintOutcome(
                    id=cached.id if cached is not None else str(uuid.uuid4()),
                    local_track_id=local_track_id,
                    stat_revision=stat_revision,
                    fingerprinter_version=FINGERPRINTER_VERSION,
                    state="disabled",
                    failure_code="FPCALC_BINARY_ABSENT",
                    first_attempt_at=(cached.first_attempt_at if cached else timestamp),
                    last_attempt_at=timestamp,
                )
                await self._store.record_fingerprint_outcome(binary_absent)
                logger.debug(
                    "fingerprint fresh: local_track_id=%s state=disabled",
                    local_track_id,
                )
                return (
                    await self._store.get_fingerprint_outcome(
                        local_track_id, stat_revision, FINGERPRINTER_VERSION
                    ),
                    True,
                )
            except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
                # F-MATCH-04: a LOCAL failure is a transient with a bounded
                # retry deadline, mirroring the lookup-failure branch. Before
                # the deadline the cached outcome is reused; at the deadline
                # generation is attempted again on the same stat_revision key.
                failed = FingerprintOutcome(
                    id=cached.id if cached is not None else str(uuid.uuid4()),
                    local_track_id=local_track_id,
                    stat_revision=stat_revision,
                    fingerprinter_version=FINGERPRINTER_VERSION,
                    state="failed",
                    failure_code="FINGERPRINT_LOCAL_FAILURE",
                    attempt_count=(
                        cached.attempt_count + 1 if cached is not None else 1
                    ),
                    first_attempt_at=(cached.first_attempt_at if cached else timestamp),
                    last_attempt_at=timestamp,
                    retry_after=timestamp + TRANSIENT_RETRY_SECONDS,
                )
                await self._store.record_fingerprint_outcome(failed)
                logger.debug(
                    "fingerprint fresh: local_track_id=%s state=failed",
                    local_track_id,
                )
                return (
                    await self._store.get_fingerprint_outcome(
                        local_track_id, stat_revision, FINGERPRINTER_VERSION
                    ),
                    True,
                )
            await self._store.record_fingerprint_outcome(
                FingerprintOutcome(
                    id=cached.id if cached is not None else str(uuid.uuid4()),
                    local_track_id=local_track_id,
                    stat_revision=stat_revision,
                    fingerprinter_version=FINGERPRINTER_VERSION,
                    state="deferred",
                    fingerprint=fingerprint,
                    duration_seconds=float(duration),
                    failure_code="LOOKUP_PENDING",
                    first_attempt_at=(cached.first_attempt_at if cached else timestamp),
                    last_attempt_at=timestamp,
                    retry_after=timestamp,
                    partial_decode=partial_decode,
                )
            )
            if checkpoint is not None and not await checkpoint():
                return (
                    await self._store.get_fingerprint_outcome(
                        local_track_id, stat_revision, FINGERPRINTER_VERSION
                    ),
                    True,
                )
        result = await self._fingerprinter.lookup_fingerprint(fingerprint, duration)
        if checkpoint is not None and not await checkpoint():
            return (
                await self._store.get_fingerprint_outcome(
                    local_track_id, stat_revision, FINGERPRINTER_VERSION
                ),
                True,
            )
        state_by_status = {
            "pass": "matched",
            "skip": "no_match",
            "fail": "no_match",
            "disabled": "disabled",
            "error": "failed",
        }
        state = state_by_status[result.status]
        outcome = FingerprintOutcome(
            id=cached.id if cached is not None else str(uuid.uuid4()),
            local_track_id=local_track_id,
            stat_revision=stat_revision,
            fingerprinter_version=FINGERPRINTER_VERSION,
            state=state,
            fingerprint=fingerprint,
            duration_seconds=float(duration),
            recording_mbid=result.recording_id,
            release_group_ids=result.release_group_ids,
            score=result.score,
            failure_code=(
                "FINGERPRINT_TRANSIENT_FAILURE" if state == "failed" else None
            ),
            attempt_count=(cached.attempt_count + 1 if cached is not None else 1),
            first_attempt_at=(
                cached.first_attempt_at if cached is not None else timestamp
            ),
            last_attempt_at=timestamp,
            retry_after=(
                timestamp + TRANSIENT_RETRY_SECONDS if state == "failed" else None
            ),
            partial_decode=partial_decode,
        )
        await self._store.record_fingerprint_outcome(outcome)
        logger.debug(
            "fingerprint fresh: local_track_id=%s state=%s",
            local_track_id,
            state,
        )
        return (
            await self._store.get_fingerprint_outcome(
                local_track_id, stat_revision, FINGERPRINTER_VERSION
            ),
            True,
        )
