// DALTON-HOOK(dalton-setup): unified Dalton team-model availability hook and forced-refresh retry
import { useCallback, useContext, useMemo } from 'react';
import { useQueries, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { getProviderAvailableModels } from '@/api/model';
import { SUBSCRIPTIONS, subscriptionKey, type OAuthStatus } from '@/api/oauth';
import type { ByokProvider } from '@/components/model/types';
import { isDaltonBuild } from '@/config/brand';
import { isDaltonAuth } from '@/config/daltonAuth';
import { hasAvailableDaltonModels, type DaltonDeploymentProfile } from '@/config/daltonSetup';
import { isPlatformMode } from '@/config/hostMode';
import { AuthContext } from '@/contexts/AuthContext';
import { useAccount } from '@/hooks/useAccount';
import { useAllModels, type UseAllModelsResult } from '@/hooks/useAllModels';
import { useConfiguredProviders } from '@/hooks/useConfiguredProviders';
import { useTeamCredentials, type TeamCredentialsStatus } from '@/hooks/useTeamCredentials';
import { allows, RULE } from '@/lib/accountRules';
import { queryKeys } from '@/lib/queryKeys';
import type { AccountPolicy } from '@/types/api';
import {
  bumpDaltonTeamModelsRetry,
  collectRejectedProviders,
  forceRefreshProviderAvailableModels,
  REJECTED_CREDENTIAL_REASONS,
} from './daltonTeamModelsCheck';
import { useDaltonTeamModelsAvailable } from './useTeamModelsAvailable';

const STALE_MS = 10 * 60_000;
const EMPTY_PROVIDERS: readonly string[] = [];

/**
 * DALTON-HOOK(dalton-setup): derive the current list of connected providers whose available-models list
 * should be force-refreshed after `invalidateQueries` settles, so Retry also
 * picks up providers that were newly connected or recovered from an initial
 * status-read failure instead of relying on a stale pre-retry closure.
 */
function resolvePostRetryListedProviders(
  queryClient: QueryClient,
  fallbackListed: readonly string[],
  policy: AccountPolicy | null,
): string[] {
  const apiKeysData = queryClient.getQueryData<{ providers?: unknown; byok_enabled?: unknown }>(
    queryKeys.user.apiKeys(),
  );
  const oauthStatuses = SUBSCRIPTIONS.map((s) => ({
    provider: s.provider,
    status: queryClient.getQueryData<OAuthStatus>(queryKeys.oauth.status(subscriptionKey(s.provider))),
  }));
  const hasCache = apiKeysData !== undefined || oauthStatuses.some((entry) => entry.status !== undefined);
  if (!hasCache) {
    return [...fallbackListed];
  }

  // DALTON-HOOK(dalton-setup): mirror useAllModels listedProviders filter on refreshed cache
  const byokEnabled = apiKeysData?.byok_enabled === true && allows(policy, RULE.ownApiKeys);
  const ownSignIns = allows(policy, RULE.ownSubscriptions);
  const out = new Set<string>();

  const rows = Array.isArray(apiKeysData?.providers) ? (apiKeysData.providers as ByokProvider[]) : [];
  for (const p of rows) {
    if (!p.has_key || p.withdrawn || p.is_custom || p.access_type === 'local') continue;
    if (!isPlatformMode || byokEnabled) {
      out.add(p.provider);
    }
  }
  for (const { provider, status } of oauthStatuses) {
    if (ownSignIns && status?.connected && status.state === 'connected') {
      out.add(provider);
    }
  }
  return [...out];
}

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
  /** Active deployment profile reported by `/api/v1/models` (`everflow` | `core`). */
  daltonProfile?: DaltonDeploymentProfile;
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
  // DALTON-HOOK(dalton-setup): preserve previously confirmed admin identity (is_pm: true) if a subsequent credentials/status read fails
  const { isShared, callerTierPending, canManage, callerTierError } = useTeamCredentials();
  const cachedCreds = useSharedQuery && callerTierError && !canManage
    ? (queryClient.getQueryData(queryKeys.dalton.credentialsStatus(userId)) as TeamCredentialsStatus | undefined)
    : undefined;
  const effectiveCanManage = canManage || Boolean(cachedCreds?.is_pm);
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

  // DALTON-HOOK(dalton-setup): invalidate all queries behind team/all.unread (including features & Claude Code status) and force-refresh providers
  const retry = useCallback(() => {
    const retryGen = bumpDaltonTeamModelsRetry(queryClient);
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.dalton.credentialsStatus(userId) }),
      queryClient.invalidateQueries({ queryKey: queryKeys.models.all }),
      queryClient.invalidateQueries({ queryKey: queryKeys.user.apiKeys() }),
      queryClient.invalidateQueries({ queryKey: queryKeys.oauth.all }),
      queryClient.invalidateQueries({ queryKey: queryKeys.features.all }),
      queryClient.invalidateQueries({ queryKey: queryKeys.user.preferences() }),
    ]).then(async () => {
      const credsStatus = useSharedQuery
        ? queryClient.getQueryData<{ enabled?: boolean }>(queryKeys.dalton.credentialsStatus(userId))
        : null;
      if (useSharedQuery && credsStatus?.enabled) {
        await queryClient.refetchQueries({ queryKey: [...queryKeys.dalton.all, 'team-models-available'] });
        return;
      }
      // DALTON-HOOK(dalton-setup): personal credentials (sharing off / status unread) or local fallback path —
      // read refreshed connected providers from cache, cancel any in-flight normal provider list request, and force-refresh with `?refresh=1`.
      const refreshTargets = resolvePostRetryListedProviders(queryClient, listedProviders, policy);
      await Promise.allSettled(
        refreshTargets.map((provider) =>
          forceRefreshProviderAvailableModels(queryClient, provider, retryGen),
        ),
      );
    });
  }, [queryClient, userId, useSharedQuery, listedProviders, policy]);

  const daltonProfile = all.rawApiResponse?.dalton_profile ?? sharedAvailability.daltonProfile;
  // DALTON-HOOK(standalone-core): wait for /api/v1/models catalog read before deciding Dalton profile
  const catalogPending = isDaltonBuild && all.isLoading;
  const catalogUnread = isDaltonBuild && !all.isLoading && all.unread;

  if (useSharedQuery) {
    return {
      shared: sharedAvailability.shared,
      available: sharedAvailability.available,
      empty: sharedAvailability.empty,
      isLoading: sharedAvailability.isLoading || catalogPending,
      unread: sharedAvailability.unread || catalogUnread,
      canManage: effectiveCanManage,
      callerTierPending,
      rejectedProviders: sharedAvailability.rejectedProviders,
      daltonProfile,
      retry,
      all,
    };
  }

  const isLoading = isDaltonBuild && (callerTierPending || catalogPending || (localShared && (all.isLoading || listsLoading)));
  const unread = catalogUnread || (localShared && !all.isLoading && !listsLoading && all.unread);
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
      daltonProfile,
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
    daltonProfile,
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
