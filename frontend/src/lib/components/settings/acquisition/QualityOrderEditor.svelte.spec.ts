import { page } from '@vitest/browser/context';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-svelte';

import QualityOrderEditor from './QualityOrderEditor.svelte';

const BALANCED = ['lossless', 'mp3_320', 'mp3_256', 'mp3_192'];

interface EditorHarness {
	order: string[];
	minTier: string;
	maxTier: string;
	next?: { order: string[]; quality_min: string; quality_max: string };
}

const h = vi.hoisted(() => ({
	state: undefined as unknown as EditorHarness,
	onChange: vi.fn()
}));

function props() {
	return {
		order: [...h.state.order],
		minTier: h.state.minTier,
		maxTier: h.state.maxTier,
		targetKbps: 320,
		losslessDetailHint: null as string | null,
		onchange: h.onChange
	};
}

describe('QualityOrderEditor', () => {
	beforeEach(() => {
		h.state = { order: [...BALANCED], minTier: 'mp3_192', maxTier: 'lossless' };
		h.onChange = vi.fn((next: { order: string[]; quality_min: string; quality_max: string }) => {
			// feed back like the controlled parent (QualityOrderSection) would
			h.state.next = next;
			h.state.order = [...next.order];
			h.state.minTier = next.quality_min;
			h.state.maxTier = next.quality_max;
		});
	});

	it('renders the included ladder plus the rejected floor with named buttons', async () => {
		const { container } = render(QualityOrderEditor, props());
		const included = container.querySelectorAll('ol [data-tier]');
		expect(included).toHaveLength(4);
		expect(container.querySelectorAll('ul [data-tier]')).toHaveLength(1);
		await expect
			.element(page.getByRole('button', { name: 'Move Lossless to position 2', exact: true }))
			.toBeVisible();
		await expect.element(page.getByText('not accepted')).toBeVisible();
	});

	it('moves a row up via its keyboard-accessible button and rewires endpoints', async () => {
		const { rerender, container } = render(QualityOrderEditor, props());

		await page.getByRole('button', { name: 'Move Lossy 320 kbps to position 1' }).click();

		expect(h.state.next?.order).toEqual(['mp3_320', 'lossless', 'mp3_256', 'mp3_192']);
		// wire contract: endpoints follow the array ends
		expect(h.state.next?.quality_min).toBe('mp3_192');
		expect(h.state.next?.quality_max).toBe('mp3_320');

		await rerender(props());
		await expect
			.element(page.getByRole('status'))
			.toHaveTextContent(
				'Try Lossy 320 kbps, then Lossless, then Lossy 256-319, then Lossy 192-255.'
			);
		const badges = [
			...(container.querySelectorAll('ol [data-tier] span') as NodeListOf<HTMLElement>)
		]
			.filter((el) => /^\d+$/.test(el.textContent ?? ''))
			.map((el) => el.textContent);
		expect(badges).toEqual(['1', '2', '3', '4']);
	});

	it('demotes the top row through deterministic down naming', async () => {
		const { rerender } = render(QualityOrderEditor, props());

		await page.getByRole('button', { name: 'Move Lossless to position 2', exact: true }).click();
		expect(h.state.next?.order[0]).toBe('mp3_320');
		expect(h.state.next?.order[1]).toBe('lossless');

		await rerender(props());
		await expect
			.element(page.getByRole('status'))
			.toHaveTextContent(
				'Try Lossy 320 kbps, then Lossless, then Lossy 256-319, then Lossy 192-255.'
			);
	});

	it('includes the rejected floor via [add] with an endpoint-only rewrite', async () => {
		const { rerender, container } = render(QualityOrderEditor, props());

		await page.getByRole('button', { name: 'Add Lossy below 192 to accepted range' }).click();

		expect(h.state.next?.order).toEqual([...BALANCED, 'low']);
		expect(h.state.next?.quality_min).toBe('low');
		expect(h.state.next?.quality_max).toBe('lossless');

		await rerender(props());
		expect(container.querySelectorAll('ol [data-tier]')).toHaveLength(5);
		expect(container.querySelector('ul [data-tier]')).toBeNull();
	});

	it('removes an endpoint tier without touching interior tiers', async () => {
		h.state = { order: ['lossless', 'mp3_320'], minTier: 'mp3_320', maxTier: 'lossless' };
		render(QualityOrderEditor, props());

		await page.getByRole('button', { name: 'Remove Lossless from accepted range' }).click();

		expect(h.state.next?.order).toEqual(['mp3_320']);
		expect(h.state.next?.quality_min).toBe('mp3_320');
		expect(h.state.next?.quality_max).toBe('mp3_320');
	});
});
