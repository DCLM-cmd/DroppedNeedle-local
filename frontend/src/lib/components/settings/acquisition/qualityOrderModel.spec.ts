import { describe, expect, it } from 'vitest';

import {
	customBitrateError,
	losslessCapError,
	moveTier,
	presetFingerprint,
	PRESETS,
	rangeBetween,
	recomputeEndpoints,
	summarizeOrder,
	toggleTierInclusion
} from './qualityOrderModel';

describe('recomputeEndpoints', () => {
	it('maps order ends onto quality_min / quality_max', () => {
		expect(recomputeEndpoints(['lossless', 'mp3_320', 'mp3_256', 'mp3_192'])).toEqual({
			quality_min: 'mp3_192',
			quality_max: 'lossless'
		});
		expect(recomputeEndpoints(['mp3_192', 'mp3_256', 'mp3_320', 'lossless'])).toEqual({
			quality_min: 'lossless',
			quality_max: 'mp3_192'
		});
	});

	it('degenerates a single-tier order into itself at both ends', () => {
		expect(recomputeEndpoints(['lossless'])).toEqual({
			quality_min: 'lossless',
			quality_max: 'lossless'
		});
	});
});

describe('moveTier + endpoint follow-through', () => {
	it('moves a row one position and hidden endpoints follow the array ends', () => {
		const moved = moveTier(['lossless', 'mp3_320', 'mp3_256', 'mp3_192'], 2, 1);
		expect(moved).toEqual(['lossless', 'mp3_256', 'mp3_320', 'mp3_192']);
		expect(recomputeEndpoints(moved)).toEqual({
			quality_min: 'mp3_192',
			quality_max: 'lossless'
		});

		const demotedCeiling = moveTier(['lossless', 'mp3_320', 'mp3_256', 'mp3_192'], 3, 0);
		expect(demotedCeiling[0]).toBe('mp3_192');
		// assignment contract: order first element = max, last = min
		// assignment contract: order first element = max, last = min - the moved
		// array ['mp3_192','lossless','mp3_320','mp3_256'] makes both hidden
		// endpoints follow its new ends.
		expect(recomputeEndpoints(demotedCeiling)).toEqual({
			quality_min: 'mp3_256',
			quality_max: 'mp3_192'
		});
	});

	it('no-ops out-of-range indices', () => {
		expect(moveTier(['lossless'], 0, 1)).toEqual(['lossless']);
		expect(moveTier([], 0, 0)).toEqual([]);
	});
});

describe('toggleTierInclusion moves only endpoints', () => {
	it('adds the floor tier below the band and appends it to the order', () => {
		const next = toggleTierInclusion(['lossless', 'mp3_320', 'mp3_256', 'mp3_192'], 'low');
		expect(next).toEqual({
			order: ['lossless', 'mp3_320', 'mp3_256', 'mp3_192', 'low'],
			quality_min: 'low',
			quality_max: 'lossless'
		});
	});

	it('removes the floor tier endpoint-only way', () => {
		const next = toggleTierInclusion(['lossless', 'mp3_320', 'mp3_256', 'mp3_192', 'low'], 'low');
		expect(next).toEqual({
			order: ['lossless', 'mp3_320', 'mp3_256', 'mp3_192'],
			quality_min: 'mp3_192',
			quality_max: 'lossless'
		});
	});

	it('adds and removes the ceiling tier symmetrically', () => {
		const added = toggleTierInclusion(['mp3_320', 'mp3_256', 'mp3_192'], 'lossless');
		expect(added?.order).toEqual(['lossless', 'mp3_320', 'mp3_256', 'mp3_192']);

		const removed = toggleTierInclusion(['lossless', 'mp3_320', 'mp3_256', 'mp3_192'], 'lossless');
		expect(removed?.order).toEqual(['mp3_320', 'mp3_256', 'mp3_192']);
		expect(removed?.quality_max).toBe('mp3_320');
		expect(removed?.quality_min).toBe('mp3_192');
	});

	it('refuses interior removals and non-adjacent additions', () => {
		expect(
			toggleTierInclusion(PRESETS.best_available.quality_preference_order, 'mp3_256')
		).toBeNull();
		expect(toggleTierInclusion(['lossless'], 'low')).toBeNull();
	});

	it('never empties the accepted set', () => {
		expect(toggleTierInclusion(['lossless'], 'lossless')).toBeNull();
	});
});

