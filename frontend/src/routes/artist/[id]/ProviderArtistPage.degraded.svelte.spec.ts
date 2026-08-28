import { beforeEach, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-svelte';

// Degraded MusicBrainz-outage fallback on the artist page: when the provider
// /artists/{id} fetch fails but the MB-free library detail query succeeded,
// the page must render the local artist surface (with an explanatory banner)
// instead of a whole-page error.

const h = vi.hoisted(() => ({
	localView: vi.fn(),
	libraryArtist: {
		id: 'local-artist-id',
		name: 'Local Artist',
		musicbrainz_artist_id: 'mb-artist-id'
	} as Record<string, unknown> | null
}));

vi.mock('$lib/queries/artist/ArtistQueries.svelte', () => ({
	getBasicArtistQuery: () => ({
		data: null,
		isLoading: false,
		error: new Error('404')
	}),
	getExtendedArtistQuery: () => ({ data: null, isLoading: false, error: null }),
	getSimilarArtistsQuery: () => ({ data: null, isLoading: false }),
	getArtistTopSongsQuery: () => ({ data: null, isLoading: false }),
	getArtistTopAlbumsQuery: () => ({ data: null, isLoading: false }),
	getArtistLastFmEnrichmentQuery: () => ({ data: null, isLoading: false }),
	updateArtistReleaseInCache: vi.fn(),
	getArtistReleasesInfiniteQuery: () => ({
		data: { pages: [] },
		isLoading: false,
		fetchNextPage: vi.fn()
	})
}));

vi.mock('./LocalArtistPage.svelte', () => {
	const Component = function (_anchor: unknown, props: { artistId: string }) {
		h.localView(props.artistId);
	};
	Component.prototype = {};
	return { default: Component };
});

vi.mock('$lib/queries/library/LibraryQueries.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/queries/library/LibraryQueries.svelte')>()),
	getLibraryArtistDetailQuery: () => ({
		data: h.libraryArtist,
		isLoading: false,
		error: null
	})
}));

vi.mock('$lib/stores/library', () => ({
	libraryStore: {
		isInLibrary: () => false,
		isRequested: () => false,
		subscribe: () => () => {}
	}
}));

vi.mock('$lib/stores/discographyDownload.svelte', () => ({
	discographyDownloadStore: { subscribe: () => () => {} }
}));

vi.mock('$lib/queries/downloads/DownloadMutations.svelte', () => ({
	requestAlbum: () => ({ isPending: false, mutate: vi.fn() })
}));

vi.mock('runed', () => ({
	PersistedState: class {
		current: unknown;
		constructor(_key: string, initial: unknown) {
			this.current = initial;
		}
	}
}));

import ProviderArtistPage from './ProviderArtistPage.svelte';

beforeEach(() => {
	vi.clearAllMocks();
	h.libraryArtist = {
		id: 'local-artist-id',
		name: 'Local Artist',
		musicbrainz_artist_id: 'mb-artist-id'
	};
});

it('renders the local artist surface with a banner when the provider fetch fails', async () => {
	render(ProviderArtistPage, {
		props: { data: { artistId: 'mb-artist-id', primarySource: 'local' } }
	} as unknown as Parameters<typeof render>[1]);

	await vi.waitFor(() => expect(h.localView).toHaveBeenCalledWith('local-artist-id'));
	const banner = document.body.textContent ?? '';
	expect(banner).toContain('MusicBrainz is unreachable');
});
