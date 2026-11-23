// DALTON-HOOK(dalton-setup): unit tests for Dalton setup candidate list and background defaults
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  applyDaltonProviderToolProbes,
  DALTON_BACKGROUND_MODEL,
  DALTON_BACKGROUND_SERIES,
  DALTON_DEFAULT_MODEL,
  DALTON_SETUP_CANDIDATES,
  filterDaltonCuratedModels,
  getDaltonModelStepConfig,
  hasAvailableDaltonModels,
  isCoreDaltonProfile,
  isDaltonAllowedModel,
  isDaltonAllowedSystemBackground,
  isDaltonCuratedOnly,
  isDaltonSetupProviderAllowed,
  isDaltonToolCallingConfirmed,
  isDaltonToolCallingUnverified,
  pickDaltonConnectedDefault,
  resolveDaltonConfirmedSetupModels,
  resolveDaltonProfile,
  resolveDaltonRecommendedBackground,
  resolveDaltonSeriesDefaultModel,
  resolveDaltonSetupCandidates,
  resolveDaltonSetupModels,
} from '@/config/daltonSetup';
import { buildSeriesCatalog } from '@/lib/antigravitySeries';
import { defaultChoices } from '../defaultChoices';
import { DALTON_CURATED_PROVIDERS } from '../setupSteps';

afterEach(() => {
  vi.unstubAllEnvs();
});

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

