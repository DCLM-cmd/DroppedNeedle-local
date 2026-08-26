import type { LibraryWorkItem } from '$lib/queries/library/LibraryOperationsTypes';

const phaseLabels: Record<string, string> = {
	discovering: 'Counting local files',
	indexing: 'Reading file metadata',
	reconciling: 'Finalizing the catalog',
	identifying_albums: 'Matching albums with MusicBrainz',
	planning: 'Inspecting files and release bundles',
	ready: 'Ready for review',
	applying: 'Applying planned changes',
	undoing: 'Restoring the previous file state',
	restoring: 'Restoring original state',
	preparing_snapshots: 'Preparing recovery snapshots',
	writing_staged_files: 'Writing staged files',
	validating_staged_files: 'Validating staged files',
	publishing_files: 'Publishing verified files',
	committing_catalog: 'Committing the catalog',
	cleaning_up: 'Cleaning up',
	checking_identities: 'Checking exact release identities',
	checking_exact_edition: 'Checking the selected MusicBrainz edition',
	applying_identity_decisions: 'Applying identity decisions',
	recovery: 'Recovery needs administrator attention',
	working: 'Working'
};

export function libraryWorkTitle(item: LibraryWorkItem): string {
	if (item.kind === 'scan')
		return item.state === 'failed' ? 'Library scan failed' : 'Scanning library';
	if (item.kind === 'identification')
		return item.state === 'failed' ? 'Album identification failed' : 'Identifying changed albums';
	if (item.kind === 'identity_preparation') return 'Preparing exact MusicBrainz identities';
	if (item.kind === 'reidentification') return 'Checking an exact MusicBrainz edition';
	if (item.kind === 'identity_review') return 'Applying identity review decisions';
	if (item.kind === 'recovery') return 'File recovery needs attention';
	if (item.kind === 'maintenance') return 'Maintaining the library catalog';
	if (item.kind === 'library_management') {
		if (item.state === 'failed') {
			if (item.mode === 'preview') return 'Organization preview failed';
			if (item.mode === 'undo') return 'Organization Undo failed';
			if (item.mode === 'baseline_restore') return 'Original-state restore failed';
			if (item.mode === 'duplicate_resolution') return 'Duplicate resolution failed';
			return 'Applying organization changes failed';
		}
		if (item.mode === 'undo') return 'Undoing organization changes';
		if (item.mode === 'baseline_restore') return 'Restoring original state';
		if (item.mode === 'duplicate_resolution') return 'Resolving duplicate files';
		if (item.effect === 'file_writing') return 'Writing tags and organizing files';
		return 'Preparing a Picard-style preview';
	}
	return 'Library maintenance';
}

export function libraryWorkPhase(item: LibraryWorkItem): string {
	if (item.state === 'queued') return 'Queued';
	if (item.state === 'pausing') {
		return item.kind === 'scan'
			? 'Pausing after the current file'
			: 'Pausing after the current album';
	}
	if (item.state === 'paused') return 'Paused';
	if (item.state === 'stopping') {
		return item.kind === 'scan'
			? 'Stopping after the current file'
			: 'Stopping after the current album';
	}
	if (item.state === 'failed') {
		if (item.kind === 'recovery') return phaseLabels.recovery;
		const failedPhase = phaseLabels[item.phase ?? ''];
		return failedPhase
			? `Failed while ${failedPhase.charAt(0).toLowerCase()}${failedPhase.slice(1)}`
			: 'Needs attention';
	}
	return phaseLabels[item.phase ?? ''] ?? titleCase(item.phase ?? item.state);
}

export function libraryWorkEffect(item: LibraryWorkItem): string {
	if (item.effect === 'file_writing') return 'Writes music files';
	if (item.effect === 'attention') return 'Needs attention';
	return 'Music files stay unchanged';
}

export function libraryWorkProgress(item: LibraryWorkItem): string {
	if (item.remaining_count !== null) {
		return `${item.remaining_count.toLocaleString()} ${plural(item.unit, item.remaining_count)} remaining`;
	}
	if (item.total !== null && item.total > 0) {
		return `${item.processed.toLocaleString()} / ${item.total.toLocaleString()} ${plural(item.unit, item.total)}`;
	}
	if (item.processed > 0) {
		return `${item.processed.toLocaleString()} ${plural(item.unit, item.processed)} processed`;
	}
	return item.state === 'queued' ? 'Waiting to start' : 'Starting…';
}

export function libraryWorkPercentage(item: LibraryWorkItem): number | null {
	if (item.indeterminate || item.total === null || item.total <= 0) return null;
	return Math.min(100, Math.round((item.processed / item.total) * 100));
}

export function libraryWorkHref(item: LibraryWorkItem): string {
	if (item.kind === 'library_management') {
		return `/library/management/operations/${encodeURIComponent(item.id)}`;
	}
	if (
		item.kind === 'identity_preparation' ||
		item.kind === 'reidentification' ||
		item.kind === 'identity_review'
	) {
		return '/library/management?tab=organize';
	}
	if (item.kind === 'recovery') return '/library/management?tab=organize';
	return '/library/management?tab=scanning';
}

export function libraryWorkContext(item: LibraryWorkItem): string | null {
	const values = [item.profile_name, item.scope_label, item.origin ? titleCase(item.origin) : null];
	return values.filter((value): value is string => Boolean(value)).join(' · ') || null;
}

export function libraryWorkFacts(item: LibraryWorkItem): string[] {
	if (item.kind === 'scan') {
		return [
			item.new_count ? `${item.new_count.toLocaleString()} new` : null,
			item.changed_count ? `${item.changed_count.toLocaleString()} changed` : null,
			item.missing_count ? `${item.missing_count.toLocaleString()} missing` : null,
			item.failed_count ? `${item.failed_count.toLocaleString()} errors` : null
		].filter((value): value is string => value !== null);
	}
	if (item.kind === 'library_management') {
		return [
			item.subject_count ? `${item.subject_count.toLocaleString()} files` : null,
			item.warning_count ? `${item.warning_count.toLocaleString()} warnings` : null,
			item.blocked_count ? `${item.blocked_count.toLocaleString()} safely excluded` : null,
			item.failed_count ? `${item.failed_count.toLocaleString()} failed` : null,
			item.skipped_count ? `${item.skipped_count.toLocaleString()} skipped` : null
		].filter((value): value is string => value !== null);
	}
	return [
		item.warning_count ? `${item.warning_count.toLocaleString()} deferred` : null,
		item.failed_count ? `${item.failed_count.toLocaleString()} failed` : null
	].filter((value): value is string => value !== null);
}

function plural(unit: LibraryWorkItem['unit'], count: number): string {
	if (count === 1) return unit === 'releases' ? 'release' : unit.slice(0, -1);
	return unit;
}

function titleCase(value: string): string {
	return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}
