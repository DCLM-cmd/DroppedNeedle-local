import { describe, expect, it } from 'vitest';
import { updatePaginatedSearchResults } from './paginatedSearchResults';

const item = (musicbrainz_id: string) => ({ musicbrainz_id });

describe('updatePaginatedSearchResults', () => {
	it('replaces stale results and uses the live page boundary', () => {
		const current = [item('shared'), item('stale-only')];
		const incoming = [
			item('shared'),
			...Array.from({ length: 23 }, (_, index) => item(`live-${index}`))
		];

		const update = updatePaginatedSearchResults(current, incoming, 0, true);

		expect(update.items).toEqual(incoming);
		expect(update.nextOffset).toBe(24);
	});

	it('deduplicates a non-replacing first-page refresh', () => {
		const update = updatePaginatedSearchResults(
			[item('existing')],
			[item('existing'), item('new')],
			0,
			false
		);

		expect(update.items.map(({ musicbrainz_id }) => musicbrainz_id)).toEqual(['existing', 'new']);
		expect(update.nextOffset).toBe(2);
	});
});
