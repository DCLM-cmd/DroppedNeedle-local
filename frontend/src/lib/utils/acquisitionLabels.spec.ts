import { describe, expect, it } from 'vitest';

import {
	ALREADY_IN_LIBRARY_COPY,
	BLOCKED_PICK_REASON,
	EMPTY_STATE_COPY,
	NO_FALLBACK_COPY,
	batchRequestCopy,
	candidateQualityLabel,
	classifyEmptyState,
	fallbackCopy,
	labelForQualityStep,
	nextPreferenceLabel,
	requestStatusCopy,
	type AcquisitionEmptyState,
	type EmptyStateSource
} from './acquisitionLabels';

describe('labelForQualityStep', () => {
	it.each([
		[0, 5, 'Preferred'],
		[2, 5, 'Fallback 2'],
		[4, 5, 'Fallback 4'],
		[5, 5, 'Outside policy'],
		[7, 5, 'Outside policy'],
		[-1, 5, 'Outside policy'],
		[1.5, 5, 'Outside policy'],
		[0, null, 'Preferred'],
		[3, null, 'Fallback 3'],
		[null, 5, 'Outside policy'],
		[undefined, 5, 'Outside policy']
	])('maps step=%p total=%p to %p', (step, total, expected) => {
		expect(labelForQualityStep(step as number | null, total as number | null)).toBe(expected);
	});
});

describe('candidateQualityLabel', () => {
	it('falls back conservatively for legacy candidates without a computed step', () => {
		expect(candidateQualityLabel(null)).toBe('Within policy');
		expect(candidateQualityLabel(undefined)).toBe('Within policy');
		expect(candidateQualityLabel({})).toBe('Within policy');
		expect(candidateQualityLabel({ certainty: 'exact' })).toBe('Within policy');
	});

	it('reports unknown evidence before step mapping', () => {
		expect(candidateQualityLabel({ certainty: 'unknown', preference_step: 1 })).toBe('Unknown');
		expect(candidateQualityLabel({ certainty: 'unknown' })).toBe('Unknown');
	});

	it('maps computed steps including the outside-policy null', () => {
		expect(candidateQualityLabel({ preference_step: null })).toBe('Outside policy');
		expect(candidateQualityLabel({ preference_step: 0 })).toBe('Preferred');
		expect(candidateQualityLabel({ preference_step: 2, preference_steps_total: 4 })).toBe(
			'Fallback 2'
		);
	});
});

describe('fallbackCopy', () => {
	it('names the awaited preference with exact contract strings', () => {
		expect(fallbackCopy('Lossy 320', 8)).toBe('Lossy 320 fallback in 8m');
		expect(fallbackCopy('24-bit/96 kHz lossless', 8)).toBe('24-bit/96 kHz lossless fallback in 8m');
	});

	it('uses "Next accepted preference" when the preference is unnameable', () => {
		expect(fallbackCopy(null, 14)).toBe('Next accepted preference fallback in 14m');
		expect(fallbackCopy(undefined, 3)).toBe('Next accepted preference fallback in 3m');
		expect(fallbackCopy('', 9)).toBe('Next accepted preference fallback in 9m');
	});

	it('rounds fractional minutes up and clamps negatives', () => {
		expect(fallbackCopy('Lossy 256', 13.5)).toBe('Lossy 256 fallback in 14m');
		expect(fallbackCopy('Lossy 256', -0.4)).toBe('Lossy 256 fallback in 0m');
		expect(fallbackCopy('Lossy 128', 0)).toBe('Lossy 128 fallback in 0m');
	});

	it('degrades to the explicit no-fallback copy without minutes', () => {
		expect(fallbackCopy('Lossy 320', null)).toBe(NO_FALLBACK_COPY);
		expect(fallbackCopy(null, undefined)).toBe('No fallback accepted');
	});
});

