import type { QueryClient } from '@tanstack/react-query';
import { apiErrorStatus } from '@/api/errors';
import { testApiKey, type ModelsResponse } from '@/api/model';
import { isDaltonBuild } from '@/config/brand';
import { CLAUDE_CODE, isClaudeCodeModel } from '@/lib/claudeCode';
import type { Payer } from '@/lib/modelPayer';
import { modelPrefs } from '@/lib/modelPreferences';
import { queryKeys } from '@/lib/queryKeys';
import type { CustomModelEntry, ProviderModelsData } from '@/components/model/types';
import type { ConfiguredProvider } from '@/hooks/useConfiguredProviders';
import type { UserPreferences } from '@/types/api';
import {
  resolveLowEffortVariantKey,
  type AntigravitySeriesCatalog,
  type SelectableAntigravitySeries,
} from '@/lib/antigravitySeries';
import { EFFORT_ORDER, resolveEffort } from '@/lib/modelTuning';

/** Recommended primary series for Dalton onboarding. */
export const DALTON_DEFAULT_SERIES = 'antigravity-oauth:gemini-4-neon';

/** Recommended background series for Dalton onboarding. */
export const DALTON_BACKGROUND_SERIES = 'antigravity-oauth:gemini-4-hydrogen';

/** Fallback primary model key when antigravity_series catalog metadata is unavailable. */
export const DALTON_DEFAULT_MODEL = 'gemini-4-neon-high-oauth';

/** Recommended background model for Dalton deployments (background slot runs at fixed low effort). */
export const DALTON_BACKGROUND_MODEL = 'gemini-4-hydrogen-low-oauth';

export interface DaltonCandidateEntry {
  /** Preferred model key for this slot. */
  primary: string;
  /** Optional fallback model key used when `primary` is unavailable to the current member. */
  fallback?: string;
}

/**
 * Fixed ordered candidate list for Dalton's `/setup/default` model picker (and
 * Customize routing dropdowns). Only entries actually available to the member
 * are shown, preserving this exact order.
 */
export const DALTON_SETUP_CANDIDATES: readonly DaltonCandidateEntry[] = [
  { primary: 'gemini-4-neon-high-oauth' },
  { primary: 'gemini-4-carbon-medium-oauth' },
  { primary: 'claude-opus-5-5-oauth', fallback: 'claude-opus-5-5-medium-oauth' },
  { primary: 'gpt-6.2-astra-oauth' },
  { primary: 'gpt-6.2-sol-oauth' },
];

export type DaltonDeploymentProfile = 'everflow' | 'core';

/**
 * Resolve the active Dalton deployment profile from the backend's runtime
 * `/api/v1/models` `dalton_profile` field, falling back to build-time
 * `VITE_DALTON_PROFILE` (`'everflow'` when neither specifies `'core'`).
 * Never inferred from brand name or mark.
 */
export function resolveDaltonProfile(runtimeProfile?: string | null): DaltonDeploymentProfile {
  const runtime = runtimeProfile?.trim().toLowerCase();
  if (runtime === 'core') return 'core';
  if (runtime === 'everflow') return 'everflow';
  const envProfile = (import.meta.env.VITE_DALTON_PROFILE as string | undefined)?.trim().toLowerCase();
  return envProfile === 'core' ? 'core' : 'everflow';
}

/** True when the active deployment is Standalone Core (`dalton_profile === 'core'`). */
export function isCoreDaltonProfile(runtimeProfile?: string | null): boolean {
  return resolveDaltonProfile(runtimeProfile) === 'core';
}

/** True when Dalton setup should stay restricted to the 5 curated OAuth candidates (Everflow only). */
export function isDaltonCuratedOnly(dalton: boolean, runtimeProfile?: string | null): boolean {
  return dalton && !isCoreDaltonProfile(runtimeProfile);
}

/** True when `provider` satisfies the `/setup/model` connection check under Dalton. */
export function isDaltonSetupProviderAllowed(
  provider: string,
  curatedProviders: ReadonlySet<string>,
  runtimeProfile?: string | null,
): boolean {
  if (isCoreDaltonProfile(runtimeProfile)) return true;
  return curatedProviders.has(provider);
}

