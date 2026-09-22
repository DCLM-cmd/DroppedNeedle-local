import { api } from '$lib/api/client';
import { API } from '$lib/constants';
import { authStore } from '$lib/stores/authStore.svelte';
import type { DiscoverActivity, DiscoverQueuePreview } from '$lib/types';
import { createMutation } from '@tanstack/svelte-query';
import { untrack } from 'svelte';
import {
	musicBrainzSourceKey,
	setMusicBrainzSourceScope
} from '$lib/queries/musicbrainz/sourceScope.svelte';
import type { MusicBrainzSettingsResponse } from '$lib/queries/musicbrainz/types';

type ActivityResponse = Pick<
	MusicBrainzSettingsResponse,
	'source_mode' | 'source_id' | 'generation'
>;

export async function recordDiscoverActivity(
	activity: DiscoverActivity,
	signal?: AbortSignal
): Promise<void> {
	const userId = authStore.user?.id;
	const before = JSON.stringify(musicBrainzSourceKey());
	const source = await api.global.post<ActivityResponse>(API.discoverActivity(), activity, {
		signal
	});
	if (
		!signal?.aborted &&
		userId === authStore.user?.id &&
		before === JSON.stringify(musicBrainzSourceKey())
	) {
		setMusicBrainzSourceScope(source, userId);
	}
}

export const getQueuePreviewMutation = () =>
	createMutation(() => ({
		retry: false,
		mutationFn: ({ mbid, signal }: { mbid: string; signal: AbortSignal }) =>
			api.global.post<DiscoverQueuePreview>(API.discoverQueuePreview(mbid), undefined, { signal })
	}));

export function useDiscoverActivity(
	getActivity: () => DiscoverActivity | null,
	getElement?: () => HTMLElement | undefined
): void {
	const mutation = createMutation(() => ({
		retry: false,
		mutationFn: (activity: DiscoverActivity) => recordDiscoverActivity(activity)
	}));
	$effect(() => {
		const userId = authStore.user?.id;
		const activity = getActivity();
		const element = getElement?.();
		if (!userId || !activity || (getElement && !element)) return;
		let visible = !getElement;
		let entered = false;
		const signalEntry = () => {
			const active = visible && document.visibilityState === 'visible';
			if (active && !entered) untrack(() => mutation.mutate(activity));
			entered = active;
		};
		const onFocus = () => {
			entered = false;
			signalEntry();
		};
		const observer = element
			? new IntersectionObserver(([entry]) => {
					visible = entry.isIntersecting;
					signalEntry();
				})
			: null;
		if (element) observer?.observe(element);
		document.addEventListener('visibilitychange', signalEntry);
		window.addEventListener('focus', onFocus);
		signalEntry();
		return () => {
			observer?.disconnect();
			document.removeEventListener('visibilitychange', signalEntry);
			window.removeEventListener('focus', onFocus);
		};
	});
}
