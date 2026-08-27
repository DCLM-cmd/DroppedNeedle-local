<script lang="ts">
	import QualityOrderEditor from './QualityOrderEditor.svelte';
	import {
		customBitrateError,
		LOSSLESS_PREFERENCE_OPTIONS,
		losslessCapError,
		presetFingerprint,
		PRESETS,
		PRESET_KEYS,
		rangeBetween,
		type PresetKey
	} from './qualityOrderModel';
	import { UNKNOWN_BEHAVIOR_OPTIONS } from './qualityOrderModel';

	interface Props {
		qualityMin: string;
		qualityMax: string;
		preferenceOrder: string[];
		preferredLossyBitrateKbps: number | null;
		losslessPreference: string;
		losslessMaxBitDepth: number | null;
		losslessMaxSampleRateHz: number | null;
		unknownQualityBehavior: string;
		flacMp3Only: boolean;
		baseline?: string | null;
	}

	let {
		qualityMin = $bindable(),
		qualityMax = $bindable(),
		preferenceOrder = $bindable(),
		preferredLossyBitrateKbps = $bindable(),
		losslessPreference = $bindable(),
		losslessMaxBitDepth = $bindable(),
		losslessMaxSampleRateHz = $bindable(),
		unknownQualityBehavior = $bindable(),
		flacMp3Only = $bindable(),
		baseline = null
	}: Props = $props();

	const LOSSY_TARGET_OPTIONS = [128, 192, 256, 320] as const;

	// --- Preset application -------------------------------------------------
	let pendingPresetKey = $state<PresetKey | null>(null);
	const pendingLabel = $derived(pendingPresetKey ? PRESETS[pendingPresetKey].label : '');
	let confirmDialog = $state<HTMLDialogElement | null>(null);
	let presetTrigger = $state<HTMLElement | null>(null);

	const currentFingerprint = $derived(
		presetFingerprint({
			quality_min: qualityMin,
			quality_max: qualityMax,
			quality_preference_order: preferenceOrder,
			preferred_lossy_bitrate_kbps: preferredLossyBitrateKbps,
			lossless_preference: losslessPreference,
			lossless_max_bit_depth: losslessMaxBitDepth,
			lossless_max_sample_rate_hz: losslessMaxSampleRateHz,
			unknown_quality_behavior: unknownQualityBehavior
		})
	);

	const dirty = $derived(baseline != null && baseline !== currentFingerprint);

	function requestPreset(key: PresetKey, e: MouseEvent) {
		if (!dirty) {
			applyPreset(key);
			return;
		}
		presetTrigger = e.currentTarget instanceof HTMLElement ? e.currentTarget : null;
		pendingPresetKey = key;
		confirmDialog?.showModal();
	}

	function applyPreset(key: PresetKey) {
		const fill = PRESETS[key];
		qualityMin = fill.quality_min;
		qualityMax = fill.quality_max;
		preferenceOrder = [...fill.quality_preference_order];
		preferredLossyBitrateKbps = fill.preferred_lossy_bitrate_kbps;
		losslessPreference = fill.lossless_preference;
		losslessMaxBitDepth = fill.lossless_max_bit_depth;
		losslessMaxSampleRateHz = fill.lossless_max_sample_rate_hz;
		unknownQualityBehavior = fill.unknown_quality_behavior;
	}

	function confirmPreset() {
		if (pendingPresetKey) applyPreset(pendingPresetKey);
		confirmDialog?.close();
	}

	// Native dialog close path (Cancel / Esc / backdrop): the form was never
	// touched while open, and focus returns to the preset trigger that opened it.
	function onDialogClose() {
		pendingPresetKey = null;
		presetTrigger?.focus();
		presetTrigger = null;
	}

	// --- Editor change callback --------------------------------------------
	function applyEditorChange(next: { order: string[]; quality_min: string; quality_max: string }) {
		preferenceOrder = next.order;
		qualityMin = next.quality_min;
		qualityMax = next.quality_max;
	}

	// --- Lossy target --------------------------------------------------------
	let customTargetDraft = $state<string>('');

	const targetError = $derived(customBitrateError(preferredLossyBitrateKbps));

	function selectTarget(kbps: number) {
		customTargetDraft = '';
		preferredLossyBitrateKbps = kbps;
	}

	function commitCustomTarget() {
		if (customTargetDraft === '') return;
		const parsed = Number(customTargetDraft);
		if (!customBitrateError(parsed)) preferredLossyBitrateKbps = parsed;
	}

	// --- Lossless detail -----------------------------------------------------
	const depthError = $derived(losslessCapError('bit_depth', losslessMaxBitDepth));
	const rateError = $derived(losslessCapError('sample_rate', losslessMaxSampleRateHz));

	function parseCapInput(raw: string): number | null {
		if (raw === '') return null;
		const n = Number(raw);
		return Number.isFinite(n) ? n : null;
	}

	const detailHint = $derived(
		losslessPreference === 'highest'
			? null
			: (LOSSLESS_PREFERENCE_OPTIONS.find((o) => o.value === losslessPreference)?.label ?? null)
	);
</script>

