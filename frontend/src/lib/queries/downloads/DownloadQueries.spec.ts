import { describe, expect, it, vi } from 'vitest';

import { DownloadQueryKeyFactory } from './DownloadQueryKeyFactory';

vi.mock('@tanstack/svelte-query', () => ({
	createQuery: vi.fn((factory: () => unknown) => factory()),
	createMutation: vi.fn((factory: () => unknown) => factory()),
	queryOptions: vi.fn((opts: unknown) => opts)
}));

const mockGet = vi.fn();
const mockPost = vi.fn();
vi.mock('$lib/api/client', () => ({
	api: {
		global: {
			get: (...args: unknown[]) => mockGet(...args),
			post: (...args: unknown[]) => mockPost(...args)
		}
	}
}));

const { mockInvalidate } = vi.hoisted(() => ({ mockInvalidate: vi.fn() }));
vi.mock('$lib/queries/QueryClient', () => ({
	invalidateQueriesWithPersister: (...args: unknown[]) => mockInvalidate(...args)
}));

vi.mock('$lib/stores/authStore.svelte', () => ({
	authStore: { user: { id: 'user-1' } }
}));

const { mockToast } = vi.hoisted(() => ({ mockToast: vi.fn() }));
vi.mock('$lib/stores/toast', () => ({
	toastStore: { show: (...args: unknown[]) => mockToast(...args) }
}));

import {
	getDownloadActivitySummaryQueryOptions,
	getDownloadsQueryOptions
} from './DownloadQueries.svelte';
import {
	cancelDownload,
	requestAlbum,
	requestTrack,
	retryHeldManagementUnit,
	retryDownload,
	tryNextSource
} from './DownloadMutations.svelte';

