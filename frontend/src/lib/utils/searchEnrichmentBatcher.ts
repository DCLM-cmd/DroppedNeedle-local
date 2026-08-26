import type {
	Album,
	AlbumEnrichmentRequest,
	Artist,
	ArtistEnrichmentRequest,
	EnrichmentResponse
} from '$lib/types';

const DEFAULT_DELAY_MS = 120;
const DEFAULT_MAX_ITEMS = 10;

type LoadEnrichment = (
	artists: ArtistEnrichmentRequest[],
	albums: AlbumEnrichmentRequest[],
	signal: AbortSignal
) => Promise<EnrichmentResponse | null>;

interface SearchEnrichmentBatcherOptions {
	load: LoadEnrichment;
	onresult: (result: EnrichmentResponse) => void;
	delayMs?: number;
	maxItems?: number;
}

export interface SearchEnrichmentBatcher {
	requestArtist: (artist: Artist) => void;
	requestAlbum: (album: Album) => void;
	reset: () => void;
	dispose: () => void;
}

export function createSearchEnrichmentBatcher({
	load,
	onresult,
	delayMs = DEFAULT_DELAY_MS,
	maxItems = DEFAULT_MAX_ITEMS
}: SearchEnrichmentBatcherOptions): SearchEnrichmentBatcher {
	const pendingArtists = new Map<string, ArtistEnrichmentRequest>();
	const pendingAlbums = new Map<string, AlbumEnrichmentRequest>();
	const attempted = new Set<string>();
	let timer: ReturnType<typeof setTimeout> | null = null;
	let activeController: AbortController | null = null;
	let runningGeneration: number | null = null;
	let generation = 0;
	let disposed = false;

	function schedule(): void {
		if (disposed || runningGeneration === generation || timer !== null) return;
		timer = setTimeout(() => void flush(), delayMs);
	}

	async function flush(): Promise<void> {
		timer = null;
		if (
			disposed ||
			runningGeneration === generation ||
			(pendingArtists.size === 0 && pendingAlbums.size === 0)
		)
			return;

		const artists = [...pendingArtists.values()];
		const albums = [...pendingAlbums.values()];
		pendingArtists.clear();
		pendingAlbums.clear();

		const requestGeneration = generation;
		const controller = new AbortController();
		activeController = controller;
		runningGeneration = requestGeneration;
		try {
			const result = await load(artists, albums, controller.signal);
			if (result && !disposed && !controller.signal.aborted && generation === requestGeneration) {
				onresult(result);
			}
		} finally {
			if (activeController === controller) activeController = null;
			if (runningGeneration === requestGeneration) runningGeneration = null;
			if (pendingArtists.size > 0 || pendingAlbums.size > 0) schedule();
		}
	}

	function reserve(key: string): boolean {
		if (disposed || attempted.has(key) || attempted.size >= maxItems) return false;
		attempted.add(key);
		return true;
	}

	return {
		requestArtist(artist) {
			if (artist.listen_count != null) return;
			const key = `artist:${artist.musicbrainz_id}`;
			if (!reserve(key)) return;
			pendingArtists.set(artist.musicbrainz_id, {
				musicbrainz_id: artist.musicbrainz_id,
				name: artist.title
			});
			schedule();
		},
		requestAlbum(album) {
			if (album.listen_count != null) return;
			const key = `album:${album.musicbrainz_id}`;
			if (!reserve(key)) return;
			pendingAlbums.set(album.musicbrainz_id, {
				musicbrainz_id: album.musicbrainz_id,
				artist_name: album.artist ?? '',
				album_name: album.title
			});
			schedule();
		},
		reset() {
			generation += 1;
			if (timer !== null) clearTimeout(timer);
			timer = null;
			activeController?.abort();
			pendingArtists.clear();
			pendingAlbums.clear();
			attempted.clear();
		},
		dispose() {
			disposed = true;
			generation += 1;
			if (timer !== null) clearTimeout(timer);
			timer = null;
			activeController?.abort();
			pendingArtists.clear();
			pendingAlbums.clear();
		}
	};
}