/** Consolidated configuration for `/setup/model`'s subscription/key step under Dalton. */
export interface DaltonModelStepConfig {
  curatedOnly: boolean;
  showPlanGroups: boolean;
  showApiKeyLink: boolean;
  allowedSignIns: ReadonlySet<string> | undefined;
  canContinue: boolean;
}

export function getDaltonModelStepConfig(params: {
  dalton: boolean;
  daltonProfile?: string | null;
  ownKeys: boolean;
  configured: ReadonlySet<string>;
  curatedProviders: ReadonlySet<string>;
}): DaltonModelStepConfig {
  const curatedOnly = isDaltonCuratedOnly(params.dalton, params.daltonProfile);
  const canContinue = !curatedOnly || [...params.configured].some((p) => params.curatedProviders.has(p));
  return {
    curatedOnly,
    showPlanGroups: !curatedOnly && params.ownKeys,
    showApiKeyLink: !curatedOnly && params.ownKeys,
    allowedSignIns: curatedOnly ? params.curatedProviders : undefined,
    canContinue,
  };
}

export interface DaltonSetupFilterOptions {
  profile?: string | null;
  metadata?: Record<string, DaltonModelMeta | undefined>;
  /** Version-aligned curated setup candidate list returned by `GET /api/v1/models` (`dalton_setup_candidates`). */
  serverCandidates?: readonly string[] | null;
}

const ratingScore = (value: number | undefined): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : -1;

/** True when metadata has positive evidence (catalog, overlay, or live probe) of tool calling. */
export function isDaltonToolCallingConfirmed(meta: DaltonModelMeta | undefined): boolean {
  return meta?.tool_calling === true;
}

/** True when metadata explicitly marks the model as non-tool-calling (text-only or failed probe). */
export function isDaltonToolCallingRejected(meta: DaltonModelMeta | undefined): boolean {
  return meta?.tool_calling === false;
}

/** True when tool-calling capability has neither been confirmed nor rejected. */
export function isDaltonToolCallingUnverified(meta: DaltonModelMeta | undefined): boolean {
  return meta?.tool_calling !== true && meta?.tool_calling !== false;
}

/** True unless model metadata explicitly marks the model as non-tool-calling (text-only). */
export function isDaltonToolCallingModel(meta: DaltonModelMeta | undefined): boolean {
  return !isDaltonToolCallingRejected(meta);
}

/**
 * Merge per-model Core `/api/v1/keys/test` recorded tool-calling probe verdicts
 * from configured provider rows into model metadata (only filling entries whose
 * `tool_calling` is currently unknown).
 *
 * Strictly per-model: a probe result for one model NEVER propagates to other
 * unprobed models on the same provider.
 */
export function applyDaltonProviderToolProbes<T extends DaltonModelMeta>(
  metadata: Record<string, T>,
  providers: readonly ConfiguredProvider[],
  customModels: readonly CustomModelEntry[] = [],
): Record<string, T> {
  if (!providers.length) return metadata;
  const byProvider = new Map<string, Record<string, boolean>>();
  for (const p of providers) {
    if (p.tool_calling_models && Object.keys(p.tool_calling_models).length > 0) {
      byProvider.set(p.provider, p.tool_calling_models);
    }
  }
  if (byProvider.size === 0) return metadata;

  const customByName = new Map<string, CustomModelEntry>();
  for (const cm of customModels) {
    customByName.set(cm.name, cm);
  }

  let changed = false;
  const out: Record<string, T> = { ...metadata };
  for (const [modelKey, entry] of Object.entries(metadata)) {
    if (!entry || typeof entry.tool_calling === 'boolean') continue;
    const cm = customByName.get(modelKey);
    const providerKey = cm?.provider ?? entry.provider;
    if (!providerKey) continue;
    const probedModels = byProvider.get(providerKey);
    if (!probedModels) continue;
    const modelId = cm?.model_id ?? entry.model_id ?? modelKey;
    const verdict = probedModels[modelId];
    if (typeof verdict === 'boolean') {
      out[modelKey] = { ...entry, tool_calling: verdict };
      changed = true;
    }
  }
  return changed ? out : metadata;
}

