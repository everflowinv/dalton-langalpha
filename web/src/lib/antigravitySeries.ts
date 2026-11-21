import type { AntigravitySeriesEntry } from '@/api/model';
import type { CustomModelEntry, ProviderModelsData } from '@/components/model/types';
import { customPairKey, type ModelMetadataEntry } from '@/hooks/useFilteredModels';
import { EFFORT_ORDER, effortLabelFor, resolveEffort } from '@/lib/modelTuning';

export const ANTIGRAVITY_PROVIDER = 'antigravity-oauth';

export interface AntigravitySeriesCatalog {
  series: readonly AntigravitySeriesEntry[];
  bySeriesId: Readonly<Record<string, AntigravitySeriesEntry>>;
  bySeriesPrefix: Readonly<Record<string, AntigravitySeriesEntry>>;
  byVariantKey: Readonly<Record<string, { series: AntigravitySeriesEntry; effort: string }>>;
}

export const EMPTY_ANTIGRAVITY_SERIES_CATALOG: AntigravitySeriesCatalog = {
  series: [],
  bySeriesId: {},
  bySeriesPrefix: {},
  byVariantKey: {},
};

export interface SavedAntigravityMatch {
  series: AntigravitySeriesEntry;
  effort: string;
  displayName: string;
  qualifiedDisplayName: string;
}

export interface SelectableAntigravitySeries {
  series: AntigravitySeriesEntry;
  seriesId: string;
  displayName: string;
  qualifiedDisplayName: string;
  efforts: string[];
  variants: Record<string, string>;
  defaultEffort: string;
  defaultVariantKey: string;
}

export interface InspectAntigravityOptions {
  seriesCatalog?: AntigravitySeriesCatalog | null;
  rawMetadata?: Record<string, { provider?: string } | undefined> | null;
  customModels?: readonly CustomModelEntry[] | null;
}

export function buildSeriesCatalog(
  seriesList?: readonly AntigravitySeriesEntry[] | null,
): AntigravitySeriesCatalog {
  if (!seriesList || seriesList.length === 0) {
    return EMPTY_ANTIGRAVITY_SERIES_CATALOG;
  }
  const bySeriesId: Record<string, AntigravitySeriesEntry> = {};
  const bySeriesPrefix: Record<string, AntigravitySeriesEntry> = {};
  const byVariantKey: Record<string, { series: AntigravitySeriesEntry; effort: string }> = {};
  for (const entry of seriesList) {
    bySeriesId[entry.series_id] = entry;
    bySeriesPrefix[entry.series_prefix] = entry;
    for (const [effort, variantKey] of Object.entries(entry.variants ?? {})) {
      byVariantKey[variantKey] = { series: entry, effort };
    }
  }
  return {
    series: seriesList,
    bySeriesId,
    bySeriesPrefix,
    byVariantKey,
  };
}

/**
 * Inspect a saved or displayed model key using the unfiltered `seriesCatalog`.
 *
 * Returns `null` when shadowed by a user custom model or when `rawMetadata`
 * indicates the key is not a built-in `antigravity-oauth` entry.
 */
export function inspectSavedAntigravityModel(
  modelKey: string | null | undefined,
  { seriesCatalog, rawMetadata, customModels }: InspectAntigravityOptions,
): SavedAntigravityMatch | null {
  if (!modelKey || !seriesCatalog?.byVariantKey) return null;
  if (customModels?.some((cm) => cm.name === modelKey)) return null;
  const hit = seriesCatalog.byVariantKey[modelKey];
  if (!hit) return null;
  if (rawMetadata?.[modelKey]?.provider !== ANTIGRAVITY_PROVIDER) return null;
  return {
    series: hit.series,
    effort: hit.effort,
    displayName: hit.series.display_name,
    qualifiedDisplayName: hit.series.qualified_display_name,
  };
}

export function sameAntigravitySeries(
  a: string | null | undefined,
  b: string | null | undefined,
  options: InspectAntigravityOptions,
): boolean {
  if (!a || !b) return false;
  const hitA = inspectSavedAntigravityModel(a, options);
  if (!hitA) return false;
  const hitB = inspectSavedAntigravityModel(b, options);
  return !!hitB && hitA.series.series_id === hitB.series.series_id;
}