describe('nextPreferenceLabel', () => {
	it('formats known lossless resolution from depth and rate', () => {
		expect(nextPreferenceLabel({ format: 'flac', bit_depth: 16, sample_rate: 44_100 })).toBe(
			'16-bit/44.1 kHz lossless'
		);
		expect(nextPreferenceLabel({ format: 'flac', bit_depth: 24, sample_rate: 48_000 })).toBe(
			'24-bit/48 kHz lossless'
		);
		expect(nextPreferenceLabel({ format: 'alac', bit_depth: 24, sample_rate: 96_000 })).toBe(
			'24-bit/96 kHz lossless'
		);
		expect(nextPreferenceLabel({ format: 'FLAC', bit_depth: 17, sample_rate: 88_200 })).toBe(
			'17-bit/88.2 kHz lossless'
		);
	});

	it('formats named lossy bitrates without the word MP3', () => {
		expect(nextPreferenceLabel({ format: 'mp3', bitrate: 320 })).toBe('Lossy 320');
		expect(nextPreferenceLabel({ format: 'opus', bitrate: 160 })).toBe('Lossy 160');
		expect(nextPreferenceLabel({ format: 'ogg', bitrate: 245.6 })).toBe('Lossy 246');
	});

	it('returns null when evidence is incomplete or the family is unknown', () => {
		expect(nextPreferenceLabel({ format: 'flac', bitrate: 999 })).toBeNull();
		expect(nextPreferenceLabel({ format: 'flac', bit_depth: 16, sample_rate: 0 })).toBeNull();
		expect(nextPreferenceLabel({ format: 'mp3' })).toBeNull();
		expect(nextPreferenceLabel({ format: 'mka', bitrate: 320 })).toBeNull();
		expect(nextPreferenceLabel({ format: '' })).toBeNull();
		expect(nextPreferenceLabel(null)).toBeNull();
	});
});

describe('requestStatusCopy', () => {
	const submitted = 'Submitted for approval. The current server policy will apply when approved.';
	const summary = 'FLAC and better for lossless, Lossy 320 kbps and better otherwise.';

	it('lines up dispatched copy with and without a snapshot summary', () => {
		expect(requestStatusCopy('dispatched', summary)).toBe(
			`Requested - searching now using ${summary}.`
		);
		expect(requestStatusCopy('searching_now', '')).toBe('Requested - searching now.');
		expect(requestStatusCopy('accepted_auto', null)).toBe('Requested - searching now.');
		expect(requestStatusCopy('auto_dispatched', '   ')).toBe('Requested - searching now.');
	});

	it('keeps approval-pending copy stable regardless of summary', () => {
		expect(requestStatusCopy('submitted')).toBe(submitted);
		expect(requestStatusCopy('pending_approval', summary)).toBe(submitted);
		expect(requestStatusCopy(null)).toBe(submitted);
		expect(requestStatusCopy('totally-unknown-token', summary)).toBe(submitted);
	});

	it('says already-acquired only for duplicate tokens', () => {
		expect(requestStatusCopy('duplicate_active', summary)).toBe(
			`Already being acquired using ${summary}.`
		);
		expect(requestStatusCopy('already_queued', null)).toBe('Already being acquired.');
		// 'submitted'-ish tokens never produce this line.
		expect(requestStatusCopy('submitted', summary)).not.toContain('Already being acquired');
	});

	it('reuses the previous already-in-library wording verbatim', () => {
		expect(ALREADY_IN_LIBRARY_COPY).toBe('Album is already in the library');
		expect(requestStatusCopy('in_library')).toBe('Album is already in the library');
		expect(requestStatusCopy('already_in_library', summary)).toBe(
			'Album is already in the library'
		);
	});

	it('trims summaries into a single template sentence', () => {
		expect(requestStatusCopy('dispatched', `  ${summary} `)).toBe(
			`Requested - searching now using ${summary}.`
		);
		// Summaries are inserted verbatim; callers pass backend sentences without a
		// trailing period so the template owns the punctuation.
		expect(requestStatusCopy('duplicate_active', 'Already downloading Lossy 320')).toBe(
			'Already being acquired using Already downloading Lossy 320.'
		);
	});
});

