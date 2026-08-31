import type { AlbumEditionItem } from '$lib/types';

/** Label for one entry in the Edition picker dropdown.
 *
 * A release group can hold releases with genuinely different tracklists under
 * one title - e.g. a "Side A"/"Side B" pair released separately as one EP. In
 * that case the release's own title (which carries the "(side b)" distinction)
 * is the only thing that tells the two apart: disambiguation/date/country/track
 * count can be identical across them. So the title is shown whenever it differs
 * from the album's own title, ahead of the other descriptive bits; when it
 * matches (the common case - most editions share the release group's title) it
 * is omitted to avoid repeating it verbatim in every dropdown row. */
export function editionLabel(edition: AlbumEditionItem, albumTitle: string): string {
	const titleDiffers =
		!!edition.title && edition.title.trim().toLowerCase() !== albumTitle.trim().toLowerCase();
	const bits = [
		titleDiffers ? edition.title : null,
		edition.disambiguation,
		edition.date?.slice(0, 4),
		edition.country,
		`${edition.track_count} tracks`
	].filter(Boolean);
	return bits.join(' · ') || edition.release_mbid.slice(0, 8);
}
