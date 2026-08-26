import { page } from '@vitest/browser/context';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-svelte';
import { searchStore } from '$lib/stores/search';
import { authStore } from '$lib/stores/authStore.svelte';
import { resetQueryCacheForUserSwitch } from '$lib/queries/QueryClient';
import SearchPageTestHarness from './SearchPageTestHarness.svelte';

const originalFetch = globalThis.fetch;

function jsonResponse(body: unknown): Response {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

describe('search result enrichment demand', () => {
	beforeEach(async () => {
		searchStore.clear();
		await resetQueryCacheForUserSwitch();
		authStore.setUser({
			id: 'search-user',
			display_name: 'Search User',
			role: 'admin',
			email: null,
			avatar_url: null,
			username: 'search-user',
			username_display: 'Search User',
			providers: ['local']
		});
	});
	afterEach(async () => {
		globalThis.fetch = originalFetch;
		await resetQueryCacheForUserSwitch();
		authStore.clear();
	});

	it('renders usable primary results without enrichment traffic, then enriches on intent', async () => {
		let finishArtists: ((response: Response) => void) | undefined;
		const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.startsWith('/api/v1/search/artists?')) {
				return new Promise<Response>((resolve) => {
					finishArtists = resolve;
				});
			}
			if (url.startsWith('/api/v1/search/albums?')) {
				return jsonResponse({
					bucket: 'albums',
					limit: 24,
					offset: 0,
					results: [],
					top_result: null,
					status: 'ok'
				});
			}
			if (url.startsWith('/api/v1/library/artists?')) {
				return jsonResponse({ items: [], total: 0, album_artist_total: 0, contributor_total: 0 });
			}
			if (url.startsWith('/api/v1/library/albums?')) {
				return jsonResponse({ items: [], total: 0 });
			}
			if (url === '/api/v1/search/enrich/batch') {
				return jsonResponse({
					artists: [{ musicbrainz_id: 'artist-1', listen_count: 100 }],
					albums: [],
					source: 'listenbrainz'
				});
			}
			throw new Error(`Unexpected request: ${url}`);
		});
		globalThis.fetch = mockFetch as typeof fetch;

		render(SearchPageTestHarness, { data: { query: 'muse' } });
		await expect.element(page.getByRole('heading', { name: 'Albums' })).toBeInTheDocument();
		await page.getByRole('heading', { name: 'Albums' }).hover();
		finishArtists?.(
			jsonResponse({
				bucket: 'artists',
				limit: 6,
				offset: 0,
				results: [
					{
						type: 'artist',
						title: 'Muse',
						musicbrainz_id: 'artist-1',
						in_library: false,
						score: 95
					}
				],
				top_result: null,
				status: 'ok'
			})
		);
		await expect.element(page.getByText('Muse')).toBeInTheDocument();
		await new Promise((resolve) => setTimeout(resolve, 250));

		const enrichmentCalls = () =>
			mockFetch.mock.calls.filter(([input]) => String(input) === '/api/v1/search/enrich/batch');
		expect(enrichmentCalls()).toHaveLength(0);
		expect(mockFetch).toHaveBeenCalledTimes(4);

		await page.getByText('Muse').hover();
		await vi.waitFor(() => expect(enrichmentCalls()).toHaveLength(1));
	});

	it('shows local artist results while reserving pending remote artist slots', async () => {
		let finishArtists: ((response: Response) => void) | undefined;
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.startsWith('/api/v1/library/artists?')) {
				return jsonResponse({
					items: [
						{
							id: 'local-artist',
							name: 'Local First',
							musicbrainz_artist_id: null,
							artist_identity_state: 'local_only',
							album_count: 1,
							track_count: 8,
							appearance_release_count: 0,
							appearance_track_count: 0,
							library_relationship: 'album_artist',
							date_added: null,
							row_revision: 1
						}
					],
					total: 1,
					album_artist_total: 1,
					contributor_total: 0
				});
			}
			if (url.startsWith('/api/v1/library/albums?')) return jsonResponse({ items: [], total: 0 });
			if (url.startsWith('/api/v1/search/artists?')) {
				return new Promise<Response>((resolve) => {
					finishArtists = resolve;
				});
			}
			if (url.startsWith('/api/v1/search/albums?')) {
				return jsonResponse({
					bucket: 'albums',
					limit: 24,
					offset: 0,
					results: [],
					status: 'ok'
				});
			}
			throw new Error(`Unexpected request: ${url}`);
		}) as typeof fetch;

		render(SearchPageTestHarness, { data: { query: 'local first' } });

		await expect.element(page.getByText('Local First')).toBeInTheDocument();
		await expect
			.element(page.getByRole('link', { name: /Local First/ }))
			.toHaveAttribute('href', '/artist/local-artist');
		await expect
			.element(page.getByLabelText('Artist search results'))
			.toHaveAttribute('aria-busy', 'true');
		await expect.element(page.getByLabelText('Loading top search results')).toBeInTheDocument();

		finishArtists?.(
			jsonResponse({
				bucket: 'artists',
				limit: 6,
				offset: 0,
				results: [],
				status: 'error'
			})
		);
		await expect
			.element(page.getByText(/MusicBrainz artists are temporarily unavailable/))
			.toBeInTheDocument();
		await expect
			.element(page.getByLabelText('Artist search results'))
			.toHaveAttribute('aria-busy', 'false');
		await expect.element(page.getByLabelText('Loading top search results')).not.toBeInTheDocument();
	});

	it('keeps a successful local artist bucket when the local album request fails', async () => {
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.startsWith('/api/v1/library/artists?')) {
				return jsonResponse({
					items: [
						{
							id: 'local-artist',
							name: 'Local Survivor',
							musicbrainz_artist_id: null,
							artist_identity_state: 'local_only',
							album_count: 1,
							track_count: 8,
							appearance_release_count: 0,
							appearance_track_count: 0,
							library_relationship: 'album_artist',
							date_added: null,
							row_revision: 1
						}
					],
					total: 1,
					album_artist_total: 1,
					contributor_total: 0
				});
			}
			if (url.startsWith('/api/v1/library/albums?')) {
				return new Response('{}', { status: 500 });
			}
			if (url.startsWith('/api/v1/search/artists?')) {
				return jsonResponse({
					bucket: 'artists',
					limit: 6,
					offset: 0,
					results: [],
					status: 'ok'
				});
			}
			if (url.startsWith('/api/v1/search/albums?')) {
				return jsonResponse({
					bucket: 'albums',
					limit: 24,
					offset: 0,
					results: [],
					status: 'ok'
				});
			}
			throw new Error(`Unexpected request: ${url}`);
		}) as typeof fetch;

		render(SearchPageTestHarness, { data: { query: 'local survivor' } });

		await expect.element(page.getByText('Local Survivor')).toBeVisible();
		await expect
			.element(page.getByRole('link', { name: /Local Survivor/ }))
			.toHaveAttribute('href', '/artist/local-artist');
	});
});
