import { CLAUDE_CODE, isClaudeCodeModel } from '@/lib/claudeCode';
import type { Payer } from '@/lib/modelPayer';
import type { ProviderModelsData } from '@/components/model/types';
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

/**
 * Resolve `DALTON_SETUP_CANDIDATES` against the member's currently available
 * models: takes `primary`, else `fallback`, else skips.
 */
export function resolveDaltonSetupCandidates(available: Iterable<string>): string[] {
  const set = available instanceof Set ? available : new Set(available);
  const out: string[] = [];
  for (const entry of DALTON_SETUP_CANDIDATES) {
    if (set.has(entry.primary)) {
      out.push(entry.primary);
    } else if (entry.fallback && set.has(entry.fallback)) {
      out.push(entry.fallback);
    }
  }
  return out;
}

/**
 * Narrow provider model groups to the resolved `DALTON_SETUP_CANDIDATES` in
 * their exact fixed order (preserving provider grouping). Falls back to
 * `groups` untouched when none of the curated candidates is available (e.g.
 * BYOK-only setups without team OAuth lines).
 */
export function filterDaltonCuratedModels(
  groups: Record<string, ProviderModelsData>,
  dalton: boolean,
): Record<string, ProviderModelsData> {
  if (!dalton) return groups;
  const ownerOf = new Map<string, string>();
  for (const [provider, data] of Object.entries(groups)) {
    for (const m of data.models ?? []) {
      if (!ownerOf.has(m)) ownerOf.set(m, provider);
    }
  }
  const curated = resolveDaltonSetupCandidates(ownerOf.keys());
  if (curated.length === 0) return groups;
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

export interface DaltonModelMeta {
  sdk?: string;
  provider?: string;
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

/** Remove Claude Code, credits-only, and explicitly rejected-credential models from provider groups under Dalton. */
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
    if (provider === CLAUDE_CODE || provider === 'platform') continue;
    if (rejectedProviders?.has(provider)) continue;
    const models = (data.models ?? []).filter(
      (m) => isDaltonAllowedModel(metadata[m], payers[m], rejectedProviders),
    );
    if (models.length > 0) {
      out[provider] = { ...data, models };
    }
  }
  return out;
}

/** True when at least one Dalton-allowed model is available in the catalog. */
export function hasAvailableDaltonModels(
  groups: Record<string, ProviderModelsData>,
  metadata: Record<string, DaltonModelMeta | undefined>,
  payers: Record<string, Payer>,
  dalton: boolean,
  rejectedProviders?: ReadonlySet<string>,
): boolean {
  const filtered = filterDaltonProviderModels(groups, metadata, payers, dalton, rejectedProviders);
  return Object.values(filtered).some((data) => (data.models?.length ?? 0) > 0);
}
