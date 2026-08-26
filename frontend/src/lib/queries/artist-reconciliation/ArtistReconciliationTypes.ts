export type ArtistReconciliationGroupState =
	| 'waiting_for_identity'
	| 'provider_conflict'
	| 'ambiguous_credit_structure'
	| 'same_name_only'
	| 'resolved_automatically';

export interface ArtistReconciliationProgress {
	state: string;
	completed_count: number;
	expected_count: number;
	automatically_resolved_count: number;
	waiting_for_identity_count: number;
	genuine_review_count: number;
	provider_conflict_count: number;
	ambiguous_credit_structure_count: number;
	same_name_only_count: number;
	operation_job_id: string | null;
}

export interface ArtistReconciliationMember {
	id: string;
	name: string;
	sort_name: string | null;
	row_revision: number;
	provider_mbid: string | null;
	album_credit_count: number;
	track_credit_count: number;
	primary_album_count: number;
	favorite_count: number;
	playlist_count: number;
	history_count: number;
	compatibility_id_count: number;
	proven_credit_count: number;
	active_credit_count: number;
}

export interface ArtistDuplicateGroupSummary {
	id: string;
	display_name: string;
	state: ArtistReconciliationGroupState;
	member_count: number;
	members: ArtistReconciliationMember[];
	provider_mbids: string[];
	recommended_survivor_id: string | null;
	affected_reference_count: number;
	reason_code: string;
	resolved_at: number | null;
}

export interface ArtistDuplicateGroupListResponse {
	items: ArtistDuplicateGroupSummary[];
	next_cursor: string | null;
	has_more: boolean;
	total: number;
	counts: Record<string, number>;
}

export interface ArtistCreditEvidence {
	subject_kind: 'album' | 'track';
	subject_id: string;
	subject_name: string;
	source_local_artist_id: string | null;
	local_artist_id: string;
	artist_mbid: string;
	canonical_name: string;
	credited_name: string;
	join_phrase: string;
	release_mbid: string;
	release_track_mbid: string | null;
	album_identity_revision: number;
	track_identity_revision: number | null;
	evidence_hash: string;
}

export interface ArtistOwnedReference {
	id: string;
	name: string;
	row_revision: number;
	identity_ready: boolean;
	exact_track_mapping_ready: boolean;
}

export interface ArtistDuplicateGroupDetail extends ArtistDuplicateGroupSummary {
	evidence: ArtistCreditEvidence[];
	releases: ArtistOwnedReference[];
	tracks: ArtistOwnedReference[];
	reference_counts: Record<string, number>;
	member_revisions: Record<string, number>;
}

export interface ArtistDuplicateGroupDismissResponse {
	group_id: string;
	dismissed_pairs: number;
}

export interface ArtistDuplicateGroupParams {
	state?: ArtistReconciliationGroupState;
	search?: string;
}
