import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { apiGet, focus, playbackToast } = vi.hoisted(() => ({
	apiGet: vi.fn(),
	focus: { claim: vi.fn(), release: vi.fn(), interrupt: vi.fn() },
	playbackToast: { show: vi.fn(), dismiss: vi.fn() }
}));
vi.mock('$lib/api/client', () => ({
	api: { global: { get: (...args: unknown[]) => apiGet(...args) } }
}));
vi.mock('$lib/stores/audioFocus.svelte', () => ({ audioFocus: focus }));
vi.mock('$lib/stores/playbackToast.svelte', () => ({ playbackToast }));

// A controllable stand-in for HTMLAudioElement: tests drive currentTime / ended
// to simulate a clip reaching its end, then let the ticker advance.
type DeferredPlay = {
	promise: Promise<void>;
	resolve: () => void;
	reject: (reason?: unknown) => void;
};

class FakeAudio {
	static created: FakeAudio[] = [];
	/** elements that attempted a real clip (excludes muted gesture unlocks) */
	static started: FakeAudio[] = [];
	static realPlayFailures: unknown[] = [];
	static deferredRealPlays: DeferredPlay[] = [];
	src = '';
	preload = '';
	muted = false;
	volume = 1;
	currentTime = 0;
	duration = 30;
	ended = false;
	play = vi.fn(() => {
		if (this.muted || !this.src) return Promise.resolve();
		FakeAudio.started.push(this);
		const deferred = FakeAudio.deferredRealPlays.shift();
		if (deferred) return deferred.promise;
		const failure = FakeAudio.realPlayFailures.shift();
		return failure === undefined ? Promise.resolve() : Promise.reject(failure);
	});
	pause = vi.fn(() => {});
	removeAttribute = vi.fn((name: string) => {
		if (name === 'src') this.src = '';
	});
	load = vi.fn(() => {
		this.currentTime = 0;
		this.ended = false;
	});
	constructor() {
		FakeAudio.created.push(this);
	}
	/** create a play promise that a test can settle after a restart */
	static deferRealPlay(): DeferredPlay {
		let resolve!: () => void;
		let reject!: (reason?: unknown) => void;
		const promise = new Promise<void>((res, rej) => {
			resolve = res;
			reject = rej;
		});
		const deferred = { promise, resolve, reject };
		FakeAudio.deferredRealPlays.push(deferred);
		return deferred;
	}
	/** simulate this clip finishing */
	finish() {
		this.currentTime = this.duration;
		this.ended = true;
	}
}

import { deckSampler } from './deckSampler.svelte';

function albumPreview(n: number, provider = 'deezer') {
	return {
		provider,
		tracks: Array.from({ length: n }, (_, i) => ({
			title: `Track ${i + 1}`,
			artist_name: 'Artist',
			preview_url: `https://p/${i}.mp3`,
			duration_s: 30,
			position: i + 1
		}))
	};
}

beforeEach(() => {
	vi.clearAllMocks();
	FakeAudio.created = [];
	FakeAudio.started = [];
	FakeAudio.realPlayFailures = [];
	FakeAudio.deferredRealPlays = [];
	(globalThis as unknown as { Audio: typeof FakeAudio }).Audio = FakeAudio;
});

afterEach(() => {
	deckSampler.stop();
	playbackToast.dismiss();
	vi.useRealTimers();
});

async function waitForStatus(status: string) {
	await vi.waitFor(() => expect(deckSampler.status).toBe(status));
}

describe('deckSampler single album', () => {
	it('plays an album’s clips back-to-back then ends', async () => {
		apiGet.mockResolvedValue(albumPreview(2));

		deckSampler.start('rg-1', 'Artist', 'Album', { artistMbid: 'a-1', coverUrl: 'c.jpg' });
		await waitForStatus('playing');

		expect(deckSampler.currentEntry?.title).toBe('Album');
		expect(deckSampler.currentEntry?.albumMbid).toBe('rg-1');
		expect(deckSampler.provider).toBe('deezer');
		expect(deckSampler.trackIndex).toBe(0);

		// clip 1 ends -> advance to clip 2
		FakeAudio.started.at(-1)!.finish();
		await vi.waitFor(() => expect(deckSampler.trackIndex).toBe(1));

		// clip 2 ends -> station of one is exhausted -> idle
		FakeAudio.started.at(-1)!.finish();
		await waitForStatus('idle');
		expect(focus.release).toHaveBeenCalled();
	});

	it('reports error when the album has no previews', async () => {
		apiGet.mockResolvedValue(albumPreview(0));
		deckSampler.start('rg-x', 'Artist', 'Nope');
		await waitForStatus('error');
	});
});

