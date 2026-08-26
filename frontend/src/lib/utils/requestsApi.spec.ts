import { beforeEach, describe, expect, it, vi } from 'vitest';

const { mockDelete, mockGet, mockInvalidate, mockPost, mockSet } = vi.hoisted(() => ({
	mockDelete: vi.fn(),
	mockGet: vi.fn(),
	mockInvalidate: vi.fn(),
	mockPost: vi.fn(),
	mockSet: vi.fn()
}));

vi.mock('$lib/queries/QueryClient', () => ({
	invalidateQueriesWithPersister: (...args: unknown[]) => mockInvalidate(...args),
	setQueryDataWithPersister: (...args: unknown[]) => mockSet(...args)
}));

vi.mock('$lib/stores/authStore.svelte', () => ({
	authStore: { user: { id: 'admin-1' } }
}));

vi.mock('$lib/api/client', () => ({
	api: { global: { get: mockGet, post: mockPost, delete: mockDelete } }
}));

import {
	approveRequest,
	cancelRequest,
	clearHistoryItem,
	fetchActiveRequests,
	fetchPendingApprovals,
	fetchRequestHistory,
	notifyPendingApprovalCountChanged,
	rejectRequest,
	retryRequest
} from './requestsApi';

describe('pending approval count notifications', () => {
	beforeEach(() => vi.clearAllMocks());

	it('invalidates the exact persisted admin-user count key', () => {
		notifyPendingApprovalCountChanged();
		expect(mockInvalidate).toHaveBeenCalledWith({
			queryKey: ['following', 'admin-approvals', 'count', 'admin-1'],
			exact: true
		});
	});

	it('writes an explicit count through the persister', () => {
		notifyPendingApprovalCountChanged(4);
		expect(mockSet).toHaveBeenCalledWith(['following', 'admin-approvals', 'count', 'admin-1'], {
			count: 4
		});
	});
});

describe('request API URLs', () => {
	beforeEach(() => vi.clearAllMocks());

	it('uses registry builders for request list reads', async () => {
		const signal = new AbortController().signal;

		await fetchActiveRequests(signal);
		expect(mockGet).toHaveBeenNthCalledWith(1, '/api/v1/requests/active', { signal });

		await fetchRequestHistory(2, 50, 'failed', signal, 'oldest');
		expect(mockGet).toHaveBeenNthCalledWith(
			2,
			'/api/v1/requests/history?page=2&page_size=50&status=failed&sort=oldest',
			{ signal }
		);

		await fetchPendingApprovals(signal);
		expect(mockGet).toHaveBeenNthCalledWith(3, '/api/v1/requests/pending-approvals', { signal });
	});

	it('encodes mutation IDs and includes the album default or track kind', async () => {
		const id = 'release/group 1';

		await cancelRequest(id);
		expect(mockDelete).toHaveBeenNthCalledWith(
			1,
			'/api/v1/requests/active/release%2Fgroup%201?request_kind=album'
		);
		await cancelRequest(id, 'track');
		expect(mockDelete).toHaveBeenNthCalledWith(
			2,
			'/api/v1/requests/active/release%2Fgroup%201?request_kind=track'
		);

		await retryRequest(id);
		expect(mockPost).toHaveBeenNthCalledWith(
			1,
			'/api/v1/requests/retry/release%2Fgroup%201?request_kind=album'
		);
		await retryRequest(id, 'track');
		expect(mockPost).toHaveBeenNthCalledWith(
			2,
			'/api/v1/requests/retry/release%2Fgroup%201?request_kind=track'
		);

		await clearHistoryItem(id);
		expect(mockDelete).toHaveBeenNthCalledWith(
			3,
			'/api/v1/requests/history/release%2Fgroup%201?request_kind=album'
		);
		await clearHistoryItem(id, 'track');
		expect(mockDelete).toHaveBeenNthCalledWith(
			4,
			'/api/v1/requests/history/release%2Fgroup%201?request_kind=track'
		);

		await approveRequest(id);
		expect(mockPost).toHaveBeenNthCalledWith(
			3,
			'/api/v1/requests/approve/release%2Fgroup%201?request_kind=album'
		);
		await approveRequest(id, 'track');
		expect(mockPost).toHaveBeenNthCalledWith(
			4,
			'/api/v1/requests/approve/release%2Fgroup%201?request_kind=track'
		);

		await rejectRequest(id);
		expect(mockPost).toHaveBeenNthCalledWith(
			5,
			'/api/v1/requests/reject/release%2Fgroup%201?request_kind=album'
		);
		await rejectRequest(id, 'track');
		expect(mockPost).toHaveBeenNthCalledWith(
			6,
			'/api/v1/requests/reject/release%2Fgroup%201?request_kind=track'
		);
	});
});