export function buildSelectableAntigravitySeries(
  seriesCatalog: AntigravitySeriesCatalog | null | undefined,
  pool: Record<string, ProviderModelsData> | null | undefined,
  options?: {
    rawMetadata?: Record<string, ModelMetadataEntry> | null;
    customModels?: readonly CustomModelEntry[] | null;
    customPairs?: ReadonlySet<string> | null;
  },
): SelectableAntigravitySeries[] {
  if (!seriesCatalog || seriesCatalog.series.length === 0 || !pool) return [];
  const customNameSet = new Set(
    (options?.customModels ?? []).map((cm) => cm.name).filter((n): n is string => Boolean(n)),
  );
  const rawMetadata = options?.rawMetadata;
  const customPairs = options?.customPairs;

  const availableBuiltinKeys = new Set<string>();
  for (const [groupKey, groupData] of Object.entries(pool)) {
    for (const modelKey of groupData?.models ?? []) {
      if (customNameSet.has(modelKey)) continue;
      if (customPairs?.has(customPairKey(groupKey, modelKey))) continue;
      if (rawMetadata && rawMetadata[modelKey]?.provider !== ANTIGRAVITY_PROVIDER) continue;
      availableBuiltinKeys.add(modelKey);
    }
  }

  const out: SelectableAntigravitySeries[] = [];
  for (const series of seriesCatalog.series) {
    const rawEfforts = Object.keys(series.variants ?? {});
    const orderedEfforts = [
      ...EFFORT_ORDER.filter((lv) => rawEfforts.includes(lv)),
      ...rawEfforts.filter((lv) => !EFFORT_ORDER.includes(lv)),
    ];
    const availableEfforts = orderedEfforts.filter((lv) => {
      const variantKey = series.variants[lv];
      return !!variantKey && availableBuiltinKeys.has(variantKey);
    });
    if (availableEfforts.length === 0) continue;

    const availableVariants: Record<string, string> = {};
    for (const lv of availableEfforts) {
      availableVariants[lv] = series.variants[lv];
    }
    const defaultEffort =
      resolveEffort(series.default_effort, availableEfforts) ?? availableEfforts[0];
    const defaultVariantKey =
      availableVariants[defaultEffort] ?? availableVariants[availableEfforts[0]];

    out.push({
      series,
      seriesId: series.series_id,
      displayName: series.display_name,
      qualifiedDisplayName: series.qualified_display_name,
      efforts: availableEfforts,
      variants: availableVariants,
      defaultEffort,
      defaultVariantKey,
    });
  }
  return out;
}

export function indexSelectableSeries(
  list?: readonly SelectableAntigravitySeries[] | null,
): Readonly<Record<string, SelectableAntigravitySeries>> {
  const map: Record<string, SelectableAntigravitySeries> = {};
  for (const item of list ?? []) {
    map[item.seriesId] = item;
  }
  return map;
}

/**
 * Resolve the variant key for a fixed-low slot (e.g. Background model):
 * prefers the `'low'` tier when selectable, otherwise falls back to the
 * series' lowest available tier.
 */
export function resolveLowEffortVariantKey(
  series: SelectableAntigravitySeries | undefined,
): string | undefined {
  if (!series || series.efforts.length === 0) return undefined;
  return series.variants.low ?? series.variants[series.efforts[0]];
}

/**
 * Return the variant keys of `series` that are currently in `favorites` and
 * still resolve to this built-in series via `inspectSavedAntigravityModel`
 * (excluding any key shadowed by a same-name custom model or non-Antigravity
 * metadata entry).
 */
export function favoriteAntigravitySeriesVariants(
  series: AntigravitySeriesEntry,
  favorites: ReadonlySet<string> | readonly string[],
  options: InspectAntigravityOptions,
): string[] {
  const hasFav = Array.isArray(favorites)
    ? (k: string) => favorites.includes(k)
    : (k: string) => (favorites as ReadonlySet<string>).has(k);
  return Object.values(series.variants).filter((variantKey) => {
    if (!hasFav(variantKey)) return false;
    const match = inspectSavedAntigravityModel(variantKey, options);
    return match !== null && match.series.series_id === series.series_id;
  });
}

/**
 * Format an unshadowed built-in Antigravity variant as `"Series"` or
 * `"Series · Effort"`; returns `null` when the key is not an unshadowed
 * built-in Antigravity variant.
 */
export function formatAntigravityVariantLabel(
  modelKey: string | null | undefined,
  options: InspectAntigravityOptions & {
    t?: (key: string) => string;
    includeEffort?: boolean;
  },
): string | null {
  const hit = inspectSavedAntigravityModel(modelKey, options);
  if (!hit) return null;
  if (!options.includeEffort) return hit.displayName;
  const effortText =
    (options.t ? effortLabelFor(options.t, hit.effort) : null) ??
    (hit.effort ? hit.effort.charAt(0).toUpperCase() + hit.effort.slice(1) : '');
  return effortText ? `${hit.displayName} · ${effortText}` : hit.displayName;
}
