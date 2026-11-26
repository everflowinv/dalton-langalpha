// DALTON-HOOK(dalton-setup): lightweight entry-safe Dalton team-model availability hook
// DALTON-HOOK(team-model-catalog): active 5-minute catalog refresh, header invalidation, and version-aligned curated setup fields
import { useContext, useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';
import type { ModelsResponse } from '@/api/model';
import { isDaltonBuild } from '@/config/brand';
import { isDaltonAuth } from '@/config/daltonAuth';
import type { DaltonDeploymentProfile } from '@/config/daltonSetup';
import { AuthContext } from '@/contexts/AuthContext';
import type { ConfiguredProvider } from '@/hooks/useConfiguredProviders';
import { fetchTeamCredentialsStatus } from '@/hooks/useTeamCredentials';
import { attachCatalogRevisionQueryClient } from '@/lib/dalton/catalogRevision';
import { queryKeys } from '@/lib/queryKeys';
import type { AccountPolicy } from '@/types/api';

export interface TeamModelsAvailability {
  shared: boolean;
  available: boolean;
  empty: boolean;
  isLoading: boolean;
  unread: boolean;
  rejectedProviders: ReadonlySet<string>;
  daltonProfile?: DaltonDeploymentProfile;
  catalogRevision?: number;
  daltonSetupCandidates?: readonly string[];
  daltonCuratedProviders?: readonly string[];
}

const EMPTY_REJECTED: ReadonlySet<string> = new Set();
const INERT: TeamModelsAvailability = {
  shared: false,
  available: false,
  empty: false,
  isLoading: false,
  unread: false,
  rejectedProviders: EMPTY_REJECTED,
};

/**
 * Single shared React Query source of truth for Dalton team-model availability,
 * read by both `useSetupFacts` (app-shell routing) and `useDaltonTeamModels`
 * (wizard pages) under the exact same query keys so routing and steps can never
 * disagree. Only depends on modules already on the critical path; the full
 * model-catalog / Dalton-filter pipeline (`daltonTeamModelsCheck`) is loaded
 * lazily only when team credential sharing is active.
 */
function useDaltonTeamModelsAvailableImpl(
  enabled: boolean,
  providers: ConfiguredProvider[],
  byokEnabled: boolean,
  providersLoading: boolean,
  providersUnread: boolean,
  policy: AccountPolicy | null,
): TeamModelsAvailability {
  const queryClient = useQueryClient();
  useEffect(() => attachCatalogRevisionQueryClient(queryClient), [queryClient]);
  const userId = useContext(AuthContext)?.userId ?? null;
  const active = enabled && isDaltonAuth && Boolean(userId);

  const modelsCatalog = useQuery({
    queryKey: queryKeys.models.all,
    queryFn: async () => (await api.get<ModelsResponse>('/api/v1/models')).data,
    enabled,
    staleTime: 5 * 60_000,
    refetchInterval: (query) =>
      typeof query.state.data?.catalog_revision === 'number' ? 5 * 60_000 : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    retry: false,
  });
  const daltonProfile = modelsCatalog.data?.dalton_profile;
  const catalogRevision = modelsCatalog.data?.catalog_revision;
  const daltonSetupCandidates = modelsCatalog.data?.dalton_setup_candidates;
  const daltonCuratedProviders = modelsCatalog.data?.dalton_curated_providers;
  const catalogFields = {
    ...(daltonProfile ? { daltonProfile } : {}),
    ...(catalogRevision !== undefined ? { catalogRevision } : {}),
    ...(daltonSetupCandidates !== undefined ? { daltonSetupCandidates } : {}),
    ...(daltonCuratedProviders !== undefined ? { daltonCuratedProviders } : {}),
  };
  // DALTON-HOOK(standalone-core): do not guess deployment profile while /api/v1/models is pending or failed
  const catalogPending = enabled && modelsCatalog.isPending;
  const catalogUnread = enabled && modelsCatalog.isError && modelsCatalog.data === undefined;

  const creds = useQuery({
    queryKey: queryKeys.dalton.credentialsStatus(userId),
    queryFn: fetchTeamCredentialsStatus,
    enabled: active,
    staleTime: 5 * 60_000,
    retry: false,
  });

  const shared = active && Boolean(creds.data?.enabled);
  const ready = shared && !providersLoading;

  const { data, isPending, isError } = useQuery({
    queryKey: [
      ...queryKeys.dalton.all,
      'team-models-available',
      userId,
      providers,
      byokEnabled,
      providersUnread,
      policy,
      catalogRevision,
      daltonSetupCandidates,
    ],
    queryFn: async () =>
      (await import('./daltonTeamModelsCheck')).checkDaltonTeamModelsAvailable({
        queryClient,
        providers,
        byokEnabled,
        providersUnread,
        policy,
      }),
    enabled: ready,
    staleTime: 60_000,
    retry: false,
  });

  if (catalogPending) return { ...INERT, isLoading: true, ...catalogFields };
  if (catalogUnread) return { ...INERT, unread: true, ...catalogFields };
  if (!active) return Object.keys(catalogFields).length > 0 ? { ...INERT, ...catalogFields } : INERT;
  if (creds.isPending) return { ...INERT, isLoading: true, ...catalogFields };
  if (!shared) return Object.keys(catalogFields).length > 0 ? { ...INERT, ...catalogFields } : INERT;
  if (providersLoading || (ready && isPending)) return { ...INERT, shared: true, isLoading: true, ...catalogFields };
  if (isError && data === undefined) return { ...INERT, shared: true, unread: true, ...catalogFields };
  const available = Boolean(data?.available);
  return {
    shared: true,
    available,
    empty: !available,
    isLoading: false,
    unread: false,
    rejectedProviders: data?.rejectedProviders ?? EMPTY_REJECTED,
    ...catalogFields,
  };
}

// `isDaltonBuild` is fixed at build time, so each build always calls the same hooks.
export const useDaltonTeamModelsAvailable: typeof useDaltonTeamModelsAvailableImpl =
  isDaltonBuild ? useDaltonTeamModelsAvailableImpl : () => INERT;