describe('download queue queries', () => {
	it('the downloads list query hits /api/v1/downloads', async () => {
		const opts = getDownloadsQueryOptions() as { queryFn: (a: unknown) => unknown };
		await opts.queryFn({ signal: undefined });
		expect(String(mockGet.mock.calls.at(-1)?.[0])).toContain('/api/v1/downloads');
	});

	it('uses one visibility-aware compact summary owner with active and idle cadences', async () => {
		const opts = getDownloadActivitySummaryQueryOptions() as unknown as {
			queryFn: (a: { signal?: AbortSignal }) => unknown;
			queryKey: readonly unknown[];
			refetchInterval: (query: { state: { data?: { active_count: number } } }) => number;
			refetchIntervalInBackground: boolean;
			refetchOnReconnect: string;
			refetchOnWindowFocus: string | undefined;
			staleTime: number;
		};

		await opts.queryFn({ signal: undefined });

		expect(mockGet.mock.calls.at(-1)?.[0]).toBe('/api/v1/downloads/activity-summary');
		expect(opts.queryKey).toEqual(['downloads', 'tasks', 'user-1', 'activity']);
		expect(opts.refetchInterval({ state: { data: { active_count: 1 } } })).toBe(750);
		expect(opts.refetchInterval({ state: { data: { active_count: 0 } } })).toBe(120_000);
		expect(opts.refetchIntervalInBackground).toBe(false);
		expect(opts.refetchOnReconnect).toBe('always');
		// B6: focus-'always' dropped - invalidations + the interval own freshness
		expect(opts.refetchOnWindowFocus).toBeUndefined();
		expect(opts.staleTime).toBe(0);
	});

	it('does not give the detailed downloads list a competing interval', () => {
		const opts = getDownloadsQueryOptions() as unknown as {
			refetchInterval?: number;
			refetchOnWindowFocus: string | undefined;
			refetchOnReconnect: string | undefined;
			staleTime: number;
		};

		expect(opts.refetchInterval).toBeUndefined();
		// B6: neither always-flag remains; 30 s stale window instead
		expect(opts.refetchOnWindowFocus).toBeUndefined();
		expect(opts.refetchOnReconnect).toBeUndefined();
		expect(opts.staleTime).toBe(30_000);
	});

	it('requestAlbum posts to /requests/new with the mapped body', async () => {
		const m = requestAlbum() as unknown as { mutationFn: (i: unknown) => unknown };
		await m.mutationFn({
			release_group_mbid: 'rg',
			artist_name: 'A',
			album_title: 'B',
			year: 2000
		});
		const call = mockPost.mock.calls.at(-1);
		expect(String(call?.[0])).toContain('/requests/new');
		expect(call?.[1]).toMatchObject({ musicbrainz_id: 'rg', artist: 'A', album: 'B', year: 2000 });
	});

	it('requestTrack posts the complete exact-track payload', async () => {
		const m = requestTrack() as unknown as { mutationFn: (i: unknown) => unknown };
		await m.mutationFn({
			recording_mbid: 'rec',
			artist_name: 'A',
			track_title: 'T',
			album_title: 'B',
			duration_seconds: 287,
			release_group_mbid: 'rg',
			artist_mbid: 'artist',
			release_id: 'release'
		});
		const call = mockPost.mock.calls.at(-1);
		expect(call?.[0]).toBe('/api/v1/tracks/rec/request');
		expect(call?.[1]).toEqual({
			artist_name: 'A',
			track_title: 'T',
			album_title: 'B',
			duration_seconds: 287,
			release_group_mbid: 'rg',
			artist_mbid: 'artist',
			release_id: 'release'
		});
	});

	it('cancelDownload posts to /downloads/{id}/cancel', async () => {
		const m = cancelDownload() as unknown as { mutationFn: (i: string) => unknown };
		await m.mutationFn('t1');
		expect(String(mockPost.mock.calls.at(-1)?.[0])).toContain('/downloads/t1/cancel');
	});

	it('retryDownload posts to /downloads/{id}/retry', async () => {
		const m = retryDownload() as unknown as { mutationFn: (i: string) => unknown };
		await m.mutationFn('t1');
		expect(String(mockPost.mock.calls.at(-1)?.[0])).toContain('/downloads/t1/retry');
	});

	it('tryNextSource posts the rendered candidate index to the task endpoint', async () => {
		const m = tryNextSource() as unknown as { mutationFn: (i: unknown) => unknown };
		await m.mutationFn({ id: 't1', candidateIndex: 4 });
		const call = mockPost.mock.calls.at(-1);
		expect(String(call?.[0])).toContain('/downloads/t1/next-source');
		expect(call?.[1]).toEqual({ expected_candidate_index: 4 });
	});

	it('tryNextSource reports success and conflicts through toasts', () => {
		mockToast.mockClear();
		const m = tryNextSource() as unknown as {
			onSuccess: () => unknown;
			onError: (error: unknown) => unknown;
		};

		m.onSuccess();
		expect(mockToast).toHaveBeenLastCalledWith({
			message: 'Trying the next source',
			type: 'info'
		});

		m.onError(new Error('The transfer has already started'));
		expect(mockToast).toHaveBeenLastCalledWith({
			message: 'The transfer has already started',
			type: 'error'
		});
	});

	it('refreshes held and task data immediately when organizer retry is rejected', () => {
		mockInvalidate.mockClear();
		const mutation = retryHeldManagementUnit() as unknown as {
			onError: (error: unknown) => unknown;
		};

		mutation.onError(new Error('Exact edition proof is incomplete.'));

		expect(mockInvalidate).toHaveBeenCalledWith({
			queryKey: DownloadQueryKeyFactory.tasks('user-1')
		});
	});

	it.each([
		{
			status: 'pending',
			success: true,
			message: '',
			expectedMessage: 'Request submitted - searching for downloads',
			type: 'success'
		},
		{
			status: 'awaiting_approval',
			success: true,
			message: '',
			expectedMessage: 'Request submitted for admin approval',
			type: 'success'
		},
		{
			status: 'queued',
			success: true,
			message: 'Request already in progress',
			expectedMessage: 'Request already in progress',
			type: 'info'
		},
		{
			status: 'downloading',
			success: true,
			message: 'Request already in progress',
			expectedMessage: 'Request already in progress',
			type: 'info'
		},
		{
			status: 'cancelling',
			success: true,
			message: 'Request is being cancelled',
			expectedMessage: 'Request is being cancelled',
			type: 'info'
		},
		{
			status: 'failed',
			success: true,
			message: 'Request could not be recorded',
			expectedMessage: 'Request could not be recorded',
			type: 'error'
		},
		{
			status: 'imported',
			success: true,
			message: 'Album is already in the library',
			expectedMessage: 'Album is already in the library',
			type: 'info'
		}
	] as const)('requestAlbum shows the correct toast for $status', (response) => {
		mockToast.mockClear();
		const m = requestAlbum() as unknown as { onSuccess: (d: unknown) => unknown };
		m.onSuccess({
			success: response.success,
			message: response.message,
			musicbrainz_id: 'rg',
			status: response.status
		});
		expect(mockToast).toHaveBeenCalledWith({
			message: response.expectedMessage,
			type: response.type
		});
	});

	it('requestAlbum reports an unsuccessful response before handling its status', () => {
		mockToast.mockClear();
		const m = requestAlbum() as unknown as { onSuccess: (d: unknown) => unknown };
		m.onSuccess({
			success: false,
			message: 'Request could not be recorded',
			musicbrainz_id: 'rg',
			status: 'pending'
		});
		expect(mockToast).toHaveBeenCalledWith({
			message: 'Request could not be recorded',
			type: 'error'
		});
	});
	it.each([
		['already_in_library', 'That track is already in your library'],
		['awaiting_approval', 'Track request submitted for admin approval'],
		['queued', 'Track requested - searching for downloads']
	] as const)('requestTrack shows the correct toast for %s', (status, message) => {
		mockToast.mockClear();
		const m = requestTrack() as unknown as { onSuccess: (d: unknown) => unknown };
		m.onSuccess({ status });
		expect(mockToast).toHaveBeenCalledWith({ message, type: 'success' });
	});

	it('the key factory builds stable keys', () => {
		expect(DownloadQueryKeyFactory.tasks('user-1')).toEqual(['downloads', 'tasks', 'user-1']);
		expect(DownloadQueryKeyFactory.tasks('user-2')).not.toEqual(
			DownloadQueryKeyFactory.tasks('user-1')
		);
		expect(DownloadQueryKeyFactory.activity('user-1')).toEqual([
			'downloads',
			'tasks',
			'user-1',
			'activity'
		]);
		expect(DownloadQueryKeyFactory.held('user-1')).toEqual([
			'downloads',
			'tasks',
			'user-1',
			'held',
			'all'
		]);
		expect(DownloadQueryKeyFactory.quarantine()).toEqual(['downloads', 'quarantine']);
	});
});
