// DALTON-HOOK(dalton-setup): real-router & real-progress first-run integration tests for Dalton SetupWizard
import type { ComponentType } from 'react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { OnboardingProgress, OnboardingProgressPatch } from '@/api/onboarding';
import type { ApiResponse, api as ApiClient } from '@/api/client';

const ok = <T,>(data: T): ApiResponse<T> => ({ data, status: 200, statusText: 'OK', headers: {} });

interface ServerState {
  teamSharingEnabled: boolean;
  teamModelsConfigured: boolean;
  callerTier: 'external' | 'internal' | 'admin';
  progress: OnboardingProgress;
  preferences: Record<string, unknown>;
  systemDefaults: { default_model?: string; background_model?: string };
  userApiKeys: Array<{ provider: string; display_name?: string; has_key: boolean; access_type?: string }>;
}

const makeInitialState = (): ServerState => ({
  teamSharingEnabled: true,
  teamModelsConfigured: true,
  callerTier: 'external',
  progress: {
    models: null,
    runs_on: null,
    plan_step_done_at: null,
    account_setup_completed_at: null,
    this_mac: null,
    setup_on: null,
    setup_step: null,
    setup_completed_at: null,
    created_at: '2026-11-21T00:00:00Z',
    updated_at: '2026-11-21T00:00:00Z',
  },
  preferences: {},
  systemDefaults: {
    default_model: 'credits-only-model',
    background_model: 'credits-only-model',
  },
  userApiKeys: [],
});

