import { beforeEach, expect, it, vi } from 'vitest';

const idb = vi.hoisted(() => ({
	clear: vi.fn().mockResolvedValue(undefined),
	del: vi.fn().mockResolvedValue(undefined),
	entries: vi.fn().mockResolvedValue([]),
	get: vi.fn().mockResolvedValue(undefined),
	set: vi.fn().mockResolvedValue(undefined)
}));

vi.mock('idb-keyval', () => idb);

import {
	queryClient,
	resetQueryCacheForUserSwitch,
	setQueryDataWithPersister
} from './QueryClient';

beforeEach(() => {
	vi.clearAllMocks();
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
