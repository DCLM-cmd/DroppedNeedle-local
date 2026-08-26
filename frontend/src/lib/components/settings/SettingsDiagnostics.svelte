<script lang="ts">
	import { Activity, ArrowUpRight, Gauge } from 'lucide-svelte';

	import {
		getProviderStatsQuery,
		getQueueStatsQuery
	} from '$lib/queries/diagnostics/DiagnosticsQueries.svelte';

	import {
		buildQueueLanes,
		formatCount,
		groupProviderRows,
		isDiagnosticsPollingEnabled
	} from './diagnosticsDisplay';

	// The section mounts only while its settings tab is active; document
	// visibility is mirrored into state so both query `enabled` flags flip
	// without a remount and a hidden window stops issuing requests.
	let sectionVisible = $state(
		typeof document === 'undefined' || document.visibilityState === 'visible'
	);

	function handleVisibilityChange(): void {
		sectionVisible = document.visibilityState === 'visible';
	}

	const queueQuery = getQueueStatsQuery(() => isDiagnosticsPollingEnabled(sectionVisible));
	const providerQuery = getProviderStatsQuery(() => isDiagnosticsPollingEnabled(sectionVisible));

	const lanes = $derived(queueQuery.data ? buildQueueLanes(queueQuery.data.stats) : []);
	const providerGroups = $derived(groupProviderRows(providerQuery.data?.providers ?? []));
	const providerTableRows = $derived(
		providerGroups.flatMap((group) =>
			group.rows.map((row, index) => ({
				key: `${group.provider}:${row.lane}:${row.outcome}`,
				providerCell: index === 0 ? group.label : '',
				laneText: row.laneText,
				outcomeText: row.outcomeText,
				countTotal: row.countTotal,
				ratePerMinText: row.ratePerMinText
			}))
		)
	);
	const providersEmpty = $derived(
		!providerQuery.isLoading && !providerQuery.error && providerTableRows.length === 0
	);
</script>

<svelte:window onvisibilitychange={handleVisibilityChange} />

<div class="card bg-base-200">
	<div class="card-body">
		<div class="flex items-center gap-2">
			<Activity class="h-5 w-5 text-primary" aria-hidden="true" />
			<h2 class="card-title">Diagnostics</h2>
		</div>
		<p class="text-sm text-base-content/60">
			Live gauges from the backend process. Counters are held in memory: they reset whenever the
			server restarts, and they describe this single worker process only. Both panels refresh every
			five seconds while you have this page open.
		</p>

		<section class="mt-2 space-y-3" aria-label="Outbound request queues">
			<div class="flex items-center gap-2">
				<Gauge class="h-4 w-4 text-base-content/60" aria-hidden="true" />
				<h3 class="font-semibold">Outbound request queues</h3>
			</div>
			{#if queueQuery.isLoading}
				<div class="grid gap-3 sm:grid-cols-3" aria-busy="true" aria-label="Loading queue gauges">
					<div class="skeleton h-24 rounded-2xl"></div>
					<div class="skeleton h-24 rounded-2xl"></div>
					<div class="skeleton h-24 rounded-2xl"></div>
				</div>
			{:else if queueQuery.error}
				<div class="alert alert-error" role="alert">
					<span>Couldn't load queue stats.</span>
				</div>
			{:else}
				<div class="grid gap-3 sm:grid-cols-3">
					{#each lanes as lane (lane.key)}
						<div class="rounded-2xl border border-base-content/10 bg-base-100/60 p-4">
							<p class="text-xs font-semibold tracking-wide text-base-content/50 uppercase">
								{lane.label}
							</p>
							<p class="mt-1 flex items-baseline gap-1.5">
								<span class="text-2xl font-bold tabular-nums">{lane.slotsAvailable}</span>
								<span class="text-sm text-base-content/50">slots free</span>
							</p>
							{#if lane.active !== null}
								<p class="mt-0.5 text-xs text-base-content/55">
									{lane.active ? 'In use now' : 'Idle'}
								</p>
							{/if}
							{#if lane.waiting !== null}
								<p class="mt-0.5 text-xs text-base-content/55">{lane.waiting} waiting for a slot</p>
							{/if}
						</div>
					{/each}
				</div>
			{/if}
		</section>

		<section class="space-y-3" aria-label="Outbound provider calls">
			<div class="flex items-center gap-2">
				<ArrowUpRight class="h-4 w-4 text-base-content/60" aria-hidden="true" />
				<h3 class="font-semibold">Outbound provider calls</h3>
			</div>
			{#if providerQuery.isLoading}
				<div
					class="skeleton h-40 w-full rounded-2xl"
					aria-busy="true"
					aria-label="Loading provider stats"
				></div>
			{:else if providerQuery.error}
				<div class="alert alert-error" role="alert">
					<span>Couldn't load provider stats.</span>
				</div>
			{:else if providersEmpty}
				<p class="text-sm text-base-content/50">
					No outbound provider calls counted since process start.
				</p>
			{:else}
				<div class="overflow-x-auto">
					<table class="table table-sm">
						<thead>
							<tr>
								<th scope="col">Provider</th>
								<th scope="col">Lane</th>
								<th scope="col">Outcome</th>
								<th scope="col" class="text-right">Calls</th>
								<th scope="col" class="text-right">Calls/min</th>
							</tr>
						</thead>
						<tbody>
							{#each providerTableRows as row (row.key)}
								<tr>
									<td>{row.providerCell}</td>
									<td>{row.laneText}</td>
									<td>{row.outcomeText}</td>
									<td class="text-right tabular-nums">{formatCount(row.countTotal)}</td>
									<td class="text-right tabular-nums">{row.ratePerMinText}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</section>
	</div>
</div>
