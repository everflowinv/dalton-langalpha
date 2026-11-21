import { describe, expect, it } from 'vitest';
import type { AntigravitySeriesEntry } from '@/api/model';
import type { CustomModelEntry, ProviderModelsData } from '@/components/model/types';
import { customPairKey, type ModelMetadataEntry } from '@/hooks/useFilteredModels';
import {
  ANTIGRAVITY_PROVIDER,
  buildSelectableAntigravitySeries,
  buildSeriesCatalog,
  formatAntigravityVariantLabel,
  inspectSavedAntigravityModel,
  sameAntigravitySeries,
} from '../antigravitySeries';

const SERIES_LIST: AntigravitySeriesEntry[] = [
  {
    series_id: 'antigravity-oauth:gemini-4-neon',
    series_prefix: 'gemini-4-neon',
    provider: ANTIGRAVITY_PROVIDER,
    display_name: 'Gemini 4 Neon',
    qualified_display_name: 'Gemini 4 Neon (Antigravity)',
    default_effort: 'high',
    default_model: 'gemini-4-neon-high-oauth',
    efforts: ['low', 'medium', 'high', 'xhigh'],
    variants: {
      low: 'gemini-4-neon-low-oauth',
      medium: 'gemini-4-neon-medium-oauth',
      high: 'gemini-4-neon-high-oauth',
      xhigh: 'gemini-4-neon-xhigh-oauth',
    },
  },
  {
    series_id: 'antigravity-oauth:gemini-4-carbon',
    series_prefix: 'gemini-4-carbon',
    provider: ANTIGRAVITY_PROVIDER,
    display_name: 'Gemini 4 Carbon',
    qualified_display_name: 'Gemini 4 Carbon (Antigravity)',
    default_effort: 'medium',
    default_model: 'gemini-4-carbon-medium-oauth',
    efforts: ['low', 'medium', 'high'],
    variants: {
      low: 'gemini-4-carbon-low-oauth',
      medium: 'gemini-4-carbon-medium-oauth',
      high: 'gemini-4-carbon-high-oauth',
    },
  },
  {
    series_id: 'antigravity-oauth:gpt-oss-120b',
    series_prefix: 'gpt-oss-120b',
    provider: ANTIGRAVITY_PROVIDER,
    display_name: 'GPT-OSS 120B',
    qualified_display_name: 'GPT-OSS 120B (Antigravity)',
    default_effort: 'medium',
    default_model: 'gpt-oss-120b-medium-oauth',
    efforts: ['medium'],
    variants: {
      medium: 'gpt-oss-120b-medium-oauth',
    },
  },
];

const RAW_METADATA: Record<string, ModelMetadataEntry> = {
  'gemini-4-neon-low-oauth': { provider: ANTIGRAVITY_PROVIDER },
  'gemini-4-neon-medium-oauth': { provider: ANTIGRAVITY_PROVIDER },
  'gemini-4-neon-high-oauth': { provider: ANTIGRAVITY_PROVIDER },
  'gemini-4-neon-xhigh-oauth': { provider: ANTIGRAVITY_PROVIDER },
  'gemini-4-carbon-low-oauth': { provider: ANTIGRAVITY_PROVIDER },
  'gemini-4-carbon-medium-oauth': { provider: ANTIGRAVITY_PROVIDER },
  'gemini-4-carbon-high-oauth': { provider: ANTIGRAVITY_PROVIDER },
  'gpt-oss-120b-medium-oauth': { provider: ANTIGRAVITY_PROVIDER },
  'claude-opus-5-5-oauth': { provider: 'claude-oauth', display_name: 'Claude Opus 5.5' },
};

describe('antigravitySeries helpers', () => {
  const catalog = buildSeriesCatalog(SERIES_LIST);

  it('inspects saved Antigravity models from seriesCatalog even when currently unselectable', () => {
    const match = inspectSavedAntigravityModel('gemini-4-neon-xhigh-oauth', {
      seriesCatalog: catalog,
      rawMetadata: RAW_METADATA,
      customModels: [],
    });
    expect(match).not.toBeNull();
    expect(match?.displayName).toBe('Gemini 4 Neon');
    expect(match?.effort).toBe('xhigh');
    expect(
      formatAntigravityVariantLabel('gemini-4-neon-xhigh-oauth', {
        seriesCatalog: catalog,
        rawMetadata: RAW_METADATA,
        customModels: [],
        includeEffort: true,
      }),
    ).toBe('Gemini 4 Neon · Xhigh');
  });

  it('excludes same-named custom models and non-antigravity providers', () => {
    const customModels: CustomModelEntry[] = [
      { name: 'gemini-4-neon-high-oauth', provider: 'openai', model_id: 'custom-neon' },
    ];
    expect(
      inspectSavedAntigravityModel('gemini-4-neon-high-oauth', {
        seriesCatalog: catalog,
        rawMetadata: RAW_METADATA,
        customModels,
      }),
    ).toBeNull();

    expect(
      inspectSavedAntigravityModel('claude-opus-5-5-oauth', {
        seriesCatalog: catalog,
        rawMetadata: RAW_METADATA,
        customModels: [],
      }),
    ).toBeNull();

    expect(
      sameAntigravitySeries('gemini-4-neon-low-oauth', 'gemini-4-neon-high-oauth', {
        seriesCatalog: catalog,
        rawMetadata: RAW_METADATA,
        customModels,
      }),
    ).toBe(false);
  });

  it('computes selectableSeries with custom shadow filtering, default step-down, and empty-series hiding', () => {
    const customModels: CustomModelEntry[] = [
      { name: 'gemini-4-neon-high-oauth', provider: 'openai', model_id: 'custom-neon' },
    ];
    const pool: Record<string, ProviderModelsData> = {
      [ANTIGRAVITY_PROVIDER]: {
        display_name: 'Google Antigravity',
        models: [
          'gemini-4-neon-low-oauth',
          'gemini-4-neon-medium-oauth',
          'gemini-4-neon-xhigh-oauth',
          // carbon completely absent (e.g. policy narrowed) -> hidden
        ],
      },
      openai: {
        display_name: 'OpenAI',
        models: ['gemini-4-neon-high-oauth'],
      },
    };
    const customPairs = new Set([customPairKey('openai', 'gemini-4-neon-high-oauth')]);

    const selectable = buildSelectableAntigravitySeries(catalog, pool, {
      rawMetadata: RAW_METADATA,
      customModels,
      customPairs,
    });

    expect(selectable).toHaveLength(1);
    expect(selectable[0].seriesId).toBe('antigravity-oauth:gemini-4-neon');
    expect(selectable[0].efforts).toEqual(['low', 'medium', 'xhigh']);
    // Neon defaults to high; high is shadowed -> steps down to medium
    expect(selectable[0].defaultEffort).toBe('medium');
    expect(selectable[0].defaultVariantKey).toBe('gemini-4-neon-medium-oauth');
  });
});
