import { useCallback, useContext, useMemo } from 'react';
import { useQueries, useQueryClient } from '@tanstack/react-query';
import { getProviderAvailableModels } from '@/api/model';
import { isDaltonBuild } from '@/config/brand';
import { isDaltonAuth } from '@/config/daltonAuth';
import { hasAvailableDaltonModels } from '@/config/daltonSetup';
import { AuthContext } from '@/contexts/AuthContext';
import { useAccount } from '@/hooks/useAccount';
import { useAllModels, type UseAllModelsResult } from '@/hooks/useAllModels';
import { useConfiguredProviders } from '@/hooks/useConfiguredProviders';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { queryKeys } from '@/lib/queryKeys';
import { collectRejectedProviders, REJECTED_CREDENTIAL_REASONS } from './daltonTeamModelsCheck';
import { useDaltonTeamModelsAvailable } from './useTeamModelsAvailable';

const STALE_MS = 10 * 60_000;
const EMPTY_PROVIDERS: readonly string[] = [];

export { REJECTED_CREDENTIAL_REASONS };

export interface DaltonTeamModelsState {
  /** True in Dalton builds when team credential sharing is active. */
  shared: boolean;
  /** True in Dalton builds when team sharing is active AND at least one Dalton-allowed model is available. */
  available: boolean;
  /** True when team sharing is active, reads settled, and zero Dalton-allowed models are available. */
  empty: boolean;
  /** True while Dalton team credentials or team models are still loading. */
  isLoading: boolean;
  /** True when reading team models failed under team sharing. */
  unread: boolean;
  canManage: boolean;
  callerTierPending: boolean;
  /** Providers whose team credentials were explicitly rejected by the vendor. */
  rejectedProviders: ReadonlySet<string>;
  retry: () => void;
  all: UseAllModelsResult;
}

/**
 * Single source of truth for whether Dalton team-shared models are actually
 * available to the current member, shared across UseStep, nextSetupStep,
 * ModelStep, DefaultStep, MethodStep, DefaultsStep, and DoneStep.
 *
 * In Dalton auth builds (`isDaltonAuth && userId`), the four availability
 * states (`available`, `empty`, `isLoading`, `unread`) plus `shared` and
 * `rejectedProviders` are derived directly from the exact same React Query
 * entry (`useDaltonTeamModelsAvailable`) that `useSetupFacts` reads, so the
 * app-shell route and wizard pages can never disagree.
 */
function useDaltonTeamModelsImpl(): DaltonTeamModelsState {
  const queryClient = useQueryClient();
  const userId = useContext(AuthContext)?.userId ?? null;
  const useSharedQuery = isDaltonBuild && isDaltonAuth && Boolean(userId);
  const { isShared, callerTierPending, canManage } = useTeamCredentials();
  const { providers, byokEnabled, isLoading: providersLoading, unread: providersUnread } = useConfiguredProviders({
    enabled: useSharedQuery,
  });
  const { policy } = useAccount();
  const sharedAvailability = useDaltonTeamModelsAvailable(
    useSharedQuery,
    providers,
    byokEnabled,
    providersLoading,
    providersUnread,
    policy,
  );
  const all = useAllModels();

  // Fallback path used only in non-Supabase unit tests where AuthContext /
  // isDaltonAuth are absent and useTeamCredentials + useAllModels are mocked.
  const localShared = !useSharedQuery && isDaltonBuild && isShared;
  const listedProviders = all.listedProviders ?? EMPTY_PROVIDERS;
  const providerResults = useQueries({
    queries: (localShared ? listedProviders : []).map((provider) => ({
      queryKey: queryKeys.providerModels.available(provider, ''),
      queryFn: ({ signal }: { signal: AbortSignal }) => getProviderAvailableModels(provider, { signal }),
      staleTime: STALE_MS,
      retry: false,
    })),
  });
  const listsLoading = localShared && providerResults.some((r) => r.isLoading);

  const keyCandidates = all.rawApiResponse?.key_candidates;
  const localRejectedProviders = useMemo<ReadonlySet<string>>(
    () => (localShared ? collectRejectedProviders(listedProviders, providerResults, keyCandidates) : new Set()),
    [localShared, listedProviders, providerResults, keyCandidates],
  );

  const retry = useCallback(() => {
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.dalton.credentialsStatus(userId) }),
      queryClient.invalidateQueries({ queryKey: queryKeys.models.all }),
      queryClient.invalidateQueries({ queryKey: queryKeys.user.apiKeys() }),
      queryClient.invalidateQueries({ queryKey: queryKeys.oauth.all }),
      queryClient.invalidateQueries({ queryKey: queryKeys.providerModels.all }),
      queryClient.invalidateQueries({ queryKey: queryKeys.user.preferences() }),
    ]).then(() =>
      queryClient.refetchQueries({ queryKey: [...queryKeys.dalton.all, 'team-models-available'] }),
    );
  }, [queryClient, userId]);

  if (useSharedQuery) {
    return {
      shared: sharedAvailability.shared,
      available: sharedAvailability.available,
      empty: sharedAvailability.empty,
      isLoading: sharedAvailability.isLoading,
      unread: sharedAvailability.unread,
      canManage,
      callerTierPending,
      rejectedProviders: sharedAvailability.rejectedProviders,
      retry,
      all,
    };
  }

  const isLoading = isDaltonBuild && (callerTierPending || (localShared && (all.isLoading || listsLoading)));
  const unread = localShared && !all.isLoading && !listsLoading && all.unread;
  const available = localShared
    && !callerTierPending
    && !all.isLoading
    && !listsLoading
    && !all.unread
    && hasAvailableDaltonModels(
      all.models ?? {},
      all.metadata ?? {},
      all.payers ?? {},
      true,
      localRejectedProviders,
    );
  const empty = localShared && !callerTierPending && !all.isLoading && !listsLoading && !all.unread && !available;

  return {
    shared: localShared,
    available,
    empty,
    isLoading,
    unread,
    canManage,
    callerTierPending,
    rejectedProviders: localRejectedProviders,
    retry,
    all,
  };
}

const noopRetry = () => {};
const EMPTY_REJECTED: ReadonlySet<string> = new Set();

/**
 * Non-Dalton builds: team sharing never applies, so read neither auth nor the
 * team credentials status (upstream Setup steps render outside AuthProvider in
 * their tests). Only the model catalog is read, as upstream DefaultStep did.
 */
function useInertTeamModels(): DaltonTeamModelsState {
  const all = useAllModels();
  return {
    shared: false,
    available: false,
    empty: false,
    isLoading: false,
    unread: false,
    canManage: true,
    callerTierPending: false,
    rejectedProviders: EMPTY_REJECTED,
    retry: noopRetry,
    all,
  };
}

// `isDaltonBuild` is fixed at build time, so each build always calls the same hooks.
export const useDaltonTeamModels: () => DaltonTeamModelsState = isDaltonBuild
  ? useDaltonTeamModelsImpl
  : useInertTeamModels;
