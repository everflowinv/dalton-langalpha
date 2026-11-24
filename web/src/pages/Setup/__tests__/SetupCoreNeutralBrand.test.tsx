// DALTON-HOOK(standalone-core): Vitest coverage for neutral-brand Core /setup (VITE_DALTON_UI=true, BRAND_MARK=langalpha)
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router';
import i18n from '@/i18n';
import type { ConfiguredProvider } from '@/hooks/useConfiguredProviders';
import type { ModelMetadataEntry } from '@/hooks/useFilteredModels';
import type { CustomModelEntry, ProviderModelsData } from '@/components/model/types';

const h = vi.hoisted(() => ({
  teamIsLoading: false,
  teamUnread: false,
  teamRetry: vi.fn(),
  daltonProfile: 'core' as 'core' | 'everflow' | undefined,
  models: {} as Record<string, ProviderModelsData>,
  slotModels: {} as Record<string, ProviderModelsData>,
  metadata: {} as Record<string, ModelMetadataEntry>,
  payers: {} as Record<string, string>,
  customModels: [] as CustomModelEntry[],
  providers: [] as ConfiguredProvider[],
  systemDefaults: null as { default_model?: string; background_model?: string } | null,
  updatePreferences: vi.fn(async (_payload: unknown) => ({})),
  allIsLoading: false,
  allUnread: false,
}));

// Neutral-brand Core build: BRAND_MARK is 'langalpha', BRAND_NAME is 'LangAlpha',
// while Dalton UI is explicitly enabled via VITE_DALTON_UI=true.
// DefaultStep prefetches the welcome page; a stub keeps that import from
// finishing after the test environment has closed.
vi.mock('@/pages/Onboarding/welcome/Welcome', () => ({ default: () => null }));
vi.mock('@/config/brand', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config/brand')>()),
  BRAND_NAME: 'LangAlpha',
  BRAND_MARK: 'langalpha',
  isDaltonBuild: true,
  isCoreProfileBuild: true,
  DALTON_PROFILE: 'core',
  faviconHref: () => '/logo-favicon.svg',
}));

