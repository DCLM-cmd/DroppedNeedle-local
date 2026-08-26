import { createInfiniteQuery, createQuery } from '@tanstack/svelte-query';
import type { Getter } from 'runed';

import { api } from '$lib/api/client';
import { API } from '$lib/constants';
import { authStore } from '$lib/stores/authStore.svelte';

import { ArtistReconciliationQueryKeyFactory } from './ArtistReconciliationQueryKeyFactory';
import type {
	ArtistDuplicateGroupDetail,
	ArtistDuplicateGroupListResponse,
	ArtistDuplicateGroupParams,
	ArtistReconciliationProgress
} from './ArtistReconciliationTypes';

const GROUP_PAGE_SIZE = 24;

export const getArtistReconciliationProgressQuery = (getEnabled: Getter<boolean> = () => true) =>
	createQuery(() => ({
		enabled: getEnabled(),
		queryKey: ArtistReconciliationQueryKeyFactory.progress(authStore.user?.id),
		queryFn: ({ signal }) =>
			api.global.get<ArtistReconciliationProgress>(API.library.artistReconciliation(), {
				signal
			}),
		refetchInterval: (query) =>
			query.state.data && ['queued', 'running', 'pausing'].includes(query.state.data.state)
				? 2000
				: false
	}));

export const getArtistDuplicateGroupsQuery = (getParams: Getter<ArtistDuplicateGroupParams>) =>
	createInfiniteQuery(() => {
		const params = getParams();
		return {
			queryKey: ArtistReconciliationQueryKeyFactory.groups(authStore.user?.id, params),
			initialPageParam: undefined as string | undefined,
			queryFn: ({ pageParam, signal }) =>
				api.global.get<ArtistDuplicateGroupListResponse>(
					API.library.artistDuplicateGroups({
						limit: GROUP_PAGE_SIZE,
						cursor: pageParam,
						state: params.state,
						search: params.search?.trim() || undefined
					}),
					{ signal }
				),
			getNextPageParam: (lastPage: ArtistDuplicateGroupListResponse) =>
				lastPage.next_cursor ?? undefined
		};
	});

export const getArtistDuplicateGroupQuery = (getGroupId: Getter<string | null>) =>
	createQuery(() => {
		const groupId = getGroupId();
		return {
			enabled: Boolean(groupId),
			queryKey: ArtistReconciliationQueryKeyFactory.group(authStore.user?.id, groupId ?? ''),
			queryFn: ({ signal }) =>
				api.global.get<ArtistDuplicateGroupDetail>(
					API.library.artistDuplicateGroup(groupId ?? ''),
					{ signal }
				)
		};
	});
