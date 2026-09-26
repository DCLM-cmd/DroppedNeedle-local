/**
 * Hand-mirrors of the QW9 runtime-observability wire contracts (Parts 1/3):
 * GET /api/v1/system/queue-stats and GET /api/v1/system/provider-stats.
 *
 * Reconciled against the landed backend/api/v1/schemas/system.py
 * (QueueStatsRow / QueueStatsResponse / ProviderStatRow / ProviderStatsResponse).
 */

/** Priority-lane occupancy from PriorityQueueManager.get_stats() (mirrors
 * QueueStatsRow verbatim). user_active is a boolean in-flight flag, not a
 * count. Counters are per-process and reset on restart. */
export interface QueueStatsRow {
	user_slots_available: number;
	image_slots_available: number;
	background_slots_available: number;
	user_active: boolean;
	background_waiters: number;
}

/** Envelope for the queue gauges (mirrors QueueStatsResponse). */
export interface QueueStats {
	stats: QueueStatsRow;
}

/** One (provider, priority lane, outcome) counter bucket with its windowed
 * rate. Counters are per-process and reset on restart. */
export interface ProviderStatsRow {
	provider: string;
	priority: string;
	outcome: string;
	count_total: number;
	rate_per_min_window: number;
	source_mode?: string;
	source_id?: string;
	source_generation?: number;
	request_category?: string;
	include_profile?: string;
	workload?: string;
	downloaded_body_bytes_total?: number;
	decoded_body_bytes_total?: number;
	unknown_body_attempts_total?: number;
	overflow?: boolean;
}

/** Envelope for the windowed provider-call counters (mirrors
 * ProviderStatsResponse). */
export interface ProviderStats {
	providers: ProviderStatsRow[];
	window_seconds: number;
	counters_since: number | null;
	body_byte_scope: string;
	detailed_series_limit: number;
	process_epoch: string;
}
