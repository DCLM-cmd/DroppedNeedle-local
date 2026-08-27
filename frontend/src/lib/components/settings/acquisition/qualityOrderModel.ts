// Pure model for the acquisition-quality order editor. No Svelte imports:
// everything here runs in node under the `server` vitest project so presets,
// endpoint arithmetic and validation are pinned by unit specs.
import { QUALITY_TIERS } from '../qualityTiers';

import type { DownloadPolicySettings } from '$lib/types';

// The canonical fidelity axis, worst to best (qualityTiers is already in this
// order). Fidelity rank = array index.
const TIER_KEYS = QUALITY_TIERS.map((t) => t.key);

export function tierLabel(key: string): string {
	return QUALITY_TIERS.find((t) => t.key === key)?.label ?? key;
}

export function tierIndex(key: string): number {
	const i = TIER_KEYS.indexOf(key);
	return i < 0 ? 0 : i;
}

// ---------------------------------------------------------------------------
// Policy shape extension
//
// types.ts carries the hand-mirrored backend DownloadPolicySettings without
// the nine acquisition-quality fields (the frozen API contract documents them
// on the wire; the backend PUT accepts/returns them). Extend locally - never
// edit types.ts from a settings slice.
// ---------------------------------------------------------------------------

export interface AcquisitionPolicyFields {
	quality_preference_order: string[];
	preferred_lossy_bitrate_kbps: number | null;
	lossy_min_bitrate_kbps: number | null;
	lossy_max_bitrate_kbps: number | null;
	lossless_preference: string;
	lossless_max_bit_depth: number | null;
	lossless_max_sample_rate_hz: number | null;
	unknown_quality_behavior: string;
	source_selection_mode: string;
}

export type FullDownloadPolicySettings = DownloadPolicySettings & AcquisitionPolicyFields;

export const ACQUISITION_POLICY_DEFAULTS: AcquisitionPolicyFields = {
	quality_preference_order: [],
	preferred_lossy_bitrate_kbps: null,
	lossy_min_bitrate_kbps: null,
	lossy_max_bitrate_kbps: null,
	lossless_preference: 'highest',
	lossless_max_bit_depth: null,
	lossless_max_sample_rate_hz: null,
	unknown_quality_behavior: 'allow_as_fallback',
	source_selection_mode: 'source_first'
};

/**
 * Mirror of backend `derive_default_order`: every tier inside
 * [quality_min, quality_max], HIGHEST first. Used when a stored policy predates
 * the order field (empty array) so the editor still renders all five rows.
 */
export function rangeBetween(qualityMin: string, qualityMax: string): string[] {
	const best = Math.max(tierIndex(qualityMin), tierIndex(qualityMax));
	const worst = Math.min(tierIndex(qualityMin), tierIndex(qualityMax));
	return TIER_KEYS.slice(worst, best + 1).reverse();
}

/**
 * Wire invariant (frozen API contract): the saved order starts at
 * quality_max and ends at quality_min. Reordering rows therefore rewrites the
 * hidden range endpoints to follow the array ends.
 */
export function recomputeEndpoints(order: string[]): {
	quality_min: string;
	quality_max: string;
} {
	if (!order.length) return { quality_min: 'mp3_192', quality_max: 'lossless' };
	return { quality_min: order[order.length - 1], quality_max: order[0] };
}

/** One-position move inside the order (pure). Out-of-range indices are no-ops. */
export function moveTier(order: string[], from: number, to: number): string[] {
	if (from === to || from < 0 || to < 0 || from >= order.length || to >= order.length) {
		return [...order];
	}
	const next = [...order];
	const [moved] = next.splice(from, 1);
	next.splice(to, 0, moved);
	return next;
}

/**
 * Inclusion toggle: add/remove moves ONLY the contiguous endpoints
 * (quality_min/quality_max) plus the matching extreme slot in the order.
 * A middle tier can never be omitted on its own.
 *
 * Returns null when the request cannot be expressed as an endpoint move
 * (removing an interior tier, removing the last included tier).
 */
