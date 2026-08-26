import { afterEach, describe, expect, it, vi } from 'vitest';

import {
	buildQueueLanes,
	formatCount,
	formatRatePerMin,
	groupProviderRows,
	humanizeWireValue,
	isDiagnosticsPollingEnabled,
	laneLabel,
	outcomeLabel,
	providerLabel
} from './diagnosticsDisplay';
import type { ProviderStatsRow, QueueStatsRow } from '$lib/queries/diagnostics/types';

function stubDocumentVisibility(state: 'visible' | 'hidden'): void {
	vi.stubGlobal('document', { visibilityState: state });
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('wire label mapping', () => {
	it('maps the pinned providers to their human names', () => {
		expect(providerLabel('musicbrainz')).toBe('MusicBrainz');
		expect(providerLabel('listenbrainz')).toBe('ListenBrainz');
		expect(providerLabel('lastfm')).toBe('Last.fm');
		expect(providerLabel('coverart')).toBe('Cover Art Archive');
		expect(providerLabel('audiodb')).toBe('AudioDB');
	});

	it('humanizes wire values that arrive without a curated label', () => {
		expect(providerLabel('other_service')).toBe('Other service');
		expect(laneLabel('user_initiated')).toBe('User initiated');
		expect(outcomeLabel('circuit_open')).toBe('Circuit open');
		expect(humanizeWireValue('')).toBe('');
	});

	it('labels the three queue lanes and counter outcomes', () => {
		expect(laneLabel('user')).toBe('User requests');
		expect(laneLabel('image')).toBe('Artwork');
		expect(laneLabel('background')).toBe('Background');
		expect(outcomeLabel('ok')).toBe('OK');
		expect(outcomeLabel('empty_404')).toBe('Not found (404)');
		expect(outcomeLabel('http_error')).toBe('HTTP error');
	});
});

describe('formatRatePerMin', () => {
	it('keeps slow providers readable instead of collapsing to zero', () => {
		expect(formatRatePerMin(0)).toBe('0/min');
		expect(formatRatePerMin(0.04)).toBe('<0.1/min');
		expect(formatRatePerMin(1.24)).toBe('1.2/min');
		expect(formatRatePerMin(9.96)).toBe('10.0/min');
		expect(formatRatePerMin(12.4)).toBe('12/min');
	});

	it('treats non-finite rates as zero traffic', () => {
		expect(formatRatePerMin(Number.NaN)).toBe('0/min');
		expect(formatRatePerMin(Number.NEGATIVE_INFINITY)).toBe('0/min');
	});
});

describe('formatCount', () => {
	it('groups digits for large totals', () => {
		expect(formatCount(128)).toBe('128');
		expect(formatCount(12345)).toBe('12,345');
	});
});

describe('buildQueueLanes', () => {
	it('maps all five queue-stats fields onto the three lane cards', () => {
		const stats: QueueStatsRow = {
			user_slots_available: 17,
			image_slots_available: 24,
			background_slots_available: 3,
			user_active: true,
			background_waiters: 5
		};

		const lanes = buildQueueLanes(stats);

		expect(lanes.map((lane) => lane.key)).toEqual(['user', 'image', 'background']);
		expect(lanes[0]).toMatchObject({ slotsAvailable: 17, active: true, waiting: null });
		expect(lanes[1]).toMatchObject({ slotsAvailable: 24, active: null, waiting: null });
		expect(lanes[2]).toMatchObject({ slotsAvailable: 3, active: null, waiting: 5 });
	});
});

describe('groupProviderRows', () => {
	const rows: ProviderStatsRow[] = [
		{
			provider: 'discogs',
			priority: 'background',
			outcome: 'http_error',
			count_total: 4,
			rate_per_min_window: 0.02
		},
		{
			provider: 'musicbrainz',
			priority: 'image',
			outcome: 'ok',
			count_total: 50,
			rate_per_min_window: 2.5
		},
		{
			provider: 'musicbrainz',
			priority: 'user',
			outcome: 'empty_404',
			count_total: 7,
			rate_per_min_window: 0.4
		}
	];

	it('groups per provider with curated providers first, unknown after alphabetically', () => {
		const groups = groupProviderRows(rows);

		expect(groups.map((g) => g.provider)).toEqual(['musicbrainz', 'discogs']);
		expect(groups[0].label).toBe('MusicBrainz');
		expect(groups[1].label).toBe('Discogs');
	});

	it('orders lanes and outcomes inside a group and sums provider totals', () => {
		const groups = groupProviderRows(rows);

		expect(groups[0].rows.map((row) => [row.laneText, row.outcomeText])).toEqual([
			['User requests', 'Not found (404)'],
			['Artwork', 'OK']
		]);
		expect(groups[0].totalCalls).toBe(57);
		expect(groups[1].rows[0].ratePerMinText).toBe('<0.1/min');
	});

	it('returns an empty table for an empty counter map', () => {
		expect(groupProviderRows([])).toEqual([]);
	});
});

describe('isDiagnosticsPollingEnabled', () => {
	it('never polls when the section is not being viewed', () => {
		stubDocumentVisibility('visible');
		expect(isDiagnosticsPollingEnabled(false)).toBe(false);
	});

	it('polls while the section is viewed and the document is visible', () => {
		stubDocumentVisibility('visible');
		expect(isDiagnosticsPollingEnabled(true)).toBe(true);
	});

	it('stops polling when the document is hidden', () => {
		stubDocumentVisibility('hidden');
		expect(isDiagnosticsPollingEnabled(true)).toBe(false);
	});

	it('defaults to allowed in non-browser environments without a document', () => {
		// node project has no DOM global; unstub leaves nothing behind
		vi.unstubAllGlobals();
		expect(isDiagnosticsPollingEnabled(true)).toBe(true);
	});
});
