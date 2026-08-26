"""Typed result wrapper for external integration calls.

Replaces silent ``return [] / return None`` degradation patterns with
an explicit result that carries upstream status metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

T = TypeVar("T")

IntegrationStatus = Literal["ok", "degraded", "error"]


@dataclass(frozen=True, slots=True)
class IntegrationResult(Generic[T]):
    """Outcome of an external-service call.

    ``data`` is ``None`` only when ``status == "error"`` (upstream failed
    completely).  For ``"degraded"`` the caller received *some* data -
    possibly stale or partial.
    """

    data: T | None
    source: str
    status: IntegrationStatus
    error_message: str | None = None
    # True only for deterministic payload-shape/validation failures (e.g. a
    # typed MusicBrainz response that cannot decode). Never set for HTTP,
    # network, rate-limit, or breaker-open failures.
    deterministic: bool = False


    @property
    def is_ok(self) -> bool:
        return self.status == "ok"

    @property
    def is_degraded(self) -> bool:
        return self.status == "degraded"

    @property
    def is_error(self) -> bool:
        return self.status == "error"


    @staticmethod
    def ok(data: T, source: str) -> IntegrationResult[T]:
        return IntegrationResult(data=data, source=source, status="ok")

    @staticmethod
    def degraded(
        data: T, source: str, msg: str
    ) -> IntegrationResult[T]:
        return IntegrationResult(
            data=data, source=source, status="degraded", error_message=msg
        )

    @staticmethod
    def deterministic_error(source: str, msg: str) -> IntegrationResult[None]:
        return IntegrationResult(
            data=None,
            source=source,
            status="error",
            error_message=msg,
            deterministic=True,
        )

    @staticmethod
    def error(source: str, msg: str) -> IntegrationResult[None]:
        return IntegrationResult(
            data=None, source=source, status="error", error_message=msg
        )


    def data_or(self, default: T) -> T:
        """Return ``self.data`` if present, else *default*."""
        return self.data if self.data is not None else default


def aggregate_status(
    *results: IntegrationResult,  # type: ignore[type-arg]
) -> IntegrationStatus:
    """Compute the worst status across multiple results.

    error > degraded > ok
    """
    dominated: IntegrationStatus = "ok"
    for r in results:
        if r.status == "error":
            return "error"
        if r.status == "degraded":
            dominated = "degraded"
    return dominated
