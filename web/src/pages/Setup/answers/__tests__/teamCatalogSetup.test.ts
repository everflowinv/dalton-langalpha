// DALTON-HOOK(team-model-catalog): unit tests for server-driven curated setup candidates & providers across all setup entrypoints
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import type { AvailableModels, ModelsResponse } from '@/api/model';
import type { Payer } from '@/lib/modelPayer';
import {
  filterDaltonCuratedModels,
  getDaltonModelStepConfig,
  hasAvailableDaltonModels,
  pickDaltonConnectedDefault,
  resolveDaltonSetupCandidates,
  resolveDaltonSetupModels,
} from '@/config/daltonSetup';
import { defaultChoices, type DefaultChoicesInput } from '../defaultChoices';
import {
  DALTON_CURATED_PROVIDERS,
  isDaltonEverflowCuratedEmpty,
  needsModelStep,
  nextSetupStep,
  resolveDaltonCuratedProviders,
  setupRail,
} from '../setupSteps';

const h = vi.hoisted(() => ({
  getAvailableModels: vi.fn<() => Promise<ModelsResponse>>(),
  getProviderAvailableModels: vi.fn<
    (provider: string, options?: { refresh?: boolean; signal?: AbortSignal }) => Promise<AvailableModels>
  >(),
  getPreferences: vi.fn<() => Promise<Record<string, unknown>>>(async () => ({})),
}));

vi.mock('@/config/brand', () => ({
  isDaltonBuild: true,
  isCoreProfileBuild: false,
  DALTON_PROFILE: 'everflow',
  BRAND_NAME: 'Everflow',
  BRAND_MARK: 'everflow',
  faviconHref: () => '/everflow/favicon.svg',
}));

vi.mock('@/config/hostMode', () => ({
  isPlatformMode: false,
  HOST_MODE: 'oss',
  APP_ENTRY_PATH: '/',
}));

vi.mock('@/api/model', () => ({
  getAvailableModels: h.getAvailableModels,
  getProviderAvailableModels: h.getProviderAvailableModels,
}));

vi.mock('@/pages/Dashboard/utils/api', () => ({
  getPreferences: h.getPreferences,
}));

import { checkDaltonTeamModelsAvailable } from '../daltonTeamModelsCheck';

const BASE_META = {
  'gemini-4-neon-high-oauth': {
    provider: 'antigravity-oauth',
    intelligence: 5,
    speed: 4,
    tool_calling: true,
  },
  'gpt-6.2-sol-oauth': {
    provider: 'codex-oauth',
    intelligence: 5,
    speed: 3,
    tool_calling: true,
  },
  'glm-99-preview-plan': {
    provider: 'z-ai-cn-coding',
    intelligence: 5,
    speed: 4,
    tool_calling: true,
  },
  'gpt-5': {
    provider: 'openai',
    intelligence: 5,
    speed: 4,
    tool_calling: true,
  },
  'text-only-mini': {
    provider: 'openai',
    intelligence: 3,
    speed: 5,
    tool_calling: false,
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  h.getPreferences.mockResolvedValue({});
});

