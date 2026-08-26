import type { ActiveRequestsResponse, RequestHistoryResponse, RequestKind } from '$lib/types';
import { api } from '$lib/api/client';
import { API } from '$lib/constants';
import { authStore } from '$lib/stores/authStore.svelte';
import { FollowQueryKeyFactory } from '$lib/queries/following/FollowQueryKeyFactory';
import {
	invalidateQueriesWithPersister,
	setQueryDataWithPersister
} from '$lib/queries/QueryClient';
export type { ActiveRequestsResponse, RequestHistoryResponse, RequestKind } from '$lib/types';

export function notifyPendingApprovalCountChanged(count?: number): void {
	const queryKey = FollowQueryKeyFactory.pendingApprovalCount(authStore.user?.id);
	if (typeof count === 'number') {
		void setQueryDataWithPersister<{ count: number }>(queryKey, { count });
		return;
	}
	void invalidateQueriesWithPersister({ queryKey, exact: true });
}

export async function fetchActiveRequests(signal?: AbortSignal): Promise<ActiveRequestsResponse> {
	return api.global.get<ActiveRequestsResponse>(API.requests.active(), { signal });
}

export async function fetchRequestHistory(
	page: number = 1,
	pageSize: number = 20,
	status?: string,
	signal?: AbortSignal,
	sort?: string
): Promise<RequestHistoryResponse> {
	return api.global.get<RequestHistoryResponse>(
		API.requests.history(page, pageSize, status, sort),
		{ signal }
	);
}

export async function cancelRequest(
	musicbrainzId: string,
	requestKind: RequestKind = 'album'
): Promise<{ success: boolean; message: string }> {
	const data = await api.global.delete<{ success: boolean; message: string }>(
		API.requests.cancel(musicbrainzId, requestKind)
	);
	return data;
}

export async function retryRequest(
	musicbrainzId: string,
	requestKind: RequestKind = 'album'
): Promise<{ success: boolean; message: string }> {
	const data = await api.global.post<{ success: boolean; message: string }>(
		API.requests.retry(musicbrainzId, requestKind)
	);
	return data;
}

export async function clearHistoryItem(
	musicbrainzId: string,
	requestKind: RequestKind = 'album'
): Promise<{ success: boolean }> {
	return api.global.delete<{ success: boolean }>(
		API.requests.clearHistoryItem(musicbrainzId, requestKind)
	);
}

export async function fetchPendingApprovals(signal?: AbortSignal): Promise<ActiveRequestsResponse> {
	return api.global.get<ActiveRequestsResponse>(API.requests.pendingApprovals(), { signal });
}

export async function approveRequest(
	musicbrainzId: string,
	requestKind: RequestKind = 'album'
): Promise<{ success: boolean; message: string }> {
	return api.global.post<{ success: boolean; message: string }>(
		API.requests.approve(musicbrainzId, requestKind)
	);
}

export async function rejectRequest(
	musicbrainzId: string,
	requestKind: RequestKind = 'album'
): Promise<{ success: boolean; message: string }> {
	return api.global.post<{ success: boolean; message: string }>(
		API.requests.reject(musicbrainzId, requestKind)
	);
}
