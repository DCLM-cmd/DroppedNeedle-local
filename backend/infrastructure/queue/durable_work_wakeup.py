"""Race-safe in-process wake signals for durable SQLite work queues."""

from __future__ import annotations

import asyncio
from collections import defaultdict
import heapq


class DurableWorkWakeups:
    """Wake queue workers promptly while retaining durable timeout recovery.

    SQLite remains the source of truth. The monotonic revisions close the race where
    a producer commits after a worker's empty claim but before it clears the event.
    Scheduled wakes preserve durable retry deadlines without returning to polling.
    """

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._revisions: defaultdict[str, int] = defaultdict(int)
        self._deadlines: defaultdict[str, list[float]] = defaultdict(list)
        self._deadline_handles: dict[str, asyncio.TimerHandle] = {}

    def revision(self, queue_kind: str) -> int:
        return self._revisions[queue_kind]

    def notify(self, queue_kind: str) -> None:
        self._revisions[queue_kind] += 1
        self._event(queue_kind).set()

    def notify_after(self, queue_kind: str, delay_seconds: float) -> None:
        if delay_seconds <= 0:
            self.notify(queue_kind)
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + delay_seconds
        heapq.heappush(self._deadlines[queue_kind], deadline)
        current = self._deadline_handles.get(queue_kind)
        if current is None or deadline < current.when():
            if current is not None:
                current.cancel()
            self._deadline_handles[queue_kind] = loop.call_at(
                deadline, self._notify_due, queue_kind
            )

    def _notify_due(self, queue_kind: str) -> None:
        self._deadline_handles.pop(queue_kind, None)
        loop = asyncio.get_running_loop()
        deadlines = self._deadlines[queue_kind]
        now = loop.time()
        while deadlines and deadlines[0] <= now:
            heapq.heappop(deadlines)
        self.notify(queue_kind)
        if deadlines:
            self._deadline_handles[queue_kind] = loop.call_at(
                deadlines[0], self._notify_due, queue_kind
            )

    async def wait(
        self,
        queue_kind: str,
        *,
        after_revision: int,
        timeout_seconds: float,
    ) -> bool:
        event = self._event(queue_kind)
        if self._revisions[queue_kind] != after_revision:
            return True
        event.clear()
        if self._revisions[queue_kind] != after_revision:
            event.set()
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return False
        return True

    def _event(self, queue_kind: str) -> asyncio.Event:
        event = self._events.get(queue_kind)
        if event is None:
            event = asyncio.Event()
            self._events[queue_kind] = event
        return event
