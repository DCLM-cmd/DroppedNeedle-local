<script lang="ts">
	interface Props {
		loadedCount: number;
		totalCount?: number | null;
		loading?: boolean;
		onloadmore: () => void;
	}

	let { loadedCount, totalCount = null, loading = false, onloadmore }: Props = $props();
</script>

<div
	class="flex flex-col items-center justify-between gap-3 rounded-box border border-base-content/10 bg-base-200/60 p-4 sm:flex-row"
>
	<p id="artist-release-progress" class="text-sm text-base-content/60" aria-live="polite">
		{#if totalCount}
			{loadedCount} of {totalCount} releases loaded
		{:else}
			{loadedCount} releases loaded
		{/if}
	</p>
	<button
		class="btn btn-accent btn-sm min-w-40"
		type="button"
		aria-label="Load more releases"
		aria-describedby="artist-release-progress"
		disabled={loading}
		onclick={onloadmore}
	>
		{#if loading}
			<span class="loading loading-spinner loading-xs" aria-hidden="true"></span>
			Loading releases…
		{:else}
			Load more releases
		{/if}
	</button>
</div>
