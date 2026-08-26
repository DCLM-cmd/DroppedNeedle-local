<script lang="ts">
	import { ArrowRight, RefreshCw } from 'lucide-svelte';

	import { getLibraryAlbumDetailQuery } from '$lib/queries/library/LibraryQueries.svelte';
	import AlbumIdentificationPanel from './AlbumIdentificationPanel.svelte';

	interface Props {
		albumId: string;
		label?: 'Choose edition' | 'Re-identify';
	}

	let { albumId, label = 'Choose edition' }: Props = $props();
	let requested = $state(false);
	let opener = $state<HTMLButtonElement | null>(null);
	const albumQuery = getLibraryAlbumDetailQuery(() => (requested ? albumId : ''));

	function openWorkspace(event: MouseEvent & { currentTarget: HTMLButtonElement }): void {
		opener = event.currentTarget;
		requested = true;
	}

	function closeWorkspace(): void {
		requested = false;
		opener?.focus();
		opener = null;
	}
</script>

<button
	class="btn btn-ghost btn-xs"
	disabled={requested && albumQuery.isLoading}
	onclick={openWorkspace}
>
	{#if requested && albumQuery.isLoading}<span class="loading loading-spinner loading-xs"></span> Loading…{:else if label === 'Re-identify'}<RefreshCw
			class="h-3.5 w-3.5"
		/>
		Re-identify{:else}Choose edition <ArrowRight class="h-3.5 w-3.5" />{/if}
</button>

{#if requested && albumQuery.isError}
	<span class="text-xs text-error" role="status">Could not open this identity workspace.</span>
{/if}

{#if requested && albumQuery.data}
	<AlbumIdentificationPanel
		album={albumQuery.data}
		autoOpen
		showTrigger={false}
		onclose={closeWorkspace}
	/>
{/if}
