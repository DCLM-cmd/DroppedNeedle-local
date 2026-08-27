import { page } from '@vitest/browser/context';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-svelte';

import type { DownloadPolicySettings } from '$lib/types';

const basePolicy: DownloadPolicySettings = {
	quality_min: 'mp3_320',
	quality_max: 'lossless',
	flac_mp3_only: true,
	verify_downloads: true,
	preflight_score_auto_accept: 0.7,
	preflight_score_manual_min: 0.5,
	download_stall_timeout_minutes: 30,
	download_queued_timeout_minutes: 120,
	preferred_quality_wait_minutes: 15,
	max_failover_attempts: 3,
	max_concurrent_downloads: 3,
	auto_retry_enabled: true,
	auto_retry_max_attempts: 6,
	auto_retry_base_interval_minutes: 15,
	usenet_min_release_age_minutes: 30,
	quality_cutoff: 'lossless',
	upgrade_allowed: false,
	max_library_size_gb: 0,
	default_request_quota_count: 0,
	default_request_quota_days: 7,
	default_storage_quota_gb: 0,
	background_upgrade_scan_enabled: false,
	background_upgrade_scan_interval_hours: 12,
	background_upgrade_max_per_run: 3
};

// Backend GET now also carries the acquisition-quality field set (frozen API
// contract); older cached payloads omit them and the card derives fallbacks.
const qualityFields = {
	quality_preference_order: ['lossless', 'mp3_320', 'mp3_256', 'mp3_192'],
	preferred_lossy_bitrate_kbps: 320,
	lossy_min_bitrate_kbps: null,
	lossy_max_bitrate_kbps: null,
	lossless_preference: 'cd',
	lossless_max_bit_depth: 16,
	lossless_max_sample_rate_hz: 48000,
	unknown_quality_behavior: 'review',
	source_selection_mode: 'source_first'
};

const h = vi.hoisted(() => ({
	policy: undefined as unknown,
	mutateAsync: vi.fn()
}));

vi.mock('$lib/queries/downloads/DownloadClientsQueries.svelte', () => ({
	getDownloadPolicyQuery: () => ({
		get data() {
			return h.policy;
		}
	}),
	saveDownloadPolicy: () => ({
		mutateAsync: h.mutateAsync,
		isPending: false
	})
}));

import SettingsDownloadPolicy from './SettingsDownloadPolicy.svelte';

function cutoffSelect(container: HTMLElement): HTMLSelectElement {
	const el = [...container.querySelectorAll('select')].find((s) =>
		s.getAttribute('aria-label')?.includes('Upgrade')
	);
	if (!el) throw new Error('cutoff select not rendered');
	return el as HTMLSelectElement;
}

describe('SettingsDownloadPolicy upgrade controls', () => {
	beforeEach(() => {
		h.policy = { ...basePolicy, ...qualityFields };
		h.mutateAsync = vi.fn().mockResolvedValue(undefined);
	});

	it('seeds the cutoff and upgrades toggle from the saved policy', async () => {
		h.policy = {
			...basePolicy,
			...qualityFields,
			quality_cutoff: 'mp3_320',
			upgrade_allowed: true
		};
		const { container } = render(SettingsDownloadPolicy);

		await expect
			.element(page.getByRole('checkbox', { name: 'Allow automatic upgrades' }))
			.toBeChecked();
		expect(cutoffSelect(container).value).toBe('mp3_320');
	});

	it('disables cutoff options outside the accepted quality band', async () => {
		h.policy = {
			...basePolicy,
			...qualityFields,
			quality_min: 'mp3_256',
			quality_max: 'mp3_320'
		};
		const { container } = render(SettingsDownloadPolicy);
		await expect
			.element(page.getByRole('checkbox', { name: 'Allow automatic upgrades' }))
			.toBeVisible();

		const disabledByKey = Object.fromEntries(
			Array.from(cutoffSelect(container).options).map((o) => [o.value, o.disabled])
		);
		expect(disabledByKey).toEqual({
			low: true,
			mp3_192: true,
			mp3_256: false,
			mp3_320: false,
			lossless: true
		});
	});

	it('clamps a cutoff that falls outside the band to the nearest edge', async () => {
		h.policy = {
			...basePolicy,
			...qualityFields,
			quality_min: 'mp3_192',
			quality_max: 'mp3_256',
			quality_cutoff: 'lossless'
		};
		const { container } = render(SettingsDownloadPolicy);
		await expect
			.element(page.getByRole('checkbox', { name: 'Allow automatic upgrades' }))
			.toBeVisible();

		expect(cutoffSelect(container).value).toBe('mp3_256');
	});

	it('saves the cutoff and toggle through the policy mutation', async () => {
		render(SettingsDownloadPolicy);

		await page.getByRole('checkbox', { name: 'Allow automatic upgrades' }).click();
		await page.getByRole('button', { name: 'Save' }).click();

		expect(h.mutateAsync).toHaveBeenCalledTimes(1);
		const saved = h.mutateAsync.mock.calls[0][0] as Record<string, unknown>;
		expect(saved.upgrade_allowed).toBe(true);
		expect(saved.quality_cutoff).toBe('lossless');
	});

	it('loads and saves the preferred-quality queue window', async () => {
		render(SettingsDownloadPolicy);

		await page.getByText('Download behavior (advanced)').click();
		const waitInput = page.getByRole('spinbutton', {
			name: 'Preferred-quality queue wait (min)'
		});
		await expect.element(waitInput).toHaveValue(15);
		await waitInput.fill('9');
		await page.getByRole('button', { name: 'Save' }).click();

		const saved = h.mutateAsync.mock.calls[0][0] as Record<string, unknown>;
		expect(saved.preferred_quality_wait_minutes).toBe(9);
	});
});