const server = vi.hoisted(() => ({
  state: null as unknown as ServerState,
  lastPreferencesPut: null as Record<string, unknown> | null,
  progressPatches: [] as OnboardingProgressPatch[],
}));

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  const get: typeof ApiClient.get = async <T,>(url: string): Promise<ApiResponse<T>> => {
    const s = server.state;
    if (url === '/api/v1/users/me') {
      return ok({
        user_id: 'u-ext-1',
        email: 'ext@example.com',
        deployment_serves: false,
        computer_choice: null,
        access_tier: 0,
        onboarding_completed: false,
      } as T);
    }
    if (url === '/api/v1/users/me/onboarding') {
      return ok({ ...s.progress } as T);
    }
    if (url === '/api/v1/users/me/preferences') {
      return ok({ ...s.preferences } as T);
    }
    if (url === '/api/v1/dalton/credentials/status') {
      return ok({
        sharing: s.teamSharingEnabled ? 'team' : 'off',
        enabled: s.teamSharingEnabled,
        holder_user_id: 'u-admin-1',
        credential_user_id: s.teamSharingEnabled ? 'u-admin-1' : 'u-ext-1',
        is_pm: s.callerTier === 'admin',
        can_disconnect: s.callerTier === 'admin',
        can_invite: s.callerTier !== 'external',
        caller_tier: s.callerTier,
      } as T);
    }
    if (url === '/api/v1/models') {
      return ok({
        models: {
          'antigravity-oauth': {
            display_name: 'Antigravity',
            models: ['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-high-oauth'],
          },
          platform: {
            display_name: 'Platform',
            models: ['credits-only-model'],
          },
          'claude-code': {
            display_name: 'Claude Code',
            models: ['claude-code-sonnet'],
          },
        },
        model_metadata: {
          'gemini-4-neon-high-oauth': {
            display_name: 'Gemini 4 Neon High',
            provider: 'antigravity-oauth',
            access_type: 'oauth',
            intelligence: 4,
            speed: 3,
          },
          'gemini-4-hydrogen-high-oauth': {
            display_name: 'Gemini 4 Hydrogen High',
            provider: 'antigravity-oauth',
            access_type: 'oauth',
            intelligence: 3,
            speed: 5,
          },
          'credits-only-model': {
            display_name: 'Credits Only Model',
            provider: 'platform',
            tier: 0,
            intelligence: 5,
            speed: 5,
          },
          'claude-code-sonnet': {
            display_name: 'Claude Code Sonnet',
            sdk: 'claude-code',
            provider: 'claude-code',
            intelligence: 5,
            speed: 4,
          },
        },
        system_defaults: s.systemDefaults,
        compaction_profiles: {},
        search_providers: {},
        provider_catalog: [
          { provider: 'antigravity-oauth', display_name: 'Antigravity', access_type: 'oauth' },
          { provider: 'openai', display_name: 'OpenAI', access_type: 'api_key' },
        ],
        key_candidates: {},
      } as T);
    }
    if (url === '/api/v1/users/me/api-keys') {
      return ok({ providers: s.userApiKeys, byok_enabled: false } as T);
    }
    if (url.startsWith('/api/v1/oauth/') && url.endsWith('/status')) {
      const connected = url === '/api/v1/oauth/antigravity/status' && s.teamSharingEnabled && s.teamModelsConfigured;
      return ok({
        connected,
        state: connected ? 'connected' : 'disconnected',
        account_id: connected ? 'acc-1' : null,
        email: connected ? 'team@example.com' : null,
        plan_type: null,
        expires_at: null,
        needs_reauth_at: null,
        last_error_code: null,
        can_sign_in: true,
        ends_on: null,
        lifecycle: 'active',
      } as T);
    }
    if (url.startsWith('/api/v1/providers/') && url.endsWith('/available-models')) {
      const provider = decodeURIComponent(url.split('/')[4] ?? '');
      return ok({
        provider,
        status: 'ok',
        ready: provider === 'antigravity-oauth' ? ['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-high-oauth'] : [],
        missing: [],
        others: provider === 'openai'
          ? [
              { id: 'credits-only-model', display_name: 'Credits Only Model', described: true },
              { id: 'claude-code-sonnet', display_name: 'Claude Code Sonnet', described: true },
              { id: 'gpt-5', display_name: 'GPT 5', described: true },
            ]
          : [],
      } as T);
    }
    if (url === '/api/v1/features') {
      return ok({ features: [{ key: 'claude_code_runtime', enabled: false }] } as T);
    }
    if (url === '/api/v1/claude-code/status') {
      return ok({ enabled: false, state: 'off', email: null, expires_at: null } as T);
    }
    if (url === '/api/v1/claude-code/setup') {
      return ok(null as T);
    }
    if (url === '/api/v1/computers/providers') {
      return ok({ providers: [{ kind: 'docker', allowed: true }] } as T);
    }
    if (url === '/api/v1/computers') {
      return ok({ computers: [] } as T);
    }
    if (url === '/api/v1/workspaces/home') {
      return ok({ workspace_id: 'ws-home', name: 'Home', status: 'running' } as T);
    }
    if (url === '/api/v1/workspaces') {
      return ok({ workspaces: [{ workspace_id: 'ws-home', name: 'Home', status: 'running' }] } as T);
    }
    if (url === '/api/v1/threads') {
      return ok({ threads: [] } as T);
    }
    if (url === '/api/v1/users/me/account') {
      return ok({ policy: null } as T);
    }
    return ok({} as T);
  };

  const patch: typeof ApiClient.patch = async <T,>(url: string, body?: unknown): Promise<ApiResponse<T>> => {
    if (url === '/api/v1/users/me/onboarding') {
      const p = (body ?? {}) as OnboardingProgressPatch;
      server.progressPatches.push(p);
      const cur = server.state.progress;
      server.state.progress = {
        ...cur,
        ...(p.models !== undefined ? { models: p.models } : {}),
        ...(p.runs_on !== undefined ? { runs_on: p.runs_on } : {}),
        ...(p.this_mac !== undefined ? { this_mac: p.this_mac } : {}),
        ...(p.setup_on !== undefined ? { setup_on: p.setup_on } : {}),
        ...(p.setup_step !== undefined ? { setup_step: p.setup_step } : {}),
        ...(p.setup_completed === true ? { setup_completed_at: '2026-11-21T01:00:00Z' } : {}),
        ...(p.setup_completed === false || p.setup_completed === null ? { setup_completed_at: null } : {}),
      };
      return ok({ ...server.state.progress } as T);
    }
    return ok({} as T);
  };

  const put: typeof ApiClient.put = async <T,>(url: string, body?: unknown): Promise<ApiResponse<T>> => {
    if (url === '/api/v1/users/me/preferences') {
      const payload = (body ?? {}) as Record<string, unknown>;
      server.lastPreferencesPut = payload;
      server.state.preferences = { ...server.state.preferences, ...payload };
      return ok({ ...server.state.preferences } as T);
    }
    return ok({} as T);
  };

  const post: typeof ApiClient.post = async <T,>(_url: string): Promise<ApiResponse<T>> => ok({} as T);
  const del: typeof ApiClient.delete = async <T,>(_url: string): Promise<ApiResponse<T>> => ok({} as T);

  return {
    ...actual,
    api: {
      ...actual.api,
      get,
      patch,
      put,
      post,
      delete: del,
    },
  };
});

