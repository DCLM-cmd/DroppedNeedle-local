import { createMutation } from '@tanstack/svelte-query';

import { api } from '$lib/api/client';
import { API } from '$lib/constants';
import { toastStore } from '$lib/stores/toast';

import { invalidateArtistReconciliation } from './ArtistReconciliationInvalidation';
import type { ArtistDuplicateGroupDismissResponse } from './ArtistReconciliationTypes';

export const dismissArtistDuplicateGroup = () =>
	createMutation(() => ({
		mutationFn: (input: { groupId: string; expectedMemberRevisions: Record<string, number> }) =>
			api.global.post<ArtistDuplicateGroupDismissResponse>(
				API.library.dismissArtistDuplicateGroup(input.groupId),
				{ expected_member_revisions: input.expectedMemberRevisions }
			),
		onSuccess: async () => {
			await invalidateArtistReconciliation();
			toastStore.show({ message: 'Artist records marked as distinct', type: 'success' });
		},
		onError: () =>
			toastStore.show({
				message: 'The artist records changed; review the group again',
				type: 'error'
			})
	}));
