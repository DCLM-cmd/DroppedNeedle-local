<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { SvelteURLSearchParams } from 'svelte/reactivity';
	import LibraryReviewFilters from './LibraryReviewFilters.svelte';
	import LibraryReviewTable, { reviewReasonShortLabel } from './LibraryReviewTable.svelte';
	import LibraryReviewDetail from './LibraryReviewDetail.svelte';
	import LibraryBulkActionDialog from './LibraryBulkActionDialog.svelte';
	import { getLibraryReviewsQuery } from '$lib/queries/library/LibraryReviewQueries.svelte';
	import { getLibraryActivityQuery } from '$lib/queries/library/LibraryActivityQueries.svelte';
	import { getLibraryPolicyTreeQuery } from '$lib/queries/library/LibraryPolicyQueries.svelte';
	import type { LibraryReviewFilters as Filters } from '$lib/queries/library/LibraryReviewQueries.svelte';
	import type { BulkReviewAction } from '$lib/queries/library/LibraryOperationsTypes';
	import { authStore } from '$lib/stores/authStore.svelte';
	import { withBasePath } from '$lib/utils/basePath';

	const filters = $derived<Filters>({
		cursor: page.url.searchParams.get('cursor') ?? undefined,
		state:
			page.url.searchParams.get('state') === 'all'
				? undefined
				: (page.url.searchParams.get('state') ?? 'needs_review'),
		reasonCode: page.url.searchParams.get('reason') ?? undefined,
		rootId: page.url.searchParams.get('root') ?? undefined,
		policy: page.url.searchParams.get('policy') ?? undefined,
		search: page.url.searchParams.get('q') ?? undefined,
		sort: page.url.searchParams.get('sort') ?? 'newest',
		candidateAvailable: page.url.searchParams.get('candidates') === 'only' ? true : undefined,
		hideMatching: page.url.searchParams.get('matching') === 'hide' ? true : undefined
	});
	const query = getLibraryReviewsQuery(() => filters);
	const policyTree = getLibraryPolicyTreeQuery();
	// The first-run banner only needs a cached-or-live waiting count, so a missing
	// query provider (component specs) degrades to no banner instead of throwing.
	let activityQuery: ReturnType<typeof getLibraryActivityQuery> | null = null;
	try {
		activityQuery = getLibraryActivityQuery(() => authStore.user?.id);
	} catch {
		activityQuery = null;
	}
	const response = $derived(query.data?.pages[0]);
	const items = $derived(response?.items ?? []);
	const displayedItems = $derived(items);
	let selectedIds = $state<string[]>([]);
	let allMatching = $state(false);
	const reviewId = $derived(page.url.searchParams.get('review'));
	const selected = $derived(displayedItems.filter((item) => selectedIds.includes(item.id)));
	const rootLabels = $derived(
		Object.fromEntries((policyTree.data?.roots ?? []).map((root) => [root.id, root.label]))
	);
	const waitingCount = $derived(
		activityQuery?.data?.items.find((item) => item.kind === 'identification')?.waiting_count ?? 0
	);
	const reasonCounts = $derived(
		response?.counts_by_reason_filtered ?? response?.counts_by_reason ?? {}
	);
	const reasonCountsScoped = $derived(response?.counts_by_reason_filtered !== undefined);
	// N-02: per-state depths render from the scoped field, mirroring the reason
	// buckets. `counts_by_state` is a global GROUP BY with no WHERE, so it is
	// only the unscoped fallback (labelled "All-time totals" like reasons).
	const stateOrder = ['needs_review', 'edition_to_confirm', 'keep_tagged', 'excluded', 'resolved'];
	const stateLabels: Record<string, string> = {
		needs_review: 'Needs review',
		edition_to_confirm: 'Edition to confirm',
		keep_tagged: 'Keep as tagged',
		excluded: 'Excluded',
		resolved: 'Resolved'
	};
	const stateCounts = $derived(
		response?.counts_by_state_filtered ?? response?.counts_by_state ?? {}
	);
	const stateCountsScoped = $derived(response?.counts_by_state_filtered !== undefined);
	const stateEntries = $derived(
		Object.entries(stateCounts).sort(
			([first], [second]) => orderOfState(first) - orderOfState(second)
		)
	);

	function orderOfState(state: string): number {
		const index = stateOrder.indexOf(state);
		return index === -1 ? stateOrder.length : index;
	}

	function stateLabel(code: string): string {
		return stateLabels[code] ?? code.replaceAll('_', ' ');
	}
	const isConfirmLane = $derived(filters.state === 'edition_to_confirm');
	const confirmCount = $derived(stateCounts['edition_to_confirm'] ?? 0);
	// Above this many open editions the lane shows the full explainer banner;
	// below it a quiet one-liner suffices.
	const CONFIRM_BANNER_THRESHOLD = 25;
	// The scoped state counts collapse to the active lane (the counts query
	// carries the same state filter as the page), so the clear-queue link
	// reads the global all-time total instead - otherwise it is always 0 here.
	const resolvedCount = $derived(response?.counts_by_state?.['resolved'] ?? 0);
	const reasonEntries = $derived(
		Object.entries(reasonCounts)
			.filter(([code]) => isConfirmLane || code !== 'EDITION_UNCERTAIN')
			.sort((first, second) => second[1] - first[1])
	);
	let bulkNonce = $state(0);
	let bulkRequest = $state<{ action: BulkReviewAction; nonce: number } | null>(null);
	let bulkReason = $state<string | null>(null);
	// The reason-scoped bulk filter rides along until the URL navigation lands.
	$effect(() => {
		if (bulkReason !== null && filters.reasonCode === bulkReason) bulkReason = null;
	});
	const bulkFilters = $derived<Filters>(
		bulkReason ? { ...filters, reasonCode: bulkReason, cursor: undefined } : filters
	);
	// Filters beyond the lane itself: the clear-queue message must not claim
	// an empty lane when these merely match nothing (the table owns that
	// empty state instead).
	const hasAncillaryFilters = $derived(
		Boolean(
			filters.search ||
			filters.reasonCode ||
			filters.rootId ||
			filters.policy ||
			filters.candidateAvailable ||
			filters.hideMatching
		)
	);
	const filtered = $derived(Boolean(hasAncillaryFilters || filters.state !== 'needs_review'));

	function updateUrl(next: Filters): void {
		const params = new SvelteURLSearchParams();
		if (next.cursor) params.set('cursor', next.cursor);
		params.set('state', next.state ?? 'all');
		if (next.reasonCode) params.set('reason', next.reasonCode);
		if (next.rootId) params.set('root', next.rootId);
		if (next.policy) params.set('policy', next.policy);
		if (next.search) params.set('q', next.search);
		if (next.sort && next.sort !== 'newest') params.set('sort', next.sort);
		if (next.candidateAvailable) params.set('candidates', 'only');
		if (next.hideMatching) params.set('matching', 'hide');
		void goto(withBasePath(`/library/review${params.size ? `?${params.toString()}` : ''}`), {
			noScroll: true,
			keepFocus: true
		});
		selectedIds = [];
		allMatching = false;
	}

	function selectReason(code: string): void {
		updateUrl({
			...filters,
			reasonCode: filters.reasonCode === code ? undefined : code,
			cursor: undefined
		});
	}

	function selectState(code: string): void {
		updateUrl({
			...filters,
			state: filters.state === code ? undefined : code,
			cursor: undefined
		});
	}

	// Cross-lane jump from the clear-queue link: the count is an all-time
	// total, so the stale lane cursor is dropped (keeping sort) to land on
	// the first page of the resolved lane.
	function selectStateFresh(code: string): void {
		updateUrl({ state: code, sort: filters.sort, cursor: undefined });
	}

	function openBucketBulk(code: string, action: BulkReviewAction): void {
		updateUrl({ ...filters, reasonCode: code, cursor: undefined });
		selectedIds = [];
		allMatching = true;
		bulkReason = code;
		bulkRequest = { action, nonce: ++bulkNonce };
	}

	function clearAllFilters(): void {
		void goto(withBasePath('/library/review'), { noScroll: true, keepFocus: true });
		selectedIds = [];
		allMatching = false;
	}

	function openReview(id: string): void {
		const params = new SvelteURLSearchParams(page.url.searchParams);
		params.set('review', id);
		void goto(withBasePath(`/library/review?${params.toString()}`), {
			noScroll: true,
			keepFocus: true
		});
	}

	function closeReview(): void {
		const params = new SvelteURLSearchParams(page.url.searchParams);
		params.delete('review');
		void goto(withBasePath(`/library/review${params.size ? `?${params.toString()}` : ''}`), {
			noScroll: true,
			keepFocus: true,
			replaceState: true
		});
	}
