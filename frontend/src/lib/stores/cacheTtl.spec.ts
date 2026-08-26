import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// ST6: the cacheTTL store is a process-wide singleton whose `initialized` latch
// and $state payload are the behavior under test - each case therefore reloads
// the module fresh (module-loading boundary, not a stylistic dynamic import).
const apiGet = vi.hoisted(() => vi.fn());

vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$lib/api/client', () => ({ api: { global: { get: apiGet } } }));
vi.mock('$lib/utils/homeCache', () => ({ updateHomeCacheTTL: vi.fn() }));
vi.mock('$lib/stores/recentlyAdded', () => ({
	recentlyAddedStore: { updateCacheTTL: vi.fn() }
}));
vi.mock('$lib/stores/discoveryCache', () => ({ updateDiscoveryCacheTTL: vi.fn() }));
vi.mock('$lib/utils/discoverQueueCache', () => ({ updateDiscoverQueueCacheTTL: vi.fn() }));
vi.mock('$lib/stores/search', () => ({ updateSearchCacheTTL: vi.fn() }));
vi.mock('$lib/utils/jellyfinLibraryCache', () => ({ updateJellyfinSidebarCacheTTL: vi.fn() }));
vi.mock('$lib/utils/plexLibraryCache', () => ({
	updatePlexSidebarCacheTTL: vi.fn(),
	updatePlexAlbumsListCacheTTL: vi.fn()
}));
vi.mock('$lib/utils/localFilesCache', () => ({ updateLocalFilesSidebarCacheTTL: vi.fn() }));

import { CACHE_TTL } from '$lib/constants';

async function loadStore() {
	vi.resetModules();
	return import('./cacheTtl.svelte');
}

beforeEach(() => {
	vi.clearAllMocks();
});

afterEach(() => {
	vi.restoreAllMocks();
});

describe('reactive cache TTL store (ST6)', () => {
	it('serves the caller constant before init and the resolved setting after', async () => {
		apiGet.mockResolvedValue({ home: 90_000 });
		const store = await loadStore();

		// pre-init: the consuming query's shipped constant wins
		expect(store.ttl('home', CACHE_TTL.HOME)).toBe(CACHE_TTL.HOME);

		await store.initCacheTTLs();

		expect(store.ttl('home', CACHE_TTL.HOME)).toBe(90_000);
		expect(apiGet).toHaveBeenCalledWith('/api/v1/settings/cache-ttls');
	});

	it('keeps the shipped default when the response omits a field or fails', async () => {
		apiGet.mockResolvedValue({});
		const first = await loadStore();
		await first.initCacheTTLs();
		expect(first.ttl('search', CACHE_TTL.SEARCH)).toBe(CACHE_TTL.SEARCH);

		apiGet.mockRejectedValue(new Error('offline'));
		const second = await loadStore();
		await second.initCacheTTLs();
		expect(second.ttl('home', CACHE_TTL.HOME)).toBe(CACHE_TTL.HOME);
		expect(second.getCacheTTL('home')).toBe(CACHE_TTL.HOME);
	});

	it('initializes at most once per module instance', async () => {
		apiGet.mockResolvedValue({ home: 1000 });
		const store = await loadStore();
		await store.initCacheTTLs();
		expect(apiGet).toHaveBeenCalledTimes(1);
	});
});
