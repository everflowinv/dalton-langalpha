// DALTON-HOOK(dalton-setup): lightweight entry-safe Dalton team-model availability hook
import { useContext } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { isDaltonBuild } from '@/config/brand';
import { isDaltonAuth } from '@/config/daltonAuth';
import { AuthContext } from '@/contexts/AuthContext';
import type { ConfiguredProvider } from '@/hooks/useConfiguredProviders';
import { queryKeys } from '@/lib/queryKeys';
import type { AccountPolicy } from '@/types/api';

export interface TeamModelsAvailability {
  available: boolean;
  isLoading: boolean;
  unread: boolean;
}

const INERT: TeamModelsAvailability = { available: false, isLoading: false, unread: false };

/**
 * Entry-safe boolean check for whether Dalton team-shared models are available
 * during answer-driven setup. Only depends on modules already on the critical
 * path; the full model-catalog / Dalton-filter pipeline (`daltonTeamModelsCheck`)
 * is loaded lazily only when setup is actively running on answers in a Dalton build.
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
  const ready = active && !providersLoading;

  const { data, isPending, isError } = useQuery({
    queryKey: [...queryKeys.dalton.all, 'team-models-available', userId, providers, byokEnabled, providersUnread, policy],
    queryFn: async () =>
      (await import('./daltonTeamModelsCheck')).checkDaltonTeamModelsAvailable({
        queryClient,
        userId,
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
  if (providersLoading || (ready && isPending)) return { available: false, isLoading: true, unread: false };
  if (isError && data === undefined) return { available: false, isLoading: false, unread: true };
  return { available: Boolean(data), isLoading: false, unread: false };
}

// `isDaltonBuild` is fixed at build time, so each build always calls the same hooks.
export const useDaltonTeamModelsAvailable: typeof useDaltonTeamModelsAvailableImpl =
  isDaltonBuild ? useDaltonTeamModelsAvailableImpl : () => INERT;