/**
 * Resolve the model id to probe on `/api/v1/keys/test` in Core Dalton builds
 * when the user clicks "Test" on an API-key form. Returns `undefined` in
 * Everflow or non-Dalton builds so existing key tests stay untouched.
 */
export function resolveDaltonKeyTestModel(
  queryClient: QueryClient,
  provider: string,
  modelsFrom?: string,
): string | undefined {
  if (!isDaltonBuild) return undefined;
  const modelsData = queryClient.getQueryData<ModelsResponse>(queryKeys.models.all);
  if (!isCoreDaltonProfile(modelsData?.dalton_profile)) return undefined;

  const prefs = queryClient.getQueryData<UserPreferences>(queryKeys.user.preferences());
  const mp = modelPrefs(prefs);
  const customModels = (mp.custom_models ?? []) as CustomModelEntry[];
  const customForProvider = customModels.filter((cm) => cm.provider === provider);
  const preferred = mp.preferred_model;

  if (preferred) {
    const prefCustom = customForProvider.find((cm) => cm.name === preferred);
    if (prefCustom?.model_id) return prefCustom.model_id;
    const prefMeta = modelsData?.model_metadata?.[preferred];
    if (prefMeta && (prefMeta.provider === provider || (modelsFrom && prefMeta.provider === modelsFrom))) {
      return prefMeta.model_id ?? preferred;
    }
  }
  if (customForProvider[0]?.model_id) {
    return customForProvider[0].model_id;
  }
  const sourceKey = modelsFrom || provider;
  const listed = modelsData?.models?.[sourceKey]?.models ?? [];
  if (listed.length > 0) {
    const metadata = modelsData?.model_metadata ?? {};
    const unverified = listed.find((m) => isDaltonToolCallingUnverified(metadata[m]));
    const picked = unverified ?? listed[0];
    if (picked) return metadata[picked]?.model_id ?? picked;
  }
  return undefined;
}

/**
 * In Standalone Core Dalton builds, probe the newly saved custom provider's
 * selected models via `/api/v1/keys/test` so per-model tool-calling verdicts
 * are recorded before the setup wizard picks defaults. Returns any format hint
 * (`tool_calling_hint`) surfaced by the server so the caller can display it.
 * Strict no-op (`null`) in Everflow or non-Dalton builds; errors never fail the save.
 */
export async function probeDaltonCustomProviderModels(
  queryClient: QueryClient,
  provider: string,
  apiKey: string,
  baseUrl: string,
  batch: readonly CustomModelEntry[],
): Promise<string | null> {
  if (!isDaltonBuild || batch.length === 0) return null;
  const modelsData = queryClient.getQueryData<ModelsResponse>(queryKeys.models.all);
  if (!isCoreDaltonProfile(modelsData?.dalton_profile)) return null;

  const modelIds: string[] = [];
  const seen = new Set<string>();
  for (const cm of batch) {
    const mid = cm.model_id?.trim();
    if (mid && !seen.has(mid)) {
      seen.add(mid);
      modelIds.push(mid);
    }
  }
  if (modelIds.length === 0) return null;
  try {
    const res = await testApiKey(provider, apiKey, baseUrl, modelIds[0], modelIds);
    return typeof res?.tool_calling_hint === 'string' && res.tool_calling_hint
      ? res.tool_calling_hint
      : null;
  } catch (err) {
    // Surface HTTP 429 throttle so the user retries after cooldown instead of silently skipping verification.
    if (apiErrorStatus(err) === 429 || (err as { status?: number })?.status === 429) {
      return '检查过于频繁，请稍后重试';
    }
    // Probing is best-effort: unverified models stay listed and labeled in setup.
    return null;
  }
}

const CURATED_FALLBACK_BY_PRIMARY: ReadonlyMap<string, string> = new Map(
  DALTON_SETUP_CANDIDATES
    .filter((entry): entry is { primary: string; fallback: string } => Boolean(entry.fallback))
    .map((entry) => [entry.primary, entry.fallback]),
);

