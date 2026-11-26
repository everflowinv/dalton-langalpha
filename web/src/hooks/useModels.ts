import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '../lib/queryKeys';
import { getAvailableModels } from '../api/model';
// DALTON-HOOK(team-model-catalog): active 5-minute foreground refresh gated by explicit server team_catalog_enabled flag, plus X-Dalton-Catalog-Revision invalidation
import {
  attachCatalogRevisionQueryClient,
  setTeamCatalogEnabled,
} from '../lib/dalton/catalogRevision';

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
    // Enable periodic foreground refresh only when the server explicitly advertises
    // team_catalog_enabled === true, so non-Dalton backends and upstream fake-timer
    // unit tests never schedule an infinite interval.
    refetchInterval: (query) =>
      query.state.data?.team_catalog_enabled === true
        ? CATALOG_REFRESH_INTERVAL_MS
        : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    retry: false,
  });

  useEffect(() => {
    if (data !== undefined) {
      setTeamCatalogEnabled(data.team_catalog_enabled === true);
    }
  }, [data]);

  return { models: data ?? null, ...rest };
}
