<script lang="ts">
	import { RefreshCw } from 'lucide-svelte';
	import AlbumIdentificationPanel from '$lib/components/library/AlbumIdentificationPanel.svelte';
	import { getLibraryAlbumDetailQuery } from '$lib/queries/library/LibraryQueries.svelte';
	import type { LibraryAlbumSummary } from '$lib/types';

	interface Props {
		album: LibraryAlbumSummary;
	}

	let { album }: Props = $props();
	const albumQuery = getLibraryAlbumDetailQuery(() => album.id);
</script>

{#if albumQuery.data}
	<AlbumIdentificationPanel album={albumQuery.data} className="btn btn-ghost btn-xs gap-1" />
{:else}
	<button
		class="btn btn-ghost btn-xs gap-1"
		disabled
		title={albumQuery.isError
			? 'Could not load local identification controls'
			: 'Loading local identification controls'}
	>
		<RefreshCw class="h-3.5 w-3.5 {albumQuery.isLoading ? 'animate-spin' : ''}" />
		Re-identify…
	</button>
{/if}