let Loaded: {
  QueryClient: typeof import('@tanstack/react-query').QueryClient;
  QueryClientProvider: typeof import('@tanstack/react-query').QueryClientProvider;
  MemoryRouter: typeof import('react-router').MemoryRouter;
  Navigate: typeof import('react-router').Navigate;
  Route: typeof import('react-router').Route;
  Routes: typeof import('react-router').Routes;
  useLocation: typeof import('react-router').useLocation;
  AuthContext: typeof import('@/contexts/AuthContext').AuthContext;
  _localDevValue: typeof import('@/contexts/AuthContext')._localDevValue;
  ThemeProvider: typeof import('@/contexts/ThemeContext').ThemeProvider;
  useOnboardingRoute: typeof import('@/hooks/useOnboardingRoute').useOnboardingRoute;
  shellTarget: typeof import('../answers/gate').shellTarget;
  SetupWizard: ComponentType;
};

beforeAll(async () => {
  vi.stubEnv('VITE_BRAND_NAME', 'Everflow');
  vi.stubEnv('VITE_BRAND_MARK', 'everflow');
  vi.stubEnv('VITE_HOST_MODE', 'oss');
  vi.stubEnv('VITE_DALTON_AUTH', 'supabase');
  vi.stubEnv('VITE_SUPABASE_URL', 'https://example.supabase.co');
  vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', 'sb_publishable_test');
  vi.resetModules();

  const [
    { QueryClient, QueryClientProvider },
    { MemoryRouter, Navigate, Route, Routes, useLocation },
    { AuthContext, _localDevValue },
    { ThemeProvider },
    { useOnboardingRoute },
    { shellTarget },
    { default: SetupWizard },
  ] = await Promise.all([
    import('@tanstack/react-query'),
    import('react-router'),
    import('@/contexts/AuthContext'),
    import('@/contexts/ThemeContext'),
    import('@/hooks/useOnboardingRoute'),
    import('../answers/gate'),
    import('../SetupWizard'),
    import('../steps/UseStep'),
    import('../steps/ModelStep'),
    import('../steps/DefaultStep'),
    import('../steps/MethodStep'),
    import('../steps/ModelPickStep'),
    import('@/pages/Onboarding/welcome/Welcome'),
  ]);

  Loaded = {
    QueryClient,
    QueryClientProvider,
    MemoryRouter,
    Navigate,
    Route,
    Routes,
    useLocation,
    AuthContext,
    _localDevValue,
    ThemeProvider,
    useOnboardingRoute,
    shellTarget,
    SetupWizard,
  };
});

afterAll(() => {
  vi.unstubAllEnvs();
});

const activeClients: Array<{ clear: () => void; cancelQueries: () => Promise<void> }> = [];

