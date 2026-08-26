import { browser } from '$app/environment';
import {
	type InferDataFromTag,
	type InvalidateOptions,
	type InvalidateQueryFilters,
	QueryClient,
	type QueryKey,
	type SetDataOptions,
	type Updater
} from '@tanstack/svelte-query';
import { experimental_createQueryPersister } from '@tanstack/svelte-query-persist-client';
import { clearPersistedQueryCache, createIDBStorage } from './IndexedDbPersister.svelte';

/**
 * Maximum age for queries to be persisted.
 * @see https://tanstack.com/query/latest/docs/framework/react/plugins/persistQueryClient#how-it-works
 */
const QUERY_MAX_AGE = 1000 * 60 * 60 * 24 * 7; // 7 days

const queryPersister = experimental_createQueryPersister({
	storage: createIDBStorage(),
	maxAge: QUERY_MAX_AGE,
	// No need to serialize/deserialize since we're using IndexedDB which can store complex objects.
	serialize: (persistedQuery) => persistedQuery,
	deserialize: (cached) => cached
});

export const setQueryDataWithPersister = async <
	TQueryFnData = unknown,
	TTaggedQueryKey extends QueryKey = QueryKey,
	TInferredQueryFnData = InferDataFromTag<TQueryFnData, TTaggedQueryKey>
>(
	queryKey: TTaggedQueryKey,
	updater: Updater<
		NoInfer<TInferredQueryFnData> | undefined,
		NoInfer<TInferredQueryFnData> | undefined
	>,
	options?: SetDataOptions
) => {
	// eslint-disable-next-line no-restricted-syntax
	await queryClient.setQueryData<TQueryFnData, TTaggedQueryKey, TInferredQueryFnData>(
		queryKey,
		updater,
		options
	);
	await queryPersister.persistQueryByKey(queryKey, queryClient);
};

export const invalidateQueriesWithPersister = async <TTaggedQueryKey extends QueryKey = QueryKey>(
	filters?: InvalidateQueryFilters<TTaggedQueryKey>,
	options?: InvalidateOptions,
	opts?: { removePersisted?: boolean }
) => {
	// Default keeps IndexedDB rows: queries are marked stale (active ones
	// refetch immediately, inactive ones paint the persisted payload instantly
	// and settle in the background on next mount). Pass `removePersisted: true`
	// only when a stale paint would be actively wrong - it destroys the 7-day
	// persisted-cache benefit for the swept prefix.
	if (opts?.removePersisted) {
		await queryPersister.removeQueries(filters);
	}
	// eslint-disable-next-line no-restricted-syntax
	await queryClient.invalidateQueries<TTaggedQueryKey>(filters, options);
};

export const queryClient = new QueryClient({
	defaultOptions: {
		queries: {
			enabled: browser,
			retry: false,
			refetchOnWindowFocus: true,
			staleTime: 1000 * 60 * 1, // 1 minute
			gcTime: 1000 * 60 * 5, // 5 min: keep results in memory so back-nav is instant (30s evicted before staleTime, forcing a skeleton + IDB rehydrate each return)
			persister: queryPersister.persisterFn
		}
	}
});

/**
 * Drop ALL cached query data on login / logout / user-switch (AMU-5): QueryClient +
 * IndexedDB persister form one browser-wide cache with no user dimension, so
 * personalized data would otherwise leak across users sharing a browser.
 */
export const resetQueryCacheForUserSwitch = async (): Promise<void> => {
	queryClient.clear();
	await clearPersistedQueryCache();
};
