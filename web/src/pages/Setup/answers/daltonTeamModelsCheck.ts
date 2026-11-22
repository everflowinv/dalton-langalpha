// DALTON-HOOK(dalton-setup): lazy Dalton team-model availability checker (kept off the entry bundle)
import type { QueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';
import { apiErrorStatus } from '@/api/errors';
import {
  getAvailableModels,
  getProviderAvailableModels,
  type AvailableModels,
  type ModelsResponse,
} from '@/api/model';
import { hasAvailableDaltonModels } from '@/config/daltonSetup';
import { isPlatformMode } from '@/config/hostMode';
import type { ConfiguredProvider } from '@/hooks/useConfiguredProviders';
import {
  augmentPlatformWithLocal,
  buildVisibleModels,
  customPairKey,
  planKeyHolders,
  type ProviderRow,
} from '@/hooks/useFilteredModels';
import { allows, modelRefusedBy, restrictsModels, RULE, withoutRefusedModels } from '@/lib/accountRules';
import { payerResolver, type Payer } from '@/lib/modelPayer';
import { modelPrefs } from '@/lib/modelPreferences';
import { indexCatalog, ownProviderNames } from '@/lib/providerCatalog';
import { queryKeys } from '@/lib/queryKeys';
import { getPreferences } from '@/pages/Dashboard/utils/api';
import type { CustomModelEntry } from '@/components/model/types';
import type { AccountPolicy, UserPreferences } from '@/types/api';
import type { PlatformModelsResponse } from '@/types/platform';

const MODELS_STALE_MS = 5 * 60_000;
const PROVIDER_MODELS_STALE_MS = 10 * 60_000;

// DALTON-HOOK(dalton-setup): track per-generation refreshed providers and client-wide in-flight forced refreshes
interface RefreshTracker {
  gen: number;
  providers: Set<string>;
}

const retryGenerationByClient = new WeakMap<QueryClient, number>();
const refreshedProvidersByClient = new WeakMap<QueryClient, RefreshTracker>();
const inFlightRefreshByClient = new WeakMap<QueryClient, Map<string, Promise<AvailableModels>>>();

/** Mark an explicit user retry so the next availability check refreshes server-cached provider lists. */
export function bumpDaltonTeamModelsRetry(queryClient: QueryClient): number {
  const next = (retryGenerationByClient.get(queryClient) ?? 0) + 1;
  retryGenerationByClient.set(queryClient, next);
  return next;
}

function getOrCreateRefreshTracker(queryClient: QueryClient, retryGeneration: number): RefreshTracker | undefined {
  if (retryGeneration <= 0) return undefined;
  let tracker = refreshedProvidersByClient.get(queryClient);
  if (!tracker || tracker.gen !== retryGeneration) {
    tracker = { gen: retryGeneration, providers: new Set<string>() };
    refreshedProvidersByClient.set(queryClient, tracker);
  }
  return tracker;
}

function getOrCreateInFlightMap(queryClient: QueryClient): Map<string, Promise<AvailableModels>> {
  let map = inFlightRefreshByClient.get(queryClient);
  if (!map) {
    map = new Map<string, Promise<AvailableModels>>();
    inFlightRefreshByClient.set(queryClient, map);
  }
  return map;
}

/**
 * DALTON-HOOK(dalton-setup): cancel any in-flight normal provider list request and force-refresh with `?refresh=1`.
 * In-flight forced refreshes are tracked per QueryClient (across retry generations) and registered
 * synchronously before `await cancelQueries`, so consecutive retry clicks reuse the in-flight forced
 * refresh instead of cancelling it or joining a non-refresh request. A throttled `cached: true`
 * response is never marked as refreshed so a later retry can re-attempt once the cooldown clears.
 */
export async function forceRefreshProviderAvailableModels(
  queryClient: QueryClient,
  provider: string,
  retryGeneration: number,
): Promise<AvailableModels> {
  const whole = queryKeys.providerModels.available(provider, '');
  const tracker = getOrCreateRefreshTracker(queryClient, retryGeneration);
  const inFlight = getOrCreateInFlightMap(queryClient);
  const existing = inFlight.get(provider);
  if (existing) {
    const data = await existing;
    if (!data.cached && tracker && refreshedProvidersByClient.get(queryClient) === tracker) {
      tracker.providers.add(provider);
    }
    return data;
  }

  const run = (async (): Promise<AvailableModels> => {
    await queryClient.cancelQueries({ queryKey: whole, exact: true });
    let forcedRan = false;
    const data = await queryClient.fetchQuery<AvailableModels>({
      queryKey: whole,
      queryFn: ({ signal }) => {
        forcedRan = true;
        return getProviderAvailableModels(provider, { refresh: true, signal });
      },
      staleTime: 0,
      retry: false,
    });
    // DALTON-HOOK(dalton-setup): the forced response replaces the list, so drop per-search caches;
    // mark the provider refreshed only when the server answered live (not its rate-limited cache)
    if (forcedRan) {
      queryClient.removeQueries({
        queryKey: queryKeys.providerModels.availableFor(provider),
        predicate: (q) => q.queryKey.at(-1) !== whole.at(-1),
      });
      if (!data.cached) {
        const latestTracker = refreshedProvidersByClient.get(queryClient);
        if (latestTracker && latestTracker.gen >= retryGeneration) {
          latestTracker.providers.add(provider);
        }
      }
    }
    return data;
  })();

  inFlight.set(provider, run);
  try {
    return await run;
  } finally {
    if (inFlight.get(provider) === run) {
      inFlight.delete(provider);
    }
  }
}

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

export interface ProviderAvailabilityOutcome {
  data?: AvailableModels;
  error?: unknown;
}

/**
 * Derive the set of providers whose team credentials were explicitly rejected
 * by the vendor, propagating rejection across shared coding-plan holders when
 * no working holder remains.
 */
export function collectRejectedProviders(
  listedProviders: readonly string[],
  providerResults: readonly ProviderAvailabilityOutcome[],
  keyCandidates?: ModelsResponse['key_candidates'],
): ReadonlySet<string> {
  const out = new Set<string>();
  for (let i = 0; i < listedProviders.length; i += 1) {
    const provider = listedProviders[i];
    const r = providerResults[i];
    if (!provider || !r) continue;
    const fallbackRejected =
      r.data?.status === 'fallback'
      && typeof r.data.reason === 'string'
      && REJECTED_CREDENTIAL_REASONS.has(r.data.reason);
    const httpUnauthorized = apiErrorStatus(r.error) === 401;
    if (fallbackRejected || httpUnauthorized) {
      out.add(provider);
    }
  }
  if (out.size > 0 && keyCandidates) {
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
}

export interface CheckDaltonTeamModelsParams {
  queryClient: QueryClient;
  providers: ConfiguredProvider[];
  byokEnabled: boolean;
  providersUnread: boolean;
  policy: AccountPolicy | null;
  /** Non-zero generation bumped on explicit Retry to bypass server-cached provider lists with `?refresh=1`. */
  retryGeneration?: number;
}

export interface DaltonTeamModelsCheckResult {
  available: boolean;
  rejectedProviders: ReadonlySet<string>;
}

/**
 * Evaluates whether Dalton team-shared models are currently available (and
 * which providers were explicitly rejected), using the same React Query cache
 * keys and visibility/filtering pipeline as `useAllModels`.
 *
 * Called only when team credential sharing is active. Throws when required
 * catalog/provider reads fail so subscribers treat the result as unread rather
 * than empty.
 */
export async function checkDaltonTeamModelsAvailable({
  queryClient,
  providers: configuredProviders,
  byokEnabled: byokSwitch,
  providersUnread,
  policy,
  retryGeneration = retryGenerationByClient.get(queryClient) ?? 0,
}: CheckDaltonTeamModelsParams): Promise<DaltonTeamModelsCheckResult> {
  if (providersUnread) {
    throw new Error('Configured providers could not be read');
  }

  const [modelsData, preferences] = await Promise.all([
    queryClient.fetchQuery<ModelsResponse>({
      queryKey: queryKeys.models.all,
      queryFn: getAvailableModels,
      staleTime: MODELS_STALE_MS,
      retry: false,
    }),
    queryClient
      .fetchQuery<UserPreferences>({
        queryKey: queryKeys.user.preferences(),
        queryFn: getPreferences as () => Promise<UserPreferences>,
        staleTime: 60_000,
        retry: false,
      })
      .catch(() => null),
  ]);

  const byokEnabled = byokSwitch && allows(policy, RULE.ownApiKeys);
  const ownSignIns = allows(policy, RULE.ownSubscriptions);

  const customModelsRaw = preferences ? modelPrefs(preferences).custom_models : null;
  const customModels: CustomModelEntry[] = Array.isArray(customModelsRaw)
    ? (customModelsRaw as CustomModelEntry[])
    : [];

  const providerCatalog: Record<string, ProviderRow> = {};
  const customProviders = modelPrefs(preferences).custom_providers;
  for (const [provider, row] of indexCatalog(modelsData.provider_catalog ?? [], ownProviderNames(customProviders)).rows) {
    providerCatalog[provider] = { sdk: row.sdk, display_name: row.entry.display_name, connection: row.connection };
  }
  for (const cp of customProviders ?? []) {
    if (!providerCatalog[cp.name] && cp.parent_provider && providerCatalog[cp.parent_provider]) {
      providerCatalog[cp.name] = { sdk: providerCatalog[cp.parent_provider].sdk, connection: cp.name };
    }
  }

  const planHolders = planKeyHolders(configuredProviders, modelsData.key_candidates ?? {});
  const accessProviders: ConfiguredProvider[] = [
    ...configuredProviders,
    ...[...planHolders].map(([provider, holder]) => ({ ...holder, provider })),
  ];

  let rawPlatform: PlatformModelsResponse | null = null;
  if (isPlatformMode) {
    try {
      const data = await queryClient.fetchQuery<PlatformModelsResponse>({
        queryKey: queryKeys.platform.models(),
        queryFn: async () => {
          const res = await api.get<PlatformModelsResponse>('/api/auth/models');
          return res.data;
        },
        staleTime: MODELS_STALE_MS,
        retry: false,
      });
      rawPlatform = data && Array.isArray(data.byok_providers) ? data : null;
    } catch {
      rawPlatform = null;
    }
  }
  const platform = rawPlatform ? augmentPlatformWithLocal(rawPlatform, accessProviders, byokEnabled) : null;

  const listedProviders = configuredProviders
    .filter((p) => !p.is_custom && p.access_type !== 'local')
    .filter((p) => (p.access_type === 'oauth' ? ownSignIns && p.state === 'connected' : !isPlatformMode || byokEnabled))
    .map((p) => p.provider);

  // DALTON-HOOK(dalton-setup): force-refresh unrefreshed providers via client-wide in-flight deduplication
  const refreshed = getOrCreateRefreshTracker(queryClient, retryGeneration);

  const settled = await Promise.allSettled(
    listedProviders.map(async (provider) => {
      const shouldRefresh = retryGeneration > 0 && !refreshed?.providers.has(provider);
      if (shouldRefresh) {
        return forceRefreshProviderAvailableModels(queryClient, provider, retryGeneration);
      }
      return queryClient.fetchQuery<AvailableModels>({
        queryKey: queryKeys.providerModels.available(provider, ''),
        queryFn: ({ signal }) => getProviderAvailableModels(provider, { signal }),
        staleTime: PROVIDER_MODELS_STALE_MS,
        retry: false,
      });
    }),
  );

  const outcomes: ProviderAvailabilityOutcome[] = settled.map((entry) =>
    entry.status === 'fulfilled' ? { data: entry.value } : { error: entry.reason },
  );
  const rejectedProviders = collectRejectedProviders(listedProviders, outcomes, modelsData.key_candidates);

  const missingByProvider = new Map<string, Set<string>>();
  for (let i = 0; i < listedProviders.length; i += 1) {
    const provider = listedProviders[i];
    const data = outcomes[i]?.data;
    if (provider && data?.status === 'ok' && data.missing.length > 0) {
      missingByProvider.set(provider, new Set(data.missing));
    }
  }
  for (const [provider, holder] of planHolders) {
    const missing = missingByProvider.get(holder.provider);
    if (missing && !missingByProvider.has(provider)) {
      missingByProvider.set(provider, missing);
    }
  }

  const visible = buildVisibleModels(
    modelsData.models ?? {},
    modelsData.model_metadata ?? {},
    customModels,
    providerCatalog,
    platform,
    accessProviders,
    missingByProvider,
    modelsData.gateway_index,
  );

  const payerOf = payerResolver({
    platform,
    configured: configuredProviders,
    byokEnabled,
    keyCandidates: modelsData.key_candidates ?? {},
  });
  const payers: Record<string, Payer> = {};
  for (const [group, data] of Object.entries(visible.models)) {
    for (const m of data.models ?? []) {
      const meta = visible.metadata[m];
      const custom = visible.customPairs.has(customPairKey(group, m));
      const payer = custom ? payerOf(meta, group, m) : payerOf(meta, meta?.provider ?? group);
      if (payer) payers[m] = payer;
    }
  }

  const offered = withoutRefusedModels(visible.models, policy, visible.metadata);
  const offeredPayers = restrictsModels(policy)
    ? Object.fromEntries(
        Object.entries(payers).filter(([m]) => modelRefusedBy(policy, visible.metadata, m) === null),
      )
    : payers;

  return {
    available: hasAvailableDaltonModels(offered, visible.metadata, offeredPayers, true, rejectedProviders),
    rejectedProviders,
  };
}
