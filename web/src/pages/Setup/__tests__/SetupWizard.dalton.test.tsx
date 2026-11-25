// DALTON-HOOK(dalton-setup): Vitest coverage for Dalton new-user SetupWizard & admin credential access
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import type { AntigravitySeriesEntry } from '@/api/model';
import type { ProviderModelsData } from '@/components/model/types';
import { buildSelectableAntigravitySeries, buildSeriesCatalog } from '@/lib/antigravitySeries';

const h = vi.hoisted(() => ({
  isDaltonBuild: true,
  isShared: true,
  canManage: false,
  isPm: false,
  callerTier: 'external' as 'external' | 'internal' | 'admin',
  providers: [] as { kind: string; allowed?: boolean | null }[],
  computers: [] as object[],
  progress: null as null | {
    models?: string | null;
    runs_on?: string | null;
    setup_step?: string | null;
    setup_completed_at?: string | null;
    setup_on?: string | null;
    this_mac?: string | null;
  },
  preferences: null as Record<string, unknown> | null,
  updateProgress: vi.fn(async () => ({})),
  updatePreferences: vi.fn(async () => ({})),
  finishSetup: vi.fn(async () => ({})),
  navigateToWelcome: vi.fn(),
  flowModels: 'langalpha_models' as 'langalpha_models' | 'subscription_or_key' | 'claude_code',
  flowRunsOn: 'cloud' as 'cloud' | 'own_computer',
  ownComputers: [] as { computer_id: string; name: string }[],
  apiKeys: null as Record<string, unknown> | null,
  teamHasAvailableModels: true,
}));

vi.mock('@/config/brand', () => ({
  get isDaltonBuild() {
    return h.isDaltonBuild;
  },
  DALTON_PROFILE: 'everflow' as const,
  isCoreProfileBuild: false,
  BRAND_NAME: 'Everflow',
  BRAND_MARK: 'everflow',
  APP_TAGLINE: '',
  isBetaBuild: false,
}));

vi.mock('@/config/hostMode', () => ({
  isPlatformMode: false,
  HOST_MODE: 'oss',
  APP_ENTRY_PATH: '/',
}));

vi.mock('@/config/supportLinks', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config/supportLinks')>()),
  ONBOARDING_USE_URL: null,
  DESKTOP_DOWNLOAD_URL: null,
  PLATFORM_URL: null,
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  fetchTeamCredentialsStatus: vi.fn(async () => ({
    sharing: h.isShared ? 'team' : 'off',
    enabled: h.isShared,
    holder_user_id: null,
    credential_user_id: 'u-ext-1',
    is_pm: h.isPm,
    can_disconnect: h.canManage,
    can_invite: h.canManage,
    caller_tier: h.callerTier,
    colleague_sharing_enabled: false,
    auth_mode: 'local',
  })),
  useTeamCredentials: () => ({
    isShared: h.isShared,
    canDisconnect: h.canManage,
    canManage: h.canManage,
    isPm: h.isPm,
    canInvite: h.canManage,
    callerTier: h.callerTier,
    callerTierPending: false,
    callerTierError: false,
  }),
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({
    user: { user_id: 'u-ext-1', deployment_serves: false, computer_choice: null, access_tier: 0 },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
}));

vi.mock('@/hooks/useClaudeCode', () => ({
  useClaudeCodeSetup: () => ({
    setup: { target: 'cloud', configured: false },
    isLoading: false,
    unread: false,
    retry: vi.fn(),
  }),
  useClaudeCodeStatus: () => ({ data: null, isLoading: false, isError: false, refetch: vi.fn() }),
  useClaudeCodeComputers: () => ({ data: [], isLoading: false, isError: false, refetch: vi.fn() }),
}));

