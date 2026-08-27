import { describe, expect, it } from 'vitest';

import { ALREADY_IN_LIBRARY_COPY, batchRequestCopy, requestStatusCopy } from './acquisitionLabels';
import { albumRequestOutcome } from './requestOutcome';

describe('albumRequestOutcome', () => {
	it('maps an accepted pending dispatch onto the dispatched token', () => {
		expect(
			albumRequestOutcome({ success: true, message: 'Request accepted', status: 'pending' })
		).toBe('dispatched');
	});

	it('maps awaiting approval onto the submitted token', () => {
		expect(
			albumRequestOutcome({
				success: true,
				message: 'Request submitted, awaiting admin approval',
				status: 'awaiting_approval'
			})
		).toBe('submitted');
	});

	it('maps live duplicates (queued/downloading) onto the duplicate_active token', () => {
		expect(
			albumRequestOutcome({
				success: true,
				message: 'Request already in progress',
				status: 'queued'
			})
		).toBe('duplicate_active');
		expect(
			albumRequestOutcome({
				success: true,
				message: 'Request already in progress',
				status: 'downloading'
			})
		).toBe('duplicate_active');
	});

	it('identifies an already-in-library hit by its verbatim backend message', () => {
		expect(
			albumRequestOutcome({
				success: true,
				message: ALREADY_IN_LIBRARY_COPY,
				status: 'pending'
			})
		).toBe('in_library');
	});

	it('returns no outcome for unsuccessful responses', () => {
		expect(
			albumRequestOutcome({
				success: false,
				message: 'Request could not be recorded',
				status: 'failed'
			})
		).toBeNull();
	});
});

describe('requestStatusCopy (spec request lines)', () => {
	it('renders dispatched copy without a summary when none is carried', () => {
		expect(requestStatusCopy('dispatched')).toBe('Requested - searching now.');
		expect(requestStatusCopy('dispatched', null)).toBe('Requested - searching now.');
	});

	it('renders the full dispatched sentence including the summary period', () => {
		expect(requestStatusCopy('dispatched', 'Efficient 192+')).toBe(
			'Requested - searching now using Efficient 192+.'
		);
	});

	it('renders the approval line verbatim for every synonym spelling', () => {
		expect(requestStatusCopy('submitted')).toBe(
			'Submitted for approval. The current server policy will apply when approved.'
		);
		expect(requestStatusCopy('pending_approval')).toBe(requestStatusCopy('submitted'));
	});

	it('renders duplicate copy without a dangling using-segment when summary is unknown', () => {
		expect(requestStatusCopy('already_queued', '')).toBe('Already being acquired.');
	});

	it('renders duplicate copy naming the snapshot summary', () => {
		expect(requestStatusCopy('duplicate_active', 'Balanced')).toBe(
			'Already being acquired using Balanced.'
		);
	});

	it('keeps the previous library wording for already-owned albums', () => {
		expect(requestStatusCopy('already_in_library')).toBe('Album is already in the library');
	});
});

describe('batchRequestCopy (spec batch counts)', () => {
	it('never claims anything was queued and omits the overflow note at zero', () => {
		expect(batchRequestCopy(3, 1, 0)).toBe('3 requested, 1 skipped');
	});

	it('appends the singular overflow note verbatim', () => {
		expect(batchRequestCopy(0, 0, 1)).toBe(
			'0 requested, 0 skipped, 1 was over the batch request limit'
		);
	});

	it('appends the plural overflow note verbatim', () => {
		expect(batchRequestCopy(3, 1, 2)).toBe(
			'3 requested, 1 skipped, 2 were over the batch request limit'
		);
	});
});