export function toggleTierInclusion(
	order: string[],
	tier: string
): { order: string[]; quality_min: string; quality_max: string } | null {
	if (order.includes(tier)) {
		// Removal only ever targets an endpoint tier (the band floor or ceiling).
		if (order.length <= 1) return null; // never empty the accepted set
		const idx = order.indexOf(tier);
		const ends = recomputeEndpoints(order);
		const isFloor = idx === order.length - 1 && tier === ends.quality_min;
		const isCeiling = idx === 0 && tier === ends.quality_max;
		if (!isFloor && !isCeiling) return null;
		const next = order.filter((k) => k !== tier);
		return { order: next, ...recomputeEndpoints(next) };
	}
	// Adding is valid adjacent to the band only: extend one endpoint and insert
	// the tier at the nearest fallback position beyond that end.
	const ends = recomputeEndpoints(order);
	const t = tierIndex(tier);
	if (t === tierIndex(ends.quality_min) - 1 || t === tierIndex(ends.quality_max) + 1) {
		const next = t < tierIndex(ends.quality_min) ? [...order, tier] : [tier, ...order];
		return { order: next, ...recomputeEndpoints(next) };
	}
	return null;
}

// ---------------------------------------------------------------------------
// Presets (spec table "Global policy and presets")
// ---------------------------------------------------------------------------

export type PresetKey = 'balanced' | 'lossy_only' | 'efficient_192' | 'best_available';

export interface PresetFill {
	label: string;
	quality_min: string;
	quality_max: string;
	quality_preference_order: string[];
	preferred_lossy_bitrate_kbps: number | null;
	lossless_preference: string;
	lossless_max_bit_depth: number | null;
	lossless_max_sample_rate_hz: number | null;
	unknown_quality_behavior: string;
}

const CD_CAPS = {
	lossless_preference: 'cd',
	lossless_max_bit_depth: 16 as number | null,
	lossless_max_sample_rate_hz: 48000 as number | null
};

const REVIEW_UNKNOWN = { unknown_quality_behavior: 'review' };

export const PRESETS: Record<PresetKey, PresetFill> = {
	balanced: {
		label: 'Balanced',
		quality_min: 'mp3_192',
		quality_max: 'lossless',
		quality_preference_order: ['lossless', 'mp3_320', 'mp3_256', 'mp3_192'],
		preferred_lossy_bitrate_kbps: 320,
		...CD_CAPS,
		...REVIEW_UNKNOWN
	},
	lossy_only: {
		label: 'Lossy only',
		quality_min: 'mp3_192',
		quality_max: 'mp3_320',
		quality_preference_order: ['mp3_320', 'mp3_256', 'mp3_192'],
		preferred_lossy_bitrate_kbps: 320,
		lossless_preference: 'highest',
		lossless_max_bit_depth: null,
		lossless_max_sample_rate_hz: null,
		...REVIEW_UNKNOWN
	},
	efficient_192: {
		label: 'Efficient 192+',
		quality_min: 'mp3_192',
		quality_max: 'lossless',
		quality_preference_order: ['mp3_192', 'mp3_256', 'mp3_320', 'lossless'],
		preferred_lossy_bitrate_kbps: 192,
		...CD_CAPS,
		...REVIEW_UNKNOWN
	},
	best_available: {
		label: 'Best available',
		quality_min: 'low',
		quality_max: 'lossless',
		quality_preference_order: ['lossless', 'mp3_320', 'mp3_256', 'mp3_192', 'low'],
		preferred_lossy_bitrate_kbps: null,
		lossless_preference: 'highest',
		lossless_max_bit_depth: null,
		lossless_max_sample_rate_hz: null,
		unknown_quality_behavior: 'allow_as_fallback'
	}
};

export const PRESET_KEYS: PresetKey[] = [
	'balanced',
	'lossy_only',
	'efficient_192',
	'best_available'
];

// ---------------------------------------------------------------------------
// Field option tables (copy per spec UX block)
// ---------------------------------------------------------------------------

