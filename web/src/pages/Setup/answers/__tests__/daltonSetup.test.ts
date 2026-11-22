// DALTON-HOOK(dalton-setup): unit tests for Dalton setup candidate list and background defaults
import { describe, expect, it } from 'vitest';
import {
  DALTON_BACKGROUND_MODEL,
  DALTON_BACKGROUND_SERIES,
  DALTON_DEFAULT_MODEL,
  DALTON_SETUP_CANDIDATES,
  filterDaltonCuratedModels,
  hasAvailableDaltonModels,
  resolveDaltonSeriesDefaultModel,
  resolveDaltonSetupCandidates,
} from '@/config/daltonSetup';
import { buildSeriesCatalog } from '@/lib/antigravitySeries';
import { defaultChoices } from '../defaultChoices';
import { DALTON_CURATED_PROVIDERS } from '../setupSteps';

describe('daltonSetup candidate list & background defaults', () => {
  it('uses neon-high as default and hydrogen-low as recommended background', () => {
    expect(DALTON_DEFAULT_MODEL).toBe('gemini-4-neon-high-oauth');
    expect(DALTON_BACKGROUND_MODEL).toBe('gemini-4-hydrogen-low-oauth');
    expect(DALTON_SETUP_CANDIDATES).toHaveLength(5);
  });

  it('returns all 5 curated candidates in exact fixed order when all are available', () => {
    const available = [
      'gpt-6.2-sol-oauth',
      'gemini-4-hydrogen-low-oauth',
      'claude-opus-5-5-medium-oauth',
      'gpt-6.2-astra-oauth',
      'claude-opus-5-5-oauth',
      'gemini-4-carbon-medium-oauth',
      'gemini-4-neon-high-oauth',
      'gemini-4-argon-high-oauth',
    ];
    expect(resolveDaltonSetupCandidates(available)).toEqual([
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
      'claude-opus-5-5-oauth',
      'gpt-6.2-astra-oauth',
      'gpt-6.2-sol-oauth',
    ]);
  });

  it('falls back to claude-opus-5-5-medium-oauth when claude-opus-5-5-oauth is unavailable', () => {
    const available = [
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
      'claude-opus-5-5-medium-oauth',
      'gpt-6.2-astra-oauth',
      'gpt-6.2-sol-oauth',
    ];
    expect(resolveDaltonSetupCandidates(available)).toEqual([
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
      'claude-opus-5-5-medium-oauth',
      'gpt-6.2-astra-oauth',
      'gpt-6.2-sol-oauth',
    ]);
  });

  it('skips entries that are unavailable while preserving order of the rest', () => {
    const available = [
      'gpt-6.2-sol-oauth',
      'gemini-4-neon-high-oauth',
      'gemini-4-hydrogen-low-oauth',
    ];
    expect(resolveDaltonSetupCandidates(available)).toEqual([
      'gemini-4-neon-high-oauth',
      'gpt-6.2-sol-oauth',
    ]);
  });

  it('resolves background series to -low even when the series default_effort is high', () => {
    const seriesCatalog = buildSeriesCatalog([
      {
        series_id: DALTON_BACKGROUND_SERIES,
        series_prefix: 'gemini-4-hydrogen',
        provider: 'antigravity-oauth',
        display_name: 'Gemini 4 Hydrogen',
        qualified_display_name: 'Gemini 4 Hydrogen (Antigravity)',
        default_effort: 'high',
        default_model: 'gemini-4-hydrogen-high-oauth',
        efforts: ['low', 'medium', 'high'],
        variants: {
          low: 'gemini-4-hydrogen-low-oauth',
          medium: 'gemini-4-hydrogen-medium-oauth',
          high: 'gemini-4-hydrogen-high-oauth',
        },
      },
    ]);
    expect(
      resolveDaltonSeriesDefaultModel(
        DALTON_BACKGROUND_SERIES,
        ['gemini-4-hydrogen-high-oauth', 'gemini-4-hydrogen-medium-oauth', 'gemini-4-hydrogen-low-oauth'],
        { seriesCatalog, fallbackModel: DALTON_BACKGROUND_MODEL, preferLowEffort: true },
      ),
    ).toBe('gemini-4-hydrogen-low-oauth');
  });

  it('narrows Customize provider groups to curated 5 in fixed order', () => {
    const curated = filterDaltonCuratedModels(
      {
        'codex-oauth': { display_name: 'Codex', models: ['gpt-6.2-sol-oauth', 'gpt-6.2-astra-oauth', 'gpt-5'] },
        'claude-oauth': { display_name: 'Claude', models: ['claude-opus-5-5-oauth', 'claude-sonnet-5-5-oauth'] },
        'antigravity-oauth': {
          display_name: 'Antigravity',
          models: ['gemini-4-hydrogen-low-oauth', 'gemini-4-carbon-medium-oauth', 'gemini-4-neon-high-oauth'],
        },
      },
      true,
    );
    expect(Object.keys(curated)).toEqual(['antigravity-oauth', 'claude-oauth', 'codex-oauth']);
    expect(Object.values(curated).flatMap((g) => g.models ?? [])).toEqual([
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
      'claude-opus-5-5-oauth',
      'gpt-6.2-astra-oauth',
      'gpt-6.2-sol-oauth',
    ]);
  });

  it('defaultChoices in Dalton mode shows only the 5 curated candidates in order and recommends neon-high + hydrogen-low', () => {
    const allSlotModels = [
      'gemini-4-hydrogen-low-oauth',
      'gemini-4-hydrogen-high-oauth',
      'gemini-4-argon-high-oauth',
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
      'claude-opus-5-5-oauth',
      'claude-opus-5-5-medium-oauth',
      'gpt-6.2-astra-oauth',
      'gpt-6.2-sol-oauth',
    ];
    const result = defaultChoices({
      models: 'langalpha_models',
      offered: {
        'codex-oauth': { models: ['gpt-6.2-sol-oauth', 'gpt-6.2-astra-oauth'] },
        'claude-oauth': { models: ['claude-opus-5-5-oauth'] },
        'antigravity-oauth': {
          models: [
            'gemini-4-hydrogen-low-oauth',
            'gemini-4-hydrogen-high-oauth',
            'gemini-4-argon-high-oauth',
            'gemini-4-carbon-medium-oauth',
            'gemini-4-neon-high-oauth',
            'claude-opus-5-5-medium-oauth',
          ],
        },
      },
      slotModels: allSlotModels,
      payers: {
        'gemini-4-neon-high-oauth': 'subscription:antigravity-oauth',
        'gemini-4-carbon-medium-oauth': 'subscription:antigravity-oauth',
        'gemini-4-hydrogen-low-oauth': 'subscription:antigravity-oauth',
        'claude-opus-5-5-oauth': 'subscription:claude-oauth',
        'gpt-6.2-astra-oauth': 'subscription:codex-oauth',
        'gpt-6.2-sol-oauth': 'subscription:codex-oauth',
      },
      ownProviders: [],
      metadata: {
        'gemini-4-neon-high-oauth': { intelligence: 4, speed: 4 },
        'gemini-4-carbon-medium-oauth': { intelligence: 5, speed: 3 },
        'gemini-4-hydrogen-low-oauth': { intelligence: 4, speed: 5 },
        'claude-opus-5-5-oauth': { intelligence: 5, speed: 3 },
        'gpt-6.2-astra-oauth': { intelligence: 5, speed: 2 },
        'gpt-6.2-sol-oauth': { intelligence: 4, speed: 3 },
      },
      customModels: [],
      connectionOf: (p) => p,
      systemDefault: null,
      systemBackground: null,
      preferred: null,
      dalton: true,
    });

    expect(result.list).toEqual([
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
      'claude-opus-5-5-oauth',
      'gpt-6.2-astra-oauth',
      'gpt-6.2-sol-oauth',
    ]);
    expect(result.initial).toBe('gemini-4-neon-high-oauth');
    expect(result.recommended).toEqual({
      primary: 'gemini-4-neon-high-oauth',
      primaryWhy: 'daltonRecommended',
      background: 'gemini-4-hydrogen-low-oauth',
      backgroundWhy: 'daltonRecommended',
    });
  });

  it('never falls back to uncurated models when none of the 5 fixed candidates is available', () => {
    const nonCuratedGroups = {
      openai: { display_name: 'OpenAI', models: ['gpt-5', 'gpt-5-mini'] },
      'antigravity-oauth': { display_name: 'Antigravity', models: ['gemini-4-hydrogen-low-oauth'] },
    };
    const meta = {
      'gpt-5': { provider: 'openai', intelligence: 5, speed: 3 },
      'gpt-5-mini': { provider: 'openai', intelligence: 3, speed: 5 },
      'gemini-4-hydrogen-low-oauth': { provider: 'antigravity-oauth', intelligence: 4, speed: 5 },
    };
    const payers = {
      'gpt-5': 'own_key' as const,
      'gpt-5-mini': 'own_key' as const,
      'gemini-4-hydrogen-low-oauth': 'subscription:antigravity-oauth' as const,
    };

    expect(filterDaltonCuratedModels(nonCuratedGroups, true)).toEqual({});
    expect(hasAvailableDaltonModels(nonCuratedGroups, meta, payers, true)).toBe(false);
    expect(hasAvailableDaltonModels(nonCuratedGroups, meta, payers, false)).toBe(true);

    const choices = defaultChoices({
      models: 'langalpha_models',
      offered: nonCuratedGroups,
      slotModels: ['gpt-5', 'gpt-5-mini', 'gemini-4-hydrogen-low-oauth'],
      payers,
      ownProviders: ['openai'],
      metadata: meta,
      customModels: [],
      connectionOf: (p) => p,
      systemDefault: 'gpt-5',
      systemBackground: 'gemini-4-hydrogen-low-oauth',
      preferred: 'gpt-5',
      dalton: true,
    });
    expect(choices).toEqual({
      list: [],
      initial: null,
      recommended: {
        primary: null,
        primaryWhy: null,
        background: null,
        backgroundWhy: null,
      },
    });
  });

  it('restricts Dalton curated providers to subscriptions that can supply the 5 fixed candidates', () => {
    expect([...DALTON_CURATED_PROVIDERS].sort()).toEqual([
      'antigravity-oauth',
      'claude-oauth',
      'codex-oauth',
    ]);
    expect(DALTON_CURATED_PROVIDERS.has('openai')).toBe(false);
    expect(DALTON_CURATED_PROVIDERS.has('xai-oauth')).toBe(false);
    expect(DALTON_CURATED_PROVIDERS.has('muse-oauth')).toBe(false);
  });
});
