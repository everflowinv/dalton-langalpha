import { describe, expect, it } from 'vitest';
import { defaultChoices, type DefaultChoicesInput } from '../defaultChoices';
import { connectedModel } from '../connectedDefault';
// DALTON-HOOK(antigravity-series): buildSeriesCatalog helper for series tests
import { buildSeriesCatalog } from '@/lib/antigravitySeries';

const input = (over: Partial<DefaultChoicesInput>): DefaultChoicesInput => ({
  models: 'subscription_or_key',
  offered: {},
  slotModels: [],
  payers: {},
  ownProviders: [],
  metadata: {},
  customModels: [],
  connectionOf: (p) => p,
  systemDefault: null,
  systemBackground: null,
  preferred: null,
  ...over,
});

const rated = {
  'gpt-astra': { intelligence: 5, speed: 2 },
  'gpt-sol': { intelligence: 5, speed: 3 },
  'gpt-terra': { intelligence: 4, speed: 3 },
  'gpt-luna': { intelligence: 3, speed: 5 },
};

describe('defaultChoices', () => {
  it('recommends the strongest model for conversations and the fastest other one for Background', () => {
    const choices = defaultChoices(input({
      offered: { 'codex-oauth': { models: ['gpt-astra', 'gpt-terra', 'gpt-sol', 'gpt-luna'] }, platform: { models: ['house'] } },
      slotModels: ['gpt-astra', 'gpt-terra', 'gpt-sol', 'gpt-luna', 'house'],
      ownProviders: ['codex-oauth'],
      metadata: rated,
    }));
    expect(choices.recommended).toEqual({
      primary: 'gpt-sol', primaryWhy: 'strongestFastest', background: 'gpt-luna', backgroundWhy: 'fastest',
    });
    expect(choices.initial).toBe('gpt-sol');
    expect(choices.list).toEqual(['gpt-sol', 'gpt-astra', 'gpt-terra', 'gpt-luna']);
  });

  it('opens on a default already saved, with the recommended one still listed', () => {
    const choices = defaultChoices(input({
      offered: { 'codex-oauth': { models: ['gpt-astra', 'gpt-terra', 'gpt-sol', 'gpt-luna'] } },
      slotModels: ['gpt-astra', 'gpt-terra', 'gpt-sol', 'gpt-luna'],
      ownProviders: ['codex-oauth'],
      metadata: rated,
      preferred: 'gpt-terra',
    }));
    expect(choices.initial).toBe('gpt-terra');
    expect(choices.list.slice(0, 2)).toEqual(['gpt-terra', 'gpt-sol']);
    expect(choices.recommended.primary).toBe('gpt-sol');
  });

  it('offers the deployment\'s models with its own picks recommended', () => {
    const choices = defaultChoices(input({
      models: 'langalpha_models',
      offered: { platform: { models: ['a', 'b', 'c'] }, 'codex-oauth': { models: ['mine'] } },
      slotModels: ['a', 'b', 'c', 'mine'],
      payers: { a: 'credits', b: 'credits', c: 'credits', mine: 'subscription:codex-oauth' },
      systemDefault: 'b',
      systemBackground: 'c',
      preferred: 'a',
    }));
    expect(choices.list).toEqual(['b', 'a', 'c']);
    expect(choices.recommended).toEqual({ primary: 'b', primaryWhy: 'deployment', background: 'c', backgroundWhy: 'deployment' });
  });

  it('recommends no Background for LangAlpha\'s models that a key the user connected would pay for', () => {
    const choices = defaultChoices(input({
      models: 'langalpha_models',
      offered: { platform: { models: ['a', 'b'] }, openai: { models: ['keyed'] } },
      slotModels: ['a', 'b', 'keyed'],
      payers: { a: 'credits', b: 'credits', keyed: 'own_key' },
      metadata: { a: { speed: 2 }, b: { speed: 4 } },
      systemDefault: 'a',
      systemBackground: 'keyed',
    }));
    expect(choices.recommended).toMatchObject({ primary: 'a', background: 'b', backgroundWhy: 'fastest' });
  });

  it('leaves Claude Code\'s Background to the deployment, which runs it', () => {
    const choices = defaultChoices(input({
      models: 'claude_code',
      offered: { 'claude-code': { models: ['cc-opus', 'cc-sonnet', 'cc-haiku'] } },
      slotModels: ['house-fast'],
      metadata: {
        'cc-opus': { model_id: 'claude-opus-5', intelligence: 5, speed: 3 },
        'cc-sonnet': { model_id: 'claude-sonnet-5', intelligence: 5, speed: 4 },
        'cc-haiku': { model_id: 'claude-haiku-5', intelligence: 3, speed: 5 },
      },
      systemBackground: 'house-fast',
    }));
    expect(choices.recommended).toEqual({ primary: 'cc-sonnet', primaryWhy: 'claudeCode', background: null, backgroundWhy: 'claudeCode' });
  });

  it('says Claude Code\'s Background bills credits where the deployment\'s model does', () => {
    const choices = defaultChoices(input({
      models: 'claude_code',
      offered: { 'claude-code': { models: ['cc-sonnet'] } },
      slotModels: ['house-fast'],
      payers: { 'house-fast': 'credits' },
      systemBackground: 'house-fast',
    }));
    expect(choices.recommended).toMatchObject({ background: null, backgroundWhy: 'claudeCodeCredits' });
  });

  it('recommends Claude Code\'s newest Sonnet, which suits any Claude plan, over an Opus or Fable rated higher', () => {
    const choices = defaultChoices(input({
      models: 'claude_code',
      offered: { 'claude-code': { models: ['cc-fable', 'cc-opus', 'cc-sonnet-new', 'cc-sonnet-old', 'cc-haiku'] } },
      metadata: {
        'cc-fable': { model_id: 'claude-fable-5-2', intelligence: 5, speed: 3 },
        'cc-opus': { model_id: 'claude-opus-5-5', intelligence: 5, speed: 3 },
        'cc-sonnet-new': { model_id: 'claude-sonnet-5-5', intelligence: 4, speed: 4 },
        'cc-sonnet-old': { model_id: 'claude-sonnet-5', intelligence: 4, speed: 4 },
        'cc-haiku': { model_id: 'claude-haiku-5-5', intelligence: 2, speed: 5 },
      },
    }));
    expect(choices.recommended).toMatchObject({ primary: 'cc-sonnet-new', primaryWhy: 'claudeCode' });
    expect(choices.initial).toBe('cc-sonnet-new');
  });

  it('starts Claude Code on its own pick where the manifest rates none', () => {
    const choices = defaultChoices(input({
      models: 'claude_code',
      offered: { 'claude-code': { models: ['cc-opus', 'cc-sonnet'] } },
      metadata: { 'cc-opus': { model_id: 'claude-opus-5' }, 'cc-sonnet': { model_id: 'claude-sonnet-5' } },
    }));
    expect(choices.initial).toBe('cc-sonnet');
    expect(choices.recommended.primaryWhy).toBe('claudeCode');
  });

  it('pairs a lone model with itself for Background', () => {
    const choices = defaultChoices(input({
      offered: { deepseek: { models: ['ds'] } },
      slotModels: ['ds'],
      ownProviders: ['deepseek'],
    }));
    expect(choices.recommended).toMatchObject({ primary: 'ds', background: 'ds', backgroundWhy: 'only' });
  });

  it('offers nothing where nothing is connected', () => {
    expect(defaultChoices(input({})).list).toEqual([]);
    expect(defaultChoices(input({})).initial).toBeNull();
  });

  it('finds a native variant\'s models where the list files them under the brand, and a plan\'s picked ones', () => {
    const choices = defaultChoices(input({
      offered: { moonshot: { models: ['kimi-own', 'kimi-plan'] }, 'z-ai-coding': { models: ['glm-a'] } },
      metadata: { 'kimi-own': { provider: 'moonshot' }, 'kimi-plan': { provider: 'moonshot-coding' }, 'glm-a': { provider: 'z-ai' } },
      ownProviders: ['moonshot-coding'],
      preferred: 'kimi-plan',
    }));
    expect(choices.list).toEqual(['kimi-plan']);
    const plan = defaultChoices(input({
      offered: { 'z-ai': { models: ['glm-a'] } },
      metadata: { 'glm-a': { provider: 'z-ai' } },
      customModels: [{ name: 'glm-a', model_id: 'glm-a', provider: 'z-ai-coding' }],
      ownProviders: ['z-ai-coding'],
    }));
    expect(plan.list).toEqual(['glm-a']);
  });
});

