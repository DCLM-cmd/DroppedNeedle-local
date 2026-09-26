import { createQuery, queryOptions } from '@tanstack/svelte-query';

import { api } from '$lib/api/client';
import { API, CACHE_TTL } from '$lib/constants';
import { authStore } from '$lib/stores/authStore.svelte';
import type { PluginSourcesResponse } from '$lib/types';

import { PluginQueryKeyFactory } from './PluginQueryKeyFactory';

const getPluginSourcesQueryOptions = () =>
	queryOptions({
		staleTime: CACHE_TTL.LIBRARY_NATIVE,
		queryKey: PluginQueryKeyFactory.sources(authStore.user?.id),
		queryFn: ({ signal }) =>
			api.global.get<PluginSourcesResponse>(API.plugins.sources(), { signal })
	});

// Curator-visible plugin acquisition sources (user-level endpoint, so this
// query runs for every role and labels priority/review surfaces).
export const getPluginSourcesQuery = () => createQuery(() => getPluginSourcesQueryOptions());