vi.mock('@/pages/ChatAgent/hooks/useComputers', () => ({
  useComputerProviders: () => ({
    data: { providers: h.providers },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
  useComputers: () => ({
    data: { computers: h.computers },
    isLoading: false,
  }),
}));

vi.mock('@/pages/ChatAgent/hooks/useRemoteControl', () => ({
  useThisMacAvailability: () => ({
    offered: false,
    added: false,
    settled: true,
    serverFailed: false,
    retryServer: vi.fn(),
  }),
  serverTakesRemoteControl: () => false,
}));

vi.mock('@/pages/ChatAgent/hooks/useComputerActions', () => ({
  useComputerActions: () => ({ makePrimary: { mutate: vi.fn(), isPending: false, error: null } }),
}));

vi.mock('@/pages/ChatAgent/hooks/useHomePlacement', () => ({
  useHomePlacement: () => ({ data: undefined }),
}));

vi.mock('@/hooks/useProviderGate', () => ({
  useProviderGate: () => null,
}));

vi.mock('@/hooks/useOnboardingProgress', () => ({
  useOnboardingProgress: () => ({
    progress: h.progress,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
    update: h.updateProgress,
    isUpdating: false,
  }),
}));

vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => {
    const models: Record<string, ProviderModelsData> = h.teamHasAvailableModels
      ? {
          'antigravity-oauth': {
            display_name: 'Antigravity',
            models: [
              'gemini-4-argon-high-oauth',
              'gemini-4-carbon-medium-oauth',
              'gemini-4-neon-high-oauth',
              'gemini-4-hydrogen-low-oauth',
              'gemini-4-hydrogen-high-oauth',
              'claude-opus-5-5-medium-oauth',
              'credits-only-model',
              'claude-code-sonnet',
            ],
          },
          'claude-oauth': {
            display_name: 'Claude',
            models: ['claude-opus-5-5-oauth'],
          },
          'codex-oauth': {
            display_name: 'Codex',
            models: ['gpt-6.2-sol-oauth', 'gpt-6.2-astra-oauth'],
          },
        }
      : {};
    const slotModels = h.teamHasAvailableModels
      ? {
          'antigravity-oauth': {
            display_name: 'Antigravity',
            models: [
              'gemini-4-argon-high-oauth',
              'gemini-4-carbon-medium-oauth',
              'gemini-4-neon-high-oauth',
              'gemini-4-hydrogen-low-oauth',
              'gemini-4-hydrogen-high-oauth',
              'claude-opus-5-5-medium-oauth',
              'credits-only-model',
            ],
          },
          'claude-oauth': {
            display_name: 'Claude',
            models: ['claude-opus-5-5-oauth'],
          },
          'codex-oauth': {
            display_name: 'Codex',
            models: ['gpt-6.2-sol-oauth', 'gpt-6.2-astra-oauth'],
          },
        }
      : {};
    const metadata = {
      'gemini-4-neon-high-oauth': { provider: 'antigravity-oauth', intelligence: 5, speed: 4 },
      'gemini-4-carbon-medium-oauth': { provider: 'antigravity-oauth', intelligence: 5, speed: 3 },
      'gemini-4-hydrogen-low-oauth': { provider: 'antigravity-oauth', intelligence: 4, speed: 5 },
      'gemini-4-hydrogen-high-oauth': { provider: 'antigravity-oauth', intelligence: 4, speed: 5 },
      'gemini-4-argon-high-oauth': { provider: 'antigravity-oauth', intelligence: 5, speed: 2 },
      'claude-opus-5-5-oauth': { display_name: 'Claude Opus 5.5', provider: 'claude-oauth', intelligence: 5, speed: 3 },
      'claude-opus-5-5-medium-oauth': { provider: 'antigravity-oauth', intelligence: 5, speed: 3 },
      'gpt-6.2-astra-oauth': { display_name: 'GPT-6.2 Astra', provider: 'codex-oauth', intelligence: 5, speed: 3 },
      'gpt-6.2-sol-oauth': { display_name: 'GPT-6.2 Sol', provider: 'codex-oauth', intelligence: 5, speed: 3 },
      'credits-only-model': { display_name: 'Credits Only Model', provider: 'platform', intelligence: 5, speed: 5 },
      'claude-code-sonnet': { display_name: 'Claude Code Sonnet', sdk: 'claude-code', provider: 'claude-code', intelligence: 5, speed: 4 },
    };
    const seriesList: AntigravitySeriesEntry[] = [
      {
        series_id: 'antigravity-oauth:gemini-4-neon',
        series_prefix: 'gemini-4-neon',
        display_name: 'Gemini 4 Neon',
        qualified_display_name: 'Gemini 4 Neon (Antigravity)',
        provider: 'antigravity-oauth',
        default_effort: 'high',
        default_model: 'gemini-4-neon-high-oauth',
        efforts: ['high'],
        variants: { high: 'gemini-4-neon-high-oauth' },
      },
      {
        series_id: 'antigravity-oauth:gemini-4-carbon',
        series_prefix: 'gemini-4-carbon',
        display_name: 'Gemini 4 Carbon',
        qualified_display_name: 'Gemini 4 Carbon (Antigravity)',
        provider: 'antigravity-oauth',
        default_effort: 'medium',
        default_model: 'gemini-4-carbon-medium-oauth',
        efforts: ['medium'],
        variants: { medium: 'gemini-4-carbon-medium-oauth' },
      },
      {
        series_id: 'antigravity-oauth:gemini-4-hydrogen',
        series_prefix: 'gemini-4-hydrogen',
        display_name: 'Gemini 4 Hydrogen',
        qualified_display_name: 'Gemini 4 Hydrogen (Antigravity)',
        provider: 'antigravity-oauth',
        default_effort: 'high',
        default_model: 'gemini-4-hydrogen-high-oauth',
        efforts: ['low', 'high'],
        variants: {
          low: 'gemini-4-hydrogen-low-oauth',
          high: 'gemini-4-hydrogen-high-oauth',
        },
      },
      {
        series_id: 'antigravity-oauth:gemini-4-argon',
        series_prefix: 'gemini-4-argon',
        display_name: 'Gemini 4 Argon',
        qualified_display_name: 'Gemini 4 Argon (Antigravity)',
        provider: 'antigravity-oauth',
        default_effort: 'high',
        default_model: 'gemini-4-argon-high-oauth',
        efforts: ['high'],
        variants: { high: 'gemini-4-argon-high-oauth' },
      },
      {
        series_id: 'antigravity-oauth:claude-opus-5-5',
        series_prefix: 'claude-opus-5-5',
        display_name: 'Claude Opus 5.5',
        qualified_display_name: 'Claude Opus 5.5 (Antigravity)',
        provider: 'antigravity-oauth',
        default_effort: 'medium',
        default_model: 'claude-opus-5-5-medium-oauth',
        efforts: ['medium'],
        variants: { medium: 'claude-opus-5-5-medium-oauth' },
      },
    ];
    const seriesCatalog = buildSeriesCatalog(seriesList);
    const selectableSeries = buildSelectableAntigravitySeries(seriesCatalog, models, {
      rawMetadata: metadata,
      customModels: [],
    });
    return {
      models,
      slotModels,
      payers: {
        'gemini-4-neon-high-oauth': 'subscription:antigravity-oauth',
        'gemini-4-carbon-medium-oauth': 'subscription:antigravity-oauth',
        'gemini-4-hydrogen-low-oauth': 'subscription:antigravity-oauth',
        'gemini-4-hydrogen-high-oauth': 'subscription:antigravity-oauth',
        'gemini-4-argon-high-oauth': 'subscription:antigravity-oauth',
        'claude-opus-5-5-oauth': 'subscription:claude-oauth',
        'claude-opus-5-5-medium-oauth': 'subscription:antigravity-oauth',
        'gpt-6.2-astra-oauth': 'subscription:codex-oauth',
        'gpt-6.2-sol-oauth': 'subscription:codex-oauth',
        'credits-only-model': 'credits',
        'claude-code-sonnet': 'subscription:claude-code',
      },
      metadata,
      customModels: [],
      seriesCatalog,
      selectableSeries,
      rawApiResponse: { model_metadata: metadata, antigravity_series: seriesList },
      systemDefaults: { default_model: 'gemini-4-hydrogen-low-oauth', background_model: 'credits-only-model' },
      cannotRun: () => false,
      isLoading: false,
      unread: false,
    };
  },
}));