describe('deckSampler station', () => {
	it('advances from one album to the next', async () => {
		apiGet.mockResolvedValue(albumPreview(2));

		deckSampler.startStation('Station', [
			{ key: 'rg-1', kind: 'album', artist: 'A1', title: 'Album 1', albumMbid: 'rg-1' },
			{ key: 'rg-2', kind: 'album', artist: 'A2', title: 'Album 2', albumMbid: 'rg-2' }
		]);
		await waitForStatus('playing');
		expect(deckSampler.isStation).toBe(true);
		expect(deckSampler.stationPosition).toEqual({ index: 0, total: 2 });

		// exhaust the first album's 2 clips -> should load the second entry
		FakeAudio.started.at(-1)!.finish();
		await vi.waitFor(() => expect(deckSampler.trackIndex).toBe(1));
		FakeAudio.started.at(-1)!.finish();
		await vi.waitFor(() => expect(deckSampler.stationPosition.index).toBe(1));
		expect(deckSampler.currentEntry?.title).toBe('Album 2');
	});
	it('reuses two pooled elements across crossfades', async () => {
		vi.useFakeTimers();
		apiGet.mockResolvedValue(albumPreview(3));
		deckSampler.start('rg-1', 'Artist', 'Album');
		await waitForStatus('playing');

		const first = FakeAudio.started.at(-1)!;
		first.currentTime = first.duration - 0.25;
		await vi.advanceTimersByTimeAsync(100);
		await vi.waitFor(() => expect(FakeAudio.started).toHaveLength(2));

		const second = FakeAudio.started.at(-1)!;
		second.currentTime = second.duration - 0.25;
		await vi.advanceTimersByTimeAsync(100);
		await vi.waitFor(() => expect(FakeAudio.started).toHaveLength(3));

		expect(new Set(FakeAudio.started)).toHaveLength(2);
		expect(FakeAudio.started[2]).toBe(first);
		expect(FakeAudio.started[1]).not.toBe(first);
	});

	it('rapid next() never starts two <audio> at once (session-guarded race)', async () => {
		// deferred fetches so we can pile up skips before any entry resolves
		const resolvers: ((v: unknown) => void)[] = [];
		apiGet.mockImplementation(
			() => new Promise((resolve) => resolvers.push(resolve as (v: unknown) => void))
		);

		deckSampler.startStation('Station', [
			{ key: 'rg-1', kind: 'album', artist: 'A1', title: 'Album 1', albumMbid: 'rg-1' },
			{ key: 'rg-2', kind: 'album', artist: 'A2', title: 'Album 2', albumMbid: 'rg-2' },
			{ key: 'rg-3', kind: 'album', artist: 'A3', title: 'Album 3', albumMbid: 'rg-3' }
		]);
		// entry 0 is fetching; skip twice before it (or entry 1) resolves
		deckSampler.next();
		deckSampler.next();

		// now let all three in-flight fetches resolve; only the last (current session)
		// chain should survive its `mySession !== session` guard and play
		resolvers.forEach((r) => r(albumPreview(2)));
		await vi.waitFor(() => expect(deckSampler.status).toBe('playing'));

		expect(deckSampler.stationPosition.index).toBe(2);
		expect(deckSampler.currentEntry?.title).toBe('Album 3');
		// exactly one element was ever started -> no double audio
		expect(FakeAudio.started.length).toBe(1);
	});
	it('reports feedback when every station entry is exhausted', async () => {
		vi.useFakeTimers();
		apiGet.mockResolvedValue(albumPreview(0));
		deckSampler.startStation('Station', [
			{ key: 'rg-1', kind: 'album', artist: 'A1', title: 'Album 1', albumMbid: 'rg-1' },
			{ key: 'rg-2', kind: 'album', artist: 'A2', title: 'Album 2', albumMbid: 'rg-2' }
		]);

		await waitForStatus('error');
		expect(playbackToast.show).toHaveBeenCalledWith(
			'Preview station ended: no more playable clips',
			'warning'
		);
	});

	it('ignores a stale play fulfillment after a restart', async () => {
		vi.useFakeTimers();
		apiGet.mockResolvedValue(albumPreview(1));
		const stalePlay = FakeAudio.deferRealPlay();

		deckSampler.start('rg-old', 'Artist', 'Old');
		await vi.waitFor(() => expect(FakeAudio.started).toHaveLength(1));

		deckSampler.start('rg-new', 'Artist', 'New');
		await waitForStatus('playing');
		await vi.waitFor(() => expect(FakeAudio.started).toHaveLength(2));
		const current = FakeAudio.started.at(-1)!;

		current.currentTime = 15;
		stalePlay.resolve();
		await vi.advanceTimersByTimeAsync(100);
		current.currentTime = 20;
		await vi.advanceTimersByTimeAsync(100);

		expect(deckSampler.activeKey).toBe('rg-new');
		expect(deckSampler.currentTrack?.title).toBe('Track 1');
		expect(deckSampler.progress).toBeCloseTo(20 / current.duration);
	});
	it('does not let a stale fade timer pause a reused element after restart', async () => {
		vi.useFakeTimers();
		apiGet.mockResolvedValue(albumPreview(2));

		deckSampler.start('rg-old', 'Artist', 'Old');
		await waitForStatus('playing');
		const first = FakeAudio.started.at(-1)!;
		first.currentTime = first.duration - 0.25;
		await vi.advanceTimersByTimeAsync(100);
		await vi.waitFor(() => expect(FakeAudio.started).toHaveLength(2));

		deckSampler.start('rg-new', 'Artist', 'New');
		await waitForStatus('playing');
		await vi.waitFor(() => expect(FakeAudio.started).toHaveLength(3));
		const replacement = FakeAudio.started.at(-1)!;
		replacement.pause.mockClear();

		await vi.advanceTimersByTimeAsync(600);

		expect(deckSampler.status).toBe('playing');
		expect(replacement.pause).not.toHaveBeenCalled();
	});

	it('next() skips to the following entry immediately', async () => {
		apiGet.mockResolvedValue(albumPreview(2));
		deckSampler.startStation('Station', [
			{ key: 'rg-1', kind: 'album', artist: 'A1', title: 'Album 1', albumMbid: 'rg-1' },
			{ key: 'rg-2', kind: 'album', artist: 'A2', title: 'Album 2', albumMbid: 'rg-2' }
		]);
		await waitForStatus('playing');
		expect(deckSampler.hasNext).toBe(true);

		deckSampler.next();
		await vi.waitFor(() => expect(deckSampler.stationPosition.index).toBe(1));
		expect(deckSampler.hasNext).toBe(false);
	});
});

