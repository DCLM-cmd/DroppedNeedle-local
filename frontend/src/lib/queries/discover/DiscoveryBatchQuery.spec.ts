import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
	post: vi.fn(),
	delete: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
	api: { global: { post: h.post, delete: h.delete } }
}));

vi.mock('@tanstack/svelte-query', () => ({
	createQuery: vi.fn((factory: () => Record<string, unknown>) => factory())
}));

vi.mock('$lib/stores/authStore.svelte', () => ({
	authStore: { user: { id: 'user-1' } }
}));

const invalidate = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));

vi.mock('$lib/queries/QueryClient', () => ({
	invalidateQueriesWithPersister: invalidate
}));

vi.mock('$lib/stores/toast', () => ({
	toastStore: { show: vi.fn() }
}));

import { createDiscoveryBatch, removeDiscoveryBatch } from './DiscoveryBatchQuery.svelte';

const BATCH_LIST_KEY = ['discover', 'user-1', 'batches'];
const TASKS_KEY = ['downloads', 'tasks', 'user-1'];
const STATS_KEY = ['library', 'stats'];
const RECENTLY_ADDED_KEY = ['library', 'recently-added'];

beforeEach(() => {
	vi.clearAllMocks();
});

describe('outcome-gated discovery batch invalidation', () => {
	it('sweeps tasks when a create places requests', async () => {
		h.post.mockResolvedValue({
			id: 'batch-1',
			items: [
				{ outcome: 'requested', release_group_mbid: 'rg-1' },
				{ outcome: 'skipped_in_library', release_group_mbid: 'rg-2' }
			]
		});

		await createDiscoveryBatch({ name: 'n', source_section: 's', items: [] });

		const keys = invalidate.mock.calls.map(([filters]) => filters.queryKey);
		expect(keys).toContainEqual(BATCH_LIST_KEY);
		expect(keys).toContainEqual(TASKS_KEY);
		expect(keys).not.toContainEqual(STATS_KEY);
		expect(keys).not.toContainEqual(RECENTLY_ADDED_KEY);
	});

	it('sweeps nothing but the batch list when every item was skipped', async () => {
		h.post.mockResolvedValue({
			id: 'batch-1',
			items: [{ outcome: 'skipped_duplicate', release_group_mbid: 'rg-1' }]
		});

		await createDiscoveryBatch({ name: 'n', source_section: 's', items: [] });

		expect(invalidate).toHaveBeenCalledOnce();
		expect(invalidate.mock.calls[0][0].queryKey).toEqual(BATCH_LIST_KEY);
	});

	it('sweeps only the batch list when albums are kept and nothing was cancelled', async () => {
		h.delete.mockResolvedValue({ removed_albums: 0, cancelled_requests: 0, kept: 4 });

		await removeDiscoveryBatch('batch-1', false);

		expect(invalidate).toHaveBeenCalledOnce();
		expect(invalidate.mock.calls[0][0].queryKey).toEqual(BATCH_LIST_KEY);
	});

	it('adds the tasks sweep when pending requests were cancelled', async () => {
		h.delete.mockResolvedValue({ removed_albums: 0, cancelled_requests: 2, kept: 1 });

		await removeDiscoveryBatch('batch-1', false);

		const keys = invalidate.mock.calls.map(([filters]) => filters.queryKey);
		expect(keys).toEqual([BATCH_LIST_KEY, TASKS_KEY]);
	});

	it('adds stats and recency sweeps when albums were removed to the recycle bin', async () => {
		h.delete.mockResolvedValue({ removed_albums: 3, cancelled_requests: 1, kept: 0 });

		await removeDiscoveryBatch('batch-1', true);

		const keys = invalidate.mock.calls.map(([filters]) => filters.queryKey);
		expect(keys).toEqual([BATCH_LIST_KEY, TASKS_KEY, STATS_KEY, RECENTLY_ADDED_KEY]);
	});
});
