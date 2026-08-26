import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@tanstack/svelte-query', () => ({
	createMutation: vi.fn((factory: () => Record<string, unknown>) => factory())
}));

const post = vi.hoisted(() => vi.fn());
const invalidate = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));
const toast = vi.hoisted(() => vi.fn());

vi.mock('$lib/api/client', () => ({ api: { global: { post } } }));
vi.mock('./ArtistReconciliationInvalidation', () => ({
	invalidateArtistReconciliation: invalidate
}));
vi.mock('$lib/stores/toast', () => ({ toastStore: { show: toast } }));

import { dismissArtistDuplicateGroup } from './ArtistReconciliationMutations.svelte';

beforeEach(() => {
	vi.clearAllMocks();
	post.mockResolvedValue({ group_id: 'group-1', dismissed_pairs: 1 });
});

describe('dismissArtistDuplicateGroup', () => {
	it('posts the exact member revisions and invalidates persisted reconciliation caches', async () => {
		const mutation = dismissArtistDuplicateGroup() as unknown as {
			mutationFn: (input: {
				groupId: string;
				expectedMemberRevisions: Record<string, number>;
			}) => Promise<unknown>;
			onSuccess: () => Promise<void>;
		};
		await mutation.mutationFn({
			groupId: 'group-1',
			expectedMemberRevisions: { 'artist-1': 3, 'artist-2': 5 }
		});
		expect(post).toHaveBeenCalledWith('/api/v1/library/artists/duplicate-groups/group-1/dismiss', {
			expected_member_revisions: { 'artist-1': 3, 'artist-2': 5 }
		});

		await mutation.onSuccess();
		expect(invalidate).toHaveBeenCalledOnce();
		expect(toast).toHaveBeenCalledWith({
			message: 'Artist records marked as distinct',
			type: 'success'
		});
	});
});
