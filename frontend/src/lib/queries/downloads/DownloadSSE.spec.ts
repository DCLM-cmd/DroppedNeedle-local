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

beforeEach(() => {
	FakeEventSource.instances = [];
	vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
	vi.unstubAllGlobals();
});

const { createDownloadStream } = await import('./DownloadSSE.svelte');

describe('createDownloadStream', () => {
	it('maps progress events to rune state', () => {
		const s = createDownloadStream();
		s.start('t1');
		FakeEventSource.instances[0].emit('progress', {
			bytes_downloaded: 5,
			bytes_total: 10,
			files_completed: 1,
			files_total: 2,
			progress_percent: 50,
			candidate_index: 1,
			source: 'soulseek',
			quality_format: 'flac',
			quality_bit_depth: 16,
			quality_sample_rate: 44100,
			advertised_queue_depth: 0,
			queue_position_start: 91,
			queue_position_end: 100,
			remote_queued: true,
			preferred_quality_fallback_at: 1234.5,
			attempt_number: 1,
			attempt_total: 3,
			has_next_source: true
		});
		expect(s.state.progress?.progress_percent).toBe(50);
		expect(s.state.progress?.bytes_total).toBe(10);
		expect(s.state.source).toEqual({
			candidate_index: 1,
			source: 'soulseek',
			quality_format: 'flac',
			quality_bit_depth: 16,
			quality_sample_rate: 44100,
			advertised_queue_depth: 0,
			queue_position_start: 91,
			queue_position_end: 100,
			remote_queued: true,
			preferred_quality_fallback_at: 1234.5,
			attempt_number: 1,
			attempt_total: 3,
			has_next_source: true
		});
	});

	it('captures status events', () => {
		const s = createDownloadStream();
		s.start('t1');
		FakeEventSource.instances[0].emit('progress', {
			queue_position_start: 91,
			queue_position_end: 100
		});
		FakeEventSource.instances[0].emit('status', {
			status: 'retrying',
			candidate_index: 1,
			source: 'soulseek',
			quality_format: 'flac',
			quality_bit_depth: 16,
			quality_sample_rate: 44100,
			advertised_queue_depth: 0,
			queue_position_start: null,
			queue_position_end: null,
			remote_queued: false,
			attempt: 2,
			attempt_total: 3,
			has_next_source: true
		});
		expect(s.state.status).toBe('retrying');
		expect(s.state.source?.attempt_number).toBe(2);
		expect(s.state.source?.quality_bit_depth).toBe(16);
		expect(s.state.source?.candidate_index).toBe(1);
		expect(s.state.source?.queue_position_start).toBeNull();
		expect(s.state.source?.remote_queued).toBe(false);
	});

	it('marks done and closes the stream on the complete event', () => {
		const s = createDownloadStream();
		s.start('t1');
		const es = FakeEventSource.instances[0];
		es.emit('complete', { status: 'completed' });
		expect(s.state.done).toBe(true);
		expect(s.state.status).toBe('completed');
		expect(es.closed).toBe(true);
	});

	it('stop() closes the underlying EventSource', () => {
		const s = createDownloadStream();
		s.start('t1');
		const es = FakeEventSource.instances[0];
		s.stop();
		expect(es.closed).toBe(true);
	});
});
