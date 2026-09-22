import { writable } from 'svelte/store';
import { browser } from '$app/environment';
import { API } from '$lib/constants';
import { getCacheTTLs } from '$lib/stores/cacheTtl.svelte';
import { api, ApiError } from '$lib/api/client';
import { authStore } from '$lib/stores/authStore.svelte';
import { musicBrainzSourceKey } from '$lib/queries/musicbrainz/sourceScope.svelte';

export type QueueBuildStatus = 'idle' | 'building' | 'ready' | 'error' | 'unknown';

interface DiscoverQueueStatusState {
	status: QueueBuildStatus;
	queueId?: string;
	itemCount?: number;
	error?: string;
	lastChecked: number;
}

type QueueStatusPayload = {
	status: QueueBuildStatus;
	queue_id?: string;
	item_count?: number;
	error?: string;
};

const INITIAL: DiscoverQueueStatusState = {
	status: 'unknown',
	lastChecked: 0
};

function createDiscoverQueueStatusStore() {
	const { subscribe, set, update } = writable<DiscoverQueueStatusState>({ ...INITIAL });

	let pollTimer: ReturnType<typeof setInterval> | null = null;
	let isPolling = false;
	let epoch = 0;
	const requestScope = () =>
		`${epoch}:${authStore.user?.id}:${JSON.stringify(musicBrainzSourceKey())}`;

	function getPollingInterval(): number {
		return getCacheTTLs().discoverQueuePollingInterval;
	}

	function applyStatusData(data: QueueStatusPayload): void {
		set({
			status: data.status,
			queueId: data.queue_id,
			itemCount: data.item_count,
			error: data.error,
			lastChecked: Date.now()
		});
	}

	async function fetchStatus(): Promise<QueueStatusPayload | null> {
		if (!browser) return null;
		const scope = requestScope();
		try {
			const data = await api.global.get<QueueStatusPayload>(API.discoverQueueStatus());
			if (scope !== requestScope()) return null;
			applyStatusData(data);
			return data;
		} catch {
			return null;
		}
	}

	async function triggerGenerate(force = false): Promise<void> {
		if (!browser) return;
		const scope = requestScope();
		try {
			update((s) => ({ ...s, status: 'building' }));
			const data = await api.global.post<QueueStatusPayload>(API.discoverQueueGenerate(), {
				force
			});
			if (scope !== requestScope()) return;
			applyStatusData(data);
			if (data.status === 'building') {
				startPolling();
			}
		} catch (e) {
			if (scope !== requestScope()) return;
			if (e instanceof ApiError) {
				update((s) => ({
					...s,
					status: 'error',
					error: `Server responded with ${e.status}`
				}));
			} else {
				update((s) => ({ ...s, status: 'error', error: 'Failed to trigger generation' }));
			}
		}
	}

	function startPolling(): void {
		if (pollTimer || !browser) return;
		isPolling = true;
		const interval = getPollingInterval();
		pollTimer = setInterval(async () => {
			const result = await fetchStatus();
			if (result && result.status !== 'building') {
				stopPolling();
			}
		}, interval);
	}

	function stopPolling(): void {
		if (pollTimer) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
		isPolling = false;
	}

	async function init(): Promise<void> {
		if (!browser) return;
		const result = await fetchStatus();
		if (!result) return;

		if (result.status === 'building') {
			startPolling();
		}
	}

	function reset(): void {
		epoch++;
		stopPolling();
		set({ ...INITIAL });
	}

	function markConsumed(): void {
		update((s) => ({ ...s, status: 'idle', queueId: undefined, itemCount: undefined }));
	}

	return {
		subscribe,
		fetchStatus,
		triggerGenerate,
		startPolling,
		stopPolling,
		init,
		reset,
		markConsumed,
		get isPolling() {
			return isPolling;
		}
	};
}

export const discoverQueueStatusStore = createDiscoverQueueStatusStore();
