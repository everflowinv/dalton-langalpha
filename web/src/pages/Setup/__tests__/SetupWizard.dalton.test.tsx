// DALTON-HOOK(dalton-setup): Vitest coverage for Dalton new-user SetupWizard & admin credential access
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';

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
  useAllModels: () => ({
    models: h.teamHasAvailableModels
      ? {
          'antigravity-oauth': {
            display_name: 'Antigravity',
            models: [
              'gemini-4-neon-high-oauth',
              'gemini-4-hydrogen-high-oauth',
              'credits-only-model',
              'claude-code-sonnet',
            ],
          },
        }
      : {},
    slotModels: h.teamHasAvailableModels
      ? {
          'antigravity-oauth': {
            display_name: 'Antigravity',
            models: [
              'gemini-4-neon-high-oauth',
              'gemini-4-hydrogen-high-oauth',
              'credits-only-model',
            ],
          },
        }
      : {},
    payers: {
      'gemini-4-neon-high-oauth': 'subscription:antigravity-oauth',
      'gemini-4-hydrogen-high-oauth': 'subscription:antigravity-oauth',
      'credits-only-model': 'credits',
      'claude-code-sonnet': 'subscription:claude-code',
    },
    metadata: {
      'gemini-4-neon-high-oauth': { display_name: 'Gemini 4 Neon High', intelligence: 4, speed: 3 },
      'gemini-4-hydrogen-high-oauth': { display_name: 'Gemini 4 Hydrogen High', intelligence: 3, speed: 5 },
      'credits-only-model': { display_name: 'Credits Only Model', intelligence: 5, speed: 5 },
      'claude-code-sonnet': { display_name: 'Claude Code Sonnet', sdk: 'claude-code', provider: 'claude-code', intelligence: 5, speed: 4 },
    },
    customModels: [],
    systemDefaults: { default_model: 'gemini-4-hydrogen-high-oauth', background_model: 'credits-only-model' },
    cannotRun: () => false,
    isLoading: false,
    unread: false,
  }),
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

  it('defaultChoices & DefaultStep recommend Neon High + Hydrogen High without credits or Claude Code', async () => {
    const result = defaultChoices({
      models: 'langalpha_models',
      offered: {
        'antigravity-oauth': {
          models: [
            'gemini-4-neon-high-oauth',
            'gemini-4-hydrogen-high-oauth',
            'credits-only-model',
            'claude-code-sonnet',
          ],
        },
      },
      slotModels: ['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-high-oauth', 'credits-only-model'],
      payers: {
        'gemini-4-neon-high-oauth': 'subscription:antigravity-oauth',
        'gemini-4-hydrogen-high-oauth': 'subscription:antigravity-oauth',
        'credits-only-model': 'credits',
        'claude-code-sonnet': 'subscription:claude-code',
      },
      ownProviders: [],
      metadata: {
        'gemini-4-neon-high-oauth': { intelligence: 4, speed: 3 },
        'gemini-4-hydrogen-high-oauth': { intelligence: 3, speed: 5 },
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

    expect(result.list).toEqual(['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-high-oauth']);
    expect(result.recommended).toEqual({
      primary: 'gemini-4-neon-high-oauth',
      primaryWhy: 'daltonRecommended',
      background: 'gemini-4-hydrogen-high-oauth',
      backgroundWhy: 'daltonRecommended',
    });

    const user = userEvent.setup();
    renderWithQuery(<DefaultStep />);

    expect(screen.queryByText(/Credits/i)).toBeNull();
    expect(screen.queryByText(/Claude Code/i)).toBeNull();
    expect(screen.getByRole('radio', { name: /Gemini 4 Neon High/i })).toBeChecked();

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
  });

  it('DefaultsStep pre-selects Neon High and Hydrogen High with team note and no credits', () => {
    renderWithQuery(<DefaultsStep />);
    expect(screen.getByText(/Your team has already configured shared models/i)).toBeInTheDocument();
    expect(screen.queryByText(/credits/i)).toBeNull();
    const selects = screen.getAllByRole('combobox') as HTMLSelectElement[];
    expect(selects.map((s) => s.value)).toEqual(
      expect.arrayContaining(['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-high-oauth']),
    );
  });

  it('DoneStep displays team-configured provider label and no Mac/ClaudeCode/credits/trading links', () => {
    h.preferences = {
      preferred_model: 'gemini-4-neon-high-oauth',
      other_preference: { background_model: 'gemini-4-hydrogen-high-oauth' },
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
