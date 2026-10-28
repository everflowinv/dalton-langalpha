/**
 * `offPlanModels` is what the composer checks a saved Default against, so it
 * has to name every model the server refuses on the platform's key, and none
 * it runs: the composer names what it starts on, and a refused one blocks
 * every send until the user changes Settings.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import type { ConfiguredProvider } from '../useConfiguredProviders';
import type { PlatformModelsResponse } from '@/types/platform';

const h = vi.hoisted(() => ({
  platform: null as PlatformModelsResponse | null,
  providers: [] as ConfiguredProvider[],
  byokEnabled: true,
  customModels: [] as Array<{ name: string; model_id: string; provider: string }>,
}));

// The providers' own lists are read through React Query; none is read here.
vi.mock('../useProviderModels', () => ({ useMissingModels: () => ({ missingByProvider: new Map(), isLoading: false }) }));
vi.mock('../useModels', () => ({
  useModels: () => ({
    models: {
      models: {
        'prov-a': { models: ['m-free', 'm-pro', 'm-untiered'], display_name: 'A' },
        'codex-oauth': { models: ['m-oauth'], display_name: 'Codex' },
      },
      model_metadata: {
        'm-free': { provider: 'prov-a', access_type: 'api_key', tier: 0 },
        'm-pro': { provider: 'prov-a', access_type: 'api_key', tier: 2 },
        'm-untiered': { provider: 'prov-a', access_type: 'api_key' },
        'm-oauth': { provider: 'codex-oauth', access_type: 'oauth' },
      },
      key_candidates: {
        'prov-a': { slugs: ['prov-a', 'prov-a-cn', 'prov-a-coding'], shared: ['prov-a', 'prov-a-cn'] },
        'prov-b-cn': { slugs: ['prov-b-cn', 'prov-b', 'prov-b-coding'], shared: ['prov-b-cn', 'prov-b'] },
      },
    },
    isLoading: false,
  }),
}));
vi.mock('../usePreferences', () => ({
  usePreferences: () => ({
    preferences: { model_preference: { custom_models: h.customModels } },
    isLoading: false,
    isLoaded: true,
  }),
}));
vi.mock('../useConfiguredProviders', () => ({
  useConfiguredProviders: () => ({ providers: h.providers, byokEnabled: h.byokEnabled, isLoading: false }),
}));
vi.mock('../usePlatformModels', async (importActual) => ({
  ...(await importActual<typeof import('../usePlatformModels')>()),
  usePlatformModels: () => ({ platform: h.platform, isLoading: false }),
}));

import { useAllModels } from '../useAllModels';

const KEY_A: ConfiguredProvider = { provider: 'prov-a', display_name: 'A', access_type: 'api_key' };
const KEY_B: ConfiguredProvider = { provider: 'prov-b', display_name: 'B', access_type: 'api_key' };

function offPlan(): string[] {
  const { result } = renderHook(() => useAllModels());
  return [...result.current.offPlanModels].sort();
}

describe('useAllModels — offPlanModels', () => {
  beforeEach(() => {
    h.platform = { model_tier: 1, byok_providers: [], oauth_providers: [] };
    h.providers = [];
    h.byokEnabled = true;
    h.customModels = [];
  });

  it('names a model above the plan and one with no tier, which no plan includes', () => {
    expect(offPlan()).toEqual(['m-pro', 'm-untiered']);
  });

  it("names none of a provider the user holds a key for", () => {
    h.providers = [KEY_A];
    expect(offPlan()).toEqual([]);
  });

  it('counts a stored key for nothing while the BYOK switch is off', () => {
    h.providers = [KEY_A];
    h.byokEnabled = false;
    expect(offPlan()).toEqual(['m-pro', 'm-untiered']);
  });

  it('leaves an OAuth model out, so a lapsed connection keeps its reconnect refusal', () => {
    expect(offPlan()).not.toContain('m-oauth');
  });

  it("judges a custom model under a built-in's name as the built-in until a key is held for it", () => {
    h.customModels = [
      { name: 'm-pro', model_id: 'x', provider: 'prov-b' },
      { name: 'm-free', model_id: 'y', provider: 'prov-b' },
      { name: 'my-own', model_id: 'z', provider: 'prov-b' },
    ];
    expect(offPlan()).toEqual(['m-pro', 'm-untiered']);

    h.providers = [KEY_B];
    expect(offPlan()).toEqual(['m-untiered']);

    h.byokEnabled = false;
    expect(offPlan()).toEqual(['m-pro', 'm-untiered']);
  });

  it('counts a key the server finds under a related slug when its vendor issues the two as one', () => {
    h.providers = [{ provider: 'prov-a-cn', display_name: 'A CN', access_type: 'api_key' }];
    expect(offPlan()).toEqual([]);

    h.providers = [{ provider: 'prov-a-coding', display_name: 'A coding', access_type: 'coding_plan' }];
    expect(offPlan()).toEqual(['m-pro', 'm-untiered']);

    h.providers = [{ ...h.providers[0], base_url: 'https://gateway.example.com/v1' }];
    expect(offPlan()).toEqual([]);
  });

  it("takes any key the server finds for a custom model, which is built against the slug holding it", () => {
    h.customModels = [{ name: 'm-pro', model_id: 'x', provider: 'prov-b-cn' }];
    h.providers = [{ provider: 'prov-c', display_name: 'C', access_type: 'api_key' }];
    expect(offPlan()).toContain('m-pro');

    h.providers = [{ provider: 'prov-b-coding', display_name: 'B coding', access_type: 'coding_plan' }];
    expect(offPlan()).not.toContain('m-pro');
  });

  it('is empty without a platform, which gates no model', () => {
    h.platform = null;
    expect(offPlan()).toEqual([]);
  });
});
