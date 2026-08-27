<script lang="ts">
	import {
		getDownloadPolicyQuery,
		saveDownloadPolicy
	} from '$lib/queries/downloads/DownloadClientsQueries.svelte';
	import { toastStore } from '$lib/stores/toast';
	import { untrack } from 'svelte';

	import AdvancedBehaviorSection from './acquisition/AdvancedBehaviorSection.svelte';
	import QualityOrderSection from './acquisition/QualityOrderSection.svelte';
	import {
		customBitrateError,
		losslessCapError,
		presetFingerprint,
		rangeBetween,
		type AcquisitionPolicyFields,
		type FullDownloadPolicySettings
	} from './acquisition/qualityOrderModel';
	import SourceSelectionSection from './acquisition/SourceSelectionSection.svelte';
	import UpgradesSection from './acquisition/UpgradesSection.svelte';

	const policyQuery = getDownloadPolicyQuery();
	const save = saveDownloadPolicy();

	let qualityMin = $state('mp3_320');
	let qualityMax = $state('lossless');
	let qualityCutoff = $state('lossless');
	let upgradeAllowed = $state(false);
	let backgroundScan = $state(false);
	let preferenceOrder = $state<string[]>([]);
	let preferredLossyBitrateKbps = $state<number | null>(null);
	let lossyMinBitrateKbps = $state<number | null>(null);
	let lossyMaxBitrateKbps = $state<number | null>(null);
	let losslessPreference = $state('highest');
	let losslessMaxBitDepth = $state<number | null>(null);
	let losslessMaxSampleRateHz = $state<number | null>(null);
	let unknownQualityBehavior = $state('allow_as_fallback');
	let sourceSelectionMode = $state('source_first');
	let flacMp3Only = $state(true);
	let verifyDownloads = $state(true);
	let autoAccept = $state(0.7);
	let manualMin = $state(0.5);
	let maxConcurrent = $state(3);
	let maxFailover = $state(3);
	let preferredQualityWait = $state(15);
	let autoRetryEnabled = $state(true);
	let autoRetryMax = $state(6);
	let usenetMinAge = $state(30);

	// Fingerprint of preset-covered fields at load time; a modified form is
	// anything that drifts from it (drives the preset confirm modal).
	let baselineFingerprint = $state<string | null>(null);
	let seeded = $state(false);

	$effect(() => {
		const d = policyQuery.data as FullDownloadPolicySettings | undefined;
		if (!d || seeded) return;
		const acquired: AcquisitionPolicyFields = { ...defaultAcquiredFields(d), ...(d ?? {}) };
		if (acquired.quality_preference_order.length === 0) {
			acquired.quality_preference_order = rangeBetween(d.quality_min, d.quality_max);
		}
		untrack(() => {
			qualityMin = d.quality_min;
			qualityMax = d.quality_max;
			qualityCutoff = d.quality_cutoff;
			upgradeAllowed = d.upgrade_allowed;
			backgroundScan = d.background_upgrade_scan_enabled;
			preferenceOrder = [...acquired.quality_preference_order];
			preferredLossyBitrateKbps = acquired.preferred_lossy_bitrate_kbps;
			lossyMinBitrateKbps = acquired.lossy_min_bitrate_kbps;
			lossyMaxBitrateKbps = acquired.lossy_max_bitrate_kbps;
			losslessPreference = acquired.lossless_preference;
			losslessMaxBitDepth = acquired.lossless_max_bit_depth;
			losslessMaxSampleRateHz = acquired.lossless_max_sample_rate_hz;
			unknownQualityBehavior = acquired.unknown_quality_behavior;
			sourceSelectionMode = acquired.source_selection_mode;
			flacMp3Only = d.flac_mp3_only;
			verifyDownloads = d.verify_downloads;
			autoAccept = d.preflight_score_auto_accept;
			manualMin = d.preflight_score_manual_min;
			maxConcurrent = d.max_concurrent_downloads;
			maxFailover = d.max_failover_attempts;
			preferredQualityWait = d.preferred_quality_wait_minutes;
			autoRetryEnabled = d.auto_retry_enabled;
			autoRetryMax = d.auto_retry_max_attempts;
			usenetMinAge = d.usenet_min_release_age_minutes;
			baselineFingerprint = null;
			seeded = true;
		});
		baselineFingerprint = presetFingerprint({
			quality_min: qualityMin,
			quality_max: qualityMax,
			quality_preference_order: preferenceOrder,
			preferred_lossy_bitrate_kbps: preferredLossyBitrateKbps,
			lossless_preference: losslessPreference,
			lossless_max_bit_depth: losslessMaxBitDepth,
			lossless_max_sample_rate_hz: losslessMaxSampleRateHz,
			unknown_quality_behavior: unknownQualityBehavior
		});
	});

	function defaultAcquiredFields(d: FullDownloadPolicySettings): AcquisitionPolicyFields {
		return {
			...ACQUIRED_DEFAULTS,
			quality_preference_order:
				d.quality_preference_order && d.quality_preference_order.length > 0
					? [...d.quality_preference_order]
					: []
		};
	}

	const ACQUIRED_DEFAULTS: AcquisitionPolicyFields = {
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

	async function onSave() {
		const d = policyQuery.data;
		if (!d) return;

		const targetError = customBitrateError(preferredLossyBitrateKbps);
		const depthError = losslessCapError('bit_depth', losslessMaxBitDepth);
		const rateError = losslessCapError('sample_rate', losslessMaxSampleRateHz);
		if (targetError || depthError || rateError) {
			toastStore.show({ message: 'Fix invalid quality values before saving', type: 'error' });
			return;
		}

		const policy: FullDownloadPolicySettings = {
			...(d as FullDownloadPolicySettings),
			quality_min: qualityMin,
			quality_max: qualityMax,
			quality_cutoff: qualityCutoff,
			upgrade_allowed: upgradeAllowed,
			background_upgrade_scan_enabled: backgroundScan,
			flac_mp3_only: flacMp3Only,
			verify_downloads: verifyDownloads,
			preflight_score_auto_accept: autoAccept,
			preflight_score_manual_min: manualMin,
			max_concurrent_downloads: maxConcurrent,
			max_failover_attempts: maxFailover,
			preferred_quality_wait_minutes: preferredQualityWait,
			auto_retry_enabled: autoRetryEnabled,
			auto_retry_max_attempts: autoRetryMax,
			usenet_min_release_age_minutes: usenetMinAge,

			quality_preference_order: preferenceOrder.length
				? preferenceOrder
				: rangeBetween(qualityMin, qualityMax),
			preferred_lossy_bitrate_kbps: preferredLossyBitrateKbps,
			lossy_min_bitrate_kbps: lossyMinBitrateKbps,
			lossy_max_bitrate_kbps: lossyMaxBitrateKbps,
			lossless_preference: losslessPreference,
			lossless_max_bit_depth: losslessMaxBitDepth,
			lossless_max_sample_rate_hz: losslessMaxSampleRateHz,
			unknown_quality_behavior: unknownQualityBehavior,
			source_selection_mode: sourceSelectionMode
		};
		try {
			await save.mutateAsync(policy);
			toastStore.show({ message: 'Download policy saved', type: 'success' });
		} catch {
			toastStore.show({ message: 'Could not save download policy', type: 'error' });
		}
	}
</script>

<section class="card border border-base-300 bg-base-100">
	<div class="card-body gap-4">
		<div>
			<h3 class="font-semibold">Acquisition quality</h3>
			<p class="text-sm text-base-content/70">
				Shared by every source - quality, what auto-downloads vs needs review, and resilience.
			</p>
		</div>

		<QualityOrderSection
			bind:qualityMin
			bind:qualityMax
			bind:preferenceOrder
			bind:preferredLossyBitrateKbps
			bind:losslessPreference
			bind:losslessMaxBitDepth
			bind:losslessMaxSampleRateHz
			bind:unknownQualityBehavior
			bind:flacMp3Only
			baseline={baselineFingerprint}
		/>

		<div class="divider my-1"></div>

		<div>
			<h4 class="font-medium">Upgrades</h4>
			<UpgradesSection
				bind:upgradeAllowed
				bind:backgroundScan
				bind:qualityCutoff
				minTier={qualityMin}
				maxTier={qualityMax}
			/>
		</div>

		<div>
			<h4 class="font-medium">Source selection</h4>
			<SourceSelectionSection bind:sourceSelectionMode />
		</div>

		<AdvancedBehaviorSection
			bind:autoAccept
			bind:manualMin
			bind:maxConcurrent
			bind:maxFailover
			bind:preferredQualityWait
			bind:autoRetryEnabled
			bind:autoRetryMax
			bind:usenetMinAge
			bind:verifyDownloads
			bind:lossyMinBitrateKbps
			bind:lossyMaxBitrateKbps
		/>
		<div class="flex justify-end">
			<button
				type="button"
				class="btn btn-primary btn-sm"
				onclick={onSave}
				disabled={save.isPending}
			>
				Save
			</button>
		</div>
	</div>
</section>