function renderRealWizardApp(initialEntry: string | { pathname: string; state?: unknown } = '/dashboard') {
  const {
    QueryClient,
    QueryClientProvider,
    MemoryRouter,
    Navigate,
    Route,
    Routes,
    useLocation,
    AuthContext,
    _localDevValue,
    ThemeProvider,
    useOnboardingRoute,
    shellTarget,
    SetupWizard,
  } = Loaded;

  function ShellGate() {
    const route = useOnboardingRoute();
    if (route.kind === 'loading') return <div data-testid="shell-loading" />;
    const target = shellTarget(route, { desktop: false, skipped: false });
    if (target?.kind === 'path') return <Navigate to={target.to} replace />;
    return <div data-testid="dashboard-reached" />;
  }

  function LocationEcho() {
    const { pathname } = useLocation();
    return <div data-testid="location">{pathname}</div>;
  }

  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  activeClients.push(qc);

  render(
    <AuthContext value={{ ..._localDevValue, userId: 'u-ext-1' }}>
      <ThemeProvider>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[initialEntry]}>
            <Routes>
              <Route path="/dashboard" element={<ShellGate />} />
              <Route path="/setup/*" element={<SetupWizard />} />
              <Route path="/welcome" element={<div data-testid="welcome-reached" />} />
            </Routes>
            <LocationEcho />
          </MemoryRouter>
        </QueryClientProvider>
      </ThemeProvider>
    </AuthContext>,
  );
}

beforeEach(() => {
  server.state = makeInitialState();
  server.lastPreferencesPut = null;
  server.progressPatches = [];
  vi.clearAllMocks();
});

afterEach(async () => {
  cleanup();
  while (activeClients.length > 0) {
    const qc = activeClients.pop()!;
    await qc.cancelQueries();
    qc.clear();
  }
});

