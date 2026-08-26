import { api } from '$lib/api/client';
import { createQuery } from '@tanstack/svelte-query';
import { FollowQueryKeyFactory } from './FollowQueryKeyFactory';
import { FOLLOW_ENDPOINTS } from './endpoints';
import type { ApprovalBatchListResponse, AutoDownloadApprovalsResponse } from './types';
import { authStore } from '$lib/stores/authStore.svelte';
import { API } from '$lib/constants';

type Getter<T> = () => T;

export const getAutoDownloadApprovalsQuery = (getEnabled: Getter<boolean>) =>
	createQuery(() => ({
		queryKey: FollowQueryKeyFactory.adminApprovals(),
		queryFn: ({ signal }) =>
			api.global.get<AutoDownloadApprovalsResponse>(FOLLOW_ENDPOINTS.adminApprovals(), { signal }),
		enabled: getEnabled()
	}));

// The bulk "Lidarr Import" approval cards (LidarrImport D3).
export const getAutoDownloadApprovalBatchesQuery = (getEnabled: Getter<boolean>) =>
	createQuery(() => ({
		queryKey: FollowQueryKeyFactory.adminApprovalBatches(),
		queryFn: ({ signal }) =>
			api.global.get<ApprovalBatchListResponse>(FOLLOW_ENDPOINTS.adminApprovalBatches(), {
				signal
			}),
		enabled: getEnabled()
	}));

export const getPendingApprovalCountQuery = (getEnabled: Getter<boolean>) =>
	createQuery(() => ({
		queryKey: FollowQueryKeyFactory.pendingApprovalCount(authStore.user?.id),
		queryFn: ({ signal }) =>
			api.global.get<{ count: number }>(API.requests.pendingApprovalCount(), { signal }),
		enabled: getEnabled() && !!authStore.user?.id,
		staleTime: 0,
		refetchInterval: 120_000,
		refetchIntervalInBackground: false,
		refetchOnReconnect: 'always' as const,
		refetchOnWindowFocus: 'always' as const
	}));