describe('deckSampler transport', () => {
	it('pause halts the ticker and audio; resume continues', async () => {
		apiGet.mockResolvedValue(albumPreview(2));
		deckSampler.start('rg-1', 'Artist', 'Album');
		await waitForStatus('playing');
		const el = FakeAudio.started.at(-1)!;

		deckSampler.pause();
		expect(deckSampler.status).toBe('paused');
		expect(el.pause).toHaveBeenCalled();

		el.play.mockClear();
		deckSampler.resume();
		expect(deckSampler.status).toBe('playing');
		expect(el.play).toHaveBeenCalled();
	});
	it('pauses with feedback when autoplay blocks a clip and resumes from a gesture', async () => {
		vi.useFakeTimers();
		apiGet.mockResolvedValue(albumPreview(2));
		FakeAudio.realPlayFailures.push({ name: 'NotAllowedError' });

		deckSampler.start('rg-blocked', 'Artist', 'Blocked');
		await waitForStatus('paused');

		expect(playbackToast.show).toHaveBeenCalledWith(
			expect.stringContaining('Tap the preview play button'),
			'warning'
		);
		const el = FakeAudio.started.at(-1)!;
		el.play.mockClear();

		deckSampler.resume();
		await waitForStatus('playing');
		expect(el.play).toHaveBeenCalledTimes(1);
	});

	it('setVolume applies live to the active element and persists', async () => {
		apiGet.mockResolvedValue(albumPreview(1));
		deckSampler.start('rg-1', 'Artist', 'Album');
		await waitForStatus('playing');
		const el = FakeAudio.started.at(-1)!;

		deckSampler.setVolume(0.4);
		expect(deckSampler.volume).toBe(0.4);
		expect(el.volume).toBe(0.4);
	});

	it('stop clears the station and releases focus', async () => {
		apiGet.mockResolvedValue(albumPreview(2));
		deckSampler.start('rg-1', 'Artist', 'Album');
		await waitForStatus('playing');

		deckSampler.stop();
		expect(deckSampler.status).toBe('idle');
		expect(deckSampler.currentEntry).toBeNull();
		expect(deckSampler.activeKey).toBe('');
		expect(focus.release).toHaveBeenCalled();
	});
});
