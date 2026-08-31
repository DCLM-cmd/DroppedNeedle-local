import { API } from '$lib/constants';

import { subscribeShared, type SharedEventSourceSubscription } from '../sharedEventSource';
import { invalidateQueriesWithPersister, queryClient } from '$lib/queries/QueryClient';
import { LibraryQueryKeyFactory } from './LibraryQueryKeyFactory';
import { invalidateLibraryCatalog } from './LibraryCatalogInvalidation';
import type { LibraryActivityResponse } from './LibraryOperationsTypes';

export function createLibraryActivityEvents() {
	let activitySubscription: SharedEventSourceSubscription | null = null;
	let operationsSubscription: SharedEventSourceSubscription | null = null;
	let revisions: Record<string, number> | null = null;
	let pendingInitialRevisions: Record<string, number> | null = null;
	let admin = false;
	let userId: string | null = null;
	let unsubscribeQueryCache: (() => void) | null = null;

	function invalidateActivity(): void {
		void invalidateQueriesWithPersister({ queryKey: LibraryQueryKeyFactory.activityPrefix() });
	}

	function invalidateOperations(): void {
		void invalidateQueriesWithPersister({ queryKey: LibraryQueryKeyFactory.operationsPrefix() });
		void invalidateQueriesWithPersister({ queryKey: LibraryQueryKeyFactory.reviewsPrefix() });
	}

	function parseRevisions(event: Event): Record<string, number> | null {
		if (!(event instanceof MessageEvent) || typeof event.data !== 'string') return null;
		try {
			const payload = JSON.parse(event.data) as { revisions?: Record<string, unknown> };
			if (!payload.revisions || typeof payload.revisions !== 'object') return null;
			const parsed: Record<string, number> = {};
			for (const [kind, revision] of Object.entries(payload.revisions)) {
				if (typeof revision !== 'number' || !Number.isSafeInteger(revision) || revision < 0) {
					return null;
				}
				parsed[kind] = revision;
			}
			return parsed;
		} catch {
			return null;
		}
	}

	function applyRevisionChange(
		previous: Record<string, number>,
		next: Record<string, number>
	): void {
		const signature = JSON.stringify(
			Object.entries(next).sort(([left], [right]) => left.localeCompare(right))
		);
		const previousSignature = JSON.stringify(
			Object.entries(previous).sort(([left], [right]) => left.localeCompare(right))
		);
		if (signature === previousSignature) return;
		if (next.catalog !== previous.catalog) {
			void invalidateLibraryCatalog();
			return;
		}
		invalidateActivity();
		if (admin) invalidateOperations();
	}

	function activityChanged(event: Event): void {
		const next = parseRevisions(event);
		if (next === null) return;
		if (revisions === null) {
			const baseline = userId
				? queryClient.getQueryData<LibraryActivityResponse>(LibraryQueryKeyFactory.activity(userId))
						?.revisions
				: undefined;
			revisions = next;
			if (baseline) applyRevisionChange(baseline, next);
			else pendingInitialRevisions = next;
			return;
		}
		const previous = revisions;
		revisions = next;
		applyRevisionChange(previous, next);
	}

	function reconcilePendingInitialRevisions(): void {
		if (!pendingInitialRevisions || !userId) return;
		const baseline = queryClient.getQueryData<LibraryActivityResponse>(
			LibraryQueryKeyFactory.activity(userId)
		)?.revisions;
		if (!baseline) return;
		const pending = pendingInitialRevisions;
		pendingInitialRevisions = null;
		applyRevisionChange(baseline, pending);
	}

	function start(isAdmin: boolean, sessionUserId: string): void {
		stop();
		admin = isAdmin;
		userId = sessionUserId;
		unsubscribeQueryCache = queryClient.getQueryCache().subscribe(() => {
			reconcilePendingInitialRevisions();
		});
		activitySubscription = subscribeShared(API.library.activityStream(), {
			'activity.changed': activityChanged
		});
		if (isAdmin) {
			// Shared with the Library Management pages, which listen to the same
			// stream - see sharedEventSource for why a duplicate costs so much.
			operationsSubscription = subscribeShared(API.library.operationsStream(), {
				open: invalidateOperations,
				'activity.changed': activityChanged
			});
		}
	}

	function stop(): void {
		activitySubscription?.close();
		operationsSubscription?.close();
		activitySubscription = null;
		operationsSubscription = null;
		revisions = null;
		pendingInitialRevisions = null;
		admin = false;
		userId = null;
		unsubscribeQueryCache?.();
		unsubscribeQueryCache = null;
	}

	return { start, stop };
}
