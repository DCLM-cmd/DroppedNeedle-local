<script lang="ts">
	import { getApiUrl } from '$lib/api/api-utils';
	interface CacheStats {
		memory_entries: number;
		memory_size_bytes: number;
		memory_size_mb: number;
		memory_accounting: 'shallow';
		response_entries: number;
		response_logical_bytes: number;
		response_hits: number;
		response_evictions: number;
		response_speculative_used: number;
		database_allocated_bytes: number;
		database_wal_bytes: number;
		disk_metadata_count: number;
		disk_metadata_albums: number;
		disk_metadata_artists: number;
		disk_cover_count: number;
		disk_cover_size_bytes: number;
		disk_cover_size_mb: number;
		library_db_artist_count: number;
		library_db_album_count: number;
		library_db_size_bytes: number;
		library_db_size_mb: number;
		total_size_bytes: number;
		total_size_mb: number;
		library_db_last_sync: number | null;
		disk_audiodb_artist_count: number;
		disk_audiodb_album_count: number;
		memory_hits: number;
		memory_misses: number;
		memory_hit_rate_percent: number;
		per_prefix: {
			prefix: string;
			hits: number;
			misses: number;
			sets: number;
			hit_rate_percent: number;
			window_seconds: number;
		}[];
		counters_since: number | null;
	}

	interface CacheClearResponse {
		success: boolean;
		message: string;
		cleared_memory_entries: number;
		cleared_disk_files: number;
		cleared_response_entries: number;
		cleared_library_artists: number;
		cleared_library_albums: number;
		cover_files_cleared: number;
	}

	const megabytes = (bytes: number) => (bytes / (1024 * 1024)).toFixed(2);

	let cacheStats: CacheStats | null = $state(null);
	let loading = $state(false);
	let clearing = $state(false);
	let message = $state('');
	let needsAdmin = $state(false);

	export async function load() {
		loading = true;
		message = '';
		needsAdmin = false;
		try {
			const response = await fetch(getApiUrl('/api/v1/cache/stats'));
			if (response.ok) {
				cacheStats = await response.json();
			} else if (response.status === 401 || response.status === 403) {
				needsAdmin = true;
			} else {
				message = "Couldn't load cache stats";
			}
		} catch {
			message = "Couldn't load cache stats";
		} finally {
			loading = false;
		}
	}

	async function clearCache(
		type: 'all' | 'memory' | 'metadata' | 'library' | 'covers' | 'audiodb'
	) {
		const typeLabel =
			type === 'library'
				? 'library database'
				: type === 'covers'
					? 'cover images'
					: type === 'all'
						? 'entire'
						: type === 'audiodb'
							? 'AudioDB'
							: type;
		const prompt =
			type === 'all'
				? `Are you sure you want to wipe the entire cache? This also deletes all ${
						cacheStats?.disk_cover_count ?? 0
					} cover image files (~${cacheStats?.disk_cover_size_mb ?? 0} MB). Disposable response rows are cleared; the library catalog is preserved.`
				: type === 'metadata'
					? 'Clear memory, disk metadata and MusicBrainz response rows? Covers, genre files and the library catalog are preserved.'
					: type === 'memory'
						? 'Clear memory only? Valid MusicBrainz responses remain on disk for reuse.'
						: `Are you sure you want to clear the ${typeLabel} cache?`;
		if (!confirm(prompt)) {
			return;
		}

		clearing = true;
		message = '';
		try {
			const response = await fetch(getApiUrl(`/api/v1/cache/clear/${type}`), {
				method: 'POST'
			});

			if (response.ok) {
				const result: CacheClearResponse = await response.json();
				await load();
				message = result.message;
				setTimeout(() => {
					message = '';
				}, 5000);
			} else {
				const error = await response.json();
				message = error.error?.message || "Couldn't clear the cache";
			}
		} catch {
			message = "Couldn't clear the cache";
		} finally {
			clearing = false;
		}
	}

	$effect(() => {
		load();
	});
</script>

