import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { get, set } from 'idb-keyval';
import { PERSISTER_KEY_PREFIX, type PersistedQuery } from '@tanstack/svelte-query-persist-client';
import { clearPersistedQueryCache } from './IndexedDbPersister.svelte';
import {
	invalidateMusicBrainzProviderQueries,
	invalidateQueriesWithPersister,
	queryClient,
	resetQueryCacheForUserSwitch,
	setQueryDataWithPersister
} from './QueryClient';
import { setDownloadScope } from './downloads/downloadScope.svelte';
import { DownloadQueryKeyFactory } from './downloads/DownloadQueryKeyFactory';
import { authStore, type AuthUser } from '$lib/stores/authStore.svelte';
import { ArtistQueryKeyFactory } from './artist/ArtistQueryKeyFactory';
import {
	resetMusicBrainzSourceScope,
	setMusicBrainzSourceScope
} from './musicbrainz/sourceScope.svelte';

const keyPrefix = `${PERSISTER_KEY_PREFIX}-`;

function persistedQuery(queryKey: readonly unknown[], data: unknown): PersistedQuery {
	return {
		buster: '',
		queryHash: JSON.stringify(queryKey),
		queryKey: [...queryKey],
		state: {
			data,
			dataUpdateCount: 1,
			dataUpdatedAt: Date.now(),
			error: null,
			errorUpdateCount: 0,
			errorUpdatedAt: 0,
			fetchFailureCount: 0,
			fetchFailureReason: null,
			fetchMeta: null,
			isInvalidated: false,
			status: 'success',
			fetchStatus: 'idle'
		}
	};
}

beforeEach(async () => {
	queryClient.clear();
	authStore.clear();
	resetMusicBrainzSourceScope();
	await clearPersistedQueryCache();
});

afterEach(async () => {
	vi.restoreAllMocks();
	queryClient.clear();
	authStore.clear();
	await clearPersistedQueryCache();
});

it('removes only provider-bearing IndexedDB rows and retains unrelated or malformed rows', async () => {
	const providerKey = `${keyPrefix}provider-artist`;
	const editionKey = `${keyPrefix}provider-edition`;
	const profileKey = `${keyPrefix}profile`;
	const lastFmKey = `${keyPrefix}lastfm`;
	const localSearchKey = `${keyPrefix}local-search`;
	const malformedKey = `${keyPrefix}malformed`;
	const numericKey = 42;
	const unrelatedKey = 'unrelated-application-row';

	await Promise.all([
		set(providerKey, persistedQuery(['artist', 'artist-1'], { provider: 'musicbrainz' })),
		set(editionKey, persistedQuery(['albums', 'editions', 'user-a', 'release-group-1'], {})),
		set(profileKey, persistedQuery(['profile', 'u'], { display_name: 'User' })),
		set(
			lastFmKey,
			persistedQuery(['artist', 'artist-1', 'lastfm-enrichment', { artistName: 'Artist' }], {})
		),
		set(localSearchKey, persistedQuery(['search', 'user-a', 'local-artists', 'artist'], {})),
		set(malformedKey, { malformed: true }),
		set(numericKey, { malformed: true }),
		set(unrelatedKey, { unrelated: true })
	]);

	await invalidateMusicBrainzProviderQueries();

	expect(await get(providerKey)).toBeUndefined();
	expect(await get(editionKey)).toBeUndefined();
	expect(await get(profileKey)).toEqual(expect.objectContaining({ queryKey: ['profile', 'u'] }));
	expect(await get(lastFmKey)).toEqual(
		expect.objectContaining({
			queryKey: ['artist', 'artist-1', 'lastfm-enrichment', { artistName: 'Artist' }]
		})
	);
	expect(await get(localSearchKey)).toEqual(
		expect.objectContaining({ queryKey: ['search', 'user-a', 'local-artists', 'artist'] })
	);
	expect(await get(malformedKey)).toEqual({ malformed: true });
	expect(await get(numericKey)).toEqual({ malformed: true });
	expect(await get(unrelatedKey)).toEqual({ unrelated: true });
});

it('removes admin task state on demotion and fences late responses through role round trips', async () => {
	setDownloadScope('role-user', 'admin');
	const adminKey = DownloadQueryKeyFactory.tasks('role-user');
	const persistedKey = `${keyPrefix}${JSON.stringify(adminKey)}`;
	await set(persistedKey, persistedQuery(adminKey, { items: ['other-user-task'] }));
	let resolve!: (value: unknown) => void;
	const pending = queryClient
		.fetchQuery({
			queryKey: adminKey,
			staleTime: 0,
			queryFn: () =>
				new Promise((settle) => {
					resolve = settle;
				})
		})
		.catch(() => undefined);
	await vi.waitFor(() => expect(resolve).toBeTypeOf('function'));
	setDownloadScope('role-user', 'user');
	expect(queryClient.getQueryData(adminKey)).toBeUndefined();
	expect(queryClient.getQueryData(DownloadQueryKeyFactory.tasks('role-user'))).toBeUndefined();
	resolve({ items: ['other-user-task'] });
	await pending;
	await vi.waitFor(async () => expect(await get(persistedKey)).toBeUndefined());
	setDownloadScope('role-user', 'admin');
	expect(DownloadQueryKeyFactory.tasks('role-user')).not.toEqual(adminKey);
	expect(queryClient.getQueryData(DownloadQueryKeyFactory.tasks('role-user'))).toBeUndefined();
	queryClient.clear();
});

