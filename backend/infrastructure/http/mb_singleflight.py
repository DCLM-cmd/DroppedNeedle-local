"""Bounded physical MusicBrainz ownership, independent of caller priority."""

import asyncio
from contextvars import ContextVar, Context, copy_context
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from infrastructure.observability.optional_work import check_optional_dispatch, reserve_optional_operation, OptionalWorkDeferred
from infrastructure.queue.priority_queue import RequestPriority


@dataclass
class Owner:
    future: asyncio.Future
    priority: RequestPriority
    factory: Callable[[RequestPriority], Awaitable[Any]]
    waiters: dict[object, Context] = field(default_factory=dict)
    task: asyncio.Task | None = None
    dispatched: bool = False
    reservation: Any = None

    def before_dispatch(self) -> None:
        for context in self.waiters.values():
            try:
                context.run(check_optional_dispatch)
                break
            except OptionalWorkDeferred:
                continue
        else:
            raise OptionalWorkDeferred()
        self.dispatched = True
        if self.reservation is not None:
            self.reservation.mark_dispatched()


current_owner: ContextVar[Owner | None] = ContextVar("mb_physical_owner", default=None)
_owners: dict[str, Owner] = {}
_capacity = asyncio.Condition()
MAX_OWNERS = 256


async def _run(key: str, owner: Owner) -> None:
    task = asyncio.current_task()
    token = current_owner.set(owner)
    try:
        result = await owner.factory(owner.priority)
        if not owner.future.done():
            owner.future.set_result(result)
    except asyncio.CancelledError:
        if owner.task is task and not owner.future.done():
            owner.future.cancel()
        raise
    except Exception as exc:  # noqa: BLE001 - the error is forwarded to waiting futures, not raised
        if not owner.future.done():
            owner.future.set_exception(exc)
    finally:
        current_owner.reset(token)
        if owner.task is task:
            if owner.reservation is not None and not owner.dispatched:
                owner.reservation.refund()
            async with _capacity:
                if _owners.get(key) is owner:
                    del _owners[key]
                _capacity.notify_all()


async def run(key: str, priority: RequestPriority, factory: Callable[[RequestPriority], Awaitable[Any]]) -> Any:
    waiter = object()
    async with _capacity:
        while key not in _owners and len(_owners) >= MAX_OWNERS:
            await _capacity.wait()
        owner = _owners.get(key)
        if owner is not None and (owner.future.done() or owner.task.cancelling()):
            owner = None
        if owner is None:
            reservation = reserve_optional_operation()
            owner = Owner(asyncio.get_running_loop().create_future(), priority, factory, reservation=reservation)
            _owners[key] = owner
            owner.waiters[waiter] = copy_context()
            owner.task = asyncio.create_task(_run(key, owner))
        else:
            owner.waiters[waiter] = copy_context()
            if priority < owner.priority and not owner.dispatched:
                old_task = owner.task
                owner.priority = priority
                owner.factory = factory
                owner.task = asyncio.create_task(_run(key, owner))
                if old_task is not None:
                    old_task.cancel()
    try:
        return await asyncio.shield(owner.future)
    finally:
        owner.waiters.pop(waiter, None)
        if not owner.waiters and owner.task is not None and not owner.task.done():
            owner.task.cancel()
        if owner.future.done() and not owner.future.cancelled():
            owner.future.exception()
