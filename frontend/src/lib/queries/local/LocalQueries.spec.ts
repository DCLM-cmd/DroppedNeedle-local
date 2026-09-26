import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@tanstack/svelte-query', () => ({
	createInfiniteQuery: vi.fn((factory: () => Record<string, unknown>) => factory()),
	createQuery: vi.fn((factory: () => Record<string, unknown>) => factory()),
	queryOptions: vi.fn((options: Record<string, unknown>) => options)
}));

vi.mock('$lib/api/client', () => ({
	api: { global: { get: vi.fn().mockResolvedValue({ allowed: true }) } }
}));

import { api } from '$lib/api/client';
import { LOCAL_KEYS, getDownloadAccessQueryOptions } from './LocalQueries.svelte';

const mockGet = vi.mocked(api.global.get);

interface QueryContext {
	signal: AbortSignal;
}

function callQueryFn(options: unknown, context: QueryContext): Promise<unknown> {
	return (options as { queryFn: (value: QueryContext) => Promise<unknown> }).queryFn(context);
}

beforeEach(() => {
	vi.clearAllMocks();
	mockGet.mockResolvedValue({ allowed: true });
});

describe('LOCAL_KEYS.downloadAccess', () => {
	it('keys the bit by user under the local prefix', () => {
		expect.assertions(2);
		expect(LOCAL_KEYS.downloadAccess('user-1')).toEqual(['local', 'download-access', 'user-1']);
		expect(LOCAL_KEYS.downloadAccess('user-1')).not.toEqual(LOCAL_KEYS.downloadAccess('user-2'));
	});

	it('normalizes a missing userId to anonymous', () => {
		expect.assertions(1);
		expect(LOCAL_KEYS.downloadAccess(undefined)).toEqual(['local', 'download-access', 'anonymous']);
	});
});

describe('getDownloadAccessQueryOptions', () => {
	it('fetches the viewer capability through the api client', async () => {
		expect.assertions(2);
		const signal = new AbortController().signal;

		await callQueryFn(getDownloadAccessQueryOptions('user-1'), { signal });

		expect(mockGet).toHaveBeenCalledWith('/api/v1/download/access', { signal });
		expect(mockGet).toHaveBeenCalledTimes(1);
	});

	it('never serves a stale persisted bit', () => {
		expect.assertions(2);
		const options = getDownloadAccessQueryOptions('user-1') as {
			staleTime: number;
			refetchOnMount: string;
		};

		expect(options.staleTime).toBe(0);
		expect(options.refetchOnMount).toBe('always');
	});
});
