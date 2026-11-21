import { useCallback, useMemo } from 'react';
import { useQueries, useQueryClient } from '@tanstack/react-query';
import { getProviderAvailableModels } from '@/api/model';
import { isDaltonBuild } from '@/config/brand';
import { hasAvailableDaltonModels } from '@/config/daltonSetup';
import { useAllModels, type UseAllModelsResult } from '@/hooks/useAllModels';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { queryKeys } from '@/lib/queryKeys';
import { collectRejectedProviders, REJECTED_CREDENTIAL_REASONS } from './daltonTeamModelsCheck';

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
 */
function useDaltonTeamModelsImpl(): DaltonTeamModelsState {
  const queryClient = useQueryClient();
  const { isShared, callerTierPending, canManage } = useTeamCredentials();
  const all = useAllModels();

  const shared = isDaltonBuild && isShared;
  const listedProviders = all.listedProviders ?? EMPTY_PROVIDERS;
  const providerResults = useQueries({
    queries: (shared ? listedProviders : []).map((provider) => ({
      queryKey: queryKeys.providerModels.available(provider, ''),
      queryFn: ({ signal }: { signal: AbortSignal }) => getProviderAvailableModels(provider, { signal }),
      staleTime: STALE_MS,
      retry: false,
    })),
  });
  const listsLoading = shared && providerResults.some((r) => r.isLoading);

  const keyCandidates = all.rawApiResponse?.key_candidates;
  const rejectedProviders = useMemo<ReadonlySet<string>>(
    () => (shared ? collectRejectedProviders(listedProviders, providerResults, keyCandidates) : new Set()),
    [shared, listedProviders, providerResults, keyCandidates],
  );

  const isLoading = isDaltonBuild && (callerTierPending || (shared && (all.isLoading || listsLoading)));
  const unread = shared && !all.isLoading && !listsLoading && all.unread;
  const available = shared
    && !callerTierPending
    && !all.isLoading
    && !listsLoading
    && !all.unread
    && hasAvailableDaltonModels(
      all.models ?? {},
      all.metadata ?? {},
      all.payers ?? {},
      true,
      rejectedProviders,
    );
  const empty = shared && !callerTierPending && !all.isLoading && !listsLoading && !all.unread && !available;

  const retry = useCallback(() => {
    void Promise.all([
      queryClient.refetchQueries({ queryKey: queryKeys.dalton.all }),
      queryClient.refetchQueries({ queryKey: queryKeys.models.all }),
      queryClient.refetchQueries({ queryKey: queryKeys.user.apiKeys() }),
      queryClient.refetchQueries({ queryKey: queryKeys.oauth.all }),
      queryClient.refetchQueries({ queryKey: queryKeys.providerModels.all }),
      queryClient.refetchQueries({ queryKey: queryKeys.user.preferences() }),
    ]).then(() => queryClient.invalidateQueries({ queryKey: [...queryKeys.dalton.all, 'team-models-available'] }));
  }, [queryClient]);

  return {
    shared,
    available,
    empty,
    isLoading,
    unread,
    canManage,
    callerTierPending,
    rejectedProviders,
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