<div class="card bg-base-200">
	<div class="card-body">
		<h2 class="card-title text-2xl mb-4">Cache management</h2>
		<p class="text-base-content/70 mb-6">
			Typed views use memory. MusicBrainz display responses are reusable from disk for 24 hours,
			with stale data retained for at most seven days within a separate 128 MiB limit.
		</p>

		{#if loading}
			<div class="flex justify-center items-center py-12">
				<span class="loading loading-spinner loading-lg"></span>
			</div>
		{:else if needsAdmin}
			<div role="alert" class="alert alert-warning mt-4">
				<span>Admin access is required to view cache statistics.</span>
			</div>
		{:else if cacheStats}
			<div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 mb-6">
				<div class="stat">
					<div class="stat-title">Memory cache</div>
					<div class="stat-value text-primary">{cacheStats.memory_entries}</div>
					<div class="stat-desc">{cacheStats.memory_size_mb} MiB (shallow estimate)</div>
				</div>

				<div class="stat">
					<div class="stat-title">MusicBrainz responses</div>
					<div class="stat-value text-primary">{cacheStats.response_entries}</div>
					<div class="stat-desc">
						{megabytes(cacheStats.response_logical_bytes)} / 128 MiB logical usage
					</div>
					<div class="stat-desc whitespace-normal">
						{cacheStats.response_hits} hits · {cacheStats.response_evictions} evictions ·
						{cacheStats.response_speculative_used} speculative responses used
					</div>
				</div>

				<div class="stat">
					<div class="stat-title">Disk metadata</div>
					<div class="stat-value text-secondary">{cacheStats.disk_metadata_count}</div>
					<div class="stat-desc">
						{cacheStats.disk_metadata_albums} albums, {cacheStats.disk_metadata_artists} artists
					</div>
				</div>

				<div class="stat">
					<div class="stat-title">Cover images</div>
					<div class="stat-value text-accent">{cacheStats.disk_cover_count}</div>
					<div class="stat-desc">{cacheStats.disk_cover_size_mb} MB</div>
				</div>

				<div class="stat">
					<div class="stat-title">Library</div>
					<div class="stat-value">
						{(cacheStats.library_db_artist_count ?? 0) + (cacheStats.library_db_album_count ?? 0)}
					</div>
					<div class="stat-desc">
						{cacheStats.library_db_artist_count ?? 0} artists, {cacheStats.library_db_album_count ??
							0} albums
					</div>
				</div>

				<div class="stat">
					<div class="stat-title">AudioDB cache</div>
					<div class="stat-value text-info">
						{(cacheStats.disk_audiodb_artist_count ?? 0) +
							(cacheStats.disk_audiodb_album_count ?? 0)}
					</div>
					<div class="stat-desc">
						{cacheStats.disk_audiodb_artist_count ?? 0} artists, {cacheStats.disk_audiodb_album_count ??
							0} albums
					</div>
				</div>
			</div>

			<p class="text-sm text-base-content/70 mb-6">
				Shared database: {megabytes(cacheStats.database_allocated_bytes)} MiB allocated, plus {megabytes(
					cacheStats.database_wal_bytes
				)} MiB WAL. Response bytes are already inside this database, not additional disk usage. The shallow
				memory estimate excludes nested payloads and is not a heap size or cache budget. Memory entry
				and TTL limits are unchanged.
			</p>

			<div class="space-y-4">
				<h3 class="text-xl font-semibold">Clear cache</h3>
				<div class="flex flex-wrap gap-2">
					<button
						class="btn btn-outline btn-sm"
						onclick={() => clearCache('memory')}
						disabled={clearing}
					>
						Clear memory
					</button>
					<button
						class="btn btn-outline btn-sm"
						onclick={() => clearCache('metadata')}
						disabled={clearing}
					>
						Metadata only - covers preserved
					</button>
					<button
						class="btn btn-outline btn-sm"
						onclick={() => clearCache('covers')}
						disabled={clearing}
					>
						Clear covers
					</button>
					<button
						class="btn btn-outline btn-sm"
						onclick={() => clearCache('library')}
						disabled={clearing}
					>
						Clear library
					</button>
					<button
						class="btn btn-outline btn-sm"
						onclick={() => clearCache('audiodb')}
						disabled={clearing}
					>
						Clear AudioDB
					</button>
					<button
						class="btn btn-error btn-sm"
						onclick={() => clearCache('all')}
						disabled={clearing}
					>
						{#if clearing}
							<span class="loading loading-spinner loading-sm"></span>
						{/if}
						Full wipe - also deletes {cacheStats.disk_cover_count} cover files
					</button>
				</div>
			</div>
		{/if}

		{#if message}
			<div
				role="status"
				class="alert mt-4"
				class:alert-success={message.includes('success') || message.includes('Cleared')}
				class:alert-error={message.includes('Failed') || message.includes("Couldn't")}
			>
				<span>{message}</span>
			</div>
		{/if}
	</div>
</div>
