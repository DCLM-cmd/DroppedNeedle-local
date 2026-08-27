<script lang="ts">
	import { ChevronDown, ChevronUp } from 'lucide-svelte';

	import {
		recomputeEndpoints,
		summarizeOrder,
		tierIndex,
		tierLabel,
		toggleTierInclusion
	} from './qualityOrderModel';

	import { QUALITY_TIERS } from '../qualityTiers';

	/**
	 * Five-row ordered track-list over policy.quality_preference_order.
	 *
	 * Wire invariant (frozen API contract): order[0] === quality_max and
	 * order[order.length - 1] === quality_min. Reordering rewrites the hidden
	 * endpoints to follow the array ends; inclusion toggles move ONLY the
	 * endpoints (quality_min/quality_max) via toggleTierInclusion - a middle
	 * tier can never be omitted alone.
	 */
	interface Props {
		order: string[];
		minTier: string;
		maxTier: string;
		targetKbps?: number | null;
		losslessDetailHint?: string | null;
		disabled?: boolean;
		onchange?: (
			next: { order: string[]; quality_min: string; quality_max: string },
			kind: 'move' | 'include'
		) => void;
	}

	let {
		order,
		minTier,
		maxTier,
		targetKbps = null,
		losslessDetailHint = null,
		disabled = false,
		onchange
	}: Props = $props();

	const TIER_KEYS = QUALITY_TIERS.map((t) => t.key);
	const includedSet = $derived(new Set(order));
	const excluded = $derived(TIER_KEYS.filter((k) => !includedSet.has(k)));
	const liveSummary = $derived(summarizeOrder(order));

	function applyNext(
		next: { order: string[]; quality_min: string; quality_max: string } | null,
		kind: 'move' | 'include'
	) {
		if (!next || disabled || !onchange) return;
		onchange(next, kind);
	}

	function move(from: number, to: number) {
		if (to < 0 || to >= order.length) return;
		const nextOrder = [...order];
		const [moved] = nextOrder.splice(from, 1);
		nextOrder.splice(to, 0, moved);
		applyNext({ order: nextOrder, ...recomputeEndpoints(nextOrder) }, 'move');
	}

	function toggle(tier: string) {
		applyNext(toggleTierInclusion(order, tier), 'include');
	}

	function canAdd(tier: string): boolean {
		return toggleTierInclusion(order, tier) !== null;
	}

	// Row annotation: the lossy target sits next to its tier, the lossless
	// detail preference next to the lossless row.
	function annotation(tier: string): string | null {
		if (tier === 'lossless') return losslessDetailHint;
		if (targetKbps != null && tierIndex(tier) <= tierIndex('mp3_320')) {
			return `target ${targetKbps} kbps`;
		}
		return null;
	}
</script>

<div data-motion="acq-order">
	<p class="sr-only" role="status" aria-live="polite">{liveSummary}</p>

	<ol class="acq-order-list space-y-1.5">
		{#each order as tier, index (tier)}
			<li
				class="acq-order-row flex flex-wrap items-center gap-2 rounded-box border border-base-300 bg-base-100 p-2"
				data-tier={tier}
				role="listitem"
			>
				<span
					class="grid size-7 shrink-0 place-items-center rounded-lg bg-base-300/60 text-sm font-semibold tabular-nums text-base-content/80"
					aria-hidden="true"
				>
					{index + 1}
				</span>
				<span class="min-w-0 flex-1 font-medium">{tierLabel(tier)}</span>
				{#if annotation(tier)}
					<span class="text-xs text-base-content/55">{annotation(tier)}</span>
				{/if}
				<button
					type="button"
					class="btn btn-ghost btn-xs btn-square"
					disabled={disabled || index === 0}
					aria-label={`Move ${tierLabel(tier)} to position ${index}`}
					onclick={() => move(index, index - 1)}
				>
					<ChevronUp class="size-4" aria-hidden="true" />
				</button>
				<button
					type="button"
					class="btn btn-ghost btn-xs btn-square"
					disabled={disabled || index === order.length - 1}
					aria-label={`Move ${tierLabel(tier)} to position ${index + 2}`}
					onclick={() => move(index, index + 1)}
				>
					<ChevronDown class="size-4" aria-hidden="true" />
				</button>
				{#if order.length > 1 && (tier === minTier || tier === maxTier)}
					<button
						type="button"
						class="btn btn-outline btn-error btn-xs"
						{disabled}
						aria-label={`Remove ${tierLabel(tier)} from accepted range`}
						onclick={() => toggle(tier)}
					>
						remove
					</button>
				{/if}
			</li>
		{/each}
	</ol>

	{#if excluded.length > 0}
		<ul class="mt-2 space-y-1.5 border-t border-dashed border-base-300 pt-2">
			{#each excluded as tier (tier)}
				<li
					class="acq-order-row acq-order-row--excluded flex items-center gap-2 rounded-box bg-base-200/50 px-2 py-1.5 text-base-content/55"
					data-tier={tier}
					role="listitem"
				>
					<span class="min-w-0 flex-1 font-medium">{tierLabel(tier)}</span>
					<span class="text-xs">not accepted</span>
					<button
						type="button"
						class="btn btn-outline btn-success btn-xs"
						disabled={disabled || !canAdd(tier)}
						aria-label={`Add ${tierLabel(tier)} to accepted range`}
						onclick={() => toggle(tier)}
					>
						add
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</div>