/**
 * Resolve curated setup candidates against the member's currently available
 * models.
 *
 * When `serverCandidates` (from `GET /api/v1/models` `dalton_setup_candidates`)
 * is provided (including an empty `[]` when all curated models were hidden or
 * retired), it is authoritative and replaces the static `DALTON_SETUP_CANDIDATES`
 * list so setup admission and recommendations stay aligned with the compiled
 * catalog revision.
 *
 * - Everflow: preserves strict curated ordering (`[]` when none is available).
 * - Core: excludes curated candidates explicitly marked `tool_calling === false`.
 */
export function resolveDaltonSetupCandidates(
  available: Iterable<string>,
  metadata?: Record<string, DaltonModelMeta | undefined>,
  profile?: string | null,
  serverCandidates?: readonly string[] | null,
): string[] {
  const set = available instanceof Set ? available : new Set(available);
  const core = isCoreDaltonProfile(profile);
  const eligible = (model: string): boolean =>
    set.has(model) && (!core || !isDaltonToolCallingRejected(metadata?.[model]));
  const out: string[] = [];
  const seen = new Set<string>();

  if (Array.isArray(serverCandidates)) {
    for (const model of serverCandidates) {
      if (eligible(model)) {
        if (!seen.has(model)) {
          seen.add(model);
          out.push(model);
        }
      } else {
        const fallback = CURATED_FALLBACK_BY_PRIMARY.get(model);
        if (fallback && eligible(fallback) && !seen.has(fallback)) {
          seen.add(fallback);
          out.push(fallback);
        }
      }
    }
    return out;
  }

  for (const entry of DALTON_SETUP_CANDIDATES) {
    if (eligible(entry.primary)) {
      out.push(entry.primary);
    } else if (entry.fallback && eligible(entry.fallback)) {
      out.push(entry.fallback);
    }
  }
  return out;
}

/**
 * Return all connected non-rejected models (including BYOK and custom OpenAI-compatible
 * gateway models) ordered by:
 * 1. Confirmed tool-calling models (`tool_calling === true`) ahead of unverified models
 * 2. Highest `intelligence`, then highest `speed`, preserving catalog order on ties.
 *
 * Models explicitly marked `tool_calling === false` are excluded.
 */
export function resolveDaltonCoreFallbackModels(
  available: Iterable<string>,
  metadata?: Record<string, DaltonModelMeta | undefined>,
  options: { confirmedOnly?: boolean } = {},
): string[] {
  const unique: string[] = [];
  const seen = new Set<string>();
  for (const model of available) {
    if (seen.has(model)) continue;
    seen.add(model);
    const meta = metadata?.[model];
    if (isDaltonToolCallingRejected(meta)) continue;
    if (options.confirmedOnly && !isDaltonToolCallingConfirmed(meta)) continue;
    unique.push(model);
  }
  return unique
    .map((model, index) => ({ model, index }))
    .sort((a, b) => {
      const confirmedDiff =
        Number(isDaltonToolCallingConfirmed(metadata?.[b.model])) -
        Number(isDaltonToolCallingConfirmed(metadata?.[a.model]));
      if (confirmedDiff !== 0) return confirmedDiff;
      const intelDiff =
        ratingScore(metadata?.[b.model]?.intelligence) - ratingScore(metadata?.[a.model]?.intelligence);
      if (intelDiff !== 0) return intelDiff;
      const speedDiff =
        ratingScore(metadata?.[b.model]?.speed) - ratingScore(metadata?.[a.model]?.speed);
      if (speedDiff !== 0) return speedDiff;
      return a.index - b.index;
    })
    .map((entry) => entry.model);
}

/**
 * Resolve setup candidate models for Dalton:
 * - Everflow: returns `DALTON_SETUP_CANDIDATES` in fixed order (`[]` when none is available).
 * - Standalone Core: uses `DALTON_SETUP_CANDIDATES` when at least one curated model is
 *   confirmed (`tool_calling === true`); otherwise returns all connected non-rejected models
 *   (confirmed first, unverified after) so displayed list and auto-selected defaults always
 *   come from the exact same candidate pool.
 */
