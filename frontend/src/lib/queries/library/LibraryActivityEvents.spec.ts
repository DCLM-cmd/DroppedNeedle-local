import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
	invalidate: vi.fn().mockResolvedValue(undefined),
	invalidateCatalog: vi.fn().mockResolvedValue(undefined),
	activityData: undefined as { revisions: Record<string, number> } | undefined,
	queryCacheListener: undefined as (() => void) | undefined
}));

vi.mock('$lib/queries/QueryClient', () => ({
	invalidateQueriesWithPersister: h.invalidate,
	queryClient: {
		getQueryData: () => h.activityData,
		getQueryCache: () => ({
			subscribe: (listener: () => void) => {
				h.queryCacheListener = listener;
				return () => {
					h.queryCacheListener = undefined;
				};
			}
		})
	}
}));
vi.mock('./LibraryCatalogInvalidation', () => ({
	invalidateLibraryCatalog: h.invalidateCatalog
}));

import { LibraryQueryKeyFactory } from './LibraryQueryKeyFactory';
import { createLibraryActivityEvents } from './LibraryActivityEvents';

class FakeEventSource {
	static instances: FakeEventSource[] = [];
	readonly url: string;
	readonly listeners = new Map<string, Set<(event: Event) => void>>();
	closed = false;

	constructor(url: string | URL) {
		this.url = String(url);
		FakeEventSource.instances.push(this);
	}

	addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
		const callback = listener as (event: Event) => void;
		const listeners = this.listeners.get(type) ?? new Set<(event: Event) => void>();
		listeners.add(callback);
		this.listeners.set(type, listeners);
	}

	close(): void {
		this.closed = true;
	}

	emit(type: string, event: Event = new Event(type)): void {
		for (const listener of this.listeners.get(type) ?? []) listener(event);
	}
}

beforeEach(() => {
	vi.clearAllMocks();
	FakeEventSource.instances = [];
	h.activityData = undefined;
	h.queryCacheListener = undefined;
	vi.stubGlobal('EventSource', FakeEventSource);
});

