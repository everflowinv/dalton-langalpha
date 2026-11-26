import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '../lib/queryKeys';
import { getAvailableModels } from '../api/model';
// DALTON-HOOK(team-model-catalog): active 5-minute foreground refresh when backend reports catalog_revision, plus X-Dalton-Catalog-Revision invalidation
import { attachCatalogRevisionQueryClient } from '../lib/dalton/catalogRevision';

const CATALOG_REFRESH_INTERVAL_MS = 5 * 60_000;

/**
 * Shared hook for available model data.
 * All consumers share a single cached entry keyed by queryKeys.models.all.
 */
export function useModels() {
  const queryClient = useQueryClient();
  useEffect(() => attachCatalogRevisionQueryClient(queryClient), [queryClient]);

  const { data, ...rest } = useQuery({
    queryKey: queryKeys.models.all,
    queryFn: getAvailableModels,
    staleTime: 5 * 60_000,
    // Enable periodic foreground refresh only once a Dalton backend response carries catalog_revision,
    // so non-Dalton backends and upstream fake-timer unit tests never schedule an infinite interval.
    refetchInterval: (query) =>
      typeof query.state.data?.catalog_revision === 'number'
        ? CATALOG_REFRESH_INTERVAL_MS
        : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    retry: false,
  });
  return { models: data ?? null, ...rest };
}