export function resolveDaltonSetupModels(
  available: Iterable<string>,
  options: DaltonSetupFilterOptions = {},
  fallbackPool?: Iterable<string>,
): string[] {
  const core = isCoreDaltonProfile(options.profile);
  const curated = resolveDaltonSetupCandidates(
    available,
    options.metadata,
    options.profile,
    options.serverCandidates,
  );
  if (!core) return curated;
  if (curated.length > 0 && curated.some((m) => isDaltonToolCallingConfirmed(options.metadata?.[m]))) {
    return curated;
  }
  return resolveDaltonCoreFallbackModels(fallbackPool ?? available, options.metadata);
}

/**
 * Resolve ONLY models eligible for automatic default/recommendation selection.
 *
 * Always a subset of :func:`resolveDaltonSetupModels` so recommended/preselected
 * defaults can never fall outside the list shown to the user:
 * - Standalone Core: confirmed (`tool_calling === true`) models from the displayed pool.
 * - Everflow: identical to `resolveDaltonSetupCandidates`.
 */
export function resolveDaltonConfirmedSetupModels(
  available: Iterable<string>,
  options: DaltonSetupFilterOptions = {},
  fallbackPool?: Iterable<string>,
): string[] {
  const displayed = resolveDaltonSetupModels(available, options, fallbackPool);
  if (!isCoreDaltonProfile(options.profile)) return displayed;
  return displayed.filter((m) => isDaltonToolCallingConfirmed(options.metadata?.[m]));
}

/**
 * Filter Background slot candidates by confirmed tool-calling capability in Core
 * profile so non-tool-calling or unverified models are never auto-recommended
 * for Background. Preserves Everflow and non-Dalton slot pools untouched.
 */
export function filterDaltonCoreSlotModels(
  slotModels: readonly string[],
  metadata: Record<string, DaltonModelMeta | undefined>,
  dalton: boolean,
  profile?: string | null,
): string[] {
  if (!dalton || !isCoreDaltonProfile(profile)) return [...slotModels];
  return slotModels.filter((m) => isDaltonToolCallingConfirmed(metadata[m]));
}

/**
 * Whether a deployment's `systemDefaults.background_model` may be offered or
 * kept as the "Auto" Background option under Dalton.
 *
 * - Everflow: preserves `isDaltonAllowedModel` verbatim.
 * - Core: additionally requires confirmed tool-calling support (`tool_calling === true`).
 */
export function isDaltonAllowedSystemBackground(
  meta: DaltonModelMeta | undefined,
  payer: Payer | undefined,
  rejectedProviders: ReadonlySet<string> | undefined,
  profile?: string | null,
): boolean {
  if (!isDaltonAllowedModel(meta, payer, rejectedProviders)) return false;
  if (isCoreDaltonProfile(profile) && !isDaltonToolCallingConfirmed(meta)) return false;
  return true;
}

/**
 * Resolve the initial/recommended Background pick for `/setup/default`.
 *
 * - Everflow / non-Dalton: falls back to `autoValue` whenever `systemBackground` exists.
 * - Core: never auto-selects `autoValue` when filtered `recommendedBackground` is null.
 */
export function resolveDaltonRecommendedBackground(
  recommendedBackground: string | null,
  systemBackground: string | null,
  autoValue: string,
  dalton: boolean,
  profile?: string | null,
): string {
  if (recommendedBackground) return recommendedBackground;
  if (dalton && isCoreDaltonProfile(profile)) return '';
  return systemBackground ? autoValue : '';
}

/**
 * Pick the default model to auto-save when a provider finishes connecting in setup.
 * Keeps curated-only selection in Everflow and auto-picks ONLY confirmed
 * tool-calling models in Core (unverified models are never auto-selected).
 */
export function pickDaltonConnectedDefault(
  offered: readonly string[],
  metadata?: Record<string, DaltonModelMeta | undefined>,
  profile?: string | null,
  serverCandidates?: readonly string[] | null,
): string | null {
  return resolveDaltonConfirmedSetupModels(offered, { profile, metadata, serverCandidates })[0] ?? null;
}

