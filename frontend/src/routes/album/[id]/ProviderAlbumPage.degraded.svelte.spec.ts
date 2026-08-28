import { beforeEach, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-svelte';

// Degraded MusicBrainz-outage fallback: when the provider /basic fetch fails
// but the MB-free library detail query succeeded, the provider pages must
// render the local surface (with an explanatory banner) instead of a
// whole-page error.

const h = vi.hoisted(() => ({
	localView: vi.fn(),
	libraryAlbum: {
		id: 'local-album-id',
		title: 'Outage Album',
		musicbrainz_release_group_id: 'rg-id' as string | null
	} as Record<string, unknown> | null
}));

vi.mock('./albumPageState.svelte', () => ({
	createAlbumPageState: () => ({
		error: 'Error loading album',
		loadingBasic: false,
		loadingTracks: false,
		tracksError: false,
		loadingDiscovery: false,
		loadingLastfm: false,
		album: null,
		tracksInfo: null,
		showToast: false,
		toastMessage: '',
		toastType: 'success',
		showDeleteModal: false,
		requesting: false,
		refreshing: false,
		headerDownloadTask: null,
		libraryStatus: null,
		albumDownloadTasks: [],
		moreByArtist: null,
		similarAlbums: null,
		trackLinks: [],
		albumLink: null,
		quota: null,
		jellyfinMatch: null,
		localMatch: null,
		navidromeMatch: null,
		plexMatch: null,
		lastfmEnrichment: null,
		renderedTrackSections: [],
		downloadClientConfigured: false,
		headerManagementHeld: [],
		handleDeleted: vi.fn()
	})
}));

vi.mock('./LocalAlbumPage.svelte', () => {
	const Component = function (_anchor: unknown, props: { albumId: string }) {
		h.localView(props.albumId);
	};
	Component.prototype = {};
	return { default: Component };
});

vi.mock('$lib/queries/library/LibraryQueries.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/queries/library/LibraryQueries.svelte')>()),
	getLibraryAlbumDetailQuery: () => ({
		data: h.libraryAlbum,
		isLoading: false,
		error: null
	}),
	getLibraryAlbumCopiesQuery: () => ({ data: { items: [] }, isLoading: false }),
	getLibraryAlbumStatusQuery: () => ({ data: null, isLoading: false }),
	getLibraryArtistDetailQuery: () => ({
		data: null,
		isLoading: false,
		error: null
	})
}));

vi.mock('$lib/stores/integration', () => ({
	integrationStore: { subscribe: () => () => {} }
}));

vi.mock('$lib/stores/authStore.svelte', () => ({
	authStore: { user: null, isTrusted: false }
}));

import ProviderAlbumPage from './ProviderAlbumPage.svelte';

beforeEach(() => {
	vi.clearAllMocks();
	h.libraryAlbum = {
		id: 'local-album-id',
		title: 'Outage Album',
		musicbrainz_release_group_id: 'rg-id'
	};
});

it('renders the local surface with a banner when the provider fetch fails', async () => {
	render(ProviderAlbumPage, {
		props: { data: { albumId: 'rg-id' } }
	} as unknown as Parameters<typeof render>[1]);

	await vi.waitFor(() => expect(h.localView).toHaveBeenCalledWith('local-album-id'));
	const banner = document.body.textContent ?? '';
	expect(banner).toContain('MusicBrainz is unreachable');
});