export const LOSSLESS_PREFERENCE_OPTIONS = [
	{ value: 'cd', label: 'CD-quality (16-bit / 48 kHz)' },
	{ value: '24_48', label: 'Up to 24-bit / 48 kHz' },
	{ value: '24_96', label: 'Up to 24-bit / 96 kHz' },
	{ value: '24_192', label: 'Up to 24-bit / 192 kHz' },
	{ value: 'highest', label: 'No detail preference' }
];

export const UNKNOWN_BEHAVIOR_OPTIONS = [
	{ value: 'reject', label: 'Excluded' },
	{ value: 'review', label: 'Review before acquiring' },
	{ value: 'allow_as_fallback', label: 'Last-resort fallback' }
];

export const SOURCE_MODE_OPTIONS = [
	{
		value: 'source_first',
		label: 'Source order first',
		hint: 'Walk configured sources in order and use the first with an automatic candidate.'
	},
	{
		value: 'quality_first',
		label: 'Best quality across sources',
		hint: 'Search enabled sources concurrently (opt-in) and take the earliest preference step globally.'
	}
];

export const LOSSY_TARGET_PRESETS = [128, 192, 256, 320];

/**
 * Preferred lossy bitrate must be a positive integer <= 2048 kbps (backend
 * `_QUALITY_KBPS_MAX`). Returns an inline error message or null when valid.
 */
export function customBitrateError(value: string | number | null | undefined): string | null {
	if (value === null || value === undefined || value === '') return null; // cleared = unset
	const n = typeof value === 'number' ? value : Number(value);
	if (!Number.isInteger(n)) return 'Enter a whole number of kbps.';
	if (n <= 0) return 'The target must be above 0 kbps.';
	if (n > 2048) return 'The target cannot exceed 2048 kbps.';
	return null;
}

export function losslessCapError(
	field: 'bit_depth' | 'sample_rate',
	value: number | null
): string | null {
	if (value === null || value === undefined || Number.isNaN(value)) return null;
	if (field === 'bit_depth') {
		if (!Number.isInteger(value) || value < 1 || value > 64)
			return 'Bit depth must be a whole number between 1 and 64.';
	} else if (value < 8000 || value > 768000) {
		return 'Sample rate must be between 8000 and 768000 Hz.';
	}
	return null;
}

/**
 * aria-live summary of the accepted order (announced after each reorder).
 * Deterministic and mirrors the backend's "Try X, then Y." contract sentence
 * for the ordering part, without caps/rules details which get their own fields.
 */
export function summarizeOrder(order: string[]): string {
	if (!order.length) return 'No quality preference configured.';
	let text = `Try ${tierLabel(order[0])}`;
	for (let i = 1; i < order.length; i++) text += `, then ${tierLabel(order[i])}`;
	return `${text}.`;
}

/** Qualitative storage character shown BEFORE any search (never byte counts). */
export function presetStorageCharacter(key: PresetKey): string {
	if (key === 'best_available') return 'may be large';
	if (key === 'balanced') return 'CD-sized';
	if (key === 'efficient_192') return 'smaller';
	return 'smallest';
}

// ---------------------------------------------------------------------------
// Dirty tracking for the preset confirm modal
// ---------------------------------------------------------------------------

/** Canonical fingerprint of every field a preset would replace. */
export function presetFingerprint(fields: {
	quality_min: string;
	quality_max: string;
	quality_preference_order: string[];
	preferred_lossy_bitrate_kbps: number | null;
	lossless_preference: string;
	lossless_max_bit_depth: number | null;
	lossless_max_sample_rate_hz: number | null;
	unknown_quality_behavior: string;
}): string {
	return JSON.stringify([
		fields.quality_min,
		fields.quality_max,
		fields.quality_preference_order,
		fields.preferred_lossy_bitrate_kbps,
		fields.lossless_preference,
		fields.lossless_max_bit_depth,
		fields.lossless_max_sample_rate_hz,
		fields.unknown_quality_behavior
	]);
}
