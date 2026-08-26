import { createQuery } from '@tanstack/svelte-query';
import type { Getter } from 'runed';
import { SvelteMap, SvelteSet } from 'svelte/reactivity';

import { api } from '$lib/api/client';
import { API, CACHE_TTL } from '$lib/constants';
import { authStore } from '$lib/stores/authStore.svelte';
import type {
	Album,
	Artist,
	LibraryAlbumSummary,
	LibraryArtistSummary,
	NativeAlbumsResponse,
	NativeArtistsResponse,
	SearchBucketResponse,
	SearchSuggestResponse
} from '$lib/types';

import { SearchQueryKeyFactory } from './SearchQueryKeyFactory';

const enabled = (query: string) => Boolean(authStore.user?.id && query.trim().length >= 2);
const successfulSearchStaleTime = (query: { state: { data?: { status?: string } } }) =>
	query.state.data?.status === 'ok' ? CACHE_TTL.SEARCH : 0;
const successfulSuggestStaleTime = (query: { state: { data?: { remote_status?: string } } }) =>
	query.state.data?.remote_status === 'ok' ? CACHE_TTL.SEARCH : 0;

export const getLocalArtistSearchQuery = (getQuery: Getter<string>, limit = 24) =>
	createQuery(() => {
		const query = getQuery().trim();
		return {
			enabled: enabled(query),
			staleTime: CACHE_TTL.SEARCH,
			queryKey: SearchQueryKeyFactory.localArtists(authStore.user?.id, query, limit),
			queryFn: ({ signal }) =>
				api.global.get<NativeArtistsResponse>(API.library.artists(limit, 0, 'name', 'asc', query), {
					signal
				})
		};
	});

export const getLocalAlbumSearchQuery = (getQuery: Getter<string>, limit = 24) =>
	createQuery(() => {
		const query = getQuery().trim();
		return {
			enabled: enabled(query),
			staleTime: CACHE_TTL.SEARCH,
			queryKey: SearchQueryKeyFactory.localAlbums(authStore.user?.id, query, limit),
			queryFn: ({ signal }) =>
				api.global.get<NativeAlbumsResponse>(
					API.library.albums(1, 'recent', query, undefined, limit),
					{ signal }
				)
		};
	});

export const getRemoteArtistSearchQuery = (getQuery: Getter<string>, limit = 6) =>
	createQuery(() => {
		const query = getQuery().trim();
		return {
			enabled: enabled(query),
			staleTime: successfulSearchStaleTime,
			queryKey: SearchQueryKeyFactory.artists(authStore.user?.id, query, limit),
			queryFn: ({ signal }) =>
				api.global.get<SearchBucketResponse<Artist>>(API.search.artists(query, limit), {
					signal
				})
		};
	});

export const getRemoteAlbumSearchQuery = (getQuery: Getter<string>, limit = 24) =>
	createQuery(() => {
		const query = getQuery().trim();
		return {
			enabled: enabled(query),
			staleTime: successfulSearchStaleTime,
			queryKey: SearchQueryKeyFactory.albums(authStore.user?.id, query, limit),
			queryFn: ({ signal }) =>
				api.global.get<SearchBucketResponse<Album>>(API.search.albums(query, limit), { signal })
		};
	});

export const getSearchSuggestionsQuery = (
	getQuery: Getter<string>,
	getEnabled: Getter<boolean>,
	limit = 5
) =>
	createQuery(() => {
		const query = getQuery().trim();
		return {
			enabled: getEnabled() && enabled(query),
			staleTime: successfulSuggestStaleTime,
			queryKey: SearchQueryKeyFactory.suggestions(authStore.user?.id, query, limit),
			queryFn: ({ signal }) =>
				api.global.get<SearchSuggestResponse>(API.search.suggest(query, limit), { signal })
		};
	});

export function mergeSearchArtists(local: LibraryArtistSummary[], remote: Artist[]): Artist[] {
	const merged = new SvelteMap(remote.map((artist) => [artist.musicbrainz_id, artist]));
	for (const artist of local) {
		const id = artist.musicbrainz_artist_id ?? artist.id;
		merged.set(id, {
			...merged.get(id),
			title: artist.name,
			musicbrainz_id: id,
			in_library: true,
			local_id: artist.id,
			release_group_count: artist.album_count
		});
	}
	const localIds = new SvelteSet(local.map((artist) => artist.musicbrainz_artist_id ?? artist.id));
	return [...merged.values()].sort(
		(left, right) =>
			Number(localIds.has(right.musicbrainz_id)) - Number(localIds.has(left.musicbrainz_id))
	);
}

export function mergeSearchAlbums(local: LibraryAlbumSummary[], remote: Album[]): Album[] {
	const merged = new SvelteMap(remote.map((album) => [album.musicbrainz_id, album]));
	for (const album of local) {
		const id = album.musicbrainz_release_group_id ?? album.id;
		merged.set(id, {
			...merged.get(id),
			title: album.title,
			artist: album.artist_name,
			year: album.year,
			musicbrainz_id: id,
			in_library: true,
			requested: false,
			local_id: album.id,
			cover_available: album.cover_available,
			track_count: album.track_count
		});
	}
	const localIds = new SvelteSet(
		local.map((album) => album.musicbrainz_release_group_id ?? album.id)
	);
	return [...merged.values()].sort(
		(left, right) =>
			Number(localIds.has(right.musicbrainz_id)) - Number(localIds.has(left.musicbrainz_id))
	);
}