it('retains persisted data for ordinary invalidation and removes it for an explicit clear', async () => {
	const key = ['library', 'fixture'] as const;
	await setQueryDataWithPersister(key, { albums: ['album-1'] });
	const storageKey = `${keyPrefix}${queryClient.getQueryCache().find({ queryKey: key })?.queryHash}`;
	await invalidateQueriesWithPersister({ queryKey: key });
	expect(await get(storageKey)).toEqual(
		expect.objectContaining({ state: expect.objectContaining({ data: { albums: ['album-1'] } }) })
	);
	await invalidateQueriesWithPersister({ queryKey: key }, undefined, { removePersisted: true });
	expect(await get(storageKey)).toBeUndefined();
});

it('clears memory and persisted user data on account switch', async () => {
	const key = ['profile', 'old-user'] as const;
	await setQueryDataWithPersister(key, { display_name: 'Old user' });
	const storageKey = `${keyPrefix}${queryClient.getQueryCache().find({ queryKey: key })?.queryHash}`;
	await resetQueryCacheForUserSwitch();
	expect(queryClient.getQueryData(key)).toBeUndefined();
	expect(await get(storageKey)).toBeUndefined();
});

it('continues selective cleanup and invalidates memory after an individual deletion fails', async () => {
	const retained = `${keyPrefix}a-failed`;
	const removed = `${keyPrefix}b-removed`;
	const key = ['artist', 'fixture'] as const;
	await queryClient.fetchQuery({ queryKey: key, queryFn: async () => ({ name: 'Artist' }) });
	await set(retained, persistedQuery(key, { name: 'Artist' }));
	await set(removed, persistedQuery(['artist', 'second'], { name: 'Second' }));
	const failure = new DOMException('Fixture deletion failure', 'UnknownError');
	const originalDelete = IDBObjectStore.prototype.delete;
	vi.spyOn(IDBObjectStore.prototype, 'delete').mockImplementation(function (
		this: IDBObjectStore,
		item
	) {
		if (item === retained) throw failure;
		return originalDelete.call(this, item);
	});
	await expect(invalidateMusicBrainzProviderQueries()).rejects.toBe(failure);
	expect(await get(retained)).toEqual(expect.objectContaining({ queryKey: [...key] }));
	expect(await get(removed)).toBeUndefined();
	expect(queryClient.getQueryState(key)?.isInvalidated).toBe(true);
});

it('rejects late source results and persisted writes across a source round trip', async () => {
	const user: AuthUser = {
		id: 'source-user',
		role: 'admin',
		display_name: 'Source user',
		username: 'source-user',
		username_display: 'source-user',
		email: 'source@example.test',
		avatar_url: null,
		providers: []
	};
	authStore.setUser(user);
	setMusicBrainzSourceScope({ source_mode: 'mirror', source_id: 'a-first', generation: 1 });
	const oldKey = ArtistQueryKeyFactory.basic('artist-1');
	await setQueryDataWithPersister(oldKey, { name: 'Old source' });
	const storageKey = `${keyPrefix}${queryClient.getQueryCache().find({ queryKey: oldKey })?.queryHash}`;
	let settle!: (value: { name: string }) => void;
	const pending = queryClient
		.fetchQuery({
			queryKey: oldKey,
			staleTime: 0,
			queryFn: () =>
				new Promise<{ name: string }>((resolve) => {
					settle = resolve;
				})
		})
		.catch(() => undefined);
	await vi.waitFor(() => expect(settle).toBeTypeOf('function'));
	setMusicBrainzSourceScope({ source_mode: 'mirror', source_id: 'b', generation: 2 });
	settle({ name: 'Late old source' });
	await pending;
	await setQueryDataWithPersister(oldKey, { name: 'Late manual publication' });
	expect(queryClient.getQueryData(oldKey)).toBeUndefined();
	await vi.waitFor(async () => expect(await get(storageKey)).toBeUndefined());
	setMusicBrainzSourceScope({ source_mode: 'mirror', source_id: 'a-new', generation: 3 });
	const currentKey = ArtistQueryKeyFactory.basic('artist-1');
	await setQueryDataWithPersister(currentKey, { name: 'Current source' });
	expect(currentKey).not.toEqual(oldKey);
	expect(queryClient.getQueryData(currentKey)).toEqual({ name: 'Current source' });
	expect(queryClient.getQueryData(oldKey)).toBeUndefined();
});