</script>

<LibraryReviewFilters {filters} roots={policyTree.data?.roots ?? []} onchange={updateUrl} />

{#if query.isLoading}
	<div class="mt-4 space-y-2">
		<div class="skeleton h-16"></div>
		<div class="skeleton h-16"></div>
		<div class="skeleton h-16"></div>
	</div>
{:else if query.isError}
	<div class="alert alert-error mt-4">Could not load identification reviews.</div>
{:else}
	{#if (response?.filtered_total ?? 0) > 500 && waitingCount > 0}
		<div class="alert alert-info mt-4" role="status">
			<div>
				<strong>First scan in progress - large numbers are normal.</strong>
				<p class="text-sm">
					Files stay playable while matching runs. 1) Wait for Matching to drain 2) Bulk-keep rows
					with no result 3) Work conflicting or ambiguous rows.
				</p>
			</div>
		</div>
	{/if}
	{#if isConfirmLane && confirmCount > CONFIRM_BANNER_THRESHOLD}
		<div class="alert alert-info mt-4" role="status">
			<div>
				<strong>Edition to confirm - release group pinned, pressing unproven.</strong>
				<p class="text-sm">
					Title and artist matched; year, country and cover are not proven. Open a row to accept the
					exact edition or pick manually. These albums never count toward Needs review. Your files
					never change here - this only picks which pressing is shown.
				</p>
			</div>
		</div>
	{:else if isConfirmLane && confirmCount > 0}
		<p class="mt-4 text-sm text-base-content/55" role="status">
			{confirmCount.toLocaleString()}
			{confirmCount === 1 ? 'edition' : 'editions'} to confirm. Your files never change here - this only
			picks which pressing is shown.
		</p>
	{:else if isConfirmLane && response && !hasAncillaryFilters}
		<p class="mt-4 text-sm text-base-content/55" role="status">
			Edition queue is clear.
			{#if resolvedCount > 0}
				<button
					class="link link-primary"
					aria-label="View {resolvedCount.toLocaleString()} resolved reviews"
					onclick={() => selectStateFresh('resolved')}
					>{resolvedCount.toLocaleString()} resolved</button
				>
			{/if}
		</p>
	{/if}
	{#if stateEntries.length}
		<div
			class="mt-4 rounded-box border border-base-content/10 bg-base-100 p-3"
			aria-label="Review state depths"
		>
			<div class="flex flex-wrap items-center gap-2">
				<span class="text-sm font-medium">States</span>
				{#if !stateCountsScoped}<span class="text-xs text-base-content/55">All-time totals</span
					>{/if}
			</div>
			<ul class="mt-2 space-y-1.5">
				{#each stateEntries as [code, count] (code)}
					{@const active = filters.state === code}
					<li class="flex flex-wrap items-center gap-2">
						<button
							class="badge badge-lg {active ? 'badge-primary' : 'badge-outline'}"
							aria-pressed={active}
							onclick={() => selectState(code)}
							>{stateLabel(code)} · {count.toLocaleString()}</button
						>
					</li>
				{/each}
			</ul>
		</div>
	{/if}
	{#if reasonEntries.length}
		<div
			class="mt-4 rounded-box border border-base-content/10 bg-base-100 p-3"
			aria-label="Review reason buckets"
		>
			<div class="flex flex-wrap items-center gap-2">
				<span class="text-sm font-medium">Reasons</span>
				{#if !reasonCountsScoped}<span class="text-xs text-base-content/55">All-time totals</span
					>{/if}
			</div>
			<ul class="mt-2 space-y-1.5">
				{#each reasonEntries as [code, count] (code)}
					{@const active = filters.reasonCode === code}
					<li class="flex flex-wrap items-center gap-2">
						<button
							class="badge badge-lg {active ? 'badge-primary' : 'badge-outline'}"
							aria-pressed={active}
							onclick={() => selectReason(code)}
							>{reviewReasonShortLabel(code)} · {count.toLocaleString()}</button
						>
						{#if count > 0 && code !== 'EDITION_UNCERTAIN'}
							<button
								class="btn btn-ghost btn-xs"
								onclick={() => openBucketBulk(code, 'keep_tagged')}>Bulk keep...</button
							>
							<button class="btn btn-ghost btn-xs" onclick={() => openBucketBulk(code, 'retry')}
								>Bulk retry...</button
							>
						{/if}
					</li>
				{/each}
			</ul>
		</div>
	{/if}
	<div class="mt-4">
		{#if displayedItems.length}
			<div class="mb-3 flex flex-wrap items-center gap-2 text-sm">
				<button
					class="btn btn-ghost btn-sm"
					onclick={() => {
						selectedIds = displayedItems.map((item) => item.id);
						allMatching = false;
					}}>Select current page</button
				>
				{#if (response?.filtered_total ?? 0) > displayedItems.length}
					<button
						class="btn btn-ghost btn-sm"
						onclick={() => {
							selectedIds = [];
							allMatching = true;
						}}>Select all {(response?.filtered_total ?? 0).toLocaleString()} matching</button
					>
				{/if}
				{#if allMatching}<span class="badge badge-primary badge-outline"
						>Full filtered result selected</span
					>{/if}
			</div>
		{/if}
		<LibraryReviewTable
			items={displayedItems}
			{selectedIds}
			{filtered}
			state={filters.state}
			{rootLabels}
			{waitingCount}
			onclearfilters={clearAllFilters}
			onselectionchange={(ids) => {
				selectedIds = ids;
				allMatching = false;
			}}
			onreview={openReview}
		/>
	</div>
	{#if response}
		<div class="mt-4 flex items-center justify-between gap-3 text-sm">
			<span class="text-base-content/55"
				>{response.filtered_total.toLocaleString()}
				{isConfirmLane
					? 'editions to confirm'
					: filters.state === undefined
						? 'items'
						: 'review items'}</span
			>
			<div class="join">
				{#if filters.cursor}<button
						class="btn btn-sm join-item"
						onclick={() => updateUrl({ ...filters, cursor: undefined })}>First page</button
					>{/if}{#if response.next_cursor}<button
						class="btn btn-sm join-item"
						onclick={() => updateUrl({ ...filters, cursor: response.next_cursor ?? undefined })}
						>Next page</button
					>{/if}
			</div>
		</div>
		<LibraryBulkActionDialog
			{selected}
			{allMatching}
			matchingCount={response.filtered_total}
			filters={bulkFilters}
			catalogRevision={response.catalog_revision}
			bulkOpenRequest={bulkRequest}
			onbulkopened={() => (bulkRequest = null)}
			onclear={() => {
				selectedIds = [];
				allMatching = false;
			}}
		/>
	{/if}
{/if}

<LibraryReviewDetail {reviewId} onclose={closeReview} />
