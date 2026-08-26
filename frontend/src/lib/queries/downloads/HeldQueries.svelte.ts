import { createQuery } from '@tanstack/svelte-query';
import type { Getter } from 'runed';

import { api } from '$lib/api/client';
import { API } from '$lib/constants';
import { authStore } from '$lib/stores/authStore.svelte';
import type { HeldListResponse } from '$lib/types';

import { DownloadQueryKeyFactory } from './DownloadQueryKeyFactory';

// Tracks held for an "import anyway" review. Two callers: the Downloads dashboard (no
// filter, cross-album triage) and the album page (scoped to one release group). Keyed under
// tasks() so a download mutation's invalidateTasks() refreshes it too. The global activity
// revision invalidates this prefix after held rows change, so it needs no parallel timer.
export const getHeldImportsQuery = (
	getMbid: Getter<string | undefined> = () => undefined,
	getEnabled: Getter<boolean> = () => true
) =>
	createQuery(() => ({
		enabled: getEnabled(),
		queryKey: DownloadQueryKeyFactory.held(authStore.user?.id, getMbid()),
		queryFn: ({ signal }) =>
			api.global.get<HeldListResponse>(API.downloads.held(getMbid()), { signal }),
		refetchOnWindowFocus: 'always' as const
	}));
