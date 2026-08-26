<script lang="ts">
	import type { LibraryWorkItem } from '$lib/queries/library/LibraryOperationsTypes';
	import {
		libraryWorkPercentage,
		libraryWorkPhase,
		libraryWorkProgress
	} from './LibraryWorkPresentation';

	interface Props {
		item: LibraryWorkItem;
		compact?: boolean;
	}

	let { item, compact = false }: Props = $props();
	const percentage = $derived(libraryWorkPercentage(item));
	const progress = $derived(libraryWorkProgress(item));
	const phase = $derived(libraryWorkPhase(item));
</script>

<div
	class="library-live-progress"
	class:library-live-progress--compact={compact}
	data-effect={item.effect}
>
	<div class="library-live-progress__copy">
		<span>{phase}</span>
		<strong>{progress}{percentage !== null ? ` · ${percentage}%` : ''}</strong>
	</div>
	<div
		class="library-live-progress__track"
		class:library-live-progress__track--indeterminate={percentage === null &&
			item.state !== 'paused' &&
			item.state !== 'failed'}
		class:library-live-progress__track--paused={item.state === 'paused'}
		role="progressbar"
		aria-label={`${phase}: ${progress}`}
		aria-valuemin={percentage !== null ? 0 : undefined}
		aria-valuemax={percentage !== null ? 100 : undefined}
		aria-valuenow={percentage ?? undefined}
		aria-valuetext={progress}
	>
		{#if percentage !== null}
			<span
				class="library-live-progress__fill"
				data-testid="library-work-progress-fill"
				style={`width: ${percentage}%`}
			></span>
		{/if}
	</div>
</div>