describe('team catalog setup alignment (dalton_setup_candidates & dalton_curated_providers)', () => {
  it('resolveDaltonCuratedProviders uses server list (including empty []) and falls back only when omitted', () => {
    expect([...resolveDaltonCuratedProviders(['codex-oauth', 'z-ai-cn-coding'])]).toEqual([
      'codex-oauth',
      'z-ai-cn-coding',
    ]);
    expect(resolveDaltonCuratedProviders([]).size).toBe(0);
    expect(resolveDaltonCuratedProviders(undefined)).toBe(DALTON_CURATED_PROVIDERS);
    expect(resolveDaltonCuratedProviders(null)).toBe(DALTON_CURATED_PROVIDERS);
  });

  it('drops hidden/retired default curated models when server dalton_setup_candidates excludes them', () => {
    const available = ['gemini-4-neon-high-oauth', 'gpt-6.2-sol-oauth', 'glm-99-preview-plan'];
    // Admin hid gemini-4-neon-high-oauth and added glm-99-preview-plan to curated_setup
    const serverCandidates = ['glm-99-preview-plan', 'gpt-6.2-sol-oauth'];

    expect(resolveDaltonSetupCandidates(available, BASE_META, 'everflow', serverCandidates)).toEqual([
      'glm-99-preview-plan',
      'gpt-6.2-sol-oauth',
    ]);

    const choices = defaultChoices({
      models: 'langalpha_models',
      offered: {
        'antigravity-oauth': { models: ['gemini-4-neon-high-oauth'] },
        'codex-oauth': { models: ['gpt-6.2-sol-oauth'] },
        'z-ai-cn-coding': { models: ['glm-99-preview-plan'] },
      },
      slotModels: available,
      payers: {
        'gemini-4-neon-high-oauth': 'subscription:antigravity-oauth',
        'gpt-6.2-sol-oauth': 'subscription:codex-oauth',
        'glm-99-preview-plan': 'subscription:z-ai-cn-coding',
      },
      ownProviders: [],
      metadata: BASE_META,
      customModels: [],
      connectionOf: (p) => p,
      systemDefault: null,
      systemBackground: null,
      preferred: null,
      dalton: true,
      daltonProfile: 'everflow',
      daltonSetupCandidates: serverCandidates,
    } satisfies DefaultChoicesInput);

    expect(choices.list).toEqual(['glm-99-preview-plan', 'gpt-6.2-sol-oauth']);
    expect(choices.list).not.toContain('gemini-4-neon-high-oauth');
    expect(choices.recommended.primary).toBe('glm-99-preview-plan');
  });

  it('handles empty server curated list per contract: Everflow stays strict/blocked, Core falls back to tool-calling models', () => {
    const groups = {
      openai: { display_name: 'OpenAI', models: ['gpt-5', 'text-only-mini'] },
    };
    const payers: Record<string, Payer> = { 'gpt-5': 'own_key', 'text-only-mini': 'own_key' };

    // Everflow: strict curated policy -> empty list, cannot continue on /setup/model
    expect(
      resolveDaltonSetupModels(['gpt-5', 'text-only-mini'], {
        profile: 'everflow',
        metadata: BASE_META,
        serverCandidates: [],
      }),
    ).toEqual([]);
    expect(
      hasAvailableDaltonModels(groups, BASE_META, payers, true, undefined, 'everflow', []),
    ).toBe(false);
    expect(
      filterDaltonCuratedModels(groups, true, {
        profile: 'everflow',
        metadata: BASE_META,
        serverCandidates: [],
      }),
    ).toEqual({});
    expect(pickDaltonConnectedDefault(['gpt-5'], BASE_META, 'everflow', [])).toBeNull();
    const efModelStep = getDaltonModelStepConfig({
      dalton: true,
      daltonProfile: 'everflow',
      ownKeys: true,
      configured: new Set(['antigravity-oauth']),
      curatedProviders: resolveDaltonCuratedProviders([]),
    });
    expect(efModelStep.canContinue).toBe(false);
    expect(efModelStep.allowedSignIns?.size).toBe(0);

    // Admin with empty server dalton_curated_providers keeps policy-permitted sign-ins & key connections
    const efAdminModelStep = getDaltonModelStepConfig({
      dalton: true,
      daltonProfile: 'everflow',
      ownKeys: true,
      configured: new Set(),
      curatedProviders: resolveDaltonCuratedProviders([]),
      canManage: true,
    });
    expect(efAdminModelStep.allowedSignIns).toBeUndefined();
    expect(efAdminModelStep.showPlanGroups).toBe(true);
    expect(efAdminModelStep.showApiKeyLink).toBe(true);
    expect(efAdminModelStep.canContinue).toBe(false);

    const efAdminNoKeysStep = getDaltonModelStepConfig({
      dalton: true,
      daltonProfile: 'everflow',
      ownKeys: false,
      configured: new Set(),
      curatedProviders: resolveDaltonCuratedProviders([]),
      canManage: true,
    });
    expect(efAdminNoKeysStep.allowedSignIns).toBeUndefined();
    expect(efAdminNoKeysStep.showPlanGroups).toBe(false);
    expect(efAdminNoKeysStep.showApiKeyLink).toBe(false);

    // Core: falls back to resolveDaltonCoreFallbackModels (excludes text_only)
    expect(
      resolveDaltonSetupModels(['gpt-5', 'text-only-mini'], {
        profile: 'core',
        metadata: BASE_META,
        serverCandidates: [],
      }),
    ).toEqual(['gpt-5']);
    expect(
      hasAvailableDaltonModels(groups, BASE_META, payers, true, undefined, 'core', []),
    ).toBe(true);
    expect(
      filterDaltonCuratedModels(groups, true, {
        profile: 'core',
        metadata: BASE_META,
        serverCandidates: [],
      }),
    ).toEqual({
      openai: { display_name: 'OpenAI', models: ['gpt-5'] },
    });
    expect(pickDaltonConnectedDefault(['gpt-5', 'text-only-mini'], BASE_META, 'core', [])).toBe('gpt-5');
  });

  it('checkDaltonTeamModelsAvailable respects empty server dalton_setup_candidates differently in Everflow vs Core', async () => {
    const qcEverflow = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    const providers = [
      {
        provider: 'openai',
        display_name: 'OpenAI',
        access_type: 'api_key' as const,
        state: 'connected' as const,
        masked_key: 'sk-...1234',
      },
    ];
    h.getProviderAvailableModels.mockResolvedValue({
      provider: 'openai',
      status: 'ok',
      fetched_at: null,
      ready: ['gpt-5'],
      missing: [],
      context_windows: {},
      others: [],
      total: 1,
      cached: false,
    });

    // Everflow with dalton_setup_candidates: [] -> unavailable
    h.getAvailableModels.mockResolvedValueOnce({
      models: { openai: { display_name: 'OpenAI', models: ['gpt-5'] } },
      model_metadata: { 'gpt-5': { provider: 'openai', tool_calling: true, intelligence: 5 } },
      provider_catalog: [
        { provider: 'openai', display_name: 'OpenAI', access_type: 'api_key', brand_key: 'openai', byok_eligible: true },
      ],
      system_defaults: {},
      compaction_profiles: {} as ModelsResponse['compaction_profiles'],
      search_providers: {} as ModelsResponse['search_providers'],
      key_candidates: {},
      dalton_profile: 'everflow',
      dalton_setup_candidates: [],
      dalton_curated_providers: [],
      catalog_revision: 5,
    });

    const efRes = await checkDaltonTeamModelsAvailable({
      queryClient: qcEverflow,
      providers,
      byokEnabled: true,
      providersUnread: false,
      policy: null,
    });
    expect(efRes.available).toBe(false);
    qcEverflow.clear();

    // Core with dalton_setup_candidates: [] -> available via Core fallback
    const qcCore = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    h.getAvailableModels.mockResolvedValueOnce({
      models: { openai: { display_name: 'OpenAI', models: ['gpt-5'] } },
      model_metadata: { 'gpt-5': { provider: 'openai', tool_calling: true, intelligence: 5 } },
      provider_catalog: [
        { provider: 'openai', display_name: 'OpenAI', access_type: 'api_key', brand_key: 'openai', byok_eligible: true },
      ],
      system_defaults: {},
      compaction_profiles: {} as ModelsResponse['compaction_profiles'],
      search_providers: {} as ModelsResponse['search_providers'],
      key_candidates: {},
      dalton_profile: 'core',
      dalton_setup_candidates: [],
      dalton_curated_providers: [],
      catalog_revision: 5,
    });

    const coreRes = await checkDaltonTeamModelsAvailable({
      queryClient: qcCore,
      providers,
      byokEnabled: true,
      providersUnread: false,
      policy: null,
    });
    expect(coreRes.available).toBe(true);
    qcCore.clear();
  });

  it('holds saved langalpha_models members on /setup/model when Everflow curated models are empty and routes Core fallback to /setup/default', () => {
    const savedTeamAnswers = { models: 'langalpha_models' as const, runsOn: 'cloud' as const };

    // Everflow with empty curated candidates -> teamModelsEmpty: true keeps /setup/model in rail and stops at 'model'
    expect(isDaltonEverflowCuratedEmpty('everflow', [])).toBe(true);
    expect(isDaltonEverflowCuratedEmpty('everflow', ['gemini-4-neon-high-oauth'])).toBe(false);
    expect(isDaltonEverflowCuratedEmpty('everflow', undefined)).toBe(false);
    expect(isDaltonEverflowCuratedEmpty('core', [])).toBe(false);
    expect(needsModelStep(savedTeamAnswers, false, true)).toBe(true);
    expect(setupRail(savedTeamAnswers, null, false, true)).toEqual(['model', 'default']);
    expect(
      nextSetupStep({
        answers: savedTeamAnswers,
        server: null,
        ownComputer: false,
        homeOnCloud: true,
        claudeCodeToken: false,
        providerConnected: true,
        teamModelsAvailable: false,
        teamModelsEmpty: true,
        lastStep: null,
      }),
    ).toBe('model');

    // Core with confirmed tool-calling fallback model -> teamModelsAvailable: true, teamModelsEmpty: false advances to 'default'
    expect(needsModelStep(savedTeamAnswers, true, false)).toBe(false);
    expect(setupRail(savedTeamAnswers, null, true, false)).toEqual(['default']);
    expect(
      nextSetupStep({
        answers: savedTeamAnswers,
        server: null,
        ownComputer: false,
        homeOnCloud: true,
        claudeCodeToken: false,
        providerConnected: true,
        teamModelsAvailable: true,
        teamModelsEmpty: false,
        lastStep: null,
      }),
    ).toBe('default');
  });
});
