import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Album, Artist, EnrichmentResponse } from '$lib/types';
import { createSearchEnrichmentBatcher } from './searchEnrichmentBatcher';

const artist = (id: string): Artist => ({
	musicbrainz_id: id,
	title: `Artist ${id}`,
	in_library: false
});

const album = (id: string): Album => ({
	musicbrainz_id: id,
	title: `Album ${id}`,
	artist: 'Artist',
	year: null,
	in_library: false
});

const emptyResult: EnrichmentResponse = { artists: [], albums: [], source: 'none' };

describe('createSearchEnrichmentBatcher', () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	it('does no work until a result card expresses interaction intent', async () => {
		expect.assertions(1);
		const load = vi.fn().mockResolvedValue(emptyResult);
		createSearchEnrichmentBatcher({ load, onresult: vi.fn() });

		await vi.advanceTimersByTimeAsync(1_000);

		expect(load).not.toHaveBeenCalled();
	});

	it('coalesces repeated artist and album intents into one bounded batch', async () => {
		expect.assertions(5);
		const load = vi.fn().mockResolvedValue(emptyResult);
		const batcher = createSearchEnrichmentBatcher({ load, onresult: vi.fn(), maxItems: 3 });

		batcher.requestArtist(artist('a1'));
		batcher.requestArtist(artist('a1'));
		batcher.requestAlbum(album('r1'));
		batcher.requestAlbum(album('r2'));
		batcher.requestAlbum(album('r3'));
		await vi.advanceTimersByTimeAsync(120);

		expect(load).toHaveBeenCalledTimes(1);
		const [artists, albums, signal] = load.mock.calls[0];
		expect(artists.map((item: { musicbrainz_id: string }) => item.musicbrainz_id)).toEqual(['a1']);
		expect(albums.map((item: { musicbrainz_id: string }) => item.musicbrainz_id)).toEqual([
			'r1',
			'r2'
		]);
		expect(signal).toBeInstanceOf(AbortSignal);
		expect(artists.length + albums.length).toBe(3);
	});

	it('aborts navigation work and ignores a late stale result', async () => {
		expect.assertions(5);
		const resolveLoads: Array<(value: EnrichmentResponse) => void> = [];
		const load = vi.fn().mockImplementation(
			() =>
				new Promise<EnrichmentResponse>((resolve) => {
					resolveLoads.push(resolve);
				})
		);
		const onresult = vi.fn();
		const batcher = createSearchEnrichmentBatcher({ load, onresult });

		batcher.requestArtist(artist('a1'));
		await vi.advanceTimersByTimeAsync(120);
		const signal = load.mock.calls[0][2] as AbortSignal;
		batcher.reset();
		batcher.requestArtist(artist('a2'));
		await vi.advanceTimersByTimeAsync(120);
		resolveLoads[1](emptyResult);
		resolveLoads[0](emptyResult);
		await Promise.resolve();

		expect(signal.aborted).toBe(true);
		expect(load).toHaveBeenCalledTimes(2);
		expect(load.mock.calls[1][0][0].musicbrainz_id).toBe('a2');
		expect(onresult).toHaveBeenCalledTimes(1);
		expect(onresult).toHaveBeenCalledWith(emptyResult);
	});
});
