from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, TYPE_CHECKING

import msgspec

from api.v1.schemas.discover import (
    DiscoverQueueResponse,
    DiscoverQueueStatusResponse,
    QueueGenerateResponse,
)
from repositories.musicbrainz_base import (
    MbSourceContext,
    capture_mb_source_context,
    is_mb_source_current,
    mb_source_commit_lock,
)
from services.discover_service import DiscoverService
from services.preferences_service import PreferencesService
from services.discover.snapshot_codec import decode_discover_queue
from infrastructure.observability.optional_work import OptionalWorkDeferred, optional_dispatch_guard, check_optional_dispatch
from infrastructure.observability.provider_counters import ProviderWorkload, provider_workload

if TYPE_CHECKING:
    from infrastructure.persistence.discovery_snapshot_store import (
        DiscoverySnapshotStore,
    )
    from repositories.coverart_repository import CoverArtRepository

logger = logging.getLogger(__name__)


class QueueBuildStatus(str, Enum):
    IDLE = "idle"
    BUILDING = "building"
    READY = "ready"
    ERROR = "error"


class SourceQueueState:
    __slots__ = (
        "source_context",
        "status",
        "queue",
        "error",
        "built_at",
        "persisted_stale",
        "task",
        "scheduled",
    )

    def __init__(self, source_context: MbSourceContext | None = None) -> None:
        self.source_context = source_context or capture_mb_source_context()
        self.status: QueueBuildStatus = QueueBuildStatus.IDLE
        self.queue: DiscoverQueueResponse | None = None
        self.error: str | None = None
        self.built_at: float = 0.0
        self.persisted_stale: bool = False
        self.task: asyncio.Task[None] | None = None
        self.scheduled = False


_QUEUE_SNAPSHOT_PREFIX = "discover_queue:"


class PersistedQueue(msgspec.Struct):
    queue: DiscoverQueueResponse
    built_at: float


