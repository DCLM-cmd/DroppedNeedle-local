import { page } from '@vitest/browser/context';
import { describe, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-svelte';

import type { ScoredCandidate } from '$lib/types';

import SearchResultCard from './SearchResultCard.svelte';

type RenderOpts = Parameters<typeof render<typeof SearchResultCard>>[1];

function renderCard(props: Record<string, unknown>) {
	return render(SearchResultCard, { props } as unknown as RenderOpts);
}

type CandidateExtras = {
	preference_step?: number | null;
	preference_steps_total?: number | null;
	certainty?: string | null;
};

function makeCandidate(
	overrides: Partial<ScoredCandidate> & CandidateExtras = {}
): ScoredCandidate & CandidateExtras {
	return {
		username: 'alice',
		parent_directory: 'Radiohead - OK Computer (1997)',
		files: [
			{
				username: 'alice',
				filename: 'Radiohead/OK Computer/01 Airbag.flac',
				parent_directory: 'Radiohead - OK Computer (1997)',
				size: 30_000_000,
				extension: 'flac',
				bitrate: null,
				bit_depth: 16,
				sample_rate: 44100,
				duration: 284,
				has_free_slot: true,
				upload_speed: 2_000_000
			}
		],
		coherence: 0.95,
		file_confidence: 0.9,
		final_score: 0.88,
		tier: 'manual',
		...overrides
	};
}

describe('SearchResultCard.svelte', () => {
	it('renders folder, peer, score percentage and format', async () => {
		renderCard({ candidate: makeCandidate() });
		await expect.element(page.getByText('Radiohead - OK Computer (1997)')).toBeInTheDocument();
		await expect.element(page.getByText('alice')).toBeInTheDocument();
		await expect.element(page.getByText('88%')).toBeInTheDocument();
		await expect.element(page.getByText('FLAC')).toBeInTheDocument();
	});

	it('calls onPick when Pick is clicked', async () => {
		const onPick = vi.fn();
		renderCard({ candidate: makeCandidate(), onPick });
		await page.getByRole('button', { name: /Pick candidate from alice/ }).click();
		expect(onPick).toHaveBeenCalledOnce();
	});

	it('locks the Pick button when disabled (double-pick guard)', async () => {
		const onPick = vi.fn();
		renderCard({ candidate: makeCandidate(), onPick, disabled: true });
		// a disabled button can't dispatch onclick, so this proves a second pick can't fire
		await expect
			.element(page.getByRole('button', { name: /Pick candidate from alice/ }))
			.toBeDisabled();
		expect(onPick).not.toHaveBeenCalled();
	});

	it('exposes the score breakdown via a tooltip', async () => {
		renderCard({ candidate: makeCandidate() });
		await expect.element(page.getByText('88%')).toBeInTheDocument();
		const tip = document.querySelector('[data-tip]');
		expect(tip?.getAttribute('data-tip')).toContain('Coherence');
	});

	it('renders the Usenet variant with indexer, format and size', async () => {
		const usenet = makeCandidate({
			source: 'usenet',
			username: '',
			files: [],
			usenet_release: {
				indexer_id: 'ds',
				indexer_name: 'DrunkenSlug',
				guid: 'g',
				title: 'Radiohead-OK_Computer-FLAC-1997',
				nzb_url: 'https://idx/nzb',
				size_bytes: 2_315_726_631,
				category_ids: [3040],
				grabs: 205,
				files: 113,
				usenet_date: null
			}
		});
		renderCard({ candidate: usenet, albumTitle: 'OK Computer' });
		await expect.element(page.getByText('OK Computer')).toBeInTheDocument(); // clean album heading
		await expect.element(page.getByText('DrunkenSlug')).toBeInTheDocument();
		// the format badge is exactly "FLAC" (the release title also contains "FLAC").
		await expect.element(page.getByText('FLAC', { exact: true })).toBeInTheDocument();
		await expect.element(page.getByText('2.2 GB')).toBeInTheDocument();
	});

	it('shows "unknown" format when an obfuscated title has no quality category', async () => {
		const usenet = makeCandidate({
			source: 'usenet',
			username: '',
			files: [],
			usenet_release: {
				indexer_id: 'ds',
				indexer_name: 'DS',
				guid: 'g',
				title: 'aHR0cHM6 scrambled xQ',
				nzb_url: 'https://idx/nzb',
				size_bytes: 400_000_000,
				category_ids: [],
				grabs: null,
				files: null,
				usenet_date: null
			}
		});
		renderCard({ candidate: usenet, albumTitle: 'Some Album' });
		await expect.element(page.getByText('unknown')).toBeInTheDocument();
	});

	it('labels legacy candidates Within policy and keeps the pick action as Pick anyway', async () => {
		renderCard({ candidate: makeCandidate() });
		await expect.element(page.getByText('Within policy', { exact: true })).toBeVisible();
		await expect
			.element(page.getByRole('button', { name: /Pick candidate from alice/ }))
			.toHaveTextContent('Pick anyway');
	});

	it('marks the top-ranked step as Preferred with a plain Pick button', async () => {
		renderCard({ candidate: makeCandidate({ tier: 'auto', preference_step: 0 }) });
		await expect.element(page.getByText('Preferred', { exact: true })).toBeVisible();
		await expect
			.element(page.getByRole('button', { name: /Pick candidate from alice/ }))
			.toHaveTextContent('Pick');
	});

	it('maps computed fallback steps into the Quality chip', async () => {
		renderCard({
			candidate: makeCandidate({
				preference_step: 2,
				preference_steps_total: 5,
				certainty: 'partial'
			})
		});
		await expect.element(page.getByText('Fallback 2', { exact: true })).toBeVisible();
		await expect.element(page.getByText('Unknown', { exact: true })).not.toBeInTheDocument();
		renderCard({ candidate: makeCandidate({ preference_step: 2, certainty: 'unknown' }) });
		await expect.element(page.getByText('Unknown', { exact: true })).toBeVisible();
	});

	it('blocks unimportable candidates while outside-policy imports stay reachable via Show all', async () => {
		const onPick = vi.fn();
		renderCard({ candidate: makeCandidate({ tier: 'rejected' }), onPick });
		const button = page.getByRole('button', {
			name: /Blocked: outside the accepted quality policy/
		});
		await expect.element(button).toBeDisabled();
		await expect.element(button).toHaveTextContent('Unavailable');
		await expect.element(page.getByText('Outside policy', { exact: true })).toBeVisible();
		expect(onPick).not.toHaveBeenCalled();
	});
});