vi.mock('@/hooks/useProviderCatalog', () => ({
  useProviderCatalog: () => ({
    connectionOf: (p: string) => p,
    keyGroups: [],
    planGroups: [],
    localEntries: [],
    isLoading: false,
  }),
}));

vi.mock('@/hooks/useConfiguredProviders', () => ({
  SERVER_KEY: 'server_key',
  useConfiguredProviders: () => ({
    providers: [
      { provider: 'antigravity-oauth', access_type: 'oauth', state: 'connected', masked_key: null },
    ],
    byokEnabled: false,
    hasAny: true,
  }),
  useOAuthStatus: () => ({ data: null }),
}));

vi.mock('@/hooks/useAccount', () => ({
  useAccount: () => ({ policy: null, setterName: () => ({ start: '', mid: '' }) }),
  useRule: () => ({ allowed: true, who: { start: '', mid: '' } }),
}));

vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'light' }),
}));

vi.mock('@/hooks/useOnboardingRoute', () => ({
  useOnboardingRoute: () => ({ kind: 'questions', unpaired: false }),
}));

vi.mock('@/hooks/useSetupGate', () => ({
  skipSetup: vi.fn(),
}));

vi.mock('@/pages/Onboarding/welcome/Welcome', () => ({
  default: () => null,
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: h.preferences,
    isLoaded: true,
    isLoading: false,
    unread: false,
    retry: vi.fn(),
  }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => ({
    mutateAsync: h.updatePreferences,
    isPending: false,
  }),
}));

