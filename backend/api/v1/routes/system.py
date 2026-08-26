"""System status: which external services are currently degraded (drives the
header health indicator). Signal-driven from the in-process ServiceHealthRegistry."""

from fastapi import APIRouter

from api.v1.schemas.system import (
    ProviderRateLimitStat,
    ProviderStatRow,
    ProviderStatsResponse,
    QueueStatsResponse,
    QueueStatsRow,
)
from infrastructure.msgspec_fastapi import AppStruct, MsgSpecRoute
from infrastructure.observability import provider_counters
from infrastructure.queue.priority_queue import get_priority_queue
from infrastructure.service_health import service_health
from middleware import CurrentAdminDep, CurrentUserDep


class ServiceHealthItem(AppStruct):
    service: str
    capability: str
    severity: str
    message: str
    fallback: str | None = None
    degraded_seconds: int = 0


class SystemHealthResponse(AppStruct):
    degraded: list[ServiceHealthItem] = []


router = APIRouter(route_class=MsgSpecRoute, prefix="/system", tags=["system"])


@router.get("/health", response_model=SystemHealthResponse)
async def get_system_health(current_user: CurrentUserDep) -> SystemHealthResponse:
    entries = service_health.current()
    return SystemHealthResponse(
        degraded=[
            ServiceHealthItem(
                service=e.service,
                capability=e.capability,
                severity=e.severity,
                message=e.message,
                fallback=e.fallback,
                degraded_seconds=e.degraded_seconds,
            )
            for e in entries
        ]
    )


@router.get("/queue-stats", response_model=QueueStatsResponse)
async def get_queue_stats(_: CurrentAdminDep) -> QueueStatsResponse:
    """Pure in-memory gauge of priority-lane occupancy; no service layer."""
    return QueueStatsResponse(stats=QueueStatsRow(**get_priority_queue().get_stats()))


@router.get("/provider-stats", response_model=ProviderStatsResponse)
async def get_provider_stats(_: CurrentAdminDep) -> ProviderStatsResponse:
    rows = provider_counters.snapshot_provider_rows()
    return ProviderStatsResponse(
        providers=[ProviderStatRow(**row) for row in rows],
        window_seconds=provider_counters.DEFAULT_WINDOW_SECONDS,
        counters_since=provider_counters.counters_since(),
        rate_limits=[
            ProviderRateLimitStat(**row)
            for row in provider_counters.snapshot_rate_limit_rows()
        ],
    )
