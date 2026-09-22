import { createMutation, createQuery, queryOptions } from '@tanstack/svelte-query';

import { api } from '$lib/api/client';
import { API, CACHE_TTL } from '$lib/constants';
import { HomeQueryKeyFactory } from '$lib/queries/HomeQueryKeyFactory';
import { invalidateQueriesWithPersister } from '$lib/queries/QueryClient';
import type { OperationResult, ProwlarrConnectionSettings, ProwlarrTestResult } from '$lib/types';

import { DownloadQueryKeyFactory } from './DownloadQueryKeyFactory';

const getProwlarrConfigQueryOptions = () =>
	queryOptions({
		staleTime: CACHE_TTL.LIBRARY_NATIVE,
		queryKey: DownloadQueryKeyFactory.prowlarr(),
		queryFn: ({ signal }) =>
			api.global.get<ProwlarrConnectionSettings>(API.prowlarr.config(), { signal })
	});

export const getProwlarrConfigQuery = () => createQuery(() => getProwlarrConfigQueryOptions());

async function invalidateProwlarr() {
	// SABnzbd-shape sweep: a Prowlarr save flips is_usenet_ready(), so status
	// surfaces and Home (integration_status) must refresh alongside the config.
	await invalidateQueriesWithPersister({ queryKey: DownloadQueryKeyFactory.prowlarr() });
	await invalidateQueriesWithPersister({ queryKey: DownloadQueryKeyFactory.sabnzbd() });
	await invalidateQueriesWithPersister({ queryKey: DownloadQueryKeyFactory.sabnzbdStatus() });
	await invalidateQueriesWithPersister({ queryKey: DownloadQueryKeyFactory.clientStatus() });
	await invalidateQueriesWithPersister({ queryKey: HomeQueryKeyFactory.prefix });
}

export function saveProwlarrConfigMutation() {
	return createMutation(() => ({
		mutationFn: (connection: ProwlarrConnectionSettings) =>
			api.global.put<OperationResult>(API.prowlarr.config(), connection),
		onSuccess: invalidateProwlarr
	}));
}

export function testProwlarrMutation() {
	return createMutation(() => ({
		mutationFn: (connection: ProwlarrConnectionSettings) =>
			api.global.post<ProwlarrTestResult>(API.prowlarr.test(), connection)
	}));
}
