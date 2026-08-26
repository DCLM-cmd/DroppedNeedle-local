import { createMutation, createQuery } from '@tanstack/svelte-query';

import { api } from '$lib/api/client';
import { API } from '$lib/constants';
import { toastStore } from '$lib/stores/toast';
import { invalidateLibraryCatalog } from './LibraryCatalogInvalidation';
import { LibraryQueryKeyFactory } from './LibraryQueryKeyFactory';

type Getter<T> = () => T;

export interface EditionConversionTarget {
	ordinal: number;
	disc_number: number;
	track_number: number;
	release_track_mbid: string;
	recording_mbid: string;
	title: string;
	duration_seconds: number | null;
	state: 'kept' | 'pending' | 'downloading' | 'staged' | 'failed';
	kept_local_track_id: string | null;
	failure_code: string | null;
}

export interface EditionConversionLocalFile {
	local_track_id: string;
	action: 'keep' | 'recycle_conflict' | 'recycle_duplicate' | 'recycle_extra';
	target_ordinal: number | null;
	evidence_kind: string;
}

export interface EditionConversionStatus {
	job_id: string;
	local_album_id: string;
	release_group_mbid: string;
	release_mbid: string;
	album_title: string;
	artist_name: string;
	state: 'preflight' | 'acquiring' | 'ready' | 'needs_recheck' | 'cancelled' | 'failed' | 'applied';
	download_source_ready: boolean;
	required_temporary_bytes: number;
	kept_count: number;
	acquire_count: number;
	recycle_count: number;
	staged_count: number;
	failed_count: number;
	row_revision: number;
	created_at: number;
	updated_at: number;
	targets: EditionConversionTarget[];
	local_files: EditionConversionLocalFile[];
	final_preview_job_id: string | null;
	preflight_token: string | null;
	error_code: string | null;
}

export interface EditionConversionPreview {
	status: EditionConversionStatus;
	preview_token: string;
}

export function getEditionConversionQuery(
	getUserId: Getter<string | undefined>,
	getJobId: Getter<string | null>
) {
	return createQuery(() => {
		const userId = getUserId();
		const jobId = getJobId();
		return {
			enabled: Boolean(jobId),
			queryKey: LibraryQueryKeyFactory.editionConversion(userId, jobId),
			queryFn: ({ signal }) => {
				if (!jobId) throw new Error('Edition conversion is not active');
				return api.global.get<EditionConversionStatus>(API.library.editionConversion(jobId), {
					signal
				});
			},
			refetchInterval: (query) => {
				const state = query.state.data?.state;
				return state === 'acquiring' ? 2000 : false;
			}
		};
	});
}

function actionError(fallback: string) {
	return (error: Error) => toastStore.show({ message: error.message || fallback, type: 'error' });
}

export function createEditionConversionPreflight() {
	return createMutation(() => ({
		mutationFn: (input: { albumId: string; releaseGroupMbid: string; releaseMbid: string }) =>
			api.global.post<EditionConversionStatus>(
				API.library.editionConversionPreflight(input.albumId),
				{
					release_group_mbid: input.releaseGroupMbid,
					release_mbid: input.releaseMbid
				}
			),
		onSuccess: invalidateLibraryCatalog,
		onError: actionError('Could not prepare this edition conversion')
	}));
}

export function startEditionConversion() {
	return createMutation(() => ({
		mutationFn: (input: { jobId: string; preflightToken: string; expectedRevision: number }) =>
			api.global.post<EditionConversionStatus>(API.library.editionConversionStart(input.jobId), {
				preflight_token: input.preflightToken,
				expected_row_revision: input.expectedRevision,
				confirmation: true
			}),
		onSuccess: invalidateLibraryCatalog,
		onError: actionError('Could not start acquiring this edition')
	}));
}

export function createEditionConversionPreview() {
	return createMutation(() => ({
		mutationFn: (input: { jobId: string; expectedRevision: number }) =>
			api.global.post<EditionConversionPreview>(API.library.editionConversionPreview(input.jobId), {
				expected_row_revision: input.expectedRevision
			}),
		onError: actionError('Could not create the final edition preview')
	}));
}

export function retryEditionConversion() {
	return createMutation(() => ({
		mutationFn: (input: { jobId: string; targetOrdinals: number[]; expectedRevision: number }) =>
			api.global.post<EditionConversionStatus>(API.library.editionConversionRetry(input.jobId), {
				target_ordinals: input.targetOrdinals,
				expected_row_revision: input.expectedRevision
			}),
		onSuccess: invalidateLibraryCatalog,
		onError: actionError('Could not retry the unresolved tracks')
	}));
}

export function recheckEditionConversion() {
	return createMutation(() => ({
		mutationFn: (input: { jobId: string; expectedRevision: number }) =>
			api.global.post<EditionConversionStatus>(API.library.editionConversionRecheck(input.jobId), {
				expected_row_revision: input.expectedRevision
			}),
		onSuccess: invalidateLibraryCatalog,
		onError: actionError('Could not recheck this edition conversion')
	}));
}

export function cancelEditionConversion() {
	return createMutation(() => ({
		mutationFn: (input: { jobId: string; expectedRevision: number }) =>
			api.global.post<EditionConversionStatus>(API.library.editionConversionCancel(input.jobId), {
				expected_row_revision: input.expectedRevision,
				confirmation: true
			}),
		onSuccess: async () => {
			await invalidateLibraryCatalog();
			toastStore.show({ message: 'Edition conversion cancelled', type: 'success' });
		},
		onError: actionError('Could not cancel this edition conversion')
	}));
}
