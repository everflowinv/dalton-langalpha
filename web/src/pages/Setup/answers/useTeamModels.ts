import { useCallback, useMemo } from 'react';
import { useQueries, useQueryClient } from '@tanstack/react-query';
import { apiErrorStatus } from '@/api/errors';
import { getProviderAvailableModels } from '@/api/model';
import { isDaltonBuild } from '@/config/brand';
import { hasAvailableDaltonModels } from '@/config/daltonSetup';
import { useAllModels, type UseAllModelsResult } from '@/hooks/useAllModels';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { queryKeys } from '@/lib/queryKeys';

const STALE_MS = 10 * 60_000;

/**
 * Fallback reasons from `/api/v1/providers/{p}/available-models` where the
 * provider explicitly rejected the credential (401/403, revoked OAuth, or a
 * plan with no API access). Transient failures (`unreachable`, `bad_response`,
 * `refresh_unavailable`, etc.) are deliberately not listed so network blips do
 * not mark team models unavailable.
 */
export const REJECTED_CREDENTIAL_REASONS: ReadonlySet<string> = new Set([
  'unauthorized',
  'needs_reauth',
  'no_api_access',
]);

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
  const listedProviders = all.listedProviders ?? [];
  const providerResults = useQueries({
    queries: (shared ? listedProviders : []).map((provider) => ({
      queryKey: queryKeys.providerModels.available(provider, ''),
      queryFn: ({ signal }: { signal: AbortSignal }) => getProviderAvailableModels(provider, { signal }),
      staleTime: STALE_MS,
      retry: false,
    })),
  });
  const listsLoading = shared && providerResults.some((r) => r.isLoading);

  const rejectedSignature = shared
    ? providerResults
        .map((r, i) => {
          const provider = listedProviders[i];
          if (!provider) return '';
          const fallbackRejected =
            r.data?.status === 'fallback'
            && typeof r.data.reason === 'string'
            && REJECTED_CREDENTIAL_REASONS.has(r.data.reason);
          const httpUnauthorized = apiErrorStatus(r.error) === 401;
          return fallbackRejected || httpUnauthorized ? provider : '';
        })
        .filter(Boolean)
        .join('\0')
    : '';

  const keyCandidates = all.rawApiResponse?.key_candidates;
  const rejectedProviders = useMemo<ReadonlySet<string>>(() => {
    const out = new Set<string>();
    if (!rejectedSignature) return out;
    for (const p of rejectedSignature.split('\0')) {
      if (p) out.add(p);
    }
    if (keyCandidates) {
      const listedSet = new Set(listedProviders);
      for (const [provider, candidates] of Object.entries(keyCandidates)) {
        if (listedSet.has(provider)) continue;
        const sharedHolders = candidates.shared ?? [];
        const hasWorkingHolder = sharedHolders.some((h) => listedSet.has(h) && !out.has(h));
        if (!hasWorkingHolder && sharedHolders.some((h) => out.has(h))) {
          out.add(provider);
        }
      }
    }
    return out;
  }, [rejectedSignature, listedProviders, keyCandidates]);

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
    rejectedProviders,
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