describe('preset fills (spec table)', () => {
	it('Balanced: CD-capped lossless first, target 320, review', () => {
		const p = PRESETS.balanced;
		expect(p.quality_preference_order).toEqual(['lossless', 'mp3_320', 'mp3_256', 'mp3_192']);
		expect(p.quality_min).toBe('mp3_192');
		expect(p.quality_max).toBe('lossless');
		expect(p.preferred_lossy_bitrate_kbps).toBe(320);
		expect(p.lossless_preference).toBe('cd');
		expect(p.lossless_max_bit_depth).toBe(16);
		expect(p.lossless_max_sample_rate_hz).toBe(48000);
		expect(p.unknown_quality_behavior).toBe('review');
	});

	it('Lossy only: excludes lossless by quality_max', () => {
		const p = PRESETS.lossy_only;
		expect(p.quality_preference_order).toEqual(['mp3_320', 'mp3_256', 'mp3_192']);
		expect(p.quality_max).toBe('mp3_320');
		expect(p.preferred_lossy_bitrate_kbps).toBe(320);
		expect(p.unknown_quality_behavior).toBe('review');
	});

	it('Efficient 192+: 192 first, works upward, CD FLAC last', () => {
		const p = PRESETS.efficient_192;
		expect(p.quality_preference_order).toEqual(['mp3_192', 'mp3_256', 'mp3_320', 'lossless']);
		expect(p.preferred_lossy_bitrate_kbps).toBe(192);
		expect(p.lossless_preference).toBe('cd');
		expect(p.lossless_max_bit_depth).toBe(16);
		expect(p.unknown_quality_behavior).toBe('review');
	});

	it('Best available: full fidelity-first ladder including low', () => {
		const p = PRESETS.best_available;
		expect(p.quality_preference_order).toEqual([
			'lossless',
			'mp3_320',
			'mp3_256',
			'mp3_192',
			'low'
		]);
		expect(p.quality_min).toBe('low');
		expect(p.preferred_lossy_bitrate_kbps).toBeNull();
		expect(p.lossless_preference).toBe('highest');
		expect(p.unknown_quality_behavior).toBe('allow_as_fallback');
	});
});

describe('rangeBetween mirrors backend derive_default_order', () => {
	it('derives highest-to-lowest inside the band', () => {
		expect(rangeBetween('mp3_192', 'lossless')).toEqual([
			'lossless',
			'mp3_320',
			'mp3_256',
			'mp3_192'
		]);
		expect(rangeBetween('low', 'mp3_320')).toEqual(['mp3_320', 'mp3_256', 'mp3_192', 'low']);
		expect(rangeBetween('mp3_320', 'mp3_320')).toEqual(['mp3_320']);
	});
});

describe('customBitrateError', () => {
	it.each([
		['', null],
		[null, null],
		[320, null],
		[2048, null],
		[2049, 'The target cannot exceed 2048 kbps.'],
		[0, 'The target must be above 0 kbps.'],
		[-5, 'The target must be above 0 kbps.'],
		[256.5, 'Enter a whole number of kbps.']
	])('%s -> %s', (input, expected) => {
		expect(customBitrateError(input as number | null | '')).toBe(expected);
	});
});

describe('losslessCapError says bit depth/sample rate, never bitrate', () => {
	it('validates the bit depth axis', () => {
		expect(losslessCapError('bit_depth', 16)).toBeNull();
		expect(losslessCapError('bit_depth', 0)).toContain('between 1 and 64');
		expect(losslessCapError('bit_depth', 65)).toContain('between 1 and 64');
	});

	it('validates the sample rate axis', () => {
		expect(losslessCapError('sample_rate', 48000)).toBeNull();
		expect(losslessCapError('sample_rate', 8000)).toBeNull();
		expect(losslessCapError('sample_rate', 44100)).toBeNull();
		expect(losslessCapError('sample_rate', 7000)).toContain('8000 and 768000');
		expect(losslessCapError('sample_rate', 769000)).toContain('8000 and 768000');
	});
});

describe('summarizeOrder', () => {
	it('composes the deterministic try-then sentence', () => {
		expect(summarizeOrder(['lossless', 'mp3_320', 'mp3_256', 'mp3_192'])).toBe(
			'Try Lossless, then Lossy 320 kbps, then Lossy 256-319, then Lossy 192-255.'
		);
		expect(summarizeOrder(['mp3_192', 'mp3_256', 'mp3_320', 'lossless'])).toBe(
			'Try Lossy 192-255, then Lossy 256-319, then Lossy 320 kbps, then Lossless.'
		);
	});

	it('handles degenerate orders', () => {
		expect(summarizeOrder([])).toBe('No quality preference configured.');
		expect(summarizeOrder(['low'])).toBe('Try Lossy below 192.');
	});
});

describe('presetFingerprint dirty detection', () => {
	const fields = PRESETS.balanced;

	it('matches identical fields and differs on any preset-covered change', () => {
		const base = presetFingerprint(fields);
		expect(base).toBe(presetFingerprint({ ...fields }));
		expect(base).not.toBe(presetFingerprint({ ...fields, preferred_lossy_bitrate_kbps: 256 }));
		expect(base).not.toBe(presetFingerprint({ ...fields, unknown_quality_behavior: 'reject' }));
	});
});
