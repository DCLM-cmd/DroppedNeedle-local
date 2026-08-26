import { describe, expect, it, vi } from 'vitest';

vi.mock('@tanstack/svelte-query', () => ({
	createQuery: vi.fn((factory: () => Record<string, unknown>) => factory()),
	queryOptions: vi.fn((opts: Record<string, unknown>) => opts),
	// LibraryQueries pulls in ../QueryClient, which news up the real client at import
	QueryClient: class {}
}));

vi.mock('$lib/stores/authStore.svelte', () => ({
	authStore: { user: { id: 'user-1' } }
}));

import { CACHE_TTL } from '$lib/constants';

import { getHomeQuery, getHomeQueryOptions } from '../HomeQuery.svelte';
import { HomeQueryKeyFactory } from '../HomeQueryKeyFactory';
import { LibraryQueryKeyFactory } from './LibraryQueryKeyFactory';

import {
	getLibraryAlbumDetailQuery,
	getLibraryAlbumDetailQueryOptions,
	getLibraryArtistDetailQuery,
	getLibraryArtistDetailQueryOptions
} from './LibraryQueries.svelte';

// B7: the detail queries were split into queryOptions factories (prefetch surface) plus
// thin createQuery wrappers. The wrappers must keep producing exactly the option shape
// the old inline literals produced - same keys, same staleTime, same queryFn - with the
// enabled gate layered on top.
describe('library/home detail queryOptions factories (B7)', () => {
	it('album factory: same key/staleTime/queryFn shape as the former inline options', () => {
		const prefetchOpts = getLibraryAlbumDetailQueryOptions('alb-1');
		expect(prefetchOpts.queryKey).toEqual(LibraryQueryKeyFactory.albumDetail('alb-1'));
		expect(prefetchOpts.staleTime).toBe(CACHE_TTL.LIBRARY_NATIVE);
		expect(typeof prefetchOpts.queryFn).toBe('function');

		const wrapped = getLibraryAlbumDetailQuery(() => 'alb-1') as unknown as Record<string, unknown>;
		expect(wrapped.queryKey).toEqual(prefetchOpts.queryKey);
		expect(wrapped.staleTime).toBe(prefetchOpts.staleTime);
		expect(typeof wrapped.queryFn).toBe('function');
		expect(wrapped.enabled).toBe(true);
		expect(
			(getLibraryAlbumDetailQuery(() => '') as unknown as Record<string, unknown>).enabled
		).toBe(false);
	});

	it('artist factory: same key/staleTime/queryFn shape as the former inline options', () => {
		const prefetchOpts = getLibraryArtistDetailQueryOptions('art-1');
		expect(prefetchOpts.queryKey).toEqual(LibraryQueryKeyFactory.artistDetail('art-1'));
		const wrapped = getLibraryArtistDetailQuery(() => 'art-1') as unknown as Record<
			string,
			unknown
		>;
		expect(wrapped.queryKey).toEqual(prefetchOpts.queryKey);
		expect(wrapped.staleTime).toBe(prefetchOpts.staleTime);
		expect(typeof wrapped.queryFn).toBe('function');
		expect(wrapped.enabled).toBe(true);
		expect(
			(getLibraryArtistDetailQuery(() => '') as unknown as Record<string, unknown>).enabled
		).toBe(false);
	});

	it('home factory: same key/staleTime shape and the thin wrapper keeps the refreshing poll', () => {
		const prefetchOpts = getHomeQueryOptions('user-1');
		expect(prefetchOpts.queryKey).toEqual(HomeQueryKeyFactory.home('user-1'));
		expect(prefetchOpts.staleTime).toBe(CACHE_TTL.HOME);
		expect(prefetchOpts.queryKey).toEqual(['home', 'user-1']);
		expect(typeof prefetchOpts.queryFn).toBe('function');

		const wrapped = getHomeQuery() as unknown as Record<string, unknown>;
		expect(wrapped.queryKey).toEqual(HomeQueryKeyFactory.home('user-1'));
		expect(wrapped.staleTime).toBe(CACHE_TTL.HOME);
		expect(typeof wrapped.queryFn).toBe('function');
		expect(typeof wrapped.refetchInterval).toBe('function');
		const interval = wrapped.refetchInterval as (q: {
			state: { data?: { refreshing?: boolean } };
		}) => number | false;
		expect(interval({ state: { data: { refreshing: true } } })).toBe(10_000);
		expect(interval({ state: { data: { refreshing: false } } })).toBe(false);
	});
});
