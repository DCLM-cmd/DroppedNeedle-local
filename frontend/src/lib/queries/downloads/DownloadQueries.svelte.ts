import { createQuery, queryOptions } from '@tanstack/svelte-query';
import type { Getter } from 'runed';

import { api } from '$lib/api/client';
import { API } from '$lib/constants';
import { authStore } from '$lib/stores/authStore.svelte';
import type { DownloadActivitySummary, DownloadListResponse } from '$lib/types';

import { DownloadQueryKeyFactory } from './DownloadQueryKeyFactory';
import { hasActiveTask } from './downloadStatus';

const IDLE_ACTIVITY_RECOVERY_MS = 120_000;
const ACTIVE_ACTIVITY_RECOVERY_MS = 750;

// The global summary is the only recurring downloads HTTP owner. Progress is carried
// by per-task SSE; its structural revision refreshes the detailed page after status,
// ownership, held-review, insert, or delete changes.
export const getDownloadActivitySummaryQueryOptions = () =>
	queryOptions({
		staleTime: 0,
		enabled: !!authStore.user?.id,
		queryKey: DownloadQueryKeyFactory.activity(authStore.user?.id),
		queryFn: ({ signal }) =>
			api.global.get<DownloadActivitySummary>(API.downloads.activitySummary(), { signal }),
		refetchInterval: (query: { state: { data?: DownloadActivitySummary } }) =>
			(query.state.data?.active_count ?? 0) > 0
				? ACTIVE_ACTIVITY_RECOVERY_MS
				: IDLE_ACTIVITY_RECOVERY_MS,
		refetchIntervalInBackground: false,
		refetchOnReconnect: 'always' as const,
		refetchOnWindowFocus: 'always' as const
	});

export const getDownloadActivitySummaryQuery = () =>
	createQuery(() => getDownloadActivitySummaryQueryOptions());

// The downloads page owns the bounded detailed projection. It loads on entry and is
// refreshed by the global summary revision rather than running a second timer.
export const getDownloadsQueryOptions = () =>
	queryOptions({
		staleTime: 0,
		queryKey: DownloadQueryKeyFactory.tasks(authStore.user?.id),
		queryFn: ({ signal }) =>
			api.global.get<DownloadListResponse>(API.downloads.list(undefined, 1, 100), { signal }),
		refetchOnReconnect: 'always' as const,
		refetchOnWindowFocus: 'always' as const
	});

export const getDownloadsQuery = () => createQuery(() => getDownloadsQueryOptions());

// album-scoped: just this release group's tasks (album + per-track). Cheap (indexed
// on release_group_mbid), and only polls while a task is in flight - it stops itself
// when everything reaches a terminal state. Used by the album page for live progress.
export const getAlbumDownloadsQuery = (
	getMbid: Getter<string>,
	getEnabled: Getter<boolean> = () => true
) =>
	createQuery(() => ({
		staleTime: 0,
		enabled: getEnabled() && !!getMbid(),
		queryKey: DownloadQueryKeyFactory.albumTasks(authStore.user?.id, getMbid()),
		queryFn: ({ signal }) =>
			api.global.get<DownloadListResponse>(API.downloads.list(undefined, 1, 100, getMbid()), {
				signal
			}),
		refetchInterval: (query: { state: { data?: DownloadListResponse | undefined } }) =>
			hasActiveTask(query.state.data?.items ?? []) ? 2500 : false
	}));
