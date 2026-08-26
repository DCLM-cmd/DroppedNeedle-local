import { beforeEach, describe, expect, it, vi } from 'vitest';

const { post, addRequested } = vi.hoisted(() => ({
	post: vi.fn(),
	addRequested: vi.fn()
}));

vi.mock('$lib/api/client', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/client')>()),
	api: { global: { post } }
}));
vi.mock('$lib/stores/library', () => ({
	libraryStore: { addRequested }
}));
vi.mock('$lib/stores/errorModal', () => ({
	errorModal: { show: vi.fn() }
}));

import { authStore, type AuthUser } from '$lib/stores/authStore.svelte';
import { requestAlbum, requestBatch } from './albumRequest';

function user(id: string): AuthUser {
	return {
		id,
		display_name: id,
		role: 'user',
		email: null,
		avatar_url: null,
		username: id,
		username_display: id,
		providers: ['local']
	};
}

beforeEach(() => {
	vi.clearAllMocks();
	authStore.setUser(user('user-a'));
});

describe('album request session isolation', () => {
	it('drops batch cache writes when the account changes before the response', async () => {
		let resolveResponse!: (value: {
			success: boolean;
			message: string;
			requested: number;
			skipped: number;
			overflow: number;
		}) => void;
		post.mockReturnValueOnce(
			new Promise((resolve) => {
				resolveResponse = resolve;
			})
		);
		const request = requestBatch([{ musicbrainz_id: 'release-a' }]);

		authStore.setUser(user('user-b'));
		resolveResponse({
			success: true,
			message: 'ok',
			requested: 1,
			skipped: 0,
			overflow: 0
		});

		expect((await request).success).toBe(false);
		expect(addRequested).not.toHaveBeenCalled();
	});

	it('drops single-album cache writes when the account changes before the response', async () => {
		let resolveResponse!: () => void;
		post.mockReturnValueOnce(
			new Promise<void>((resolve) => {
				resolveResponse = resolve;
			})
		);
		const request = requestAlbum('release-a');

		authStore.setUser(user('user-b'));
		resolveResponse();

		expect((await request).success).toBe(false);
		expect(addRequested).not.toHaveBeenCalled();
	});
});
