<script lang="ts">
	interface Props {
		autoAccept: number;
		manualMin: number;
		maxConcurrent: number;
		maxFailover: number;
		preferredQualityWait: number;
		autoRetryEnabled: boolean;
		autoRetryMax: number;
		usenetMinAge: number;
		verifyDownloads: boolean;
		lossyMinBitrateKbps: number | null;
		lossyMaxBitrateKbps: number | null;
	}

	let {
		autoAccept = $bindable(),
		manualMin = $bindable(),
		maxConcurrent = $bindable(),
		maxFailover = $bindable(),
		preferredQualityWait = $bindable(),
		autoRetryEnabled = $bindable(),
		autoRetryMax = $bindable(),
		usenetMinAge = $bindable(),
		verifyDownloads = $bindable(),
		lossyMinBitrateKbps = $bindable(),
		lossyMaxBitrateKbps = $bindable()
	}: Props = $props();

	function nullableFrom(raw: string): number | null {
		if (raw === '') return null;
		const n = Number(raw);
		return Number.isFinite(n) ? n : null;
	}

	const boundsEqual = $derived(
		lossyMinBitrateKbps != null &&
			lossyMaxBitrateKbps != null &&
			lossyMinBitrateKbps === lossyMaxBitrateKbps
	);
</script>

<details class="collapse collapse-arrow border border-base-300 bg-base-200/40">
	<summary class="collapse-title text-sm font-medium">Download behavior (advanced)</summary>
	<div class="collapse-content flex flex-col gap-4">
		<fieldset class="fieldset">
			<legend class="fieldset-legend">Lossy rejection bounds</legend>
			<div class="grid gap-3 sm:grid-cols-2">
				<label class="form-control">
					<span class="label-text">Reject lossy below (kbps)</span>
					<input
						type="number"
						class="input input-bordered input-sm"
						min="16"
						max="2048"
						placeholder="none"
						aria-label="Reject lossy audio below this bitrate in kbps"
						value={lossyMinBitrateKbps ?? ''}
						onchange={(e) => (lossyMinBitrateKbps = nullableFrom(e.currentTarget.value))}
					/>
				</label>
				<label class="form-control">
					<span class="label-text">Reject lossy above (kbps)</span>
					<input
						type="number"
						class="input input-bordered input-sm"
						min="16"
						max="2048"
						placeholder="none"
						aria-label="Reject lossy audio above this bitrate in kbps"
						value={lossyMaxBitrateKbps ?? ''}
						onchange={(e) => (lossyMaxBitrateKbps = nullableFrom(e.currentTarget.value))}
					/>
				</label>
			</div>
			<p class="text-xs text-base-content/60">
				These are rejection bounds, not targets. Copies outside them are never taken, even as
				fallback.
			</p>
			{#if boundsEqual}
				<p class="text-warning text-xs" role="alert">
					Minimum and maximum are equal: VBR files report an average bitrate, so exactly-bounded
					rejection may miss or mis-accept variable-rate copies.
				</p>
			{/if}
		</fieldset>

		<div class="grid gap-4 sm:grid-cols-2">
			<label class="form-control">
				<span class="label-text">Auto-accept score (&ge;)</span>
				<input
					type="number"
					step="0.05"
					min="0"
					max="1"
					class="input input-bordered input-sm"
					bind:value={autoAccept}
				/>
			</label>
			<label class="form-control">
				<span class="label-text">Manual-review score (&ge;)</span>
				<input
					type="number"
					step="0.05"
					min="0"
					max="1"
					class="input input-bordered input-sm"
					bind:value={manualMin}
				/>
			</label>
			<label class="form-control">
				<span class="label-text">Preferred-quality queue wait (min)</span>
				<input
					type="number"
					min="1"
					max="1440"
					class="input input-bordered input-sm"
					bind:value={preferredQualityWait}
				/>
				<span class="mt-1 text-xs text-base-content/55">
					After this zero-byte wait, try the best lower accepted quality.
				</span>
			</label>
			<label class="form-control">
				<span class="label-text">Max concurrent downloads</span>
				<input
					type="number"
					min="1"
					max="10"
					class="input input-bordered input-sm"
					bind:value={maxConcurrent}
				/>
			</label>
			<label class="form-control">
				<span class="label-text">Max failover attempts</span>
				<input
					type="number"
					min="1"
					max="10"
					class="input input-bordered input-sm"
					bind:value={maxFailover}
				/>
			</label>
			<label class="form-control">
				<span class="label-text">Auto-retry attempts</span>
				<input
					type="number"
					min="0"
					max="20"
					class="input input-bordered input-sm"
					bind:value={autoRetryMax}
					disabled={!autoRetryEnabled}
				/>
			</label>
			<label class="form-control">
				<span class="label-text">Usenet release age before blocklisting (min)</span>
				<input
					type="number"
					min="0"
					max="1440"
					class="input input-bordered input-sm"
					bind:value={usenetMinAge}
				/>
			</label>
		</div>

		<label class="label cursor-pointer justify-start gap-3">
			<input
				type="checkbox"
				class="toggle toggle-sm toggle-primary"
				bind:checked={verifyDownloads}
			/>
			<span class="label-text">Verify downloads (AcoustID release-group check)</span>
		</label>
		<label class="label cursor-pointer justify-start gap-3 p-0">
			<input
				type="checkbox"
				class="toggle toggle-sm toggle-primary"
				bind:checked={autoRetryEnabled}
			/>
			<span class="label-text">Auto-retry failed downloads</span>
		</label>
	</div>
</details>
