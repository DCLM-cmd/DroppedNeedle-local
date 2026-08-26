import type { AlbumSort, ArtistSort } from '$lib/types';

export const LibraryQueryKeyFactory = {
	all: ['library'] as const,
	activityPrefix: () => [...LibraryQueryKeyFactory.all, 'activity'] as const,
	activity: (userId: string | undefined) =>
		[...LibraryQueryKeyFactory.activityPrefix(), userId ?? 'anonymous'] as const,
	membership: (userId: string | undefined, albumIds: string[]) =>
		[
			...LibraryQueryKeyFactory.all,
			'membership',
			userId ?? 'anonymous',
			[...albumIds].sort()
		] as const,
	operationsPrefix: () => [...LibraryQueryKeyFactory.all, 'operations'] as const,
	currentRuns: () => [...LibraryQueryKeyFactory.operationsPrefix(), 'current-runs'] as const,
	runHistory: (cursor: string | undefined) =>
		[...LibraryQueryKeyFactory.operationsPrefix(), 'run-history', cursor ?? 'first'] as const,
	run: (runId: string) => [...LibraryQueryKeyFactory.operationsPrefix(), 'run', runId] as const,
	runFailures: (runId: string) =>
		[...LibraryQueryKeyFactory.operationsPrefix(), 'run-failures', runId] as const,
	runEstimate: (scopeIds: string[]) =>
		[...LibraryQueryKeyFactory.operationsPrefix(), 'estimate', [...scopeIds].sort()] as const,
	reviewsPrefix: () => [...LibraryQueryKeyFactory.all, 'reviews'] as const,
	reviews: (params: object) => [...LibraryQueryKeyFactory.reviewsPrefix(), params] as const,
	review: (reviewId: string) =>
		[...LibraryQueryKeyFactory.reviewsPrefix(), 'detail', reviewId] as const,
	policyPrefix: () => [...LibraryQueryKeyFactory.all, 'policy'] as const,
	targetSettings: () => [...LibraryQueryKeyFactory.policyPrefix(), 'settings'] as const,
	policyTree: () => [...LibraryQueryKeyFactory.policyPrefix(), 'tree'] as const,
	pathMapping: () => [...LibraryQueryKeyFactory.policyPrefix(), 'path-mapping'] as const,
	restorableRoots: () => [...LibraryQueryKeyFactory.policyPrefix(), 'restorable-roots'] as const,
	repairsPrefix: () => [...LibraryQueryKeyFactory.operationsPrefix(), 'repairs'] as const,
	repairs: (cursor: string | undefined) =>
		[...LibraryQueryKeyFactory.repairsPrefix(), 'history', cursor ?? 'first'] as const,
	repairEstimate: (rootIds: string[]) =>
		[...LibraryQueryKeyFactory.repairsPrefix(), 'estimate', [...rootIds].sort()] as const,
	repair: (jobId: string) => [...LibraryQueryKeyFactory.repairsPrefix(), 'detail', jobId] as const,
	repairFindings: (jobId: string, findingCategory: string, cursor: string | undefined) =>
		[
			...LibraryQueryKeyFactory.repairsPrefix(),
			'findings',
			jobId,
			findingCategory,
			cursor ?? 'first'
		] as const,
	identityPreparationsPrefix: (userId: string | undefined) =>
		[
			...LibraryQueryKeyFactory.operationsPrefix(),
			'identity-preparations',
			userId ?? 'anonymous'
		] as const,
	identityPreparations: (userId: string | undefined, cursor: string | undefined) =>
		[
			...LibraryQueryKeyFactory.identityPreparationsPrefix(userId),
			'history',
			cursor ?? 'first'
		] as const,
	identityPreparationEstimate: (userId: string | undefined, rootIds: string[]) =>
		[
			...LibraryQueryKeyFactory.identityPreparationsPrefix(userId),
			'estimate',
			[...rootIds].sort()
		] as const,
	identityPreparationFindings: (
		userId: string | undefined,
		jobId: string,
		findingCategory: string,
		cursor: string | undefined
	) =>
		[
			...LibraryQueryKeyFactory.identityPreparationsPrefix(userId),
			'findings',
			jobId,
			findingCategory,
			cursor ?? 'first'
		] as const,
	albums: (page: number, sort: AlbumSort, q: string, format: string) =>
		[...LibraryQueryKeyFactory.all, 'albums', { page, sort, q, format }] as const,
	artists: (scope: string, sortBy: ArtistSort, sortOrder: string, q: string) =>
		[...LibraryQueryKeyFactory.all, 'artists', { scope, sortBy, sortOrder, q }] as const,
	album: (mbid: string) => [...LibraryQueryKeyFactory.all, 'album', mbid] as const,
	albumDetail: (albumId: string) =>
		[...LibraryQueryKeyFactory.all, 'album-detail', albumId] as const,
	editionConversionsPrefix: (userId: string | undefined) =>
		[...LibraryQueryKeyFactory.all, 'edition-conversions', userId ?? 'anonymous'] as const,
	editionConversion: (userId: string | undefined, jobId: string | null) =>
		[...LibraryQueryKeyFactory.editionConversionsPrefix(userId), jobId ?? 'inactive'] as const,
	reidentificationReleases: (
		userId: string | undefined,
		albumId: string,
		title: string,
		artist: string,
		offset: number
	) =>
		[
			...LibraryQueryKeyFactory.all,
			'reidentification-releases',
			userId ?? 'anonymous',
			albumId,
			title,
			artist,
			offset
		] as const,
	albumCopies: (albumId: string) =>
		[...LibraryQueryKeyFactory.all, 'album-copies', albumId] as const,
	artistDetail: (artistId: string) =>
		[...LibraryQueryKeyFactory.all, 'artist-detail', artistId] as const,
	artistAlbums: (artistId: string) =>
		[...LibraryQueryKeyFactory.all, 'artist-albums', artistId] as const,
	artistAppearances: (artistId: string) =>
		[...LibraryQueryKeyFactory.all, 'artist-appearances', artistId] as const,
	recentlyAdded: () => [...LibraryQueryKeyFactory.all, 'recently-added'] as const,
	stats: () => [...LibraryQueryKeyFactory.all, 'stats'] as const,
	scanSchedule: () => [...LibraryQueryKeyFactory.all, 'scan-schedule'] as const,
	albumSearch: (q: string) => [...LibraryQueryKeyFactory.all, 'album-search', q] as const,
	albumTracks: (mbid: string) => [...LibraryQueryKeyFactory.all, 'album-tracks', mbid] as const,
	search: (q: string) => [...LibraryQueryKeyFactory.all, 'search', q] as const,
	artistThumbs: () => [...LibraryQueryKeyFactory.all, 'artist-thumbs'] as const
};