<div class="flex flex-col gap-4">
	<!-- Presets -->
	<fieldset class="fieldset">
		<legend class="fieldset-legend">Presets</legend>
		<div class="flex flex-wrap items-center gap-1.5" role="group" aria-label="Quality presets">
			{#each PRESET_KEYS as key (key)}
				<button
					type="button"
					class="btn btn-sm"
					data-preset={key}
					onclick={(e) => requestPreset(key, e)}
					aria-label={`Apply ${PRESETS[key].label} preset`}
				>
					{PRESETS[key].label}
				</button>
			{/each}
		</div>
	</fieldset>

	<!-- Accepted quality and order -->
	<fieldset class="fieldset">
		<legend class="fieldset-legend">Accepted quality and order</legend>
		<QualityOrderEditor
			order={preferenceOrder.length > 0 ? preferenceOrder : rangeBetween(qualityMin, qualityMax)}
			minTier={qualityMin}
			maxTier={qualityMax}
			targetKbps={preferredLossyBitrateKbps}
			losslessDetailHint={detailHint}
			onchange={applyEditorChange}
		/>
	</fieldset>

	<!-- Preferred lossy target -->
	<fieldset class="fieldset">
		<div
			class="flex flex-wrap items-center gap-1.5"
			role="radiogroup"
			aria-label="Preferred lossy bitrate in kbps"
		>
			{#each LOSSY_TARGET_OPTIONS as kbps (kbps)}
				<button
					type="button"
					class="btn btn-xs"
					aria-pressed={preferredLossyBitrateKbps === kbps && customTargetDraft === ''}
					onclick={() => selectTarget(kbps)}
				>
					{kbps}
				</button>
			{/each}
			<label class="input input-bordered input-xs flex items-center gap-1 max-w-36">
				<span class="text-xs text-base-content/55">Custom</span>
				<input
					type="number"
					class="w-16 bg-transparent outline-none"
					min="1"
					max="2048"
					step="1"
					placeholder="kbps"
					aria-label="Custom preferred lossy bitrate in kbps"
					bind:value={customTargetDraft}
					onblur={commitCustomTarget}
					onchange={commitCustomTarget}
				/>
			</label>
		</div>
		{#if targetError}
			<p class="mt-1 text-error text-xs" role="alert">{targetError}</p>
		{/if}
	</fieldset>

	<!-- Lossless resolution -->
	<fieldset class="fieldset">
		<legend class="fieldset-legend">Lossless resolution</legend>
		<div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
			<label class="form-control">
				<span class="label-text">Detail preference</span>
				<select class="select select-bordered select-sm" bind:value={losslessPreference}>
					{#each LOSSLESS_PREFERENCE_OPTIONS as opt (opt.value)}
						<option value={opt.value}>{opt.label}</option>
					{/each}
				</select>
			</label>
			<label class="form-control">
				<span class="label-text">Maximum bit depth</span>
				<input
					type="number"
					class="input input-bordered input-sm"
					min="1"
					max="64"
					placeholder="none"
					aria-label="Maximum lossless bit depth"
					value={losslessMaxBitDepth ?? ''}
					onchange={(e) => (losslessMaxBitDepth = parseCapInput(e.currentTarget.value))}
				/>
				{#if depthError}<p class="text-error text-xs" role="alert">{depthError}</p>{/if}
			</label>
			<label class="form-control">
				<span class="label-text">Maximum sample rate (Hz)</span>
				<input
					type="number"
					class="input input-bordered input-sm"
					min="8000"
					max="768000"
					step="1000"
					placeholder="none"
					aria-label="Maximum lossless sample rate in hertz"
					value={losslessMaxSampleRateHz ?? ''}
					onchange={(e) => (losslessMaxSampleRateHz = parseCapInput(e.currentTarget.value))}
				/>
				{#if rateError}<p class="text-error text-xs" role="alert">{rateError}</p>{/if}
			</label>
			<label class="form-control max-w-xs">
				<span class="label-text">Unknown-quality audio</span>
				<select class="select select-bordered select-sm" bind:value={unknownQualityBehavior}>
					{#each UNKNOWN_BEHAVIOR_OPTIONS as opt (opt.value)}
						<option value={opt.value}>{opt.label}</option>
					{/each}
				</select>
			</label>
		</div>
	</fieldset>

	<!-- Formats -->
	<label class="label cursor-pointer justify-start gap-3">
		<input type="checkbox" class="toggle toggle-sm toggle-primary" bind:checked={flacMp3Only} />
		<span class="label-text">Only accept FLAC and MP3</span>
	</label>

	<!-- Preset-over-modified-order confirm (house native modal) -->
	<dialog class="modal" bind:this={confirmDialog} onclose={onDialogClose}>
		<div class="modal-box">
			<h3 class="font-semibold">Replace this quality order?</h3>
			<p class="mt-2 text-sm text-base-content/70">
				You modified the accepted quality or order. Applying
				<strong>{pendingLabel}</strong>&nbsp;replaces it with the preset definition.
			</p>
			<div class="modal-action">
				<form method="dialog"><button class="btn btn-ghost">Cancel</button></form>
				<button type="button" class="btn btn-primary" onclick={confirmPreset}>
					Apply preset
				</button>
			</div>
		</div>
		<form method="dialog" class="modal-backdrop"><button>close</button></form>
	</dialog>
</div>