describe('SettingsDownloadPolicy acquisition fields', () => {
	beforeEach(() => {
		h.policy = { ...basePolicy, ...qualityFields };
		h.mutateAsync = vi.fn().mockResolvedValue(undefined);
	});

	it('sends all NINE acquisition-quality fields through the same mutation', async () => {
		render(SettingsDownloadPolicy);
		await page.getByRole('button', { name: 'Save' }).click();

		expect(h.mutateAsync).toHaveBeenCalledTimes(1);
		const saved = h.mutateAsync.mock.calls[0][0] as Record<string, unknown>;
		for (const key of [
			'quality_preference_order',
			'preferred_lossy_bitrate_kbps',
			'lossy_min_bitrate_kbps',
			'lossy_max_bitrate_kbps',
			'lossless_preference',
			'lossless_max_bit_depth',
			'lossless_max_sample_rate_hz',
			'unknown_quality_behavior',
			'source_selection_mode'
		]) {
			expect(saved[key], key).toBeDefined();
		}
		expect(saved.quality_preference_order).toEqual(['lossless', 'mp3_320', 'mp3_256', 'mp3_192']);
		expect(saved.preferred_lossy_bitrate_kbps).toBe(320);
		expect(saved.source_selection_mode).toBe('source_first');
	});

	it('derives legacy fallback fields when the saved policy predates them', async () => {
		h.policy = { ...basePolicy }; // no acquisition fields at all
		render(SettingsDownloadPolicy);
		await page.getByRole('button', { name: 'Save' }).click();

		const saved = h.mutateAsync.mock.calls[0][0] as Record<string, unknown>;
		// range derivation mirrors backend derive_default_order for mp3_320..lossless
		expect(saved.quality_preference_order).toEqual(['lossless', 'mp3_320']);
		expect(saved.lossless_preference).toBe('highest');
		expect(saved.unknown_quality_behavior).toBe('allow_as_fallback');
		expect(saved.source_selection_mode).toBe('source_first');
	});

	it('announces reorders through aria-live and flips endpoint badges', async () => {
		const { container } = render(SettingsDownloadPolicy);
		await expect.element(page.getByRole('status')).toHaveTextContent(/Try Lossless/);

		// keyboard reorder: row 4 moves up into position 3
		await page.getByRole('button', { name: 'Move Lossy 192-255 to position 3' }).click();

		await expect
			.element(page.getByRole('status'))
			.toHaveTextContent(
				'Try Lossless, then Lossy 320 kbps, then Lossy 192-255, then Lossy 256-319.'
			);
		const rows = container.querySelectorAll('[data-motion="acq-order"] [data-tier]');
		expect(rows[0]?.getAttribute('data-tier')).toBe('lossless');
	});

	it('marks the editor region for reduced-motion coverage', async () => {
		const { container } = render(SettingsDownloadPolicy);
		const motionField = container.querySelector('[data-motion="acq-order"]');
		expect(motionField).not.toBeNull();
	});

	it('opens a confirm modal over a modified order; Cancel keeps it and restores focus', async () => {
		const { container } = render(SettingsDownloadPolicy);
		// dirty a preset-covered field so applying requires confirmation
		await page.getByRole('button', { name: 'Move Lossy 192-255 to position 3' }).click();
		const trigger = page.getByRole('button', { name: 'Apply Best available preset' });
		await trigger.click();

		const dialog = container.querySelector('dialog.modal') as HTMLDialogElement | null;
		expect(dialog).toBeTruthy();
		expect(dialog?.open).toBe(true);

		await page.getByRole('button', { name: 'Cancel' }).click();
		// old order preserved: still the (modified) Balanced ladder with four
		// tiers - Cancel must NOT have applied Best available
		const tiers = [
			...(container.querySelectorAll(
				'[data-motion="acq-order"] ol [data-tier]'
			) as NodeListOf<HTMLElement>)
		].map((el) => el.getAttribute('data-tier'));
		expect(tiers).toEqual(['lossless', 'mp3_320', 'mp3_192', 'mp3_256']);
		// focus returned to the preset trigger
		expect(container.ownerDocument.activeElement?.getAttribute('data-preset')).toBe(
			'best_available'
		);
	});
});
