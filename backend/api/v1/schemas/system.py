"""Admin-gated runtime observability contracts.

Pure gauges over in-process state: priority-lane occupancy from
``PriorityQueueManager.get_stats()``, outbound provider-call counters, and
upstream rate-limit telemetry. No cache keys are created anywhere behind these
responses, so there is no ``*_prefixes()`` membership and no sweep trigger
("nothing to invalidate").
"""

from infrastructure.msgspec_fastapi import AppStruct


class QueueStatsRow(AppStruct):
    """Mirrors ``PriorityQueueManager.get_stats()`` verbatim (snake_case)."""

    user_slots_available: int
    image_slots_available: int
    background_slots_available: int
    user_active: bool
    background_waiters: int


class QueueStatsResponse(AppStruct):
    stats: QueueStatsRow


class ProviderStatRow(AppStruct, omit_defaults=True):
    provider: str
    priority: str
    outcome: str
    count_total: int
    rate_per_min_window: float
    source_mode: str | None = None
    source_id: str | None = None
    source_generation: int | None = None
    request_category: str = "other"
    include_profile: str = "other"
    workload: str = "other"
    downloaded_body_bytes_total: int = 0
    decoded_body_bytes_total: int = 0
    unknown_body_attempts_total: int = 0
    overflow: bool = False


class ProviderRateLimitStat(AppStruct):
    """Latest observed upstream rate-limit headers for one provider. Pure
    telemetry - deliberately separate from the call counters so observations
    can never perturb call outcomes."""

    provider: str
    limit: int | None = None
    remaining: int | None = None
    # Verbatim x-ratelimit-reset value (MusicBrainz sends epoch seconds);
    # consumers compute seconds-until as reset_epoch - now.
    reset_epoch: float | None = None
    observed_at: float = 0.0
    low_remaining_events_window: int = 0


class ProviderStatsResponse(AppStruct):
    providers: list[ProviderStatRow] = []
    window_seconds: int = 3600
    counters_since: int | None = None
    rate_limits: list[ProviderRateLimitStat] = []
    body_byte_scope: str = "HTTP response bodies; excludes headers and TLS"
    detailed_series_limit: int = 1024
    process_epoch: str = ""
