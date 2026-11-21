// DALTON-HOOK(dalton-setup): lightweight entry-safe Dalton team-model availability hook
import { useContext } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';
import { isDaltonBuild } from '@/config/brand';
import { isDaltonAuth } from '@/config/daltonAuth';
import { AuthContext } from '@/contexts/AuthContext';
import type { ConfiguredProvider } from '@/hooks/useConfiguredProviders';
import { queryKeys } from '@/lib/queryKeys';
import type { AccountPolicy } from '@/types/api';

export interface TeamModelsAvailability {
  shared: boolean;
  available: boolean;
  empty: boolean;
  isLoading: boolean;
  unread: boolean;
  rejectedProviders: ReadonlySet<string>;
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
  const userId = useContext(AuthContext)?.userId ?? null;
  const active = enabled && isDaltonAuth && Boolean(userId);

  const creds = useQuery({
    queryKey: queryKeys.dalton.credentialsStatus(userId),
    queryFn: async () => (await api.get<{ enabled?: boolean }>('/api/v1/dalton/credentials/status')).data,
    enabled: active,
    staleTime: 5 * 60_000,
    retry: false,
  });

  const shared = active && Boolean(creds.data?.enabled);
  const ready = shared && !providersLoading;

  const { data, isPending, isError } = useQuery({
    queryKey: [...queryKeys.dalton.all, 'team-models-available', userId, providers, byokEnabled, providersUnread, policy],
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

  if (!active) return INERT;
  if (creds.isPending) return { ...INERT, isLoading: true };
  if (!shared) return INERT;
  if (providersLoading || (ready && isPending)) return { ...INERT, shared: true, isLoading: true };
  if (isError && data === undefined) return { ...INERT, shared: true, unread: true };
  const available = Boolean(data?.available);
  return {
    shared: true,
    available,
    empty: !available,
    isLoading: false,
    unread: false,
    rejectedProviders: data?.rejectedProviders ?? EMPTY_REJECTED,
  };
}

// `isDaltonBuild` is fixed at build time, so each build always calls the same hooks.
export const useDaltonTeamModelsAvailable: typeof useDaltonTeamModelsAvailableImpl =
  isDaltonBuild ? useDaltonTeamModelsAvailableImpl : () => INERT;