vi.mock('@/hooks/useApiKeys', () => ({
  useApiKeys: () => ({ apiKeys: h.apiKeys }),
}));

vi.mock('@/hooks/useModelName', () => ({
  useModelName: () => (id: string) => id,
}));

vi.mock('@/pages/Onboarding/welcome/useWelcomeOffer', () => ({
  useWelcomeOffer: () => null,
}));

vi.mock('../answers/flow', () => ({
  useAnswerFlow: () => ({
    answers: { models: h.flowModels, runsOn: h.flowRunsOn },
    first: true,
    ownComputers: h.ownComputers,
    facts: {
      answers: { models: h.flowModels, runsOn: h.flowRunsOn },
      ownComputer: h.ownComputers.length > 0,
      homeOnCloud: true,
      claudeCodeToken: false,
      providerConnected: true,
      lastStep: null,
    },
    progress: h.progress,
    isLoading: false,
    tokenTarget: { kind: 'cloud' },
    finish: h.finishSetup,
    finishing: false,
    finishFailed: false,
    back: null,
    complete: h.finishSetup,
    toWelcome: h.navigateToWelcome,
    retryFacts: vi.fn(),
  }),
  useChangeAnswers: () => vi.fn(),
}));

import UseStep from '../steps/UseStep';
import ComputerStep from '../steps/ComputerStep';
import ModelStep from '../steps/ModelStep';
import DefaultStep from '../steps/DefaultStep';
import DefaultsStep from '../steps/DefaultsStep';
import DoneStep from '../steps/DoneStep';
import SetupWizard from '../SetupWizard';
import { defaultChoices } from '../answers/defaultChoices';
import { onboardingRoute, shellTarget } from '../answers/gate';
import { effectiveModelsAnswer } from '../answers/setupSteps';
import { sectionShown } from '@/pages/Settings/sections';

function LocationEcho() {
  const { pathname } = useLocation();
  return <div data-testid="location">{pathname}</div>;
}

function renderWithQuery(ui: React.ReactElement, initialPath = '/setup/use') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialPath]}>
        {ui}
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  h.isDaltonBuild = true;
  h.isShared = true;
  h.canManage = false;
  h.isPm = false;
  h.callerTier = 'external';
  h.providers = [];
  h.computers = [];
  h.progress = null;
  h.preferences = {};
  h.flowModels = 'langalpha_models';
  h.flowRunsOn = 'cloud';
  h.ownComputers = [];
  h.apiKeys = null;
  h.teamHasAvailableModels = true;
  vi.clearAllMocks();
});