class DiscoverQueueManager:
    def __init__(
        self,
        discover_service: DiscoverService,
        preferences_service: PreferencesService,
        cover_repo: CoverArtRepository | None = None,
        snapshot_store: "DiscoverySnapshotStore | None" = None,
    ) -> None:
        self._discover = discover_service
        self._preferences = preferences_service
        self._cover_repo = cover_repo
        self._snapshot_store = snapshot_store
        # Keyed per user; the queue follows the user's primary source internally.
        self._states: dict[str, SourceQueueState] = {}
        self._lock = asyncio.Lock()
        self._loaded_users: set[str] = set()
        self._loaded_sources: dict[str, MbSourceContext] = {}
    @staticmethod
    def _snapshot_key(user_id: str) -> str:
        return f"{_QUEUE_SNAPSHOT_PREFIX}{user_id}"

    async def ensure_loaded(self, user_id: str) -> None:
        if self._snapshot_store is None:
            return

        source_context = capture_mb_source_context()
        loaded_context = self._loaded_sources.get(user_id)
        if (
            user_id in self._loaded_users
            and loaded_context == source_context
            and is_mb_source_current(source_context)
        ):
            return

        async with self._lock:
            source_context = capture_mb_source_context()
            loaded_context = self._loaded_sources.get(user_id)
            if (
                user_id in self._loaded_users
                and loaded_context == source_context
                and is_mb_source_current(source_context)
            ):
                return

            self._loaded_users.add(user_id)
            self._loaded_sources[user_id] = source_context
            state = self._get_state(user_id, source_context)
            saved = await self._snapshot_store.get_with_stale(
                self._snapshot_key(user_id)
            )
            if saved is None or not self._state_is_current(
                user_id, state, source_context
            ):
                return
            payload, stale = saved
            try:
                queue, built_at = decode_discover_queue(payload)
            except (
                msgspec.DecodeError,
                msgspec.ValidationError,
                TypeError,
                ValueError,
            ):
                logger.warning("Ignoring an invalid Discover queue snapshot")
                return
            if not self._state_is_current(user_id, state, source_context):
                return
            state.queue = queue
            state.built_at = built_at
            state.persisted_stale = stale
            state.status = QueueBuildStatus.READY

    def _get_state(
        self, user_id: str, source_context: MbSourceContext | None = None
    ) -> SourceQueueState:
        source_context = source_context or capture_mb_source_context()
        state = self._states.get(user_id)
        if state is None or state.source_context != source_context:
            state = SourceQueueState(source_context)
            self._states[user_id] = state
        return state

    def _state_is_current(
        self,
        user_id: str,
        state: SourceQueueState,
        source_context: MbSourceContext,
    ) -> bool:
        return (
            self._states.get(user_id) is state
            and state.source_context == source_context
            and is_mb_source_current(source_context)
        )

    def _get_ttl(self) -> int:
        adv = self._preferences.get_advanced_settings()
        return adv.discover_queue_ttl


    def scheduled_enabled(self) -> bool:
        return self._preferences.get_advanced_settings().discover_queue_warm_cycle_build
    def _is_stale(self, state: SourceQueueState) -> bool:
        if state.queue is None:
            return True
        return state.persisted_stale or (time.time() - state.built_at) > self._get_ttl()

    def get_status(self, user_id: str) -> DiscoverQueueStatusResponse:
        state = self._get_state(user_id)
        if state.status == QueueBuildStatus.READY and state.queue:
            return DiscoverQueueStatusResponse(
                status=state.status.value,
                queue_id=state.queue.queue_id,
                item_count=len(state.queue.items),
                built_at=state.built_at,
                stale=self._is_stale(state),
            )
        if state.status == QueueBuildStatus.ERROR:
            return DiscoverQueueStatusResponse(
                status=state.status.value,
                error=state.error,
            )
        return DiscoverQueueStatusResponse(status=state.status.value)

    @staticmethod
    def _build_generate_response(
        action: str, status: DiscoverQueueStatusResponse
    ) -> QueueGenerateResponse:
        return QueueGenerateResponse(
            action=action,
            status=status.status,
            queue_id=status.queue_id,
            item_count=status.item_count,
            built_at=status.built_at,
            stale=status.stale,
            error=status.error,
        )

    def get_queue(self, user_id: str) -> DiscoverQueueResponse | None:
        state = self._get_state(user_id)
        if (
            state.status == QueueBuildStatus.READY
            and state.queue
            and not self._is_stale(state)
        ):
            return state.queue
        return None
    async def start_build(
        self, user_id: str, *, force: bool = False, scheduled: bool = False
    ) -> QueueGenerateResponse:
        await self.ensure_loaded(user_id)
        async with self._lock:
            source_context = capture_mb_source_context()
            state = self._get_state(user_id, source_context)
            if scheduled and not self.scheduled_enabled():
                return self._build_generate_response("disabled", self.get_status(user_id))
            if state.status == QueueBuildStatus.BUILDING and state.scheduled and not scheduled:
                if state.task:
                    state.task.cancel()
                previous = state
                state = SourceQueueState(source_context)
                state.queue, state.built_at = previous.queue, previous.built_at
                state.persisted_stale = previous.persisted_stale
                self._states[user_id] = state

            if state.status == QueueBuildStatus.BUILDING:
                return self._build_generate_response(
                    "already_building", self.get_status(user_id)
                )

            if (
                not force
                and state.status == QueueBuildStatus.READY
                and not self._is_stale(state)
            ):
                return self._build_generate_response(
                    "already_ready", self.get_status(user_id)
                )

            if state.task and not state.task.done():
                state.task.cancel()

            state.status = QueueBuildStatus.BUILDING
            state.error = None
            state.scheduled = scheduled
            state.task = asyncio.create_task(
                self._do_build(user_id, state, source_context)
            )
            state.task.add_done_callback(_log_queue_task_error)
            from core.task_registry import TaskRegistry

            try:
                TaskRegistry.get_instance().register(
                    f"discover-build-{user_id}-g{source_context.generation}",
                    state.task,
                )
            except RuntimeError:
                pass

        return self._build_generate_response("started", self.get_status(user_id))

    async def wait_for_build(self, user_id: str) -> bool:
        state = self._get_state(user_id)
        task = state.task
        if task is not None:
            await asyncio.shield(task)
        return (
            self._state_is_current(user_id, state, state.source_context)
            and state.status == QueueBuildStatus.READY
            and not self._is_stale(state)
        )

    async def build_lightweight_queue(
        self, user_id: str, count: int | None = None
    ) -> DiscoverQueueResponse:
        with provider_workload(ProviderWorkload.QUEUE):
            return await self._discover.build_queue(user_id, count=count)

    async def _do_build(
        self,
        user_id: str,
        state: SourceQueueState | None = None,
        source_context: MbSourceContext | None = None,
    ) -> None:
        source_context = source_context or capture_mb_source_context()
        state = state or self._get_state(user_id, source_context)
        lease = self._snapshot_store.user_lease(user_id) if self._snapshot_store else None
        def eligible() -> bool:
            return self._state_is_current(user_id, state, source_context) and (lease is None or lease.active) and (
                not state.scheduled or self.scheduled_enabled()
            )
        try:
            with optional_dispatch_guard(eligible):
                check_optional_dispatch()
                if not eligible():
                    raise OptionalWorkDeferred()
                queue = await self.build_lightweight_queue(user_id)
            async with self._lock:
                async with mb_source_commit_lock:
                    check_optional_dispatch()
                    if not eligible():
                        raise OptionalWorkDeferred()
                    state.queue = queue
                    state.built_at = time.time()
                    state.persisted_stale = False
                    state.status = QueueBuildStatus.READY
                    if self._snapshot_store:
                        try:
                            saved = PersistedQueue(
                                queue=queue, built_at=state.built_at
                            )
                            await self._snapshot_store.save(
                                self._snapshot_key(user_id),
                                user_id,
                                msgspec.json.encode(saved),
                                state.built_at,
                            )
                        except Exception as exc:  # noqa: BLE001 - queue remains usable in memory
                            logger.warning(
                                "Could not persist Discover queue snapshot: %s", exc
                            )
        except OptionalWorkDeferred:
            if self._state_is_current(user_id, state, source_context):
                state.status = QueueBuildStatus.READY if state.queue else QueueBuildStatus.IDLE
            raise
        except asyncio.CancelledError:
            if self._state_is_current(user_id, state, source_context):
                if state.status == QueueBuildStatus.BUILDING:
                    state.status = QueueBuildStatus.READY if state.queue else QueueBuildStatus.IDLE
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Background queue build failed: %s", e)
            if self._state_is_current(user_id, state, source_context):
                state.status = QueueBuildStatus.ERROR
                state.error = str(e)


    async def consume_queue(self, user_id: str) -> DiscoverQueueResponse | None:
        await self.ensure_loaded(user_id)
        async with self._lock:
            state = self._get_state(user_id)
            source_context = state.source_context
            async with mb_source_commit_lock:
                if not self._state_is_current(user_id, state, source_context):
                    return None
                return state.queue

    def invalidate(self, user_id: str | None = None) -> None:
        if user_id is None:
            # Invalidate every user's queues (shutdown / global cache clear).
            for key in list(self._states.keys()):
                self.invalidate(key)
            return

        previous_state = self._states.get(user_id)
        if previous_state and previous_state.task and not previous_state.task.done():
            previous_state.task.cancel()
        source_context = capture_mb_source_context()
        self._states[user_id] = SourceQueueState(source_context)
        self._loaded_users.add(user_id)
        self._loaded_sources[user_id] = source_context


def _log_queue_task_error(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None and not isinstance(exc, OptionalWorkDeferred):
        logger.error("Discover queue background task failed: %s", exc, exc_info=exc)
