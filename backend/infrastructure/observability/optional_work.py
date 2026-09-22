"""Owner-operation accounting shared by optional discovery child tasks."""
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable, Iterator
from dataclasses import dataclass


class OptionalWorkDeferred(Exception):
    """Optional demand yielded without asserting provider failure or absence."""


@dataclass
class OptionalWorkBudget:
    remaining: int = 10
    guard: Callable[[], bool] | None = None


_budget: ContextVar[OptionalWorkBudget | None] = ContextVar("optional_work_budget", default=None)


def is_optional_work() -> bool:
    return _budget.get() is not None


class OptionalWorkReservation:
    def __init__(self, budget: OptionalWorkBudget) -> None:
        self._budget = budget
        self._dispatched = False
        self._refunded = False

    def mark_dispatched(self) -> None:
        if not self._refunded:
            self._dispatched = True

    def refund(self) -> None:
        if not self._dispatched and not self._refunded:
            self._refunded = True
            self._budget.remaining += 1


def check_optional_dispatch() -> None:
    budget = _budget.get()
    if budget is not None and budget.guard is not None and not budget.guard():
        raise OptionalWorkDeferred()


def reserve_optional_operation() -> OptionalWorkReservation | None:
    budget = _budget.get()
    if budget is None:
        return None
    check_optional_dispatch()
    if budget.remaining <= 0:
        raise OptionalWorkDeferred()
    budget.remaining -= 1
    return OptionalWorkReservation(budget)


@contextmanager
def optional_work_budget(budget: OptionalWorkBudget | None = None) -> Iterator[OptionalWorkBudget]:
    shared = budget if budget is not None else OptionalWorkBudget()
    token = _budget.set(shared)
    try:
        yield shared
    finally:
        _budget.reset(token)


@contextmanager
def optional_dispatch_guard(guard: Callable[[], bool]) -> Iterator[None]:
    budget = _budget.get()
    if budget is None:
        yield
        return
    previous = budget.guard
    budget.guard = guard if previous is None else lambda: previous() and guard()
    try:
        yield
    finally:
        budget.guard = previous
