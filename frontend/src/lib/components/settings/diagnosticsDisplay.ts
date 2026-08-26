/**
 * Pure view logic for the admin Diagnostics settings card (QW9 Part 5): label
 * curation, formatting, grouping, and the polling gate - kept here so specs can
 * pin them without a browser.
 *
 * Provider-counter `priority`/`outcome` strings come from backend Parts 1/3;
 * curated labels cover the pinned values, everything else falls through to the
 * humanizer instead of breaking the panel.
 */
import type { ProviderStatsRow, QueueStatsRow } from '$lib/queries/diagnostics/types';

export const PROVIDER_LABELS: Readonly<Record<string, string>> = {
	musicbrainz: 'MusicBrainz',
	listenbrainz: 'ListenBrainz',
	lastfm: 'Last.fm',
	coverart: 'Cover Art Archive',
	audiodb: 'AudioDB',
	discogs: 'Discogs'
};

export const LANE_LABELS: Readonly<Record<string, string>> = {
	user: 'User requests',
	image: 'Artwork',
	background: 'Background'
};

export const OUTCOME_LABELS: Readonly<Record<string, string>> = {
	ok: 'OK',
	empty_404: 'Not found (404)',
	http_error: 'HTTP error'
};

const PROVIDER_ORDER: ReadonlyArray<string> = Object.keys(PROVIDER_LABELS);
const LANE_ORDER: Readonly<Record<string, number>> = { user: 0, image: 1, background: 2 };
const OUTCOME_ORDER: Readonly<Record<string, number>> = { ok: 0, empty_404: 1, http_error: 2 };

/** 'user_initiated' -> 'User initiated'; unknown wire values stay readable. */
export function humanizeWireValue(value: string): string {
	const spaced = value.replaceAll('_', ' ').trim();
	if (!spaced) return value;
	return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function providerLabel(provider: string): string {
	return PROVIDER_LABELS[provider] ?? humanizeWireValue(provider);
}

export function laneLabel(lane: string): string {
	return LANE_LABELS[lane] ?? humanizeWireValue(lane);
}

export function outcomeLabel(outcome: string): string {
	return OUTCOME_LABELS[outcome] ?? humanizeWireValue(outcome);
}

/** Windowed call rate in human units: sub-0.1 rates read as "<0.1/min" rather
 * than collapsing to 0, one decimal below ten, whole numbers above. */
export function formatRatePerMin(rate: number): string {
	if (!Number.isFinite(rate) || rate <= 0) return '0/min';
	if (rate < 0.1) return '<0.1/min';
	const value = rate < 10 ? rate.toFixed(1) : Math.round(rate).toLocaleString('en-US');
	return `${value}/min`;
}

export function formatCount(count: number): string {
	return count.toLocaleString('en-US');
}

export interface QueueLaneView {
	key: 'user' | 'image' | 'background';
	label: string;
	slotsAvailable: number;
	active: boolean | null;
	waiting: number | null;
}

/** Maps the queue-stats row onto one gauge cell per priority lane;
 * user_active rides the user lane, background_waiters the background lane. */
export function buildQueueLanes(stats: QueueStatsRow): QueueLaneView[] {
	return [
		{
			key: 'user',
			label: laneLabel('user'),
			slotsAvailable: stats.user_slots_available,
			active: stats.user_active,
			waiting: null
		},
		{
			key: 'image',
			label: laneLabel('image'),
			slotsAvailable: stats.image_slots_available,
			active: null,
			waiting: null
		},
		{
			key: 'background',
			label: laneLabel('background'),
			slotsAvailable: stats.background_slots_available,
			active: null,
			waiting: stats.background_waiters
		}
	];
}

export interface ProviderRowView {
	lane: string;
	outcome: string;
	laneText: string;
	outcomeText: string;
	countTotal: number;
	ratePerMinText: string;
}

export interface ProviderGroupView {
	provider: string;
	label: string;
	rows: ProviderRowView[];
	totalCalls: number;
}

function curatedOrder(order: Readonly<Record<string, number>>, key: string): number {
	const index = order[key];
	return index === undefined ? Number.MAX_SAFE_INTEGER : index;
}

/** Groups counter rows per provider - curated providers first in registry
 * order, unknown ones after alphabetically - and orders lanes/outcomes inside
 * each group so the table reads identically between polls. */
export function groupProviderRows(rows: readonly ProviderStatsRow[]): ProviderGroupView[] {
	const byProvider: Record<string, ProviderGroupView> = {};
	for (const row of rows) {
		let group = byProvider[row.provider];
		if (!group) {
			group = {
				provider: row.provider,
				label: providerLabel(row.provider),
				rows: [],
				totalCalls: 0
			};
			byProvider[row.provider] = group;
		}
		group.rows.push({
			lane: row.priority,
			outcome: row.outcome,
			laneText: laneLabel(row.priority),
			outcomeText: outcomeLabel(row.outcome),
			countTotal: row.count_total,
			ratePerMinText: formatRatePerMin(row.rate_per_min_window)
		});
		group.totalCalls += row.count_total;
	}
	return Object.values(byProvider)
		.sort((a, b) => {
			const ai = PROVIDER_ORDER.indexOf(a.provider);
			const bi = PROVIDER_ORDER.indexOf(b.provider);
			if (ai !== -1 && bi !== -1) return ai - bi;
			if (ai !== -1) return -1;
			if (bi !== -1) return 1;
			return a.provider.localeCompare(b.provider);
		})
		.map((group) => ({
			...group,
			rows: group.rows.sort(
				(a, b) =>
					curatedOrder(LANE_ORDER, a.lane) - curatedOrder(LANE_ORDER, b.lane) ||
					curatedOrder(OUTCOME_ORDER, a.outcome) - curatedOrder(OUTCOME_ORDER, b.outcome) ||
					a.laneText.localeCompare(b.laneText)
			)
		}));
}

/**
 * Single gating predicate for both gauge queries: poll only while this section
 * is actually being viewed (the component mounts only for its settings tab)
 * AND the document itself is visible. Non-browser environments have no hidden
 * state, so they default to allowed.
 */
export function isDiagnosticsPollingEnabled(sectionVisible: boolean): boolean {
	if (!sectionVisible) return false;
	if (typeof document === 'undefined') return true;
	return document.visibilityState === 'visible';
}
