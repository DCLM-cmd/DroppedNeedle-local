import { page } from '@vitest/browser/context';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-svelte';

const h = vi.hoisted(() => ({
	data: {
		api_url: 'https://musicbrainz.org/ws/2',
		rate_limit: 1.0,
		concurrent_searches: 6,
		clamped_to_official_limits: false
	},
	testResult: null as { valid: boolean; message: string } | null,
	save: vi.fn()
}));

vi.mock('$lib/utils/settingsForm.svelte', () => ({
	createSettingsForm: () => ({
		get data() {
			return h.data;
		},
		loading: false,
		saving: false,
		testing: false,
		get testResult() {
			return h.testResult;
		},
		message: '',
		messageType: 'success',
		load: vi.fn(),
		save: h.save,
		test: vi.fn(),
		cleanup: vi.fn()
	})
}));

import SettingsMusicBrainz from './SettingsMusicBrainz.svelte';

function setData(overrides: Partial<typeof h.data>) {
	h.data = { ...h.data, ...overrides };
}

describe('SettingsMusicBrainz three-way source picker', () => {
	beforeEach(() => {
		h.testResult = null;
		setData({
			api_url: 'https://musicbrainz.org/ws/2',
			rate_limit: 1.0,
			concurrent_searches: 6,
			clamped_to_official_limits: false
		});
	});

	it('renders the three selectable cards with Official marked recommended', async () => {
		render(SettingsMusicBrainz);

		await expect.element(page.getByRole('radio', { name: 'Official' })).toBeVisible();
		await expect.element(page.getByRole('radio', { name: 'Self-hosted mirror' })).toBeVisible();
		await expect
			.element(page.getByRole('radio', { name: 'Community / external server' }))
			.toBeVisible();
		await expect
			.element(
				page.getByRole('radio', { name: 'Official' }).getByText('Recommended', { exact: true })
			)
			.toBeVisible();
	});

	it('highlights Official as selected for an official URL and shows the cap copy', async () => {
		render(SettingsMusicBrainz);

		await expect
			.element(page.getByRole('radio', { name: 'Official' }))
			.toHaveAttribute('aria-checked', 'true');
		await expect
			.element(page.getByRole('radio', { name: 'Self-hosted mirror' }))
			.toHaveAttribute('aria-checked', 'false');
		await expect.element(page.getByText(/clamped here, not refused/)).toBeVisible();
	});

	it('requires the community acknowledgment before saving', async () => {
		h.testResult = { valid: true, message: 'Connected to MusicBrainz' };
		render(SettingsMusicBrainz);

		await page.getByRole('radio', { name: 'Community / external server' }).click();

		const save = page.getByRole('button', { name: 'Save Settings' });
		await expect.element(save).toBeDisabled();
		await expect.element(page.getByText(/routing identity data through a server/)).toBeVisible();

		// the protocol caveat lives in the collapsed More info disclosure - open it
		await page
			.getByRole('radio', { name: 'Community / external server' })
			.getByText('More info')
			.click();
		await expect.element(page.getByText(/BrainzMash shared pool/)).toBeVisible();

		await page.getByRole('checkbox').click();

		await expect.element(save).toBeEnabled();
	});

	it('shows the clamp warning when the backend applied official limits', async () => {
		setData({ clamped_to_official_limits: true });
		render(SettingsMusicBrainz);

		await expect.element(page.getByText(/Values were clamped to official limits/)).toBeVisible();
	});

	it('loads a non-official URL on the mirror card with banner and guide link', async () => {
		setData({ api_url: 'http://mirror-host:5000/ws/2', rate_limit: 25, concurrent_searches: 20 });
		render(SettingsMusicBrainz);

		await expect
			.element(page.getByRole('radio', { name: 'Self-hosted mirror' }))
			.toHaveAttribute('aria-checked', 'true');
		await expect
			.element(page.getByRole('radio', { name: 'Official' }))
			.toHaveAttribute('aria-checked', 'false');
		await expect.element(page.getByText(/reindex schedule/)).toBeVisible();
		await expect
			.element(page.getByRole('link', { name: 'Mirror setup guide' }))
			.toHaveAttribute('href', '/docs/musicbrainz-mirror-selfhosting.md');
		await expect.element(page.getByText('Unlimited', { exact: true })).not.toBeInTheDocument();
	});
});