describe('Dalton SetupWizard external & internal member flows', () => {
  it('UseStep hides Claude Code & account connection, shows team note, and proceeds on cloud', async () => {
    const user = userEvent.setup();
    renderWithQuery(<UseStep />);

    expect(screen.getByTestId('setup-use-team-models')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /^Set up / })).toBeInTheDocument();
    expect(screen.queryByText(/How will you use/i)).toBeNull();
    expect(screen.queryByText(/Two questions/i)).toBeNull();
    expect(screen.queryByText(/Which models do you want/i)).toBeNull();
    expect(screen.queryByText(/Claude Code/i)).toBeNull();
    expect(screen.queryByText(/Another subscription or an API key/i)).toBeNull();

    const continueBtn = screen.getByRole('button', { name: 'Continue' });
    expect(continueBtn).toBeEnabled();
    await user.click(continueBtn);

    expect(h.updateProgress).toHaveBeenCalledWith(
      expect.objectContaining({
        models: 'langalpha_models',
        runs_on: 'cloud',
      }),
    );
  });

  it('UseStep also applies team-shared simplified flow to internal non-admin members', async () => {
    h.callerTier = 'internal';
    const user = userEvent.setup();
    renderWithQuery(<UseStep />);

    expect(screen.getByTestId('setup-use-team-models')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /^Set up / })).toBeInTheDocument();
    expect(screen.queryByText(/How will you use/i)).toBeNull();
    expect(screen.queryByText(/Two questions/i)).toBeNull();
    expect(screen.queryByText(/Which models do you want/i)).toBeNull();
    expect(screen.queryByText(/Claude Code/i)).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(h.updateProgress).toHaveBeenCalledWith(
      expect.objectContaining({
        models: 'langalpha_models',
        runs_on: 'cloud',
      }),
    );
  });

  it('UseStep defaults to cloud and keeps SSH under Advanced when SSH is offered', async () => {
    h.providers = [{ kind: 'ssh', allowed: true }];
    const user = userEvent.setup();
    renderWithQuery(<UseStep />);

    expect(screen.queryByText(/On your own computer/i)).toBeNull();
    const advBtn = screen.getByRole('button', { name: 'Advanced' });
    expect(advBtn).toHaveAttribute('aria-expanded', 'false');
    await user.click(advBtn);
    expect(screen.getByText(/On your own computer/i)).toBeInTheDocument();
  });

  it('never routes to Mac pairing or handoff in shellTarget and ComputerStep hides Mac UI', async () => {
    expect(
      shellTarget(
        { kind: 'answers', next: 'default', owner: 'mac_pending', unpaired: true, nextPath: '/setup/default' },
        { desktop: true, skipped: false, canPair: true },
      ),
    ).toEqual({ kind: 'path', to: '/setup/default' });

    const user = userEvent.setup();
    renderWithQuery(
      <Routes>
        <Route path="/setup/computer" element={<ComputerStep />} />
        <Route path="/setup" element={<LocationEcho />} />
      </Routes>,
      '/setup/computer',
    );

    expect(screen.queryByTestId('setup-computer-mac')).toBeNull();
    expect(screen.getByTestId('setup-computer-cloud')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    expect(h.updateProgress).toHaveBeenCalledWith({ runs_on: 'cloud' });
    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent('/setup');
    });
  });

  it('ModelStep skips straight to /setup/default when team credentials are shared', async () => {
    h.flowModels = 'subscription_or_key';
    renderWithQuery(
      <Routes>
        <Route path="/setup/model" element={<ModelStep />} />
        <Route path="/setup/default" element={<LocationEcho />} />
      </Routes>,
      '/setup/model',
    );
    expect(screen.getByTestId('location')).toHaveTextContent('/setup/default');
  });

  it('SetupWizard redirects Mac and Claude Code routes away in Dalton build', async () => {
    for (const path of ['/setup/mac', '/setup/this-mac', '/setup/continue', '/setup/claude-code']) {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const { unmount } = render(
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/setup/*" element={<SetupWizard />} />
            </Routes>
            <LocationEcho />
          </MemoryRouter>
        </QueryClientProvider>,
      );
      await waitFor(() => {
        expect(screen.getByTestId('location').textContent).not.toBe(path);
      });
      unmount();
    }
  });

  it('defaultChoices & DefaultStep show curated 5 with ratings, omit effort in background label, and save neon-high + hydrogen-low', async () => {
    const result = defaultChoices({
      models: 'langalpha_models',
      offered: {
        'antigravity-oauth': {
          models: [
            'gemini-4-argon-high-oauth',
            'gemini-4-carbon-medium-oauth',
            'gemini-4-neon-high-oauth',
            'gemini-4-hydrogen-low-oauth',
            'credits-only-model',
            'claude-code-sonnet',
          ],
        },
        'claude-oauth': { models: ['claude-opus-5-5-oauth'] },
        'codex-oauth': { models: ['gpt-6.2-sol-oauth', 'gpt-6.2-astra-oauth'] },
      },
      slotModels: [
        'gemini-4-neon-high-oauth',
        'gemini-4-carbon-medium-oauth',
        'gemini-4-hydrogen-low-oauth',
        'claude-opus-5-5-oauth',
        'gpt-6.2-astra-oauth',
        'gpt-6.2-sol-oauth',
        'credits-only-model',
      ],
      payers: {
        'gemini-4-neon-high-oauth': 'subscription:antigravity-oauth',
        'gemini-4-carbon-medium-oauth': 'subscription:antigravity-oauth',
        'gemini-4-hydrogen-low-oauth': 'subscription:antigravity-oauth',
        'claude-opus-5-5-oauth': 'subscription:claude-oauth',
        'gpt-6.2-astra-oauth': 'subscription:codex-oauth',
        'gpt-6.2-sol-oauth': 'subscription:codex-oauth',
        'credits-only-model': 'credits',
        'claude-code-sonnet': 'subscription:claude-code',
      },
      ownProviders: [],
      metadata: {
        'gemini-4-neon-high-oauth': { intelligence: 5, speed: 4 },
        'gemini-4-carbon-medium-oauth': { intelligence: 5, speed: 3 },
        'gemini-4-hydrogen-low-oauth': { intelligence: 4, speed: 5 },
        'claude-opus-5-5-oauth': { intelligence: 5, speed: 3 },
        'gpt-6.2-astra-oauth': { intelligence: 5, speed: 3 },
        'gpt-6.2-sol-oauth': { intelligence: 5, speed: 3 },
        'credits-only-model': { intelligence: 5, speed: 5 },
        'claude-code-sonnet': { sdk: 'claude-code', provider: 'claude-code', intelligence: 5, speed: 4 },
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
    expect(result.recommended).toEqual({
      primary: 'gemini-4-neon-high-oauth',
      primaryWhy: 'daltonRecommended',
      background: 'gemini-4-hydrogen-low-oauth',
      backgroundWhy: 'daltonRecommended',
    });

    const user = userEvent.setup();
    renderWithQuery(<DefaultStep />);

    expect(screen.queryByText(/Credits/i)).toBeNull();
    expect(screen.queryByText(/Claude Code/i)).toBeNull();
    const radios = screen.getAllByRole('radio') as HTMLInputElement[];
    expect(radios.map((r) => r.value)).toEqual([
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
      'claude-opus-5-5-oauth',
      'gpt-6.2-astra-oauth',
      'gpt-6.2-sol-oauth',
    ]);
    expect(radios[0]).toBeChecked();
    // Neon (5/4) and Carbon (5/3) both render Intelligence & Speed ratings
    expect(screen.getAllByRole('img', { name: 'Intelligence: 5 of 5' }).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole('img', { name: 'Speed: 4 of 5' })).toBeInTheDocument();
    expect(screen.getAllByRole('img', { name: 'Speed: 3 of 5' }).length).toBeGreaterThanOrEqual(1);

    // Customize: background dropdown selects hydrogen-low without "Low" in option text;
    // routing dropdowns list only the curated 5 in order
    await user.click(screen.getByRole('button', { name: 'Customize' }));
    const bgSelect = screen.getByLabelText(/Background model/i) as HTMLSelectElement;
    expect(bgSelect.value).toBe('gemini-4-hydrogen-low-oauth');
    const bgOption = [...bgSelect.options].find((o) => o.value === 'gemini-4-hydrogen-low-oauth');
    expect(bgOption?.textContent).toContain('Gemini 4 Hydrogen');
    expect(bgOption?.textContent).not.toMatch(/\bLow\b/i);
    const fetchSelect = screen.getByLabelText('Web fetch model') as HTMLSelectElement;
    expect([...fetchSelect.options].map((o) => o.value).filter(Boolean)).toEqual([
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
      'claude-opus-5-5-oauth',
      'gpt-6.2-astra-oauth',
      'gpt-6.2-sol-oauth',
    ]);

    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => {
      expect(h.updatePreferences).toHaveBeenCalledWith(
        expect.objectContaining({
          model_preference: expect.objectContaining({
            preferred_model: 'gemini-4-neon-high-oauth',
            preferred_background_model: 'gemini-4-hydrogen-low-oauth',
          }),
        }),
      );
    });
  });

  it('DefaultsStep pre-selects Neon High and Hydrogen Low with team note and no credits', () => {
    renderWithQuery(<DefaultsStep />);
    expect(screen.getByText(/Your team has already configured shared models/i)).toBeInTheDocument();
    expect(screen.queryByText(/credits/i)).toBeNull();
    const selects = screen.getAllByRole('combobox') as HTMLSelectElement[];
    expect(selects.map((s) => s.value)).toEqual(
      expect.arrayContaining(['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-low-oauth']),
    );
  });

  it('DefaultsStep keeps a saved Hydrogen High background consistent with the deduped dropdown and saves fixed-low when re-picked', async () => {
    const user = userEvent.setup();
    h.preferences = {
      model_preference: {
        preferred_model: 'gemini-4-neon-high-oauth',
        preferred_background_model: 'gemini-4-hydrogen-high-oauth',
      },
    };
    renderWithQuery(<DefaultsStep />);

    const bgSelect = screen.getByLabelText(/Background model/i) as HTMLSelectElement;
    // Dropdown is NOT blank, collapses Hydrogen to 1 option without effort suffix, and matches saved value
    expect(bgSelect.value).toBe('gemini-4-hydrogen-high-oauth');
    expect(bgSelect.selectedOptions[0]?.textContent).toContain('Gemini 4 Hydrogen');
    expect(bgSelect.selectedOptions[0]?.textContent).not.toMatch(/\bHigh\b|\bLow\b/i);
    const hydrogenOptions = [...bgSelect.options].filter((o) => o.textContent?.includes('Gemini 4 Hydrogen'));
    expect(hydrogenOptions).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => {
      expect(h.updatePreferences).toHaveBeenCalledWith(
        expect.objectContaining({
          model_preference: expect.objectContaining({
            preferred_model: 'gemini-4-neon-high-oauth',
            preferred_background_model: 'gemini-4-hydrogen-high-oauth',
          }),
        }),
      );
    });

    // Switching away and re-selecting Hydrogen picks the fixed-low variant
    h.updatePreferences.mockClear();
    await user.selectOptions(bgSelect, 'gemini-4-neon-high-oauth');
    const repickHydrogen = [...bgSelect.options].find((o) => o.textContent?.includes('Gemini 4 Hydrogen'));
    expect(repickHydrogen?.value).toBe('gemini-4-hydrogen-low-oauth');
    await user.selectOptions(bgSelect, 'gemini-4-hydrogen-low-oauth');
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => {
      expect(h.updatePreferences).toHaveBeenCalledWith(
        expect.objectContaining({
          model_preference: expect.objectContaining({
            preferred_background_model: 'gemini-4-hydrogen-low-oauth',
          }),
        }),
      );
    });
  });

  it('DefaultsStep preserves untouched uncurated advanced models on Continue and only writes fields changed on this page', async () => {
    const user = userEvent.setup();
    h.preferences = {
      model_preference: {
        preferred_model: 'gemini-4-neon-high-oauth',
        preferred_background_model: 'gemini-4-hydrogen-low-oauth',
        fetch_model: 'gemini-4-argon-high-oauth',
        compaction_model: 'gemini-4-hydrogen-low-oauth',
        fallback_models: ['gemini-4-argon-high-oauth', 'gemini-4-hydrogen-low-oauth'],
      },
    };
    renderWithQuery(<DefaultsStep />);

    // 1) Never opening Advanced: Continue writes only primary + background, preserving all saved advanced fields
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => {
      expect(h.updatePreferences).toHaveBeenCalledWith({
        model_preference: {
          preferred_model: 'gemini-4-neon-high-oauth',
          preferred_background_model: 'gemini-4-hydrogen-low-oauth',
        },
      });
    });

    // 2) Opening Advanced: new optgroup options stay restricted to the curated 5, while saved values remain selected
    h.updatePreferences.mockClear();
    await user.click(screen.getByRole('button', { name: 'Advanced' }));
    const fetchSelect = screen.getByLabelText('Web fetch model') as HTMLSelectElement;
    const compactionSelect = screen.getByLabelText('Compaction model') as HTMLSelectElement;
    expect(fetchSelect.value).toBe('gemini-4-argon-high-oauth');
    expect(compactionSelect.value).toBe('gemini-4-hydrogen-low-oauth');
    const fetchGroupOptions = [...fetchSelect.querySelectorAll('optgroup option')].map(
      (o) => (o as HTMLOptionElement).value,
    );
    expect(fetchGroupOptions).toEqual([
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
      'claude-opus-5-5-oauth',
      'gpt-6.2-astra-oauth',
      'gpt-6.2-sol-oauth',
    ]);

    // Edit only fetch_model and add one curated fallback; leave compaction_model untouched
    await user.selectOptions(fetchSelect, 'gpt-6.2-sol-oauth');
    await user.click(screen.getByRole('button', { name: /\+ Add/i }));
    await user.click(screen.getByRole('button', { name: /GPT-6\.2 Astra/i }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => {
      expect(h.updatePreferences).toHaveBeenCalledWith({
        model_preference: {
          preferred_model: 'gemini-4-neon-high-oauth',
          preferred_background_model: 'gemini-4-hydrogen-low-oauth',
          fetch_model: 'gpt-6.2-sol-oauth',
          fallback_models: [
            'gemini-4-argon-high-oauth',
            'gemini-4-hydrogen-low-oauth',
            'gpt-6.2-astra-oauth',
          ],
        },
      });
    });

    // 3) Explicitly clearing fetch_model to placeholder writes fetch_model: null while compaction_model stays untouched
    h.updatePreferences.mockClear();
    await user.selectOptions(fetchSelect, '');
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => {
      expect(h.updatePreferences).toHaveBeenCalledWith({
        model_preference: {
          preferred_model: 'gemini-4-neon-high-oauth',
          preferred_background_model: 'gemini-4-hydrogen-low-oauth',
          fetch_model: null,
          fallback_models: [
            'gemini-4-argon-high-oauth',
            'gemini-4-hydrogen-low-oauth',
            'gpt-6.2-astra-oauth',
          ],
        },
      });
    });
  });

  it('DoneStep displays team-configured provider label and no Mac/ClaudeCode/credits/trading links', () => {
    h.preferences = {
      preferred_model: 'gemini-4-neon-high-oauth',
      other_preference: { background_model: 'gemini-4-hydrogen-low-oauth' },
    };
    h.apiKeys = { providers: [{ provider: 'openai', display_name: 'OpenAI', has_key: true }] };
    renderWithQuery(<DoneStep />);

    expect(screen.getByText('Team-configured models')).toBeInTheDocument();
    expect(screen.queryByText('OpenAI')).toBeNull();
    expect(screen.queryByText(/Mac/i)).toBeNull();
    expect(screen.queryByText(/Claude Code/i)).toBeNull();
    expect(screen.queryByText(/credits/i)).toBeNull();
    expect(screen.queryByRole('button', { name: /investing profile|brokerage|trading/i })).toBeNull();
  });

  it('DoneStep displays Not configured when team sharing is active but zero Dalton models are available', () => {
    h.apiKeys = { providers: [{ provider: 'openai', display_name: 'OpenAI', has_key: true }] };
    h.teamHasAvailableModels = false;
    renderWithQuery(<DoneStep />);

    expect(screen.getByText('Not configured')).toBeInTheDocument();
    expect(screen.queryByText('Team-configured models')).toBeNull();
  });

  it('preserves admin access to credential management in Settings while blocking non-admins', () => {
    expect(sectionShown('teamModels', { callerTier: 'admin' })).toBe(true);
    expect(sectionShown('teamModels', { callerTier: 'internal' })).toBe(false);
    expect(sectionShown('teamModels', { callerTier: 'external' })).toBe(false);
  });

  it('normalizes legacy claude_code answers and setup_on=app in Dalton while preserving upstream behavior when isDaltonBuild is false', () => {
    expect(effectiveModelsAnswer('claude_code', true)).toBe('langalpha_models');
    expect(effectiveModelsAnswer('claude_code', false)).toBe('subscription_or_key');
    expect(
      onboardingRoute({
        platform: false,
        onboarded: true,
        hasAnswers: true,
        setupCompleted: false,
        setupOn: 'app',
        next: 'default',
        needsProvider: false,
        unpaired: false,
      }),
    ).toMatchObject({ kind: 'answers', owner: 'browser', nextPath: '/setup/default' });

    h.isDaltonBuild = false;
    expect(effectiveModelsAnswer('claude_code', false)).toBe('claude_code');
    expect(
      onboardingRoute({
        platform: false,
        onboarded: true,
        hasAnswers: true,
        setupCompleted: false,
        setupOn: 'app',
        next: 'default',
        needsProvider: false,
        unpaired: false,
      }),
    ).toMatchObject({ kind: 'answers', owner: 'app', nextPath: '/setup/default' });
  });

  it('leaves upstream defaultChoices & shellTarget Mac pairing behavior unchanged when isDaltonBuild is false', () => {
    h.isDaltonBuild = false;
    expect(
      shellTarget(
        { kind: 'answers', next: 'default', owner: 'mac_pending', unpaired: true, nextPath: '/setup/default' },
        { desktop: true, skipped: false, canPair: true },
      ),
    ).toEqual({ kind: 'path', to: '/setup/this-mac' });

    const upstream = defaultChoices({
      models: 'langalpha_models',
      offered: { openai: { models: ['gpt-5', 'gpt-4o-mini'] } },
      slotModels: ['gpt-5', 'gpt-4o-mini'],
      payers: { 'gpt-5': 'credits', 'gpt-4o-mini': 'credits' },
      ownProviders: [],
      metadata: { 'gpt-5': { intelligence: 5, speed: 3 }, 'gpt-4o-mini': { intelligence: 3, speed: 5 } },
      customModels: [],
      connectionOf: (p) => p,
      systemDefault: 'gpt-5',
      systemBackground: 'gpt-4o-mini',
      preferred: null,
    });
    expect(upstream.recommended).toEqual({
      primary: 'gpt-5',
      primaryWhy: 'deployment',
      background: 'gpt-4o-mini',
      backgroundWhy: 'deployment',
    });
  });
});
