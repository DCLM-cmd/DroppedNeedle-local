import { describe, expect, it } from 'vitest';

import { load } from './+layout';

describe('Library Management authorization', () => {
	it('admits administrators', async () => {
		await expect(
			load({ parent: async () => ({ user: { role: 'admin' } }) } as never)
		).resolves.toEqual({});
	});

	it('redirects non-administrators before the artist identity desk renders', async () => {
		await expect(
			load({ parent: async () => ({ user: { role: 'user' } }) } as never)
		).rejects.toMatchObject({ status: 302, location: '/library' });
	});
});