describe('createLibraryActivityEvents', () => {
	it('does not refetch activity when streams open and seed the same initial revision', () => {
		const events = createLibraryActivityEvents();
		events.start(true, 'user-1');
		expect(FakeEventSource.instances).toHaveLength(2);

		FakeEventSource.instances[0].emit('open');
		expect(h.invalidate).not.toHaveBeenCalled();

		FakeEventSource.instances[1].emit('open');
		expect(h.invalidate).toHaveBeenCalledWith({
			queryKey: LibraryQueryKeyFactory.operationsPrefix()
		});
		expect(h.invalidate).toHaveBeenCalledWith({
			queryKey: LibraryQueryKeyFactory.reviewsPrefix()
		});
		expect(h.invalidate).not.toHaveBeenCalledWith({
			queryKey: LibraryQueryKeyFactory.activityPrefix()
		});

		h.invalidate.mockClear();
		const initial = revisionEvent({ scan: 1, identification: 2, operation: 3, catalog: 4 });
		FakeEventSource.instances[0].emit('activity.changed', initial);
		FakeEventSource.instances[1].emit('activity.changed', initial);
		expect(h.invalidate).not.toHaveBeenCalled();
		expect(h.invalidateCatalog).not.toHaveBeenCalled();
	});

	it('invalidates activity and admin surfaces once for a duplicated genuine change', () => {
		const events = createLibraryActivityEvents();
		events.start(true, 'user-1');
		const initial = revisionEvent({ scan: 1, identification: 2, operation: 3, catalog: 4 });
		FakeEventSource.instances[0].emit('activity.changed', initial);
		FakeEventSource.instances[1].emit('activity.changed', initial);
		h.invalidate.mockClear();

		const changed = revisionEvent({ scan: 1, identification: 2, operation: 4, catalog: 4 });
		FakeEventSource.instances[0].emit('activity.changed', changed);
		FakeEventSource.instances[1].emit('activity.changed', changed);

		expect(h.invalidate).toHaveBeenCalledTimes(3);
		expect(h.invalidate).toHaveBeenCalledWith({
			queryKey: LibraryQueryKeyFactory.activityPrefix()
		});
		expect(h.invalidate).toHaveBeenCalledWith({
			queryKey: LibraryQueryKeyFactory.operationsPrefix()
		});
		expect(h.invalidate).toHaveBeenCalledWith({
			queryKey: LibraryQueryKeyFactory.reviewsPrefix()
		});
		expect(h.invalidateCatalog).not.toHaveBeenCalled();
	});

	it('uses one catalog sweep for a duplicated catalog revision change', () => {
		const events = createLibraryActivityEvents();
		events.start(true, 'user-1');
		FakeEventSource.instances[0].emit(
			'activity.changed',
			revisionEvent({ scan: 1, identification: 2, operation: 3, catalog: 4 })
		);

		const changed = revisionEvent({ scan: 1, identification: 2, operation: 3, catalog: 5 });
		FakeEventSource.instances[0].emit('activity.changed', changed);
		FakeEventSource.instances[1].emit('activity.changed', changed);

		expect(h.invalidateCatalog).toHaveBeenCalledOnce();
		expect(h.invalidate).not.toHaveBeenCalled();
	});

	it('does not refetch activity on a same-revision reconnect but catches an advanced revision', () => {
		const events = createLibraryActivityEvents();
		events.start(true, 'user-1');
		const source = FakeEventSource.instances[0];
		const operations = FakeEventSource.instances[1];
		const initial = revisionEvent({ scan: 1, identification: 2, operation: 3, catalog: 4 });
		source.emit('activity.changed', initial);
		operations.emit('activity.changed', initial);

		h.invalidate.mockClear();
		source.emit('open');
		operations.emit('open');
		source.emit('activity.changed', initial);
		operations.emit('activity.changed', initial);
		expect(h.invalidate).not.toHaveBeenCalledWith({
			queryKey: LibraryQueryKeyFactory.activityPrefix()
		});

		h.invalidate.mockClear();
		source.emit(
			'activity.changed',
			revisionEvent({ scan: 1, identification: 3, operation: 3, catalog: 4 })
		);
		expect(h.invalidate).toHaveBeenCalledWith({
			queryKey: LibraryQueryKeyFactory.activityPrefix()
		});
	});

	it('limits non-admin sessions to activity invalidation', () => {
		const events = createLibraryActivityEvents();
		events.start(false, 'user-1');
		expect(FakeEventSource.instances).toHaveLength(1);
		FakeEventSource.instances[0].emit(
			'activity.changed',
			revisionEvent({ scan: 1, identification: 2, operation: 3, catalog: 4 })
		);
		h.invalidate.mockClear();

		FakeEventSource.instances[0].emit(
			'activity.changed',
			revisionEvent({ scan: 2, identification: 2, operation: 3, catalog: 4 })
		);

		expect(h.invalidate).toHaveBeenCalledOnce();
		expect(h.invalidate).toHaveBeenCalledWith({
			queryKey: LibraryQueryKeyFactory.activityPrefix()
		});
	});

	it('closes replaced streams and resets revision state for the next session', () => {
		const events = createLibraryActivityEvents();
		events.start(true, 'user-1');
		const first = [...FakeEventSource.instances];
		first[0].emit(
			'activity.changed',
			revisionEvent({ scan: 1, identification: 2, operation: 3, catalog: 4 })
		);

		events.start(false, 'user-1');
		expect(first.every((source) => source.closed)).toBe(true);
		expect(FakeEventSource.instances).toHaveLength(3);
		h.invalidate.mockClear();
		FakeEventSource.instances[2].emit(
			'activity.changed',
			revisionEvent({ scan: 2, identification: 2, operation: 3, catalog: 4 })
		);
		expect(h.invalidate).not.toHaveBeenCalled();
		events.stop();
		expect(FakeEventSource.instances[2].closed).toBe(true);
	});

	it('ignores malformed revision payloads', () => {
		const events = createLibraryActivityEvents();
		events.start(false, 'user-1');
		FakeEventSource.instances[0].emit('activity.changed', new MessageEvent('activity.changed'));
		FakeEventSource.instances[0].emit(
			'activity.changed',
			new MessageEvent('activity.changed', { data: '{"revisions":{"scan":"one"}}' })
		);
		expect(h.invalidate).not.toHaveBeenCalled();
		expect(h.invalidateCatalog).not.toHaveBeenCalled();
	});

	it('invalidates when the first SSE revision is newer than the HTTP snapshot', () => {
		const events = createLibraryActivityEvents();
		events.start(false, 'user-1');
		FakeEventSource.instances[0].emit(
			'activity.changed',
			revisionEvent({ scan: 2, identification: 2, operation: 3, catalog: 4 })
		);
		expect(h.invalidate).not.toHaveBeenCalled();

		h.activityData = {
			revisions: { scan: 1, identification: 2, operation: 3, catalog: 4 }
		};
		h.queryCacheListener?.();

		expect(h.invalidate).toHaveBeenCalledOnce();
		expect(h.invalidate).toHaveBeenCalledWith({
			queryKey: LibraryQueryKeyFactory.activityPrefix()
		});
	});
});

function revisionEvent(revisions: Record<string, number>): MessageEvent<string> {
	return new MessageEvent('activity.changed', { data: JSON.stringify({ revisions }) });
}
