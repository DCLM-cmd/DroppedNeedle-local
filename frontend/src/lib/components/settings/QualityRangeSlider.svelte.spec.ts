import { page } from '@vitest/browser/context';
import { beforeAll, describe, expect, it } from 'vitest';
import { render } from 'vitest-browser-svelte';

import QualityRangeSlider from './QualityRangeSlider.svelte';

beforeAll(() => {
	// Synthetic PointerEvents have no active pointer; capture would throw NotFoundError.
	Element.prototype.setPointerCapture = () => {};
});

function railRect(container: HTMLElement): DOMRect {
	const rail = container.querySelector('.qr-rail');
	if (!rail) throw new Error('qr-rail not rendered');
	const rect = rail.getBoundingClientRect();
	if (!rect.width) throw new Error('qr-rail has zero width in test viewport');
	return rect;
}

function dragThumb(thumb: HTMLElement, rect: DOMRect, fromIdx: number, toIdx: number) {
	const x = (idx: number) => rect.left + (idx / 4) * rect.width;
	const fire = (type: string, clientX: number) =>
		thumb.dispatchEvent(
			new PointerEvent(type, { bubbles: true, clientX, clientY: rect.top + 1, pointerId: 1 })
		);
	fire('pointerdown', x(fromIdx));
	for (let s = 1; s <= 4; s++) fire('pointermove', x(fromIdx + ((toIdx - fromIdx) * s) / 4));
	fire('pointerup', x(toIdx));
}

describe('QualityRangeSlider', () => {
	it('GH #270: overlapped handles at FLAC can be pulled apart leftward', async () => {
		const { container } = render(QualityRangeSlider, {
			minKey: 'lossless',
			maxKey: 'lossless'
		});
		const minSlider = page.getByRole('slider', { name: 'Minimum quality' });
		const maxSlider = page.getByRole('slider', { name: 'Maximum quality' });
		await expect.element(minSlider).toHaveAttribute('aria-valuenow', '4');
		await expect.element(maxSlider).toHaveAttribute('aria-valuenow', '4');

		const maxThumb = container.querySelectorAll('.qr-thumb')[1] as HTMLElement;
		dragThumb(maxThumb, railRect(container), 4, 2);

		await expect.element(minSlider).toHaveAttribute('aria-valuenow', '2');
		await expect.element(maxSlider).toHaveAttribute('aria-valuenow', '4');
	});

	it('crossing the other handle swaps roles mid-drag and keeps the band ordered', async () => {
		const { container } = render(QualityRangeSlider, {
			minKey: 'mp3_192',
			maxKey: 'mp3_256'
		});
		const minSlider = page.getByRole('slider', { name: 'Minimum quality' });
		const maxSlider = page.getByRole('slider', { name: 'Maximum quality' });
		await expect.element(minSlider).toHaveAttribute('aria-valuenow', '1');
		await expect.element(maxSlider).toHaveAttribute('aria-valuenow', '2');

		const minThumb = container.querySelectorAll('.qr-thumb')[0] as HTMLElement;
		dragThumb(minThumb, railRect(container), 1, 4);

		await expect.element(minSlider).toHaveAttribute('aria-valuenow', '2');
		await expect.element(maxSlider).toHaveAttribute('aria-valuenow', '4');
		const summary = container.querySelector('.qr-summary');
		expect(summary?.textContent).toBe(
			'Accept 256 kbps → FLAC / lossless · always take the best available'
		);
	});
});
