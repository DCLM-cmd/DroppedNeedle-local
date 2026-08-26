<script lang="ts">
	import { QueryClientProvider } from '@tanstack/svelte-query';
	import { queryClient } from '$lib/queries/QueryClient';
	import SearchPage from './+page.svelte';

	interface Props {
		data: { query: string };
	}

	let { data }: Props = $props();
</script>

<QueryClientProvider client={queryClient}>
	<SearchPage {data} />
</QueryClientProvider>
