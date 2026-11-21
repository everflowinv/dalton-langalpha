import { CLAUDE_CODE, isClaudeCodeModel } from '@/lib/claudeCode';
import type { Payer } from '@/lib/modelPayer';
import type { ProviderModelsData } from '@/components/model/types';
import {
  type AntigravitySeriesCatalog,
  type SelectableAntigravitySeries,
} from '@/lib/antigravitySeries';
import { resolveEffort } from '@/lib/modelTuning';

/** Recommended primary series for Dalton onboarding. */
export const DALTON_DEFAULT_SERIES = 'antigravity-oauth:gemini-4-neon';

/** Recommended background series for Dalton onboarding. */
export const DALTON_BACKGROUND_SERIES = 'antigravity-oauth:gemini-4-hydrogen';

/** Fallback primary model key when antigravity_series catalog metadata is unavailable. */
export const DALTON_DEFAULT_MODEL = 'gemini-4-neon-high-oauth';

/** Fallback background model key when antigravity_series catalog metadata is unavailable. */
export const DALTON_BACKGROUND_MODEL = 'gemini-4-hydrogen-high-oauth';

/**
 * Resolve the recommended variant key for a Dalton Antigravity series from
 * `antigravity_series` metadata, falling back to `fallbackModel` when series
 * metadata is absent (e.g. legacy test mocks).
 */
export function resolveDaltonSeriesDefaultModel(
  seriesId: string,
  candidates: readonly string[],
  options: {
    seriesCatalog?: AntigravitySeriesCatalog | null;
    selectableSeries?: readonly SelectableAntigravitySeries[] | null;
    fallbackModel?: string;
  } = {},
): string | null {
  const candidateSet = new Set(candidates);
  const selectable = options.selectableSeries?.find((s) => s.seriesId === seriesId);
  if (selectable && candidateSet.has(selectable.defaultVariantKey)) {
    return selectable.defaultVariantKey;
  }
  const series = options.seriesCatalog?.bySeriesId[seriesId];
  if (series) {
    const availableEfforts = Object.entries(series.variants)
      .filter(([, variantKey]) => candidateSet.has(variantKey))
      .map(([effort]) => effort);
    if (availableEfforts.length > 0) {
      const chosen = resolveEffort(series.default_effort, availableEfforts) ?? availableEfforts[0];
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

/** True when a model is permitted in Dalton (neither Claude Code nor credits-billed). */
export function isDaltonAllowedModel(
  meta: DaltonModelMeta | undefined,
  payer: Payer | undefined,
): boolean {
  return !isClaudeCodeMeta(meta) && meta?.provider !== 'platform' && payer !== 'credits';
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

/** Remove Claude Code and credits-only models from provider groups under Dalton. */
export function filterDaltonProviderModels(
  groups: Record<string, ProviderModelsData>,
  metadata: Record<string, DaltonModelMeta | undefined>,
  payers: Record<string, Payer>,
  dalton: boolean,
): Record<string, ProviderModelsData> {
  if (!dalton) return groups;
  const out: Record<string, ProviderModelsData> = {};
  for (const [provider, data] of Object.entries(groups)) {
    if (provider === CLAUDE_CODE || provider === 'platform') continue;
    const models = (data.models ?? []).filter(
      (m) => isDaltonAllowedModel(metadata[m], payers[m]),
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
): boolean {
  const filtered = filterDaltonProviderModels(groups, metadata, payers, dalton);
  return Object.values(filtered).some((data) => (data.models?.length ?? 0) > 0);
}
