import { page } from '@vitest/browser/context';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-svelte';

import SettingsCache from './SettingsCache.svelte';

const STATS = {
	memory_entries: 4,
	memory_size_bytes: 4096,
	memory_size_mb: 0.004,
	disk_metadata_count: 12,
	disk_metadata_albums: 3,
	disk_metadata_artists: 2,
	disk_cover_count: 1550,
	disk_cover_size_bytes: 162529280,
	disk_cover_size_mb: 155,
	library_db_artist_count: 5,
	library_db_album_count: 8,
	library_db_size_bytes: 8192,
	library_db_size_mb: 0.008,
	total_size_bytes: 162541568,
	total_size_mb: 155.01,
	library_db_last_sync: null,
	disk_audiodb_artist_count: 0,
	disk_audiodb_album_count: 0
};

const originalFetch = globalThis.fetch;

function statusResponse(status: number) {
	return {
		ok: status >= 200 && status < 300,
		status,
		json: () => Promise.resolve(STATS)
	};
}

function stubStatsFetch(status: number) {
	globalThis.fetch = vi
		.fn()
		.mockResolvedValue(statusResponse(status)) as unknown as typeof globalThis.fetch;
}

describe('SettingsCache', () => {
	afterEach(() => {
		globalThis.fetch = originalFetch;
		vi.restoreAllMocks();
	});

	it('renders an admin-required warning instead of stats when the endpoint answers 403', async () => {
		stubStatsFetch(403);

		render(SettingsCache);

		await expect.element(page.getByText(/admin access is required/i)).toBeVisible();
		const alerts = await page.getByRole('alert').all();
		expect(alerts.length).toBe(1);
		expect(page.getByText('Memory cache').query()).toBeNull();
	});

	it('renders the same admin-required warning on 401', async () => {
		stubStatsFetch(401);

		render(SettingsCache);

		await expect.element(page.getByText(/admin access is required/i)).toBeVisible();
		const alerts = await page.getByRole('alert').all();
		expect(alerts.length).toBe(1);
	});

	it('labels the destructive and metadata-scoped clears honestly using live counts', async () => {
		stubStatsFetch(200);

		render(SettingsCache);

		await expect
			.element(page.getByRole('button', { name: 'Full wipe - also deletes 1550 cover files' }))
			.toBeVisible();
		await expect
			.element(page.getByRole('button', { name: 'Metadata only - covers preserved' }))
			.toBeVisible();
	});

	it('states the full-wipe cover deletion in the confirmation prompt', async () => {
		stubStatsFetch(200);
		const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);

		render(SettingsCache);

		await page.getByRole('button', { name: /Full wipe/i }).click();

		expect(confirmSpy).toHaveBeenCalledTimes(1);
		const prompt = String(confirmSpy.mock.calls[0]?.[0] ?? '');
		expect(prompt).toMatch(/deletes all 1550 cover image files/i);
		expect(prompt).toMatch(/re-fetched from upstream/i);
	});
});