describe('Dalton real-router & real-progress SetupWizard first-run flows', () => {
  it('(a) team models available -> skips model connect -> preselects Neon High & Hydrogen High -> completes to /welcome', async () => {
    const user = userEvent.setup();
    renderRealWizardApp('/dashboard');

    // Shell gate routes first-run user to /setup/use
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/use'), { timeout: 5000 });
    expect(await screen.findByTestId('setup-use-team-models', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText(/Claude Code/i)).toBeNull();
    expect(screen.queryByText(/Another subscription or an API key/i)).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Continue' }));

    // Real SetupStart -> nextSetupStep -> skips /setup/model -> lands on /setup/default
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/default'), { timeout: 5000 });
    const neonRadio = await screen.findByRole('radio', { name: /Gemini 4 Neon High/i }, { timeout: 5000 });
    expect(neonRadio).toBeChecked();
    expect(screen.queryByText(/Credits Only Model/i)).toBeNull();
    expect(screen.queryByText(/Claude Code Sonnet/i)).toBeNull();

    // Open Customize: Hydrogen High is preselected and Auto (credits-only deployment default) is filtered out
    await user.click(screen.getByRole('button', { name: 'Customize' }));
    const bgSelect = screen.getByLabelText('Background model') as HTMLSelectElement;
    expect(bgSelect.value).toBe('gemini-4-hydrogen-high-oauth');
    expect(screen.queryByRole('option', { name: /Auto/i })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Continue' }));

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/welcome'), { timeout: 5000 });
    expect(screen.getByTestId('welcome-reached')).toBeInTheDocument();
    expect(server.lastPreferencesPut).toMatchObject({
      model_preference: expect.objectContaining({
        preferred_model: 'gemini-4-neon-high-oauth',
        preferred_background_model: 'gemini-4-hydrogen-high-oauth',
      }),
    });
    expect(server.state.progress.setup_completed_at).not.toBeNull();
  }, 15_000);

  it('(b) team sharing enabled with zero available models -> prompts to contact admin with Retry and blocks completion', async () => {
    const user = userEvent.setup();
    server.state.teamModelsConfigured = false;
    renderRealWizardApp('/setup/use');

    const emptyNotice = await screen.findByTestId('setup-use-team-models-empty', {}, { timeout: 5000 });
    expect(emptyNotice).toHaveTextContent(/contact your administrator/i);
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();

    // Also verify DefaultStep cannot mark setup complete when model pool is empty
    cleanup();
    server.state.progress.models = 'langalpha_models';
    server.state.progress.runs_on = 'cloud';
    renderRealWizardApp('/setup/default');

    const defaultEmpty = await screen.findByTestId('setup-default-team-models-empty', {}, { timeout: 5000 });
    expect(defaultEmpty).toHaveTextContent(/contact your administrator/i);
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
    expect(server.state.progress.setup_completed_at).toBeNull();

    // Admin configures team models; clicking Retry loads them and unlocks Continue
    server.state.teamModelsConfigured = true;
    await user.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.getByRole('radio', { name: /Gemini 4 Neon High/i })).toBeChecked(), { timeout: 5000 });
    expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled();
  }, 15_000);

  it('(c) restores legacy subscription_or_key progress with providerConnected=false without redirect loop', async () => {
    const user = userEvent.setup();
    server.state.progress.models = 'subscription_or_key';
    server.state.progress.runs_on = 'cloud';
    server.state.progress.setup_on = 'browser';

    // Entering at /setup/model redirects once to /setup/default and stays there (no loop back to /setup/model)
    renderRealWizardApp('/setup/model');

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/default'), { timeout: 5000 });
    const neonRadio = await screen.findByRole('radio', { name: /Gemini 4 Neon High/i }, { timeout: 5000 });
    expect(neonRadio).toBeChecked();

    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/welcome'), { timeout: 5000 });
    expect(server.state.progress.setup_completed_at).not.toBeNull();
  }, 15_000);

  it('(c2) stays on /setup/model without looping when sharing is on but team has no available models', async () => {
    server.state.teamModelsConfigured = false;
    server.state.progress.models = 'subscription_or_key';
    server.state.progress.runs_on = 'cloud';

    renderRealWizardApp('/setup/model');
    expect(await screen.findByTestId('setup-model-team-models-empty', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/setup/model');
  });

  it('(d) restores legacy mac_pending progress back to browser flow and enables Continue to complete', async () => {
    const user = userEvent.setup();
    server.state.progress.models = 'langalpha_models';
    server.state.progress.runs_on = 'cloud';
    server.state.progress.setup_on = 'mac_pending';

    renderRealWizardApp('/dashboard');

    // gate.shellTarget must not route to /setup/mac; must land on /setup/default
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/default'), { timeout: 5000 });
    const continueBtn = await screen.findByRole('button', { name: 'Continue' }, { timeout: 5000 });
    await waitFor(() => expect(continueBtn).toBeEnabled());

    await user.click(continueBtn);
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/welcome'), { timeout: 5000 });
    expect(server.state.progress.setup_completed_at).not.toBeNull();
  }, 15_000);

  it('MethodStep hides Claude Code copy for admins / sharing-off users in Dalton', async () => {
    server.state.callerTier = 'admin';
    renderRealWizardApp('/setup/method');
    expect(await screen.findByText(/Sign in to Google Antigravity, ChatGPT or Grok/i, {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText(/Use Claude Code/i)).toBeNull();
  });

  it('legacy /setup/models filters credits and Claude Code models from selection and save', async () => {
    const user = userEvent.setup();
    server.state.callerTier = 'admin';
    server.state.userApiKeys = [{ provider: 'openai', display_name: 'OpenAI', has_key: true, access_type: 'api_key' }];
    renderRealWizardApp({
      pathname: '/setup/models',
      state: { provider: 'openai', displayName: 'OpenAI', modelsFrom: 'openai', justConnected: true },
    });

    expect(await screen.findByText('GPT 5', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText('Credits Only Model')).toBeNull();
    expect(screen.queryByText('Claude Code Sonnet')).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(server.lastPreferencesPut).not.toBeNull(), { timeout: 5000 });
    const savedStr = JSON.stringify(server.lastPreferencesPut);
    expect(savedStr).not.toContain('credits-only-model');
    expect(savedStr).not.toContain('claude-code-sonnet');
  });
});