describe('batchRequestCopy', () => {
	it('renders counts verbatim and never claims queued work', () => {
		expect(batchRequestCopy(3, 1, 0)).toBe('3 requested, 1 skipped');
		expect(batchRequestCopy(0, 0, 0)).toBe('0 requested, 0 skipped');
		expect(batchRequestCopy(12, 7, 5)).toBe(
			'12 requested, 7 skipped, 5 were over the batch request limit'
		);
	});

	it('handles the singular overflow note', () => {
		expect(batchRequestCopy(2, 0, 1)).toBe(
			'2 requested, 0 skipped, 1 was over the batch request limit'
		);
	});

	it('treats non-finite or negative overflow as none', () => {
		expect(batchRequestCopy(1, 0, NaN)).toBe('1 requested, 0 skipped');
		expect(batchRequestCopy(1, 0, -3)).toBe('1 requested, 0 skipped');
	});
});

describe('classifyEmptyState', () => {
	function taskOf(overrides: Partial<EmptyStateSource>): EmptyStateSource {
		return { status: 'failed', ...overrides };
	}

	it('puts manual overrides and quality-related failures first', () => {
		expect(classifyEmptyState(taskOf({ manual_quality_override: true }))).toBe('probe-mismatch');
		expect(
			classifyEmptyState(taskOf({ error_message: 'Post-download quality check failed' }))
		).toBe('probe-mismatch');
		expect(
			classifyEmptyState({
				manual_quality_override: true,
				error_message: 'outside policy',
				quality_certainty: 'unknown'
			})
		).toBe('probe-mismatch');
	});

	it('classifies family-unknown evidence through certainty or snapshot wording', () => {
		expect(classifyEmptyState(taskOf({ quality_certainty: 'unknown' }))).toBe('unknown-only');
		expect(
			classifyEmptyState({
				status: 'queued',
				quality_snapshot_summary:
					'Only unknown-resolution copies were found - review is required before acquiring.'
			})
		).toBe('unknown-only');
		// A generic message merely containing "unknown" must NOT trigger unknown-only.
		expect(classifyEmptyState(taskOf({ error_message: 'Unknown peer error' }))).toBe(
			'source-failure'
		);
	});

	it('separates outside-policy exhaustion from nothing-found', () => {
		expect(
			classifyEmptyState(
				taskOf({ error_message: 'Every candidate was outside policy for this task.' })
			)
		).toBe('all-outside-policy');
		expect(
			classifyEmptyState(
				taskOf({ error_message: 'All candidates were outside the accepted range' })
			)
		).toBe('all-outside-policy');
		expect(
			classifyEmptyState(taskOf({ error_message: 'No usable candidates found by any source' }))
		).toBe('nothing-found');
		expect(classifyEmptyState(taskOf({ error_message: 'Release not found on any source' }))).toBe(
			'nothing-found'
		);
	});

	it('falls back to source-failure and stays quiet for healthy tasks', () => {
		expect(classifyEmptyState(taskOf({ error_message: 'Soulseek connection timed out' }))).toBe(
			'source-failure'
		);
		expect(classifyEmptyState(taskOf({ error_message: null }))).toBeNull();
		expect(classifyEmptyState(taskOf({ status: 'downloading' }))).toBeNull();
	});

	it('ships copy for every state', () => {
		const states = Object.keys(EMPTY_STATE_COPY);
		expect(states.sort()).toEqual(
			[
				'all-outside-policy',
				'nothing-found',
				'probe-mismatch',
				'source-failure',
				'unknown-only'
			].sort()
		);
		for (const state of states as AcquisitionEmptyState[]) {
			expect(EMPTY_STATE_COPY[state].title.length).toBeGreaterThan(0);
			expect(EMPTY_STATE_COPY[state].detail.length).toBeGreaterThan(0);
		}
		expect(EMPTY_STATE_COPY['probe-mismatch'].detail).toContain('verification');
	});
});

describe('blocked pick reason', () => {
	it('is a single stable sentence', () => {
		expect(BLOCKED_PICK_REASON).toBe('Blocked: outside the accepted quality policy.');
	});
});
