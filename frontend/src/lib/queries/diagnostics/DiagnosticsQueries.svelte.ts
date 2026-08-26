import type { Getter } from 'runed';

import { api } from '$lib/api/client';
import { API } from '$lib/constants';
import { createQuery } from '@tanstack/svelte-query';

import { DiagnosticsQueryKeyFactory } from './DiagnosticsQueryKeyFactory';
import type { ProviderStats, QueueStats } from './types';

/** Live gauges: a stale second is worth less than a spare request, but the
 * cadence stays modest - the backend counters are plain dict reads. */
const POLL_INTERVAL_MS = 5_000;

/**
 * Outbound queue-lane occupancy (QW9 Part 1). Polls every 5 s while the
 * Diagnostics section is on screen and the document is visible; `enabled` is
 * handed in by the caller so a closed settings tab or a hidden window issues
 * no requests at all.
 */
export const getQueueStatsQuery = (getEnabled: Getter<boolean> = () => true) =>
	createQuery(() => ({
		queryKey: DiagnosticsQueryKeyFactory.queueStats(),
		enabled: getEnabled(),
		staleTime: 0, // gauges must be live: never serve a persisted snapshot on re-entry
		refetchInterval: POLL_INTERVAL_MS,
		// default, stated explicitly: background-tab polling is part of the contract
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
