<script lang="ts">
	import BaseImage from './BaseImage.svelte';

	interface Props {
		mbid: string;
		alt?: string;
		size?: 'xs' | 'sm' | 'md' | 'lg' | 'xl' | 'hero' | 'full';
		requestSize?: 250 | 500 | 1200;
		responsiveSizes?: string;
		lazy?: boolean;
		showPlaceholder?: boolean;
		className?: string;
		rounded?: 'none' | 'sm' | 'md' | 'lg' | 'xl' | 'full';
		remoteUrl?: string | null;
		onload?: () => void;
		source?: 'provider' | 'local';
		available?: boolean;
		testId?: string;
	}

	let {
		mbid,
		alt = 'Artist',
		size = 'md',
		requestSize = undefined,
		responsiveSizes = undefined,
		lazy = true,
		showPlaceholder = true,
		className = '',
		rounded = 'full',
		remoteUrl = null,
		onload = undefined,
		source = 'provider',
		available = true,
		testId = undefined
	}: Props = $props();
</script>

<BaseImage
	{mbid}
	{alt}
	{size}
	{requestSize}
	{responsiveSizes}
	{lazy}
	{showPlaceholder}
	{className}
	{rounded}
	{remoteUrl}
	{onload}
	{source}
	{available}
	{testId}
	imageType="artist"
/>
