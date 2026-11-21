import { useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { isDaltonBuild } from '@/config/brand';
import { hasAvailableDaltonModels } from '@/config/daltonSetup';
import { useAllModels, type UseAllModelsResult } from '@/hooks/useAllModels';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { queryKeys } from '@/lib/queryKeys';

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
  retry: () => void;
  all: UseAllModelsResult;
}

/**
 * Single source of truth for whether Dalton team-shared models are actually
 * available to the current member, shared across UseStep, nextSetupStep,
 * ModelStep, and DefaultStep.
 */
function useDaltonTeamModelsImpl(): DaltonTeamModelsState {
  const queryClient = useQueryClient();
  const { isShared, callerTierPending, canManage } = useTeamCredentials();
  const all = useAllModels();

  const shared = isDaltonBuild && isShared;
  const isLoading = isDaltonBuild && (callerTierPending || (shared && all.isLoading));
  const unread = shared && !all.isLoading && all.unread;
  const available = shared
    && !callerTierPending
    && !all.isLoading
    && !all.unread
    && hasAvailableDaltonModels(all.models, all.metadata, all.payers, true);
  const empty = shared && !callerTierPending && !all.isLoading && !all.unread && !available;

  const retry = useCallback(() => {
    void Promise.all([
      queryClient.refetchQueries({ queryKey: queryKeys.dalton.all }),
      queryClient.refetchQueries({ queryKey: queryKeys.models.all }),
      queryClient.refetchQueries({ queryKey: queryKeys.user.apiKeys() }),
      queryClient.refetchQueries({ queryKey: queryKeys.oauth.all }),
      queryClient.refetchQueries({ queryKey: queryKeys.user.preferences() }),
    ]);
  }, [queryClient]);

  return {
    shared,
    available,
    empty,
    isLoading,
    unread,
    canManage,
    callerTierPending,
    retry,
    all,
  };
}

const noopRetry = () => {};

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
    retry: noopRetry,
    all,
  };
}

// `isDaltonBuild` is fixed at build time, so each build always calls the same hooks.
export const useDaltonTeamModels: () => DaltonTeamModelsState = isDaltonBuild
  ? useDaltonTeamModelsImpl
  : useInertTeamModels;

