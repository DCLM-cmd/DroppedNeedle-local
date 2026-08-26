import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mockGet = vi.hoisted(() => vi.fn());

vi.mock('$lib/api/client', () => ({
	api: { global: { get: mockGet } }
}));

import { watchWarmingCover, type CoverWarmUpdate } from './coverWarmCoordinator';

describe('coverWarmCoordinator', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		mockGet.mockReset();
	});

	afterEach(() => {
		vi.useRealTimers();
	});

	it('coalesces subscribers into one request per retry generation and shares late success', async () => {
		mockGet.mockResolvedValueOnce(new Response('', { status: 202 })).mockResolvedValueOnce(
			new Response(new Blob(['cover'], { type: 'image/jpeg' }), {
				status: 200,
				headers: { 'x-cover-source': 'cover-art-archive' }
			})
		);
		const first: CoverWarmUpdate[] = [];
		const second: CoverWarmUpdate[] = [];

		const stopFirst = watchWarmingCover('/api/v1/covers/artist/a?size=250', (value) =>
			first.push(value)
		);
		const stopSecond = watchWarmingCover('/api/v1/covers/artist/a?size=250', (value) =>
			second.push(value)
		);

		expect(first).toEqual([{ status: 'warming' }]);
		expect(second).toEqual([{ status: 'warming' }]);
		await vi.advanceTimersByTimeAsync(1500);
		expect(mockGet).toHaveBeenCalledTimes(1);
		expect(mockGet).toHaveBeenLastCalledWith(
			'/api/v1/covers/artist/a?size=250&_r=1',
			expect.objectContaining({ raw: true, cache: 'no-store', timeoutMs: 10_000 })
		);

		await vi.advanceTimersByTimeAsync(3000);
		expect(mockGet).toHaveBeenCalledTimes(2);
		await vi.waitFor(() => {
			expect(first.at(-1)).toMatchObject({ status: 'ready' });
			expect(second.at(-1)).toEqual(first.at(-1));
		});

		stopFirst();
		stopSecond();
	});

	it('treats a stable placeholder response as terminal', async () => {
		mockGet.mockResolvedValueOnce(
			new Response(new Blob(['placeholder']), {
				status: 200,
				headers: { 'x-cover-source': 'placeholder' }
			})
		);
		const updates: CoverWarmUpdate[] = [];
		const stop = watchWarmingCover('/api/v1/covers/release-group/b?size=250', (value) =>
			updates.push(value)
		);

		await vi.advanceTimersByTimeAsync(1500);

		expect(mockGet).toHaveBeenCalledTimes(1);
		expect(updates).toEqual([{ status: 'warming' }, { status: 'failed' }]);
		stop();
	});
});