/**
 * Narrow provider model groups to the resolved curated candidates in
 * their exact fixed order (preserving provider grouping).
 *
 * When none of the curated candidates is available (or in Core, none is confirmed):
 * - Everflow profile returns `{}` (never falls back to uncurated models).
 * - Standalone Core profile returns all connected non-rejected models grouped by
 *   provider, with confirmed tool-calling models ordered ahead of unverified ones.
 */
export function filterDaltonCuratedModels(
  groups: Record<string, ProviderModelsData>,
  dalton: boolean,
  options: DaltonSetupFilterOptions = {},
): Record<string, ProviderModelsData> {
  if (!dalton) return groups;
  const core = isCoreDaltonProfile(options.profile);
  const ownerOf = new Map<string, string>();
  for (const [provider, data] of Object.entries(groups)) {
    for (const m of data.models ?? []) {
      if (!ownerOf.has(m)) ownerOf.set(m, provider);
    }
  }
  const curated = resolveDaltonSetupCandidates(
    ownerOf.keys(),
    options.metadata,
    options.profile,
    options.serverCandidates,
  );
  const useCurated =
    curated.length > 0 && (!core || curated.some((m) => isDaltonToolCallingConfirmed(options.metadata?.[m])));
  if (!useCurated) {
    if (!core) return {};
    const fallbackOut: Record<string, ProviderModelsData> = {};
    for (const [provider, data] of Object.entries(groups)) {
      const models = resolveDaltonCoreFallbackModels(data.models ?? [], options.metadata);
      if (models.length > 0) {
        fallbackOut[provider] = { ...data, models };
      }
    }
    return fallbackOut;
  }
  const out: Record<string, ProviderModelsData> = {};
  for (const m of curated) {
    const provider = ownerOf.get(m);
    if (!provider) continue;
    if (!out[provider]) {
      out[provider] = { ...groups[provider], models: [] };
    }
    out[provider].models!.push(m);
  }
  return out;
}

/**
 * Resolve the recommended variant key for a Dalton Antigravity series from
 * `antigravity_series` metadata, falling back to `fallbackModel` when series
 * metadata is absent (e.g. legacy test mocks). When `preferLowEffort` is set
 * (for fixed-low slots like Background), prefers `'low'` or the series' lowest
 * available effort instead of the series default.
 */
export function resolveDaltonSeriesDefaultModel(
  seriesId: string,
  candidates: readonly string[],
  options: {
    seriesCatalog?: AntigravitySeriesCatalog | null;
    selectableSeries?: readonly SelectableAntigravitySeries[] | null;
    fallbackModel?: string;
    preferLowEffort?: boolean;
  } = {},
): string | null {
  const candidateSet = new Set(candidates);
  const selectable = options.selectableSeries?.find((s) => s.seriesId === seriesId);
  if (selectable) {
    const picked = options.preferLowEffort
      ? resolveLowEffortVariantKey(selectable)
      : selectable.defaultVariantKey;
    if (picked && candidateSet.has(picked)) return picked;
  }
  const series = options.seriesCatalog?.bySeriesId[seriesId];
  if (series) {
    const rawEfforts = Object.entries(series.variants)
      .filter(([, variantKey]) => candidateSet.has(variantKey))
      .map(([effort]) => effort);
    if (rawEfforts.length > 0) {
      const availableEfforts = [
        ...EFFORT_ORDER.filter((lv) => rawEfforts.includes(lv)),
        ...rawEfforts.filter((lv) => !EFFORT_ORDER.includes(lv)),
      ];
      const chosen = options.preferLowEffort
        ? (availableEfforts.includes('low') ? 'low' : availableEfforts[0])
        : (resolveEffort(series.default_effort, availableEfforts) ?? availableEfforts[0]);
      const key = chosen ? series.variants[chosen] : undefined;
      if (key && candidateSet.has(key)) return key;
    }
  }
  if (options.fallbackModel && candidateSet.has(options.fallbackModel)) {
    return options.fallbackModel;
  }
  return null;
}

/**
 * Resolve the label shown for the Background slot on the Setup Recommended card.
 *
 * - When a specific recommended background model exists, display its name (all profiles).
 * - In Standalone Core when `recommended.background` is null, display "Follows primary model"
 *   so the card matches both Customize's initial blank value and the saved `background_follows: null`.
 * - In Everflow (and non-Dalton builds), preserve the original deployment Auto label.
 */
