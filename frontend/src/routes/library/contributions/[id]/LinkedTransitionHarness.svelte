<script lang="ts">
	import { QueryClientProvider } from '@tanstack/svelte-query';
	import { queryClient } from '$lib/queries/QueryClient';
	import ContributionPage from './+page.svelte';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
</script>

<QueryClientProvider client={queryClient}>
	<ContributionPage {data} />
</QueryClientProvider>
