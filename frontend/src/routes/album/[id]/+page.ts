import { getLibraryAlbumDetailQueryOptions } from '$lib/queries/library/LibraryQueries.svelte';
import { queryClient } from '$lib/queries/QueryClient';
import type { PageLoad } from './$types';

// B7: the album detail read gates the whole page (including the canonical-redirect
// hop), so start it during the layout bootstrap rather than after mount.
export const load: PageLoad = ({ params }) => {
	void queryClient.prefetchQuery(getLibraryAlbumDetailQueryOptions(params.id));
	return {
		albumId: params.id
	};
};
