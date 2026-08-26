import { api } from '$lib/api/client';
import { API } from '$lib/constants';
import { authStore } from '$lib/stores/authStore.svelte';
import { toastStore } from '$lib/stores/toast';
import { createQuery } from '@tanstack/svelte-query';
import { invalidateQueriesWithPersister } from '$lib/queries/QueryClient';
import { DownloadQueryKeyFactory } from '$lib/queries/downloads/DownloadQueryKeyFactory';
import { LibraryQueryKeyFactory } from '$lib/queries/library/LibraryQueryKeyFactory';
import { DiscoverQueryKeyFactory } from './DiscoverQueryKeyFactory';
import type {
	DiscoveryBatchCreate,
	DiscoveryBatchDetail,
	DiscoveryBatchListResponse,
	DiscoveryBatchRemoveResult
} from '$lib/types';

export const discoveryBatchKeys = {
	list: (userId: string | null | undefined) =>
		[...DiscoverQueryKeyFactory.prefix, userId ?? null, 'batches'] as const
};

export const getDiscoveryBatchesQuery = (getEnabled: () => boolean = () => true) =>
	createQuery(() => ({
		staleTime: 15_000,
		queryKey: discoveryBatchKeys.list(authStore.user?.id),
		queryFn: ({ signal }) =>
			api.global.get<DiscoveryBatchListResponse>(API.discoverBatches(), { signal }),
		enabled: getEnabled()
	}));

// Sweep only what the API result says changed:
// - create: download tasks shift when at least one item was actually requested
//   (skips change nothing outside the batch itself);
// - remove: tasks shift on cancelled requests or recycled albums; library
//   counts/recency move only when albums were removed.
async function invalidateAfterBatchChange(
	result: DiscoveryBatchDetail | DiscoveryBatchRemoveResult
): Promise<void> {
	await invalidateQueriesWithPersister({
		queryKey: discoveryBatchKeys.list(authStore.user?.id)
	});
	const isCreate = 'items' in result;
	const requested = isCreate && result.items.some((item) => item.outcome === 'requested');
	const cancelledRequests = isCreate ? 0 : result.cancelled_requests;
	const removedAlbums = isCreate ? 0 : result.removed_albums;
	if (requested || cancelledRequests > 0 || removedAlbums > 0) {
		// pending-request state lives under the tasks prefix
		await invalidateQueriesWithPersister({
			queryKey: DownloadQueryKeyFactory.tasks(authStore.user?.id)
		});
	}
	if (removedAlbums > 0) {
		// The client holds no per-item release-group ids at removal time (batch
		// list summaries omit items), so album-detail keys cannot be targeted
		// individually; stats + recently-added carry the visible shift and any
		// unopened album page self-heals via the global staleTime window.
		await invalidateQueriesWithPersister({ queryKey: LibraryQueryKeyFactory.stats() });
		await invalidateQueriesWithPersister({ queryKey: LibraryQueryKeyFactory.recentlyAdded() });
	}
}

export async function createDiscoveryBatch(
	body: DiscoveryBatchCreate
): Promise<DiscoveryBatchDetail | null> {
	try {
		const created = await api.global.post<DiscoveryBatchDetail>(API.discoverBatches(), body);
		const requested = created.items.filter((i) => i.outcome === 'requested').length;
		const skipped = created.items.length - requested;
		toastStore.show({
			message:
				`${requested} album${requested === 1 ? '' : 's'} requested` +
				(skipped ? ` · ${skipped} already yours or requested` : ''),
			type: 'success'
		});
		await invalidateAfterBatchChange(created);
		return created;
	} catch (err) {
		toastStore.show({
			message: err instanceof Error ? err.message : "Couldn't create the batch",
			type: 'error'
		});
		return null;
	}
}

export async function removeDiscoveryBatch(
	batchId: string,
	removeAlbums: boolean
): Promise<DiscoveryBatchRemoveResult | null> {
	try {
		const result = await api.global.delete<DiscoveryBatchRemoveResult>(
			API.discoverBatchRemove(batchId, removeAlbums)
		);
		if (removeAlbums) {
			toastStore.show({
				message:
					`Removed ${result.removed_albums} album${result.removed_albums === 1 ? '' : 's'} to the recycle bin` +
					(result.cancelled_requests ? `, cancelled ${result.cancelled_requests} pending` : '') +
					(result.kept ? `, left ${result.kept} untouched` : ''),
				type: 'success'
			});
		} else {
			toastStore.show({ message: 'Batch record removed - albums kept', type: 'success' });
		}
		await invalidateAfterBatchChange(result);
		return result;
	} catch (err) {
		toastStore.show({
			message: err instanceof Error ? err.message : "Couldn't remove the batch",
			type: 'error'
		});
		return null;
	}
}
