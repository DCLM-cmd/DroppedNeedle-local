import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AlbumBasicInfo, LocalAlbumMatch, LocalTrackInfo, Track } from '$lib/types';
import type { QueueItem } from '$lib/player/types';

const mocks = vi.hoisted(() => ({
	launchLocalPlayback: vi.fn(),
	addMultipleToQueue: vi.fn(),
	playMultipleNext: vi.fn()
}));

vi.mock('$lib/player/launchLocalPlayback', () => ({
	launchLocalPlayback: mocks.launchLocalPlayback
}));
vi.mock('$lib/stores/player.svelte', () => ({
	playerStore: {
		addToQueue: vi.fn(),
		playNext: vi.fn(),
		addMultipleToQueue: mocks.addMultipleToQueue,
		playMultipleNext: mocks.playMultipleNext
	}
}));
vi.mock('$lib/utils/downloadHelper', () => ({ downloadFile: vi.fn() }));

import { buildSourceCallbacks, playSourceTrack } from './albumPlaybackHandlers';

const album: AlbumBasicInfo = {
	title: 'Avalon',
	musicbrainz_id: 'release-group-1',
	artist_name: 'Anthony Green',
	artist_id: 'artist-1',
	in_library: true,
	cover_url: null
};

const localTracks: LocalTrackInfo[] = [
	{
		track_file_id: 'file-1',
		title: 'She Loves Me So',
		track_number: 1,
		disc_number: 1,
		duration_seconds: 233,
		size_bytes: 1_000,
		format: 'FLAC'
	},
	{
		track_file_id: 'file-14',
		title: 'The Fisherman Will Be Bewildered (H&D EP Version)',
		track_number: 14,
		disc_number: 1,
		duration_seconds: 212,
		size_bytes: 1_000,
		format: 'FLAC'
	}
];

const localMatch: LocalAlbumMatch = {
	found: true,
	musicbrainz_id: album.musicbrainz_id,
	tracks: localTracks,
	total_size_bytes: 2_000,
	primary_format: 'FLAC'
};

const canonicalTracks: Track[] = [
	{ position: 1, disc_number: 1, title: 'She Loves Me So' },
	{ position: 14, disc_number: 1, title: 'The Fisherman Will Be Bewildered' }
];

const tracksGetters = {
	jellyfin: () => [],
	local: () => localTracks,
	navidrome: () => [],
	plex: () => []
};

describe('album playback canonical titles', () => {
	beforeEach(() => vi.clearAllMocks());

	it('keeps the release-track title when a local album row starts playback', () => {
		playSourceTrack(
			'local',
			14,
			1,
			'The Fisherman Will Be Bewildered',
			album,
			null,
			localMatch,
			null,
			null,
			canonicalTracks
		);

		expect(mocks.launchLocalPlayback).toHaveBeenCalledTimes(1);
		const [tracks, startIndex] = mocks.launchLocalPlayback.mock.calls[0] as [
			LocalTrackInfo[],
			number
		];
		expect(startIndex).toBe(1);
		expect(tracks[1]).toMatchObject({
			track_file_id: 'file-14',
			title: 'The Fisherman Will Be Bewildered'
		});
		expect(localTracks[1].title).toBe('The Fisherman Will Be Bewildered (H&D EP Version)');
	});

	it('uses canonical titles for every album-level local queue action', () => {
		const launcher = vi.fn();
		const openPlaylist = vi.fn();
		const callbacks = buildSourceCallbacks(
			() => localMatch,
			launcher,
			'local',
			() => album,
			tracksGetters,
			() => canonicalTracks,
			() => ({ open: openPlaylist })
		);

		callbacks.onPlayAll();
		callbacks.onShuffle();
		callbacks.onAddAllToQueue();
		callbacks.onPlayAllNext();
		callbacks.onAddAllToPlaylist();

		expect(launcher).toHaveBeenCalledTimes(2);
		for (const call of launcher.mock.calls) {
			const tracks = call[0] as LocalTrackInfo[];
			expect(tracks[1].title).toBe('The Fisherman Will Be Bewildered');
		}
		const queued = mocks.addMultipleToQueue.mock.calls[0][0] as QueueItem[];
		const next = mocks.playMultipleNext.mock.calls[0][0] as QueueItem[];
		const playlist = openPlaylist.mock.calls[0][0] as QueueItem[];
		expect(queued[1].trackName).toBe('The Fisherman Will Be Bewildered');
		expect(next[1].trackName).toBe('The Fisherman Will Be Bewildered');
		expect(playlist[1].trackName).toBe('The Fisherman Will Be Bewildered');
	});
});
