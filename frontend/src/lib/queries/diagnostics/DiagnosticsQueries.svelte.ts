import type { Getter } from 'runed';

import { api } from '$lib/api/client';
import { API } from '$lib/constants';
import { createQuery } from '@tanstack/svelte-query';

import { DiagnosticsQueryKeyFactory } from './DiagnosticsQueryKeyFactory';
import type { ProviderStats, QueueStats } from './types';

/** Gauges must stay fresh; polling is cheap - the backend counters are plain
 * dict reads. */
const POLL_INTERVAL_MS = 5_000;

/**
 * Outbound queue-lane occupancy (QW9 Part 1). `enabled` comes from the caller:
 * a closed settings tab or hidden window must issue no requests at all.
 */
export const getQueueStatsQuery = (getEnabled: Getter<boolean> = () => true) =>
	createQuery(() => ({
		queryKey: DiagnosticsQueryKeyFactory.queueStats(),
		enabled: getEnabled(),
		staleTime: 0, // gauges must be live: never serve a persisted snapshot on re-entry
		refetchInterval: POLL_INTERVAL_MS,
		// library default, spelled out: no polling while the tab is hidden
		refetchIntervalInBackground: false,
		refetchOnWindowFocus: false,
		queryFn: ({ signal }) => api.global.get<QueueStats>(API.system.queueStats(), { signal })
	}));

/**
 * Outbound provider-call counters (QW9 Part 3). Same polling posture as the
 * queue gauges.
 */
export const getProviderStatsQuery = (getEnabled: Getter<boolean> = () => true) =>
	createQuery(() => ({
		queryKey: DiagnosticsQueryKeyFactory.providerStats(),
		enabled: getEnabled(),
		staleTime: 0,
		refetchInterval: POLL_INTERVAL_MS,
		refetchIntervalInBackground: false,
		refetchOnWindowFocus: false,
		queryFn: ({ signal }) => api.global.get<ProviderStats>(API.system.providerStats(), { signal })
	}));
