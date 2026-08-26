import { describe, expect, it } from 'vitest';

import { artistPurchaseOptionsKey, purchaseOptionsKey } from './GetItQueries.svelte';

describe('GetIt query keys', () => {
	it('versions album and artist purchase options independently of old persisted data', () => {
		expect(purchaseOptionsKey('release-group-1')).toEqual([
			'albums',
			'purchase-options',
			'v2',
			'release-group-1'
		]);
		expect(artistPurchaseOptionsKey('artist-1')).toEqual([
			'artists',
			'purchase-options',
			'v2',
			'artist-1'
		]);
	});
});