describe('connectedModel', () => {
  it('saves the model the default step recommends, so the two agree', () => {
    const models = { 'codex-oauth': { models: ['gpt-astra', 'gpt-terra', 'gpt-sol', 'gpt-luna'] } };
    expect(connectedModel('codex-oauth', models, rated, null, [], (p) => p)).toBe('gpt-sol');
  });

  it('keeps a Claude Code Default already saved', () => {
    const models = { 'claude-code': { models: ['cc-opus', 'cc-sonnet'] } };
    const metadata = {
      'cc-opus': { model_id: 'claude-opus-5', sdk: 'claude-code', intelligence: 5, speed: 3 },
      'cc-sonnet': { model_id: 'claude-sonnet-5', sdk: 'claude-code', intelligence: 5, speed: 4 },
    };
    expect(connectedModel('claude-code', models, metadata, null, [], (p) => p)).toBe('cc-sonnet');
    expect(connectedModel('claude-code', models, metadata, 'cc-opus', [], (p) => p)).toBe('cc-opus');
  });

  it('saves Claude Code\'s Sonnet, as the default step recommends, over an Opus rated higher', () => {
    const models = { 'claude-code': { models: ['cc-opus', 'cc-sonnet'] } };
    const metadata = {
      'cc-opus': { model_id: 'claude-opus-5-5', sdk: 'claude-code', intelligence: 5, speed: 3 },
      'cc-sonnet': { model_id: 'claude-sonnet-5-5', sdk: 'claude-code', intelligence: 4, speed: 4 },
    };
    expect(connectedModel('claude-code', models, metadata, null, [], (p) => p)).toBe('cc-sonnet');
  });
});