export function resolveDaltonRecommendedBackgroundLabel(
  recommendedBackgroundModel: string | null,
  nameOf: (model: string) => string,
  deploymentBackgroundLabel: string | undefined,
  followsPrimaryLabel: string,
  dalton: boolean,
  profile?: string | null,
): string | undefined {
  if (recommendedBackgroundModel) return nameOf(recommendedBackgroundModel);
  if (dalton && isCoreDaltonProfile(profile)) return followsPrimaryLabel;
  return deploymentBackgroundLabel;
}

export interface DaltonModelMeta {
  sdk?: string;
  provider?: string;
  model_id?: string;
  tool_calling?: boolean;
  intelligence?: number;
  speed?: number;
}

/** True when model metadata describes a Claude Code model (unsupported in Dalton). */
export function isClaudeCodeMeta(meta: DaltonModelMeta | undefined): boolean {
  return isClaudeCodeModel(meta) || meta?.provider === CLAUDE_CODE;
}

/** True when a model is permitted in Dalton (neither Claude Code nor credits-billed nor on a rejected provider). */
export function isDaltonAllowedModel(
  meta: DaltonModelMeta | undefined,
  payer: Payer | undefined,
  rejectedProviders?: ReadonlySet<string>,
): boolean {
  if (isClaudeCodeMeta(meta) || meta?.provider === 'platform' || payer === 'credits') return false;
  if (rejectedProviders?.size && meta?.provider && rejectedProviders.has(meta.provider)) return false;
  return true;
}

/** Omit `credits` payer labels under Dalton (Dalton does not bill via credits). */
export function filterDaltonPayers(
  payers: Record<string, Payer>,
  dalton: boolean,
): Record<string, Payer> {
  if (!dalton) return payers;
  const out: Record<string, Payer> = {};
  for (const [model, payer] of Object.entries(payers)) {
    if (payer !== 'credits') out[model] = payer;
  }
  return out;
}

/** Remove Claude Code, credits-billed, and rejected-credential models from a provider-grouped map under Dalton. */
export function filterDaltonProviderModels(
  groups: Record<string, ProviderModelsData>,
  metadata: Record<string, DaltonModelMeta | undefined>,
  payers: Record<string, Payer>,
  dalton: boolean,
  rejectedProviders?: ReadonlySet<string>,
): Record<string, ProviderModelsData> {
  if (!dalton) return groups;
  const out: Record<string, ProviderModelsData> = {};
  for (const [provider, data] of Object.entries(groups)) {
    if (provider === CLAUDE_CODE || provider === 'platform' || rejectedProviders?.has(provider)) continue;
    const allowed = (data.models ?? []).filter((m) =>
      isDaltonAllowedModel(metadata[m], payers[m], rejectedProviders),
    );
    if (allowed.length > 0) {
      out[provider] = { ...data, models: allowed };
    }
  }
  return out;
}

/**
 * True when the Dalton-filtered provider map contains at least one usable model
 * from the active setup candidate pool.
 *
 * - Everflow: at least one of the 5 curated OAuth candidates (original rule).
 * - Standalone Core: at least one curated or connected BYOK/custom model confirmed
 *   to support tool calling (`tool_calling === true`).
 */
export function hasAvailableDaltonModels(
  groups: Record<string, ProviderModelsData>,
  metadata: Record<string, DaltonModelMeta | undefined>,
  payers: Record<string, Payer>,
  dalton: boolean,
  rejectedProviders?: ReadonlySet<string>,
  runtimeProfile?: string | null,
  serverCandidates?: readonly string[] | null,
): boolean {
  const filtered = filterDaltonProviderModels(groups, metadata, payers, dalton, rejectedProviders);
  const offered = Object.values(filtered).flatMap((p) => p.models ?? []);
  if (!dalton) return offered.length > 0;
  return (
    resolveDaltonConfirmedSetupModels(offered, {
      profile: runtimeProfile,
      metadata,
      serverCandidates,
    }).length > 0
  );
}