vi.mock('@/config/hostMode', () => ({
  isPlatformMode: false,
  HOST_MODE: 'oss',
  APP_ENTRY_PATH: '/',
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({
    user: { user_id: 'u-core-neutral', deployment_serves: false, computer_choice: null, access_tier: 0 },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
}));

vi.mock('@/hooks/useAccount', () => ({
  useAccount: () => ({ policy: null }),
  useRule: () => ({ allowed: true, who: { start: '' } }),
}));

vi.mock('@/hooks/useConfiguredProviders', () => ({
  SERVER_KEY: '(server)',
  useConfiguredProviders: () => ({
    providers: h.providers,
    configuredSet: new Set(h.providers.map((p) => p.provider)),
    byokEnabled: true,
    isLoading: false,
    unread: false,
  }),
  useOAuthStatus: () => ({ data: null, isError: false }),
}));

vi.mock('@/hooks/useProviderCatalog', () => ({
  useProviderCatalog: () => ({
    planGroups: [],
    connectionOf: (p: string) => p,
  }),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: { model_preference: {} },
    isLoaded: true,
    unread: false,
    retry: vi.fn(),
  }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => ({
    mutateAsync: h.updatePreferences,
    isPending: false,
    isError: false,
  }),
}));

vi.mock('@/hooks/useClaudeCode', () => ({
  useClaudeCodeSetup: () => ({ setup: null, isLoading: false, unread: false, retry: vi.fn() }),
}));

vi.mock('@/hooks/useClaudeCodeCustody', () => ({
  useClaudeCodeCustody: () => ({ custody: null, isLoading: false, unread: false, retry: vi.fn() }),
}));

vi.mock('@/hooks/useTurnOnClaudeCode', () => ({
  useTurnOnClaudeCode: () => ({ pending: false, failed: false, retry: vi.fn() }),
}));

vi.mock('../answers/flow', () => ({
  useAnswerFlow: () => ({
    isLoading: false,
    first: false,
    answers: { runsOn: 'cloud', models: 'subscription_or_key' },
    facts: { providerConnected: h.providers.length > 0 },
    progress: { setup_on: 'browser', setup_completed_at: null },
    next: 'default',
    back: null,
    finishing: false,
    finishFailed: false,
    finish: vi.fn(async () => true),
    toWelcome: vi.fn(),
    retryFacts: vi.fn(),
    tokenTarget: null,
  }),
  useChangeAnswers: () => vi.fn(),
}));

vi.mock('../answers/connectedDefault', () => ({
  useSaveConnectedDefault: () => ({
    modelFor: () => null,
    settled: () => true,
    save: vi.fn(async () => true),
    saving: false,
  }),
}));

vi.mock('../answers/useTeamModels', () => ({
  useDaltonTeamModels: () => ({
    shared: false,
    available: false,
    empty: false,
    isLoading: h.teamIsLoading,
    unread: h.teamUnread,
    canManage: true,
    callerTierPending: false,
    rejectedProviders: new Set<string>(),
    daltonProfile: h.daltonProfile,
    retry: h.teamRetry,
    all: {
      models: h.models,
      slotModels: h.slotModels,
      metadata: h.metadata,
      payers: h.payers,
      customModels: h.customModels,
      systemDefaults: h.systemDefaults,
      isLoading: h.allIsLoading,
      unread: h.allUnread,
      cannotRun: () => false,
      rawApiResponse: h.daltonProfile ? { dalton_profile: h.daltonProfile, model_metadata: h.metadata } : undefined,
      seriesCatalog: undefined,
      selectableSeries: [],
    },
  }),
}));

import ModelStep from '../steps/ModelStep';
import DefaultStep from '../steps/DefaultStep';

function renderRoute(initialEntry: string, element: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/setup/model" element={element} />
          <Route path="/setup/default" element={element} />
          <Route path="/setup/provider" element={<div data-testid="provider-step" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Neutral-brand Standalone Core /setup wizard (VITE_DALTON_UI=true, BRAND_MARK=langalpha)', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('zh-CN');
    h.teamIsLoading = false;
    h.teamUnread = false;
    h.teamRetry = vi.fn();
    h.daltonProfile = 'core';
    h.allIsLoading = false;
    h.allUnread = false;
    h.models = {};
    h.slotModels = {};
    h.metadata = {};
    h.payers = {};
    h.customModels = [];
    h.providers = [];
    h.systemDefaults = null;
    h.updatePreferences = vi.fn(async () => ({}));
  });

  it('waits while /api/v1/models profile is loading and renders retry UI on failure without guessing Everflow', () => {
    h.daltonProfile = undefined;
    h.teamIsLoading = true;
    const { unmount } = renderRoute('/setup/model', <ModelStep />);
    expect(screen.queryByTestId('setup-sub-api-key')).not.toBeInTheDocument();
    unmount();

    h.teamIsLoading = false;
    h.teamUnread = true;
    renderRoute('/setup/model', <ModelStep />);
    expect(screen.queryByTestId('setup-sub-api-key')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /重试|Try again/i })).toBeInTheDocument();
  });

  it('unlocks the API key connection path on /setup/model in Core under neutral brand', () => {
    h.daltonProfile = 'core';
    renderRoute('/setup/model', <ModelStep />);
    expect(screen.getByTestId('setup-sub-api-key')).toBeInTheDocument();
  });

  it('labels unverified custom models on /setup/default and never auto-selects them, while per-model probing promotes only the probed model', () => {
    h.daltonProfile = 'core';
    h.providers = [
      {
        provider: 'acme-gateway',
        display_name: 'Acme Gateway',
        access_type: 'api_key',
        is_custom: true,
        state: 'connected',
        masked_key: 'sk-...gw',
      },
    ];
    h.models = { 'acme-gateway': { display_name: 'Acme Gateway', models: ['acme-unverified', 'acme-other'] } };
    h.slotModels = h.models;
    h.metadata = {
      'acme-unverified': { provider: 'acme-gateway', model_id: 'corp-1', is_custom_model: true },
      'acme-other': { provider: 'acme-gateway', model_id: 'corp-2', is_custom_model: true },
    };
    h.payers = { 'acme-unverified': 'own_key', 'acme-other': 'own_key' };
    h.customModels = [
      { name: 'acme-unverified', model_id: 'corp-1', provider: 'acme-gateway' },
      { name: 'acme-other', model_id: 'corp-2', provider: 'acme-gateway' },
    ];

    const { unmount } = renderRoute('/setup/default', <DefaultStep />);
    expect(screen.getByText('Acme Unverified')).toBeInTheDocument();
    expect(screen.getByText('Acme Other')).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Acme Unverified/i })).not.toBeChecked();
    expect(screen.getByRole('radio', { name: /Acme Other/i })).not.toBeChecked();
    expect(screen.getAllByTestId('setup-model-unverified-tool-calling')).toHaveLength(2);
    // Unverified models are NOT auto-selected: Continue button remains disabled until user explicitly selects.
    expect(screen.getByRole('button', { name: /继续|Continue/i })).toBeDisabled();
    unmount();

    // Once corp-1 is probed with tool_calling_models: { 'corp-1': true }, ONLY Acme Unverified becomes
    // auto-selected; unprobed Acme Other on the SAME provider stays unchecked and labeled unverified.
    h.providers = [
      {
        ...h.providers[0],
        tool_calling_models: { 'corp-1': true },
      },
    ];
    renderRoute('/setup/default', <DefaultStep />);
    expect(screen.getByRole('radio', { name: /Acme Unverified/i })).toBeChecked();
    expect(screen.getByRole('radio', { name: /Acme Other/i })).not.toBeChecked();
    expect(screen.getAllByTestId('setup-model-unverified-tool-calling')).toHaveLength(1);
    expect(screen.getByRole('button', { name: /继续|Continue/i })).not.toBeDisabled();
  });

  it('does not default Background to Auto in Core when filtered background is null or deployment background lacks confirmed tool calling', async () => {
    h.daltonProfile = 'core';
    h.providers = [
      {
        provider: 'acme-gateway',
        display_name: 'Acme Gateway',
        access_type: 'api_key',
        is_custom: true,
        state: 'connected',
        masked_key: 'sk-...gw',
        tool_calling_models: { 'corp-1': true },
      },
    ];
    // Only primary model is confirmed; slotModels has no confirmed background model, while
    // systemDefaults.background_model points to a text-only deployment model.
    h.models = { 'acme-gateway': { display_name: 'Acme Gateway', models: ['acme-verified'] } };
    h.slotModels = { openai: { display_name: 'OpenAI', models: ['deploy-text-only-bg'] } };
    h.metadata = {
      'acme-verified': { provider: 'acme-gateway', model_id: 'corp-1', is_custom_model: true },
      'deploy-text-only-bg': { provider: 'openai', tool_calling: false },
    };
    h.payers = { 'acme-verified': 'own_key', 'deploy-text-only-bg': 'server_key' };
    h.customModels = [{ name: 'acme-verified', model_id: 'corp-1', provider: 'acme-gateway' }];
    h.systemDefaults = { background_model: 'deploy-text-only-bg' };

    renderRoute('/setup/default', <DefaultStep />);
    const continueBtn = screen.getByRole('button', { name: /继续|Continue/i });
    expect(continueBtn).not.toBeDisabled();
    fireEvent.click(continueBtn);

    await waitFor(() => expect(h.updatePreferences).toHaveBeenCalledTimes(1));
    const written = h.updatePreferences.mock.calls[0]?.[0] as { model_preference?: Record<string, unknown> };
    expect(written?.model_preference?.preferred_model).toBe('acme-verified');
    expect(written?.model_preference?.background_follows).toBeNull();
  });

  it('shows "跟随主模型" on Recommended card and Customize initial background in Core when recommended.background is null, matching saved background_follows: null', async () => {
    h.daltonProfile = 'core';
    h.providers = [
      {
        provider: 'acme-gateway',
        display_name: 'Acme Gateway',
        access_type: 'api_key',
        is_custom: true,
        state: 'connected',
        masked_key: 'sk-...gw',
        tool_calling_models: { 'corp-1': true },
      },
    ];
    // Primary model is confirmed while recommended.background is null (primary is not in
    // slotModels), even though systemDefaults.background_model points to a confirmed deployment model.
    h.models = { 'acme-gateway': { display_name: 'Acme Gateway', models: ['acme-verified'] } };
    h.slotModels = {
      openai: { display_name: 'OpenAI', models: ['deploy-confirmed-bg'] },
    };
    h.metadata = {
      'acme-verified': { provider: 'acme-gateway', model_id: 'corp-1', is_custom_model: true },
      'deploy-confirmed-bg': { provider: 'openai', tool_calling: true },
    };
    h.payers = { 'acme-verified': 'own_key', 'deploy-confirmed-bg': 'server_key' };
    h.customModels = [{ name: 'acme-verified', model_id: 'corp-1', provider: 'acme-gateway' }];
    h.systemDefaults = { background_model: 'deploy-confirmed-bg' };

    renderRoute('/setup/default', <DefaultStep />);

    // Recommended card shows "跟随主模型" (NOT Auto / deployment background model).
    const recBg = screen.getByTestId('setup-recommended-background');
    expect(recBg).toHaveTextContent('跟随主模型');
    expect(recBg).not.toHaveTextContent(/Deploy Confirmed Bg|自动|Auto/i);

    // Customize panel opens with the exact same initial value ("跟随主模型", value="").
    fireEvent.click(screen.getByRole('button', { name: /自定义|Customize/i }));
    const bgSelect = screen.getByLabelText(/后台模型|Background/i) as HTMLSelectElement;
    expect(bgSelect.value).toBe('');
    expect(bgSelect.selectedOptions[0]?.textContent).toBe('跟随主模型');

    // Continuing without changing selection saves background_follows: null (follows primary).
    fireEvent.click(screen.getByRole('button', { name: /继续|Continue/i }));
    await waitFor(() => expect(h.updatePreferences).toHaveBeenCalledTimes(1));
    const written = h.updatePreferences.mock.calls[0]?.[0] as { model_preference?: Record<string, unknown> };
    expect(written?.model_preference?.preferred_model).toBe('acme-verified');
    expect(written?.model_preference?.preferred_background_model).toBeNull();
    expect(written?.model_preference?.background_follows).toBeNull();
  });
});