// DALTON-HOOK(antigravity-series): series collapsing & series default resolution in defaultChoices
describe('defaultChoices Antigravity series collapsing', () => {
  const seriesCatalog = buildSeriesCatalog([
    {
      series_id: 'antigravity-oauth:gemini-4-neon',
      series_prefix: 'gemini-4-neon',
      display_name: 'Gemini 4 Neon',
      qualified_display_name: 'Gemini 4 Neon (Antigravity)',
      provider: 'antigravity-oauth',
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
      series_id: 'antigravity-oauth:gemini-4-hydrogen',
      series_prefix: 'gemini-4-hydrogen',
      display_name: 'Gemini 4 Hydrogen',
      qualified_display_name: 'Gemini 4 Hydrogen (Antigravity)',
      provider: 'antigravity-oauth',
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
  const antigravityOffered = {
    'antigravity-oauth': {
      models: [
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
        'gemini-4-neon-xhigh-oauth',
        'gemini-4-hydrogen-low-oauth',
        'gemini-4-hydrogen-medium-oauth',
        'gemini-4-hydrogen-high-oauth',
      ],
    },
  };
  const antigravityMeta = {
    'gemini-4-neon-low-oauth': { provider: 'antigravity-oauth', display_name: 'Gemini 4 Neon (Low)', intelligence: 5, speed: 3 },
    'gemini-4-neon-medium-oauth': { provider: 'antigravity-oauth', display_name: 'Gemini 4 Neon (Medium)', intelligence: 5, speed: 3 },
    'gemini-4-neon-high-oauth': { provider: 'antigravity-oauth', display_name: 'Gemini 4 Neon (High)', intelligence: 5, speed: 3 },
    'gemini-4-neon-xhigh-oauth': { provider: 'antigravity-oauth', display_name: 'Gemini 4 Neon (XHigh)', intelligence: 5, speed: 2 },
    'gemini-4-hydrogen-low-oauth': { provider: 'antigravity-oauth', display_name: 'Gemini 4 Hydrogen (Low)', intelligence: 4, speed: 5 },
    'gemini-4-hydrogen-medium-oauth': { provider: 'antigravity-oauth', display_name: 'Gemini 4 Hydrogen (Medium)', intelligence: 4, speed: 5 },
    'gemini-4-hydrogen-high-oauth': { provider: 'antigravity-oauth', display_name: 'Gemini 4 Hydrogen (High)', intelligence: 4, speed: 5 },
  };

  it('shows curated Neon primary and recommends Hydrogen -low for background even when Hydrogen default_effort is high', () => {
    const choices = defaultChoices(input({
      dalton: true,
      offered: antigravityOffered,
      slotModels: antigravityOffered['antigravity-oauth'].models,
      ownProviders: ['antigravity-oauth'],
      metadata: antigravityMeta,
      seriesCatalog,
    }));

    expect(choices.recommended.primary).toBe('gemini-4-neon-high-oauth');
    expect(choices.recommended.background).toBe('gemini-4-hydrogen-low-oauth');
    expect(choices.initial).toBe('gemini-4-neon-high-oauth');
    expect(choices.list).toEqual(['gemini-4-neon-high-oauth']);
  });

  it('preserves a saved variant in initial while keeping its series folded to a single card', () => {
    const choices = defaultChoices(input({
      dalton: true,
      offered: antigravityOffered,
      slotModels: antigravityOffered['antigravity-oauth'].models,
      ownProviders: ['antigravity-oauth'],
      metadata: antigravityMeta,
      seriesCatalog,
      preferred: 'gemini-4-neon-medium-oauth',
    }));

    expect(choices.initial).toBe('gemini-4-neon-medium-oauth');
    expect(choices.list).toEqual(['gemini-4-neon-high-oauth']);
  });

  // DALTON-HOOK(dalton-setup): Dalton returns empty choices when none of the 5 curated candidates is available; non-Dalton still collapses series
  it('collapses Antigravity variants to one representative per series in non-Dalton mode and returns empty in Dalton mode when no curated candidate is available', () => {
    const nonCuratedOffered = {
      'antigravity-oauth': {
        models: [
          'gemini-4-neon-low-oauth',
          'gemini-4-neon-medium-oauth',
          'gemini-4-hydrogen-low-oauth',
          'gemini-4-hydrogen-high-oauth',
        ],
      },
    };
    const nonDalton = defaultChoices(input({
      dalton: false,
      offered: nonCuratedOffered,
      slotModels: nonCuratedOffered['antigravity-oauth'].models,
      ownProviders: ['antigravity-oauth'],
      metadata: antigravityMeta,
      seriesCatalog,
    }));
    expect(nonDalton.list).toEqual(['gemini-4-neon-medium-oauth', 'gemini-4-hydrogen-high-oauth']);

    const dalton = defaultChoices(input({
      dalton: true,
      offered: nonCuratedOffered,
      slotModels: nonCuratedOffered['antigravity-oauth'].models,
      ownProviders: ['antigravity-oauth'],
      metadata: antigravityMeta,
      seriesCatalog,
    }));
    expect(dalton.list).toEqual([]);
    expect(dalton.initial).toBeNull();
    expect(dalton.recommended.primary).toBeNull();
  });
});
