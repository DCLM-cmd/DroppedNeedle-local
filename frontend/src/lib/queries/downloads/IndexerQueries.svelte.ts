import { createMutation, createQuery, queryOptions } from '@tanstack/svelte-query';

import { api } from '$lib/api/client';
import { API, CACHE_TTL } from '$lib/constants';
import { HomeQueryKeyFactory } from '$lib/queries/HomeQueryKeyFactory';
import { invalidateQueriesWithPersister } from '$lib/queries/QueryClient';
import type {
	IndexerSavedResponse,
	IndexerSettings,
	IndexerTestResult,
	OperationResult,
	UsenetSearchBackend,
	UsenetSearchBackendName
} from '$lib/types';

import { DownloadQueryKeyFactory } from './DownloadQueryKeyFactory';

const getIndexersQueryOptions = () =>
	queryOptions({
		staleTime: CACHE_TTL.LIBRARY_NATIVE,
		queryKey: DownloadQueryKeyFactory.indexers(),
		queryFn: ({ signal }) => api.global.get<IndexerSettings[]>(API.indexers.list(), { signal })
	});

export const getIndexersQuery = () => createQuery(() => getIndexersQueryOptions());

async function invalidateIndexers() {
	await invalidateQueriesWithPersister({ queryKey: DownloadQueryKeyFactory.indexers() });
	await invalidateQueriesWithPersister({ queryKey: DownloadQueryKeyFactory.clientStatus() });
	// Home reads integration_status; an added/removed indexer changes it.
	await invalidateQueriesWithPersister({ queryKey: HomeQueryKeyFactory.prefix });
}

export function saveIndexerMutation() {
	return createMutation(() => ({
		mutationFn: (indexer: IndexerSettings) =>
			indexer.id
				? api.global.put<IndexerSavedResponse>(API.indexers.update(indexer.id), indexer)
				: api.global.post<IndexerSavedResponse>(API.indexers.create(), indexer),
		onSuccess: invalidateIndexers
	}));
}

export function deleteIndexerMutation() {
	return createMutation(() => ({
		mutationFn: (id: string) => api.global.delete<OperationResult>(API.indexers.remove(id)),
		onSuccess: invalidateIndexers
	}));
}

export function reorderIndexersMutation() {
	return createMutation(() => ({
		mutationFn: (orderedIds: string[]) =>
			api.global.post<OperationResult>(API.indexers.reorder(), { ordered_ids: orderedIds }),
		onSuccess: invalidateIndexers
	}));
}

export function testIndexerMutation() {
	return createMutation(() => ({
		mutationFn: (indexer: IndexerSettings) =>
			api.global.post<IndexerTestResult>(API.indexers.test(), indexer)
	}));
}

const getSearchBackendQueryOptions = () =>
	queryOptions({
		staleTime: CACHE_TTL.LIBRARY_NATIVE,
		queryKey: DownloadQueryKeyFactory.searchBackend(),
		queryFn: ({ signal }) =>
			api.global.get<UsenetSearchBackend>(API.indexers.searchBackend(), { signal })
	});

export const getSearchBackendQuery = () => createQuery(() => getSearchBackendQueryOptions());

export function saveSearchBackendMutation() {
	return createMutation(() => ({
		mutationFn: (backend: UsenetSearchBackendName) =>
			api.global.put<OperationResult>(API.indexers.searchBackend(), { backend }),
		onSuccess: async () => {
			// Switching backends flips readiness: sweep the SABnzbd-shape set so
			// status surfaces and Home refresh alongside the selector. The own key
			// is listed explicitly (it nests under indexers(), so the prefix sweep
			// below already covers it - this survives a future key move).
			await invalidateQueriesWithPersister({
				queryKey: DownloadQueryKeyFactory.searchBackend()
			});
			await invalidateIndexers();
			await invalidateQueriesWithPersister({ queryKey: DownloadQueryKeyFactory.prowlarr() });
			await invalidateQueriesWithPersister({ queryKey: DownloadQueryKeyFactory.sabnzbd() });
			await invalidateQueriesWithPersister({
				queryKey: DownloadQueryKeyFactory.sabnzbdStatus()
			});
		}
	}));
}
