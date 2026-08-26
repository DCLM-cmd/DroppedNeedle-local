import { describe, expect, it, vi } from 'vitest';

import type { ArtistReleases } from '$lib/types';

vi.mock('@tanstack/svelte-query', () => ({
	createInfiniteQuery: vi.fn((factory: () => unknown) => factory()),
	createQuery: vi.fn((factory: () => unknown) => factory()),
	queryOptions: vi.fn((options: unknown) => options)
}));

const mockGet = vi.fn();
vi.mock('$lib/api/client', () => ({
	api: { global: { get: (...args: unknown[]) => mockGet(...args) } }
}));

vi.mock('../QueryClient', () => ({ setQueryDataWithPersister: vi.fn() }));

import {
	getArtistReleasesInfiniteQuery,
	getExtendedArtistQueryOptions
} from './ArtistQueries.svelte';

function releasePage(overrides: Partial<ArtistReleases> = {}): ArtistReleases {
	return {
		albums: [],
		eps: [],
		singles: [],
		offset: 0,
		limit: 50,
		returned_count: 0,
		next_offset: null,
		has_more: false,
		source_total_count: 0,
		...overrides
	};
}

describe('artist release pagination query', () => {
	it('fetches exactly the requested page and forwards navigation cancellation', async () => {
		const response = releasePage({ offset: 100, next_offset: 200, has_more: true });
		mockGet.mockResolvedValue(response);
		const query = getArtistReleasesInfiniteQuery(() => 'artist-1') as unknown as {
			queryFn: (context: { pageParam: number; signal: AbortSignal }) => Promise<ArtistReleases>;
			getNextPageParam: (lastPage: ArtistReleases) => number | undefined;
		};
		const signal = new AbortController().signal;

		await expect(query.queryFn({ pageParam: 100, signal })).resolves.toBe(response);
		expect(mockGet).toHaveBeenCalledWith('/api/v1/artists/artist-1/releases?offset=100&limit=50', {
			signal
		});
		expect(query.getNextPageParam(response)).toBe(200);
		expect(query.getNextPageParam(releasePage({ has_more: false, next_offset: 200 }))).toBe(
			undefined
		);
		expect(query.getNextPageParam(releasePage({ has_more: true, next_offset: null }))).toBe(
			undefined
		);
	});
});

describe('extended artist query', () => {
	it('notifies the provider page when a fast result finishes before its lazy fields render', () => {
		const query = getExtendedArtistQueryOptions('artist-1');

		expect(query.notifyOnChangeProps).toBe('all');
		expect(query.queryKey).toEqual(['artist', 'artist-1', 'extended']);
	});
});
