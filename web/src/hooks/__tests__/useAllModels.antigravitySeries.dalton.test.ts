import { describe, expect, it, vi, beforeEach } from 'vitest';
import { renderHookWithProviders } from '@/test/utils';
import type { AntigravitySeriesEntry } from '@/api/model';

const SERIES_LIST: AntigravitySeriesEntry[] = [
  {
    series_id: 'antigravity-oauth:gemini-4-neon',
    series_prefix: 'gemini-4-neon',
    provider: 'antigravity-oauth',
    display_name: 'Gemini 4 Neon',
    qualified_display_name: 'Gemini 4 Neon (Antigravity)',
    default_effort: 'high',
    default_model: 'gemini-4-neon-high-oauth',
    efforts: ['low', 'medium', 'high'],
    variants: {
      low: 'gemini-4-neon-low-oauth',
      medium: 'gemini-4-neon-medium-oauth',
      high: 'gemini-4-neon-high-oauth',
    },
  },
];

const h = vi.hoisted(() => ({
  isDaltonBuild: false,
}));

vi.mock('@/config/brand', () => ({
  get isDaltonBuild() {
    return h.isDaltonBuild;
  },
  DALTON_PROFILE: 'everflow' as const,
  isCoreProfileBuild: false,
}));
vi.mock('../useProviderModels', () => ({
  useMissingModels: () => ({ missingByProvider: new Map(), isLoading: false }),
}));
vi.mock('../useModels', () => ({
  useModels: () => ({
    models: {
      models: {
        'antigravity-oauth': {
          display_name: 'Google Antigravity',
          models: [
            'gemini-4-neon-low-oauth',
            'gemini-4-neon-medium-oauth',
            'gemini-4-neon-high-oauth',
          ],
        },
      },
      model_metadata: {
        'gemini-4-neon-low-oauth': { provider: 'antigravity-oauth', access_type: 'oauth' },
        'gemini-4-neon-medium-oauth': { provider: 'antigravity-oauth', access_type: 'oauth' },
        'gemini-4-neon-high-oauth': { provider: 'antigravity-oauth', access_type: 'oauth' },
      },
      provider_catalog: [
        { provider: 'antigravity-oauth', display_name: 'Google Antigravity', access_type: 'oauth', sdk: 'google' },
      ],
      antigravity_series: SERIES_LIST,
    },
    isLoading: false,
    isError: false,
  }),
}));
vi.mock('../usePreferences', () => ({
  usePreferences: () => ({ preferences: {}, isLoading: false, isLoaded: true }),
}));
vi.mock('../useConfiguredProviders', () => ({
  useConfiguredProviders: () => ({
    providers: [{ provider: 'antigravity-oauth', access_type: 'oauth', state: 'connected' }],
    byokEnabled: true,
    isLoading: false,
    unread: false,
  }),
}));
vi.mock('../useFeatures', async (importActual) => ({
  ...(await importActual<typeof import('../useFeatures')>()),
  useFeatures: () => ({ data: [], isLoading: false }),
}));
vi.mock('../usePlatformModels', () => ({
  usePlatformModels: () => ({ platform: null, isLoading: false }),
  getModelAccess: () => 'platform',
}));
vi.mock('../useAccount', () => ({
  useAccount: () => ({ policy: null }),
}));
vi.mock('../useClaudeCode', () => ({
  useClaudeCodeStatus: () => ({ enabled: false, isLoading: false, unread: false }),
}));

import { useAllModels } from '../useAllModels';

describe('useAllModels — Antigravity series build gating', () => {
  beforeEach(() => {
    h.isDaltonBuild = false;
  });

  it('keeps upstream behavior (empty seriesCatalog/selectableSeries) in non-Dalton builds even when antigravity_series is returned', () => {
    h.isDaltonBuild = false;
    const { result } = renderHookWithProviders(() => useAllModels());

    expect(result.current.models['antigravity-oauth']?.models).toEqual([
      'gemini-4-neon-low-oauth',
      'gemini-4-neon-medium-oauth',
      'gemini-4-neon-high-oauth',
    ]);
    expect(result.current.seriesCatalog.series).toEqual([]);
    expect(result.current.seriesCatalog.byVariantKey).toEqual({});
    expect(result.current.selectableSeries).toEqual([]);
    expect(result.current.selectableSlotSeries).toEqual([]);
  });

  it('enables seriesCatalog, selectableSeries, and selectableSlotSeries in Dalton builds', () => {
    h.isDaltonBuild = true;
    const { result } = renderHookWithProviders(() => useAllModels());

    expect(result.current.seriesCatalog.series).toHaveLength(1);
    expect(result.current.selectableSeries).toHaveLength(1);
    expect(result.current.selectableSeries[0]?.defaultVariantKey).toBe('gemini-4-neon-high-oauth');
    expect(result.current.selectableSlotSeries).toHaveLength(1);
  });
});
