type SearchResultIdentity = {
	musicbrainz_id: string;
};

export type PaginatedSearchUpdate<T> = {
	items: T[];
	nextOffset: number;
};

export function updatePaginatedSearchResults<T extends SearchResultIdentity>(
	current: T[],
	incoming: T[],
	requestOffset: number,
	replace: boolean
): PaginatedSearchUpdate<T> {
	if (replace) {
		return { items: [...incoming], nextOffset: incoming.length };
	}

	if (requestOffset === 0 && current.length > 0) {
		const existingIds = new Set(current.map((item) => item.musicbrainz_id));
		const uniqueIncoming = incoming.filter((item) => !existingIds.has(item.musicbrainz_id));
		const items = [...current, ...uniqueIncoming];
		return { items, nextOffset: items.length };
	}

	return {
		items: [...current, ...incoming],
		nextOffset: requestOffset + incoming.length
	};
}
