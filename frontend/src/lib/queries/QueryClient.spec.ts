import { beforeEach, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
	removeQueries: vi.fn().mockResolvedValue(undefined)
}));

vi.mock('@tanstack/svelte-query-persist-client', () => ({
	experimental_createQueryPersister: vi.fn(() => ({
		persisterFn: vi.fn(),
		persistQueryByKey: vi.fn().mockResolvedValue(undefined),
		removeQueries: h.removeQueries
	}))
}));

const idb = vi.hoisted(() => ({
	clear: vi.fn().mockResolvedValue(undefined),
	del: vi.fn().mockResolvedValue(undefined),
	entries: vi.fn().mockResolvedValue([]),
	get: vi.fn().mockResolvedValue(undefined),
	set: vi.fn().mockResolvedValue(undefined)
}));

vi.mock('idb-keyval', () => idb);

import {
	invalidateQueriesWithPersister,
	queryClient,
	resetQueryCacheForUserSwitch,
	setQueryDataWithPersister
} from './QueryClient';

beforeEach(() => {
	vi.clearAllMocks();
	h.removeQueries.mockResolvedValue(undefined);
	queryClient.clear();
});

it('clears both memory and persisted data before an account switch', async () => {
	const oldUserKey = ['me', 'scrobble-preferences', 'user-a'] as const;
	await setQueryDataWithPersister(oldUserKey, {
		primary_music_source: 'lastfm'
	});
	expect(queryClient.getQueryData(oldUserKey)).toEqual({ primary_music_source: 'lastfm' });

	await resetQueryCacheForUserSwitch();

	expect(queryClient.getQueryData(oldUserKey)).toBeUndefined();
	expect(idb.clear).toHaveBeenCalledOnce();
});

it('keeps persisted rows on invalidation by default', async () => {
	await invalidateQueriesWithPersister({ queryKey: ['library'] });

	expect(h.removeQueries).not.toHaveBeenCalled();
});

it('destroys persisted rows only when removePersisted is opted into', async () => {
	await invalidateQueriesWithPersister({ queryKey: ['library'] }, undefined, {
		removePersisted: true
	});

	expect(h.removeQueries).toHaveBeenCalledOnce();
	expect(h.removeQueries).toHaveBeenCalledWith({ queryKey: ['library'] });
});
