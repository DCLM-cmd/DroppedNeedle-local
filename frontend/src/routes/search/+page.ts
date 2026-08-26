import {
	getLocalAlbumSearchQueryOptions,
	getLocalArtistSearchQueryOptions
} from '$lib/queries/search/SearchQueries.svelte';
import { queryClient } from '$lib/queries/QueryClient';
import { authStore } from '$lib/stores/authStore.svelte';
import type { PageLoad } from './$types';

// B7: warm the two LOCAL buckets under the same gate as the search queries
// (authenticated, >= 2 chars). Remote buckets stay unprefetched - they trigger the
// backend 2-call MusicBrainz fan-out for a navigation that may be abandoned.
export const load: PageLoad = ({ url }) => {
	const q = url.searchParams.get('q') ?? '';
	const query = q.trim();
	if (authStore.user?.id && query.length >= 2) {
		void queryClient.prefetchQuery(getLocalArtistSearchQueryOptions(query));
		void queryClient.prefetchQuery(getLocalAlbumSearchQueryOptions(query));
	}
	return { query: q };
};
