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
import { applyDaltonProviderToolProbes, hasAvailableDaltonModels } from '@/config/daltonSetup';
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
interface InFlightRefresh {
  gen: number;
  promise: Promise<AvailableModels>;
}

interface RefreshTracker {
  gen: number;
  providers: Set<string>;
  /** Forced refreshes already in flight when this retry generation started, shareable even if they settle before provider refresh runs. */
  sharedInFlight: Map<string, InFlightRefresh>;
  /** Providers for which this retry generation already issued its own follow-up refresh=1 after a reused refresh failed or was stale. */
  retriedProviders: Set<string>;
}

const retryGenerationByClient = new WeakMap<QueryClient, number>();
const refreshedProvidersByClient = new WeakMap<QueryClient, RefreshTracker>();
const inFlightRefreshByClient = new WeakMap<QueryClient, Map<string, InFlightRefresh>>();

/** True only when the server answered with a live, non-fallback provider list. */
function isLiveRefreshResult(data: AvailableModels): boolean {
  return !data.cached && data.status !== 'fallback';
}

function isLatestRetryGeneration(queryClient: QueryClient, retryGeneration: number): boolean {
  if (retryGeneration <= 0) return false;
  const latestGen = Math.max(
    retryGenerationByClient.get(queryClient) ?? 0,
    refreshedProvidersByClient.get(queryClient)?.gen ?? 0,
  );
  return retryGeneration >= latestGen;
}

/** Read the latest explicit retry generation for a QueryClient. */
export function getDaltonTeamModelsRetryGeneration(queryClient: QueryClient): number {
  return retryGenerationByClient.get(queryClient) ?? 0;
}

/**
 * Mark an explicit user retry so the next availability check refreshes server-cached provider lists.
 * Records any forced `?refresh=1` requests already in flight at retry start so this retry shares
 * them even if they settle while pre-refresh invalidations or catalog reads are still pending.
 */
export function bumpDaltonTeamModelsRetry(queryClient: QueryClient): number {
  const next = (retryGenerationByClient.get(queryClient) ?? 0) + 1;
  retryGenerationByClient.set(queryClient, next);
  const inFlight = inFlightRefreshByClient.get(queryClient);
  refreshedProvidersByClient.set(queryClient, {
    gen: next,
    providers: new Set<string>(),
    sharedInFlight: inFlight && inFlight.size > 0 ? new Map(inFlight) : new Map(),
    retriedProviders: new Set<string>(),
  });
  return next;
}

function getOrCreateRefreshTracker(queryClient: QueryClient, retryGeneration: number): RefreshTracker | undefined {
  if (retryGeneration <= 0) return undefined;
  let tracker = refreshedProvidersByClient.get(queryClient);
  if (!tracker || tracker.gen < retryGeneration) {
    tracker = {
      gen: retryGeneration,
      providers: new Set<string>(),
      sharedInFlight: new Map(),
      retriedProviders: new Set<string>(),
    };
    refreshedProvidersByClient.set(queryClient, tracker);
  }
  return tracker;
}

function getOrCreateInFlightMap(queryClient: QueryClient): Map<string, InFlightRefresh> {
  let map = inFlightRefreshByClient.get(queryClient);
  if (!map) {
    map = new Map<string, InFlightRefresh>();
    inFlightRefreshByClient.set(queryClient, map);
  }
  return map;
}

/**
 * DALTON-HOOK(dalton-setup): cancel any in-flight normal provider list request and force-refresh with `?refresh=1`.
 * In-flight forced refreshes are tracked per QueryClient (across retry generations), registered
 * synchronously before `await cancelQueries`, and snapshotted onto each new retry generation at
 * retry start (`bumpDaltonTeamModelsRetry`), so consecutive retry clicks reuse a live forced
 * refresh even if it finishes while pre-refresh invalidations are still pending. Only a live,
 * non-fallback response (`!cached && status !== 'fallback'`) marks the provider refreshed or
 * satisfies a newer retry generation; if a reused earlier-generation refresh throws or returns
 * `cached: true` / `status: 'fallback'`, the latest retry generation issues at most one fresh
 * `?refresh=1` of its own (subject to server throttling).
 */
export async function forceRefreshProviderAvailableModels(
  queryClient: QueryClient,
  provider: string,
  retryGeneration: number,
): Promise<AvailableModels> {
  const whole = queryKeys.providerModels.available(provider, '');
  const tracker = getOrCreateRefreshTracker(queryClient, retryGeneration);
  const ownTracker = tracker && tracker.gen === retryGeneration ? tracker : undefined;
  const inFlight = getOrCreateInFlightMap(queryClient);
  const existing = inFlight.get(provider) ?? ownTracker?.sharedInFlight.get(provider);
  if (existing) {
    let outcome: { ok: true; data: AvailableModels } | { ok: false; err: unknown };
    try {
      outcome = { ok: true, data: await existing.promise };
    } catch (err) {
      outcome = { ok: false, err };
    }

    if (outcome.ok && isLiveRefreshResult(outcome.data)) {
      if (tracker && refreshedProvidersByClient.get(queryClient) === tracker) {
        tracker.providers.add(provider);
      }
      return outcome.data;
    }

    if (ownTracker?.sharedInFlight.get(provider) === existing) {
      ownTracker.sharedInFlight.delete(provider);
    }
    if (inFlight.get(provider) === existing) {
      inFlight.delete(provider);
    }

    if (existing.gen < retryGeneration && isLatestRetryGeneration(queryClient, retryGeneration)) {
      const currentInFlight = inFlight.get(provider);
      if (currentInFlight && currentInFlight.gen >= retryGeneration) {
        const data = await currentInFlight.promise;
        if (isLiveRefreshResult(data) && tracker && refreshedProvidersByClient.get(queryClient) === tracker) {
          tracker.providers.add(provider);
        }
        return data;
      }
      if (!ownTracker?.retriedProviders.has(provider)) {
        ownTracker?.retriedProviders.add(provider);
      } else if (outcome.ok) {
        return outcome.data;
      } else {
        throw outcome.err;
      }
    } else if (outcome.ok) {
      return outcome.data;
    } else {
      throw outcome.err;
    }
  }

  let entry!: InFlightRefresh;
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
    // mark the provider refreshed only when the server answered live (not its rate-limited cache or fallback)
    if (forcedRan) {
      queryClient.removeQueries({
        queryKey: queryKeys.providerModels.availableFor(provider),
        predicate: (q) => q.queryKey.at(-1) !== whole.at(-1),
      });
      if (isLiveRefreshResult(data)) {
        const latestTracker = refreshedProvidersByClient.get(queryClient);
        if (latestTracker && latestTracker.gen >= retryGeneration) {
          latestTracker.providers.add(provider);
        }
      }
    }
    return data;
  })();

  entry = { gen: retryGeneration, promise: run };
  inFlight.set(provider, entry);
  try {
    return await run;
  } finally {
    if (inFlight.get(provider) === entry) {
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

  // DALTON-HOOK(standalone-core): overlay Core recorded tool-calling probe verdicts before availability gating
  const effectiveMetadata = applyDaltonProviderToolProbes(visible.metadata, configuredProviders, customModels);

  // DALTON-HOOK(team-model-catalog): pass version-aligned server `dalton_setup_candidates` into availability check
  return {
    available: hasAvailableDaltonModels(
      offered,
      effectiveMetadata,
      offeredPayers,
      true,
      rejectedProviders,
      modelsData.dalton_profile,
      modelsData.dalton_setup_candidates,
    ),
    rejectedProviders,
  };
}
