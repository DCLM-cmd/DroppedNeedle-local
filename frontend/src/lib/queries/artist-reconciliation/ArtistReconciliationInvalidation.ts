import { invalidateQueriesWithPersister } from '$lib/queries/QueryClient';

import { ArtistReconciliationQueryKeyFactory } from './ArtistReconciliationQueryKeyFactory';

export const invalidateArtistReconciliation = () =>
	invalidateQueriesWithPersister({ queryKey: ArtistReconciliationQueryKeyFactory.prefix });
