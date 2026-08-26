import { createQuery } from '@tanstack/svelte-query';

import { api } from '$lib/api/client';
import { API } from '$lib/constants';
import { LibraryQueryKeyFactory } from './LibraryQueryKeyFactory';

type Getter<T> = () => T;

export interface ReleaseEditionResult {
	release_mbid: string;
	release_group_mbid: string;
	artist_name: string;
	title: string;
	date: string | null;
	country: string | null;
	status: string | null;
	packaging: string | null;
	media_formats: string[];
	disc_count: number;
	track_count: number;
	label: string | null;
	catalogue_number: string | null;
	barcode: string | null;
	disambiguation: string | null;
	musicbrainz_url: string;
	score: number;
	belongs_to_current_release_group: boolean;
	is_current_release: boolean;
}

export interface ReleaseEditionSearchResponse {
	title_query: string;
	artist_query: string;
	items: ReleaseEditionResult[];
	total: number;
	offset: number;
	limit: number;
}

export function getReleaseEditionSearchQuery(
	getUserId: Getter<string | undefined>,
	getAlbumId: Getter<string>,
	getTitle: Getter<string>,
	getArtist: Getter<string>,
	getOffset: Getter<number>,
	getEnabled: Getter<boolean> = () => true
) {
	return createQuery(() => {
		const userId = getUserId();
		const albumId = getAlbumId();
		const title = getTitle();
		const artist = getArtist();
		const offset = getOffset();
		return {
			enabled: getEnabled() && Boolean(albumId && title.trim()),
			queryKey: LibraryQueryKeyFactory.reidentificationReleases(
				userId,
				albumId,
				title,
				artist,
				offset
			),
			queryFn: ({ signal }) =>
				api.global.get<ReleaseEditionSearchResponse>(
					API.library.reidentificationReleases(albumId, title, artist, 12, offset),
					{ signal }
				)
		};
	});
}
