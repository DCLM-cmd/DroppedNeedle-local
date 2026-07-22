import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

class FakeEventSource {
	static instances: FakeEventSource[] = [];
	url: string;
	listeners: Record<string, ((e: MessageEvent) => void)[]> = {};
	closed = false;

	constructor(url: string) {
		this.url = url;
		FakeEventSource.instances.push(this);
	}

	addEventListener(type: string, cb: (e: MessageEvent) => void) {
		(this.listeners[type] ??= []).push(cb);
	}

	close() {
		this.closed = true;
	}

	emit(type: string, data: unknown) {
		const ev = { data: JSON.stringify(data) } as MessageEvent;
		for (const cb of this.listeners[type] ?? []) cb(ev);
	}
}

const { createDownloadStream } = await import('./DownloadSSE.svelte');

// The multiplexer holds ONE module-level EventSource while ≥1 stream is started;
// stopping every stream closes it, so each test must stop what it starts.
let streams: ReturnType<typeof createDownloadStream>[] = [];

function mkStream() {
	const s = createDownloadStream();
	streams.push(s);
	return s;
}

beforeEach(() => {
	FakeEventSource.instances = [];
	vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
	for (const s of streams) s.stop();
	streams = [];
	vi.unstubAllGlobals();
});

describe('createDownloadStream (multiplexed)', () => {
	it('connects once to the shared all-downloads stream', () => {
		mkStream().start('t1');
		mkStream().start('t2');
		expect(FakeEventSource.instances).toHaveLength(1);
		expect(FakeEventSource.instances[0].url).toBe('/api/v1/downloads/stream');
	});

	it('maps progress events to rune state, demuxed by task_id', () => {
		const s1 = mkStream();
		const s2 = mkStream();
		s1.start('t1');
		s2.start('t2');
		FakeEventSource.instances[0].emit('progress', {
			task_id: 't1',
			bytes_downloaded: 5,
			bytes_total: 10,
			files_completed: 1,
			files_total: 2,
			progress_percent: 50
		});
		expect(s1.state.progress?.progress_percent).toBe(50);
		expect(s1.state.progress?.bytes_total).toBe(10);
		expect(s2.state.progress).toBeNull(); // other task untouched
	});

	it('ignores events without a task_id and for unknown tasks', () => {
		const s = mkStream();
		s.start('t1');
		FakeEventSource.instances[0].emit('status', { status: 'downloading' });
		FakeEventSource.instances[0].emit('status', { task_id: 'other', status: 'downloading' });
		expect(s.state.status).toBeNull();
	});

	it('captures status events', () => {
		const s = mkStream();
		s.start('t1');
		FakeEventSource.instances[0].emit('status', { task_id: 't1', status: 'downloading' });
		expect(s.state.status).toBe('downloading');
	});

	it('marks done on the complete event and keeps the shared stream for others', () => {
		const s1 = mkStream();
		const s2 = mkStream();
		s1.start('t1');
		s2.start('t2');
		const es = FakeEventSource.instances[0];
		es.emit('complete', { task_id: 't1', status: 'completed' });
		expect(s1.state.done).toBe(true);
		expect(s1.state.status).toBe('completed');
		expect(es.closed).toBe(false); // t2 still listening
		es.emit('status', { task_id: 't2', status: 'processing' });
		expect(s2.state.status).toBe('processing');
	});

	it('closes the shared EventSource once the last subscriber stops', () => {
		const s1 = mkStream();
		const s2 = mkStream();
		s1.start('t1');
		s2.start('t2');
		const es = FakeEventSource.instances[0];
		s1.stop();
		expect(es.closed).toBe(false);
		s2.stop();
		expect(es.closed).toBe(true);
	});

	it('reconnects with a fresh EventSource after a full stop', () => {
		const s = mkStream();
		s.start('t1');
		s.stop();
		s.start('t1');
		expect(FakeEventSource.instances).toHaveLength(2);
		expect(FakeEventSource.instances[1].closed).toBe(false);
	});
});