// DALTON-HOOK(standalone-core): profile-aware setup unlocking (Core vs Everflow)
describe('standalone-core profile gating in daltonSetup', () => {
  it('resolves deployment profile from runtime field with VITE_DALTON_PROFILE fallback (never from brand)', () => {
    vi.stubEnv('VITE_DALTON_PROFILE', '');
    expect(resolveDaltonProfile()).toBe('everflow');
    expect(isCoreDaltonProfile()).toBe(false);
    expect(isDaltonCuratedOnly(true)).toBe(true);
    expect(isDaltonSetupProviderAllowed('openai', DALTON_CURATED_PROVIDERS)).toBe(false);

    expect(resolveDaltonProfile('core')).toBe('core');
    expect(isCoreDaltonProfile('core')).toBe(true);
    expect(isDaltonCuratedOnly(true, 'core')).toBe(false);
    expect(isDaltonSetupProviderAllowed('openai', DALTON_CURATED_PROVIDERS, 'core')).toBe(true);
    expect(isDaltonSetupProviderAllowed('my-gateway', DALTON_CURATED_PROVIDERS, 'core')).toBe(true);

    // Build-time fallback when runtime dalton_profile is omitted.
    vi.stubEnv('VITE_DALTON_PROFILE', 'core');
    expect(resolveDaltonProfile()).toBe('core');
    expect(isCoreDaltonProfile()).toBe(true);
    // Explicit runtime 'everflow' overrides build-time env.
    expect(resolveDaltonProfile('everflow')).toBe('everflow');
    expect(isCoreDaltonProfile('everflow')).toBe(false);
  });

  it('unlocks connected BYOK and custom OpenAI-compatible tool-calling models in Core when curated 5 are absent, and never auto-selects unverified models', () => {
    const groups = {
      openai: { display_name: 'OpenAI', models: ['gpt-5-mini', 'gpt-5', 'o1-text-only'] },
      'acme-gateway': { display_name: 'Acme Gateway', models: ['acme-custom-v1'] },
    };
    const meta = {
      'gpt-5': { provider: 'openai', intelligence: 5, speed: 3, tool_calling: true },
      'gpt-5-mini': { provider: 'openai', intelligence: 3, speed: 5, tool_calling: true },
      'o1-text-only': { provider: 'openai', intelligence: 5, speed: 4, tool_calling: false },
      'acme-custom-v1': { provider: 'acme-gateway', is_custom_model: true }, // unverified (tool_calling omitted)
    };
    const payers = {
      'gpt-5': 'own_key' as const,
      'gpt-5-mini': 'own_key' as const,
      'o1-text-only': 'own_key' as const,
      'acme-custom-v1': 'own_key' as const,
    };

    // Everflow: stays empty / unavailable.
    expect(filterDaltonCuratedModels(groups, true, { profile: 'everflow', metadata: meta })).toEqual({});
    expect(hasAvailableDaltonModels(groups, meta, payers, true, undefined, 'everflow')).toBe(false);
    expect(pickDaltonConnectedDefault(['gpt-5', 'acme-custom-v1'], meta, 'everflow')).toBeNull();

    // Core: exposes confirmed models ahead of unverified models (excluding text-only),
    // and auto-selects only confirmed tool-calling models.
    const filteredCore = filterDaltonCuratedModels(groups, true, { profile: 'core', metadata: meta });
    expect(Object.keys(filteredCore)).toEqual(['openai', 'acme-gateway']);
    expect(filteredCore.openai?.models).toEqual(['gpt-5', 'gpt-5-mini']);
    expect(filteredCore['acme-gateway']?.models).toEqual(['acme-custom-v1']);
    expect(hasAvailableDaltonModels(groups, meta, payers, true, undefined, 'core')).toBe(true);
    expect(resolveDaltonSetupModels(['acme-custom-v1', 'gpt-5-mini', 'gpt-5', 'o1-text-only'], {
      profile: 'core',
      metadata: meta,
    })).toEqual(['gpt-5', 'gpt-5-mini', 'acme-custom-v1']);
    expect(resolveDaltonConfirmedSetupModels(['acme-custom-v1', 'gpt-5-mini', 'gpt-5', 'o1-text-only'], {
      profile: 'core',
      metadata: meta,
    })).toEqual(['gpt-5', 'gpt-5-mini']);
    expect(pickDaltonConnectedDefault(['acme-custom-v1', 'gpt-5-mini', 'gpt-5', 'o1-text-only'], meta, 'core')).toBe('gpt-5');

    const choices = defaultChoices({
      models: 'subscription_or_key',
      offered: groups,
      slotModels: ['gpt-5-mini', 'gpt-5', 'acme-custom-v1'],
      payers,
      ownProviders: ['openai', 'acme-gateway'],
      metadata: meta,
      customModels: [{ name: 'acme-custom-v1', model_id: 'corp-1', provider: 'acme-gateway' }],
      connectionOf: (p) => p,
      systemDefault: null,
      systemBackground: null,
      preferred: null,
      dalton: true,
      daltonProfile: 'core',
    });
    expect(choices.list).toEqual(['gpt-5', 'gpt-5-mini', 'acme-custom-v1']);
    expect(choices.initial).toBe('gpt-5');
    expect(choices.recommended).toEqual({
      primary: 'gpt-5',
      primaryWhy: 'strongest',
      background: 'gpt-5-mini',
      backgroundWhy: 'fastest',
    });
  });

  it('lists unverified custom gateway models without auto-selecting them until probed, and promotes them once verified', () => {
    const customGroups = {
      'acme-gateway': { display_name: 'Acme Gateway', models: ['acme-custom-v1'] },
    };
    const unverifiedMeta = {
      'acme-custom-v1': { provider: 'acme-gateway', model_id: 'corp-1', is_custom_model: true },
    };
    const customPayers = { 'acme-custom-v1': 'own_key' as const };
    const customModels = [{ name: 'acme-custom-v1', model_id: 'corp-1', provider: 'acme-gateway' }];

    // Unverified custom model is NOT treated as confirmed tool-calling and is NOT auto-selected.
    expect(isDaltonToolCallingUnverified(unverifiedMeta['acme-custom-v1'])).toBe(true);
    expect(isDaltonToolCallingConfirmed(unverifiedMeta['acme-custom-v1'])).toBe(false);
    expect(hasAvailableDaltonModels(customGroups, unverifiedMeta, customPayers, true, undefined, 'core')).toBe(false);
    expect(pickDaltonConnectedDefault(['acme-custom-v1'], unverifiedMeta, 'core')).toBeNull();
    const unverifiedChoices = defaultChoices({
      models: 'subscription_or_key',
      offered: customGroups,
      slotModels: ['acme-custom-v1'],
      payers: customPayers,
      ownProviders: ['acme-gateway'],
      metadata: unverifiedMeta,
      customModels,
      connectionOf: (p) => p,
      systemDefault: null,
      systemBackground: null,
      preferred: null,
      dalton: true,
      daltonProfile: 'core',
    });
    expect(unverifiedChoices.list).toEqual(['acme-custom-v1']);
    expect(unverifiedChoices.initial).toBeNull();
    expect(unverifiedChoices.recommended.primary).toBeNull();

    // Once /api/v1/keys/test probe records per-model tool_calling_models, ONLY the probed model
    // becomes confirmed while unprobed sibling models on the same provider remain unverified.
    const twoCustomMeta = {
      'acme-custom-v1': { provider: 'acme-gateway', model_id: 'corp-1', is_custom_model: true },
      'acme-custom-v2': { provider: 'acme-gateway', model_id: 'corp-2', is_custom_model: true },
    };
    const twoCustomModels = [
      { name: 'acme-custom-v1', model_id: 'corp-1', provider: 'acme-gateway' },
      { name: 'acme-custom-v2', model_id: 'corp-2', provider: 'acme-gateway' },
    ];
    const probedMeta = applyDaltonProviderToolProbes(
      twoCustomMeta,
      [
        {
          provider: 'acme-gateway',
          display_name: 'Acme Gateway',
          access_type: 'api_key',
          is_custom: true,
          tool_calling_models: { 'corp-1': true },
        },
      ],
      twoCustomModels,
    );
    expect(isDaltonToolCallingConfirmed(probedMeta['acme-custom-v1'])).toBe(true);
    expect(isDaltonToolCallingUnverified(probedMeta['acme-custom-v2'])).toBe(true);
    expect(hasAvailableDaltonModels(customGroups, probedMeta, customPayers, true, undefined, 'core')).toBe(true);
    const verifiedChoices = defaultChoices({
      models: 'subscription_or_key',
      offered: customGroups,
      slotModels: ['acme-custom-v1'],
      payers: customPayers,
      ownProviders: ['acme-gateway'],
      metadata: probedMeta,
      customModels,
      connectionOf: (p) => p,
      systemDefault: null,
      systemBackground: null,
      preferred: null,
      dalton: true,
      daltonProfile: 'core',
    });
    expect(verifiedChoices.list).toEqual(['acme-custom-v1']);
    expect(verifiedChoices.initial).toBe('acme-custom-v1');
    expect(verifiedChoices.recommended).toEqual({
      primary: 'acme-custom-v1',
      primaryWhy: null,
      background: 'acme-custom-v1',
      backgroundWhy: 'only',
    });

    // When the only connected model is explicitly text-only, Core excludes it completely.
    const textOnlyGroups = { openai: { display_name: 'OpenAI', models: ['text-only-m'] } };
    const textOnlyMeta = { 'text-only-m': { provider: 'openai', tool_calling: false } };
    const textOnlyPayers = { 'text-only-m': 'own_key' as const };
    expect(hasAvailableDaltonModels(textOnlyGroups, textOnlyMeta, textOnlyPayers, true, undefined, 'core')).toBe(false);
    expect(filterDaltonCuratedModels(textOnlyGroups, true, { profile: 'core', metadata: textOnlyMeta })).toEqual({});
  });

  it('keeps curated candidates unchanged in Everflow even if metadata flags tool_calling=false, while Core filters and aligns list/recommended/background pools', () => {
    const mixedGroups = {
      openai: { display_name: 'OpenAI', models: ['gpt-5'] },
      'codex-oauth': { display_name: 'Codex', models: ['gpt-6.2-sol-oauth'] },
      'antigravity-oauth': { display_name: 'Antigravity', models: ['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-low-oauth'] },
    };
    const meta = {
      'gemini-4-neon-high-oauth': { provider: 'antigravity-oauth', tool_calling: true },
      'gemini-4-hydrogen-low-oauth': { provider: 'antigravity-oauth', tool_calling: false },
      'gpt-6.2-sol-oauth': { provider: 'codex-oauth', tool_calling: false },
      'gpt-5': { provider: 'openai', tool_calling: true },
    };
    // Everflow preserves its original 5 curated rules (does not drop gpt-6.2-sol-oauth).
    const efCurated = filterDaltonCuratedModels(mixedGroups, true, { profile: 'everflow', metadata: meta });
    expect(Object.values(efCurated).flatMap((g) => g.models ?? [])).toEqual([
      'gemini-4-neon-high-oauth',
      'gpt-6.2-sol-oauth',
    ]);

    // Core drops tool_calling === false curated model and excludes tool_calling === false Hydrogen from background.
    const coreCurated = filterDaltonCuratedModels(mixedGroups, true, { profile: 'core', metadata: meta });
    expect(Object.keys(coreCurated)).toEqual(['antigravity-oauth']);
    expect(Object.values(coreCurated).flatMap((g) => g.models ?? [])).toEqual(['gemini-4-neon-high-oauth']);

    const coreChoices = defaultChoices({
      models: 'subscription_or_key',
      offered: mixedGroups,
      slotModels: ['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-low-oauth', 'gpt-5'],
      payers: {
        'gemini-4-neon-high-oauth': 'subscription:antigravity-oauth',
        'gemini-4-hydrogen-low-oauth': 'subscription:antigravity-oauth',
        'gpt-6.2-sol-oauth': 'subscription:codex-oauth',
        'gpt-5': 'own_key',
      },
      ownProviders: ['antigravity-oauth', 'codex-oauth', 'openai'],
      metadata: meta,
      customModels: [],
      connectionOf: (p) => p,
      systemDefault: null,
      systemBackground: null,
      preferred: null,
      dalton: true,
      daltonProfile: 'core',
    });
    expect(coreChoices.recommended.background).not.toBe('gemini-4-hydrogen-low-oauth');
    expect(coreChoices.list).toContain(coreChoices.initial);

    // Core: when curated candidate has unknown tool_calling and custom model is verified,
    // both displayed list and recommended/initial come from the SAME pool (verified custom model is listed & picked).
    const unverifiedCuratedChoices = defaultChoices({
      models: 'subscription_or_key',
      offered: {
        'antigravity-oauth': { models: ['gemini-4-neon-high-oauth'] },
        'acme-gateway': { models: ['acme-verified'] },
      },
      slotModels: ['gemini-4-neon-high-oauth', 'acme-verified'],
      payers: { 'gemini-4-neon-high-oauth': 'subscription:antigravity-oauth', 'acme-verified': 'own_key' },
      ownProviders: ['antigravity-oauth', 'acme-gateway'],
      metadata: {
        'gemini-4-neon-high-oauth': { provider: 'antigravity-oauth' },
        'acme-verified': { provider: 'acme-gateway', tool_calling: true },
      },
      customModels: [{ name: 'acme-verified', model_id: 'corp-1', provider: 'acme-gateway' }],
      connectionOf: (p) => p,
      systemDefault: null,
      systemBackground: null,
      preferred: null,
      dalton: true,
      daltonProfile: 'core',
    });
    expect(unverifiedCuratedChoices.list).toEqual(['acme-verified', 'gemini-4-neon-high-oauth']);
    expect(unverifiedCuratedChoices.initial).toBe('acme-verified');
    expect(unverifiedCuratedChoices.recommended.primary).toBe('acme-verified');
    expect(unverifiedCuratedChoices.list).toContain(unverifiedCuratedChoices.recommended.primary);
  });

  it('computes ModelStep control configuration centrally via getDaltonModelStepConfig', () => {
    const efCfg = getDaltonModelStepConfig({
      dalton: true,
      daltonProfile: 'everflow',
      ownKeys: true,
      configured: new Set(['openai']),
      curatedProviders: DALTON_CURATED_PROVIDERS,
    });
    expect(efCfg.curatedOnly).toBe(true);
    expect(efCfg.showPlanGroups).toBe(false);
    expect(efCfg.showApiKeyLink).toBe(false);
    expect(efCfg.allowedSignIns).toBe(DALTON_CURATED_PROVIDERS);
    expect(efCfg.canContinue).toBe(false);

    const coreCfg = getDaltonModelStepConfig({
      dalton: true,
      daltonProfile: 'core',
      ownKeys: true,
      configured: new Set(['openai']),
      curatedProviders: DALTON_CURATED_PROVIDERS,
    });
    expect(coreCfg.curatedOnly).toBe(false);
    expect(coreCfg.showPlanGroups).toBe(true);
    expect(coreCfg.showApiKeyLink).toBe(true);
    expect(coreCfg.allowedSignIns).toBeUndefined();
    expect(coreCfg.canContinue).toBe(true);
  });

  it('matches per-model probe verdicts strictly by model_id even when model names and model_ids cross-collide', () => {
    const collidingMeta = {
      'alias-a': { provider: 'acme-gateway', model_id: 'corp-1', is_custom_model: true },
      'corp-1': { provider: 'acme-gateway', model_id: 'corp-2', is_custom_model: true },
    };
    const collidingCustomModels = [
      { name: 'alias-a', model_id: 'corp-1', provider: 'acme-gateway' },
      { name: 'corp-1', model_id: 'corp-2', provider: 'acme-gateway' },
    ];
    const applied = applyDaltonProviderToolProbes(
      collidingMeta,
      [
        {
          provider: 'acme-gateway',
          display_name: 'Acme Gateway',
          access_type: 'api_key',
          is_custom: true,
          tool_calling_models: { 'corp-1': true },
        },
      ],
      collidingCustomModels,
    );
    // Only alias-a (model_id=corp-1) is confirmed; corp-1 (model_id=corp-2) must stay unverified!
    expect(isDaltonToolCallingConfirmed(applied['alias-a'])).toBe(true);
    expect(isDaltonToolCallingUnverified(applied['corp-1'])).toBe(true);
    expect(pickDaltonConnectedDefault(['corp-1'], applied, 'core')).toBeNull();
    expect(pickDaltonConnectedDefault(['corp-1', 'alias-a'], applied, 'core')).toBe('alias-a');
  });

  it('requires confirmed tool-calling for Core deployment background and never auto-selects Auto when filtered background is null', () => {
    const textOnlyBgMeta = { provider: 'openai', tool_calling: false };
    const unverifiedBgMeta = { provider: 'openai' };
    const verifiedBgMeta = { provider: 'openai', tool_calling: true };

    // Core: deployment background requires tool_calling === true.
    expect(isDaltonAllowedSystemBackground(textOnlyBgMeta, 'own_key', undefined, 'core')).toBe(false);
    expect(isDaltonAllowedSystemBackground(unverifiedBgMeta, 'own_key', undefined, 'core')).toBe(false);
    expect(isDaltonAllowedSystemBackground(verifiedBgMeta, 'own_key', undefined, 'core')).toBe(true);
    // Core: when filtered recommended.background is null, do NOT fall back to __auto__.
    expect(resolveDaltonRecommendedBackground(null, 'gpt-6-luna', '__auto__', true, 'core')).toBe('');
    expect(resolveDaltonRecommendedBackground('gpt-6-luna', 'gpt-6-luna', '__auto__', true, 'core')).toBe('gpt-6-luna');

    // Everflow: keeps exact original behavior.
    expect(isDaltonAllowedSystemBackground(textOnlyBgMeta, 'own_key', undefined, 'everflow')).toBe(
      isDaltonAllowedModel(textOnlyBgMeta, 'own_key', undefined),
    );
    expect(resolveDaltonRecommendedBackground(null, 'gemini-4-hydrogen-low-oauth', '__auto__', true, 'everflow')).toBe('__auto__');
  });
});
