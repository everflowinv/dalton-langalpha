// DALTON-HOOK(dalton-setup): unit tests for Dalton setup candidate list, background default, and effort label stripping
import { describe, expect, it } from 'vitest';
import {
  DALTON_BACKGROUND_MODEL,
  DALTON_DEFAULT_MODEL,
  DALTON_SETUP_CANDIDATES,
  dedupeDaltonBackgroundModels,
  filterDaltonCuratedModels,
  resolveDaltonSetupCandidates,
  stripEffortFromModelLabel,
} from '@/config/daltonSetup';
import { defaultChoices } from '../defaultChoices';

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

  it('strips trailing effort labels for the fixed-low background dropdown', () => {
    expect(stripEffortFromModelLabel('Gemini 4 Hydrogen Low')).toBe('Gemini 4 Hydrogen');
    expect(stripEffortFromModelLabel('Gemini 4 Hydrogen (low)')).toBe('Gemini 4 Hydrogen');
    expect(stripEffortFromModelLabel('Gemini 4 Neon High')).toBe('Gemini 4 Neon');
    expect(stripEffortFromModelLabel('Gemini 4 Hydrogen Xhigh')).toBe('Gemini 4 Hydrogen');
    expect(stripEffortFromModelLabel('GPT-6.2 Astra')).toBe('GPT-6.2 Astra');
    expect(stripEffortFromModelLabel('GPT 5 Mini')).toBe('GPT 5 Mini');
  });

  it('dedupes background effort siblings preferring -low', () => {
    const deduped = dedupeDaltonBackgroundModels(
      {
        'antigravity-oauth': {
          display_name: 'Antigravity',
          models: [
            'gemini-4-hydrogen-high-oauth',
            'gemini-4-hydrogen-low-oauth',
            'gemini-4-hydrogen-medium-oauth',
            'gemini-4-neon-high-oauth',
            'gemini-4-neon-low-oauth',
          ],
        },
        'codex-oauth': {
          display_name: 'Codex',
          models: ['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth'],
        },
      },
      true,
    );
    expect(deduped['antigravity-oauth'].models).toEqual([
      'gemini-4-hydrogen-low-oauth',
      'gemini-4-neon-low-oauth',
    ]);
    expect(deduped['codex-oauth'].models).toEqual(['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth']);
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
        'gemini-4-neon-high-oauth': { intelligence: 5, speed: 4 },
        'gemini-4-carbon-medium-oauth': { intelligence: 5, speed: 3 },
        'gemini-4-hydrogen-low-oauth': { intelligence: 4, speed: 5 },
        'claude-opus-5-5-oauth': { intelligence: 5, speed: 3 },
        'gpt-6.2-astra-oauth': { intelligence: 5, speed: 3 },
        'gpt-6.2-sol-oauth': { intelligence: 5, speed: 3 },
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
});
