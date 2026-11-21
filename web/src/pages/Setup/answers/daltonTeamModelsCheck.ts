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

interface RefreshTracker {
  gen: number;
  providers: Set<string>;
  inFlight: Map<string, symbol>;
}

const retryGenerationByClient = new WeakMap<QueryClient, number>();
const refreshedProvidersByClient = new WeakMap<QueryClient, RefreshTracker>();

/** Mark an explicit user retry so the next shared availability check refreshes server-cached provider lists. */
export function bumpDaltonTeamModelsRetry(queryClient: QueryClient): void {
  retryGenerationByClient.set(queryClient, (retryGenerationByClient.get(queryClient) ?? 0) + 1);
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

  let refreshed = refreshedProvidersByClient.get(queryClient);
  if (retryGeneration > 0 && (!refreshed || refreshed.gen !== retryGeneration)) {
    refreshed = { gen: retryGeneration, providers: new Set<string>(), inFlight: new Map<string, symbol>() };
    refreshedProvidersByClient.set(queryClient, refreshed);
  }

  const settled = await Promise.allSettled(
    listedProviders.map(async (provider) => {
      const shouldRefresh = retryGeneration > 0 && !refreshed?.providers.has(provider);
      const whole = queryKeys.providerModels.available(provider, '');
      const tracker = shouldRefresh ? refreshed : undefined;
      let forcedToken: symbol | null = null;
      if (shouldRefresh && tracker && !tracker.inFlight.has(provider)) {
        await queryClient.cancelQueries({ queryKey: whole, exact: true });
        forcedToken = Symbol(provider);
        tracker.inFlight.set(provider, forcedToken);
      }
      let forcedRan = false;
      try {
        const data = await queryClient.fetchQuery<AvailableModels>({
          queryKey: whole,
          queryFn: ({ signal }) => {
            if (shouldRefresh) forcedRan = true;
            return getProviderAvailableModels(provider, {
              ...(shouldRefresh ? { refresh: true } : {}),
              signal,
            });
          },
          staleTime: shouldRefresh ? 0 : PROVIDER_MODELS_STALE_MS,
          retry: false,
        });
        if (shouldRefresh && forcedRan && refreshedProvidersByClient.get(queryClient) === tracker) {
          tracker?.providers.add(provider);
          queryClient.removeQueries({
            queryKey: queryKeys.providerModels.availableFor(provider),
            predicate: (q) => q.queryKey.at(-1) !== whole.at(-1),
          });
        }
        return data;
      } finally {
        if (forcedToken && tracker?.inFlight.get(provider) === forcedToken) {
          tracker.inFlight.delete(provider);
        }
      }
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
