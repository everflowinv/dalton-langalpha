// DALTON-HOOK(dalton-setup): real-router & real-progress first-run integration tests for Dalton SetupWizard
import type { ComponentType } from 'react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { OnboardingProgress, OnboardingProgressPatch } from '@/api/onboarding';
import type { ApiResponse, RequestConfig, api as ApiClient } from '@/api/client';
import type { AvailableModels } from '@/api/model';

const ok = <T,>(data: T): ApiResponse<T> => ({ data, status: 200, statusText: 'OK', headers: {} });

interface ServerState {
  teamSharingEnabled: boolean;
  teamModelsConfigured: boolean;
  callerTier: 'external' | 'internal' | 'admin';
  progress: OnboardingProgress;
  preferences: Record<string, unknown>;
  systemDefaults: { default_model?: string; background_model?: string };
  userApiKeys: Array<{
    provider: string;
    display_name?: string;
    has_key: boolean;
    access_type?: string;
    masked_key?: string | null;
  }>;
  computerProviders: Array<{ kind: string; allowed: boolean }>;
  availableModelsByProvider: Record<
    string,
    | Partial<AvailableModels>
    | { httpStatus: number }
    | ((opts: { refresh: boolean }) => Partial<AvailableModels> | { httpStatus: number })
  >;
  availableModelsCalls: Array<{ provider: string; refresh: boolean }>;
  modelsExtra: Record<string, { display_name: string; models: string[] }>;
  metadataExtra: Record<string, Record<string, unknown>>;
  credentialsStatusFailCount: number;
  modelsFailCount: number;
  antigravityModelsOverride: string[] | null;
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
  computerProviders: [{ kind: 'docker', allowed: true }],
  availableModelsByProvider: {},
  availableModelsCalls: [],
  modelsExtra: {},
  metadataExtra: {},
  credentialsStatusFailCount: 0,
  modelsFailCount: 0,
  antigravityModelsOverride: null,
});

const server = vi.hoisted(() => ({
  state: null as unknown as ServerState,
  lastPreferencesPut: null as Record<string, unknown> | null,
  progressPatches: [] as OnboardingProgressPatch[],
}));

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  const { ApiError } = actual;
  const unexpected = (method: string, url: string): never => {
    throw new Error(`Unhandled mocked API request: ${method} ${url}`);
  };
  const get: typeof ApiClient.get = async <T,>(url: string, config?: RequestConfig): Promise<ApiResponse<T>> => {
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
      if (s.credentialsStatusFailCount > 0) {
        s.credentialsStatusFailCount -= 1;
        throw new ApiError('Request failed with status code 500', 'ERR_BAD_RESPONSE', {
          data: { detail: 'temporary failure' },
          status: 500,
          statusText: 'Internal Server Error',
          headers: {},
        });
      }
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
      if (s.modelsFailCount > 0) {
        s.modelsFailCount -= 1;
        throw new ApiError('Request failed with status code 500', 'ERR_BAD_RESPONSE', {
          data: { detail: 'models read failed' },
          status: 500,
          statusText: 'Internal Server Error',
          headers: {},
        });
      }
      return ok({
        models: {
          'antigravity-oauth': {
            display_name: 'Antigravity',
            models: s.antigravityModelsOverride ?? [
              'gemini-4-neon-high-oauth',
              'gemini-4-carbon-medium-oauth',
              'gemini-4-hydrogen-low-oauth',
              'gemini-4-hydrogen-high-oauth',
              'claude-opus-5-5-medium-oauth',
            ],
          },
          platform: {
            display_name: 'Platform',
            models: ['credits-only-model'],
          },
          'claude-code': {
            display_name: 'Claude Code',
            models: ['claude-code-sonnet'],
          },
          ...s.modelsExtra,
        },
        model_metadata: {
          'gemini-4-neon-high-oauth': {
            provider: 'antigravity-oauth',
            access_type: 'oauth',
            intelligence: 5,
            speed: 4,
          },
          'gemini-4-carbon-medium-oauth': {
            provider: 'antigravity-oauth',
            access_type: 'oauth',
            intelligence: 5,
            speed: 3,
          },
          'gemini-4-hydrogen-low-oauth': {
            provider: 'antigravity-oauth',
            access_type: 'oauth',
            intelligence: 4,
            speed: 5,
          },
          'gemini-4-hydrogen-high-oauth': {
            provider: 'antigravity-oauth',
            access_type: 'oauth',
            intelligence: 4,
            speed: 5,
          },
          'claude-opus-5-5-medium-oauth': {
            provider: 'antigravity-oauth',
            access_type: 'oauth',
            intelligence: 5,
            speed: 3,
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
          ...s.metadataExtra,
        },
        system_defaults: s.systemDefaults,
        antigravity_series: [
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
        ],
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
      const params = config?.params as Record<string, string> | undefined;
      const refresh = params?.refresh === '1';
      s.availableModelsCalls.push({ provider, refresh });
      const rawOverride = s.availableModelsByProvider[provider];
      const override = typeof rawOverride === 'function' ? rawOverride({ refresh }) : rawOverride;
      if (override && 'httpStatus' in override && typeof override.httpStatus === 'number') {
        const status = override.httpStatus;
        throw new ApiError(
          `Request failed with status code ${status}`,
          status >= 400 && status < 500 ? 'ERR_BAD_REQUEST' : 'ERR_BAD_RESPONSE',
          { data: { detail: 'unauthorized' }, status, statusText: 'Error', headers: {} },
        );
      }
      if (override) {
        return ok({
          provider,
          status: 'ok',
          fetched_at: null,
          ready: [],
          missing: [],
          context_windows: {},
          others: [],
          total: 0,
          cached: false,
          ...override,
        } as T);
      }
      return ok({
        provider,
        status: 'ok',
        fetched_at: null,
        ready: provider === 'antigravity-oauth'
          ? (s.antigravityModelsOverride ?? [
              'gemini-4-neon-high-oauth',
              'gemini-4-carbon-medium-oauth',
              'gemini-4-hydrogen-low-oauth',
              'gemini-4-hydrogen-high-oauth',
              'claude-opus-5-5-medium-oauth',
            ])
          : [],
        missing: [],
        context_windows: {},
        others: provider === 'openai'
          ? [
              { id: 'credits-only-model', display_name: 'Credits Only Model', described: true },
              { id: 'claude-code-sonnet', display_name: 'Claude Code Sonnet', described: true },
              { id: 'gpt-5', display_name: 'GPT 5', described: true },
            ]
          : [],
        total: provider === 'antigravity-oauth' ? 2 : provider === 'openai' ? 3 : 0,
        cached: false,
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
      return ok({ providers: s.computerProviders } as T);
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
    return unexpected('GET', url);
  };

  const patch: typeof ApiClient.patch = async <T,>(url: string, body?: unknown, _config?: RequestConfig): Promise<ApiResponse<T>> => {
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
    return unexpected('PATCH', url);
  };

  const put: typeof ApiClient.put = async <T,>(url: string, body?: unknown, _config?: RequestConfig): Promise<ApiResponse<T>> => {
    if (url === '/api/v1/users/me/preferences') {
      const payload = (body ?? {}) as Record<string, unknown>;
      server.lastPreferencesPut = payload;
      server.state.preferences = { ...server.state.preferences, ...payload };
      return ok({ ...server.state.preferences } as T);
    }
    return unexpected('PUT', url);
  };

  const post: typeof ApiClient.post = async <T,>(url: string, _data?: unknown, _config?: RequestConfig): Promise<ApiResponse<T>> =>
    unexpected('POST', url);
  const del: typeof ApiClient.delete = async <T,>(url: string, _config?: RequestConfig): Promise<ApiResponse<T>> =>
    unexpected('DELETE', url);

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
  useSetupFacts: typeof import('../answers/useSetupFacts').useSetupFacts;
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
    { useSetupFacts },
    { shellTarget },
    { default: SetupWizard },
    { initI18n, default: i18n },
    { default: enUS },
    { default: zhCN },
    { default: enUSIntros },
    { default: zhCNIntros },
    { default: enUSFirstRun },
    { default: zhCNFirstRun },
  ] = await Promise.all([
    import('@tanstack/react-query'),
    import('react-router'),
    import('@/contexts/AuthContext'),
    import('@/contexts/ThemeContext'),
    import('@/hooks/useOnboardingRoute'),
    import('../answers/useSetupFacts'),
    import('../answers/gate'),
    import('../SetupWizard'),
    import('@/i18n'),
    import('@/locales/en-US.json'),
    import('@/locales/zh-CN.json'),
    import('@/locales/intros/en-US.json'),
    import('@/locales/intros/zh-CN.json'),
    import('@/locales/thisMacFirstRun/en-US.json'),
    import('@/locales/thisMacFirstRun/zh-CN.json'),
    import('../steps/UseStep'),
    import('../steps/ComputerStep'),
    import('../steps/ModelStep'),
    import('../steps/DefaultStep'),
    import('../steps/MethodStep'),
    import('../steps/DefaultsStep'),
    import('../steps/DoneStep'),
    import('../steps/ModelPickStep'),
    import('@/pages/Onboarding/welcome/Welcome'),
  ]);

  await initI18n({
    'en-US': { translation: enUS, intros: enUSIntros, thisMacFirstRun: enUSFirstRun },
    'zh-CN': { translation: zhCN, intros: zhCNIntros, thisMacFirstRun: zhCNFirstRun },
  });
  await i18n.changeLanguage('en-US');

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
    useSetupFacts,
    shellTarget,
    SetupWizard,
  };
}, 30_000);

afterAll(async () => {
  vi.unstubAllEnvs();
  vi.resetModules();
  const [
    { initI18n },
    { default: enUS },
    { default: zhCN },
    { default: enUSIntros },
    { default: zhCNIntros },
    { default: enUSFirstRun },
    { default: zhCNFirstRun },
  ] = await Promise.all([
    import('@/i18n'),
    import('@/locales/en-US.json'),
    import('@/locales/zh-CN.json'),
    import('@/locales/intros/en-US.json'),
    import('@/locales/intros/zh-CN.json'),
    import('@/locales/thisMacFirstRun/en-US.json'),
    import('@/locales/thisMacFirstRun/zh-CN.json'),
  ]);
  await initI18n({
    'en-US': { translation: enUS, intros: enUSIntros, thisMacFirstRun: enUSFirstRun },
    'zh-CN': { translation: zhCN, intros: zhCNIntros, thisMacFirstRun: zhCNFirstRun },
  });
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
  it('(a) team models available -> skips model connect -> preselects Neon High & Hydrogen Low (without Low label) -> completes to /welcome', async () => {
    const user = userEvent.setup();
    renderRealWizardApp('/dashboard');

    // Shell gate routes first-run user to /setup/use
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/use'), { timeout: 5000 });
    expect(await screen.findByTestId('setup-use-team-models', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Set up Everflow' })).toBeInTheDocument();
    expect(screen.queryByText(/How will you use/i)).toBeNull();
    expect(screen.queryByText(/Two questions/i)).toBeNull();
    expect(screen.queryByText(/Which models do you want/i)).toBeNull();
    expect(screen.queryByText(/Claude Code/i)).toBeNull();
    expect(screen.queryByText(/Another subscription or an API key/i)).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Continue' }));

    // Real SetupStart -> nextSetupStep -> skips /setup/model -> lands on /setup/default
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/default'), { timeout: 5000 });
    const neonRadio = await screen.findByRole('radio', { name: /Gemini 4 Neon/i }, { timeout: 5000 });
    expect(neonRadio).toBeChecked();
    expect(screen.queryByText(/Gemini 4 Neon High/i)).toBeNull();
    expect(screen.queryByText(/Credits Only Model/i)).toBeNull();
    expect(screen.queryByText(/Claude Code Sonnet/i)).toBeNull();
    // Only curated available candidates appear (Neon, Carbon, Opus fallback — never Hydrogen)
    const radios = screen.getAllByRole('radio') as HTMLInputElement[];
    expect(radios.map((r) => r.value)).toEqual([
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
      'claude-opus-5-5-medium-oauth',
    ]);
    // Neon (5/4) & Carbon (5/3) display intelligence and speed ratings
    expect(screen.getByRole('img', { name: 'Speed: 4 of 5' })).toBeInTheDocument();
    expect(screen.getAllByRole('img', { name: 'Intelligence: 5 of 5' }).length).toBeGreaterThanOrEqual(2);

    // Open Customize: Hydrogen Low is preselected, label omits "Low", and Auto (credits-only deployment default) is filtered out
    await user.click(screen.getByRole('button', { name: 'Customize' }));
    const bgSelect = screen.getByLabelText('Background model') as HTMLSelectElement;
    expect(bgSelect.value).toBe('gemini-4-hydrogen-low-oauth');
    const selectedBgOption = [...bgSelect.options].find((o) => o.value === 'gemini-4-hydrogen-low-oauth');
    expect(selectedBgOption?.textContent).toContain('Gemini 4 Hydrogen');
    expect(selectedBgOption?.textContent).not.toMatch(/\bLow\b/i);
    expect(screen.queryByRole('option', { name: /Auto/i })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Continue' }));

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/welcome'), { timeout: 5000 });
    expect(screen.getByTestId('welcome-reached')).toBeInTheDocument();
    expect(server.lastPreferencesPut).toMatchObject({
      model_preference: expect.objectContaining({
        preferred_model: 'gemini-4-neon-high-oauth',
        preferred_background_model: 'gemini-4-hydrogen-low-oauth',
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

    // Admin configures team models; clicking Retry loads them, unlocks Continue, and saves BOTH recommended models
    server.state.teamModelsConfigured = true;
    await user.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.getByRole('radio', { name: /Gemini 4 Neon/i })).toBeChecked(), { timeout: 5000 });
    const continueBtn = screen.getByRole('button', { name: 'Continue' });
    expect(continueBtn).toBeEnabled();

    await user.click(continueBtn);
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/welcome'), { timeout: 5000 });
    expect(server.lastPreferencesPut).toMatchObject({
      model_preference: expect.objectContaining({
        preferred_model: 'gemini-4-neon-high-oauth',
        preferred_background_model: 'gemini-4-hydrogen-low-oauth',
      }),
    });
  }, 15_000);

  it('(b2) rejected team credential (unauthorized fallback / 401) blocks with contact-admin + Retry, while transient unreachable stays available', async () => {
    const user = userEvent.setup();
    server.state.teamModelsConfigured = false;
    server.state.modelsExtra = {
      openai: { display_name: 'OpenAI', models: ['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth'] },
    };
    server.state.metadataExtra = {
      'gpt-6.2-astra-oauth': { display_name: 'GPT-6.2 Astra', provider: 'openai', access_type: 'api_key', intelligence: 5, speed: 3 },
      'gpt-6.2-sol-oauth': { display_name: 'GPT-6.2 Sol', provider: 'openai', access_type: 'api_key', intelligence: 5, speed: 3 },
    };
    server.state.userApiKeys = [
      { provider: 'openai', display_name: 'OpenAI', has_key: true, access_type: 'api_key', masked_key: 'sk-...dead' },
    ];
    // Provider explicitly refuses the key (401 -> fallback with reason='unauthorized')
    server.state.availableModelsByProvider = {
      openai: { status: 'fallback', reason: 'unauthorized', ready: ['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth'] },
    };

    renderRealWizardApp('/setup/use');
    await waitFor(() => expect(screen.getByTestId('setup-use-team-models-empty')).toBeInTheDocument(), { timeout: 5000 });
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();

    // Same on /setup/default: rejected provider models are excluded -> contact admin + Retry
    cleanup();
    server.state.progress.models = 'langalpha_models';
    server.state.progress.runs_on = 'cloud';
    renderRealWizardApp('/setup/default');
    expect(await screen.findByTestId('setup-default-team-models-empty', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();

    // Admin replaces the key so available-models succeeds -> Retry recovers
    server.state.availableModelsByProvider = {
      openai: { status: 'ok', ready: ['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth'] },
    };
    await user.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(
      () => expect(screen.getByRole('radio', { name: /GPT-6\.2 Astra/i })).toBeChecked(),
      { timeout: 5000 },
    );
    expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled();

    // Direct HTTP 401 on available-models is also treated as explicitly rejected
    cleanup();
    server.state.progress.models = null;
    server.state.progress.runs_on = null;
    server.state.availableModelsByProvider = { openai: { httpStatus: 401 } };
    renderRealWizardApp('/setup/use');
    await waitFor(() => expect(screen.getByTestId('setup-use-team-models-empty')).toBeInTheDocument(), { timeout: 5000 });

    // Transient network blip (unreachable fallback) must NOT mark team models unavailable
    cleanup();
    server.state.availableModelsByProvider = {
      openai: { status: 'fallback', reason: 'unreachable', ready: ['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth'] },
    };
    renderRealWizardApp('/setup/use');
    await waitFor(() => expect(screen.getByTestId('setup-use-team-models')).toBeInTheDocument(), { timeout: 5000 });
    expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled();
  }, 15_000);

  it('(c) restores legacy subscription_or_key progress with actual providerConnected=false without redirect loop', async () => {
    const user = userEvent.setup();
    // Disconnect OAuth and supply only a server-level key (masked_key='(server)'),
    // which yields providerConnected=false in useSetupFacts while team.available=true.
    server.state.teamModelsConfigured = false;
    server.state.modelsExtra = {
      openai: { display_name: 'OpenAI', models: ['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth'] },
    };
    server.state.metadataExtra = {
      'gpt-6.2-astra-oauth': { display_name: 'GPT-6.2 Astra', provider: 'openai', access_type: 'api_key', intelligence: 5, speed: 3 },
      'gpt-6.2-sol-oauth': { display_name: 'GPT-6.2 Sol', provider: 'openai', access_type: 'api_key', intelligence: 5, speed: 3 },
    };
    server.state.userApiKeys = [
      { provider: 'openai', display_name: 'OpenAI', has_key: true, access_type: 'api_key', masked_key: '(server)' },
    ];
    server.state.availableModelsByProvider = {
      openai: { status: 'ok', ready: ['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth'] },
    };
    server.state.progress.models = 'subscription_or_key';
    server.state.progress.runs_on = 'cloud';
    server.state.progress.setup_on = 'browser';

    function FactsProbe() {
      const { facts } = Loaded.useSetupFacts();
      return (
        <div
          data-testid="facts-probe"
          data-provider-connected={String(facts?.providerConnected)}
          data-team-available={String(facts?.teamModelsAvailable)}
        />
      );
    }

    const { QueryClient, QueryClientProvider, MemoryRouter, Route, Routes, AuthContext, _localDevValue, ThemeProvider, SetupWizard } = Loaded;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
    activeClients.push(qc);
    function LocationEcho() {
      const { pathname } = Loaded.useLocation();
      return <div data-testid="location">{pathname}</div>;
    }

    render(
      <AuthContext value={{ ..._localDevValue, userId: 'u-ext-1' }}>
        <ThemeProvider>
          <QueryClientProvider client={qc}>
            <MemoryRouter initialEntries={['/setup/model']}>
              <Routes>
                <Route path="/setup/*" element={<SetupWizard />} />
                <Route path="/welcome" element={<div data-testid="welcome-reached" />} />
              </Routes>
              <LocationEcho />
              <FactsProbe />
            </MemoryRouter>
          </QueryClientProvider>
        </ThemeProvider>
      </AuthContext>,
    );

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/default'), { timeout: 5000 });
    expect(screen.getByTestId('facts-probe')).toHaveAttribute('data-provider-connected', 'false');
    expect(screen.getByTestId('facts-probe')).toHaveAttribute('data-team-available', 'true');
    // AnswersStrip normalizes legacy subscription_or_key to Team models when team models are available
    expect(screen.getByTestId('setup-answers')).toHaveTextContent('Team models');
    expect(screen.getByTestId('setup-answers')).not.toHaveTextContent(/Subscription or API key/i);

    const astraRadio = await screen.findByRole('radio', { name: /GPT-6\.2 Astra/i }, { timeout: 5000 });
    expect(astraRadio).toBeChecked();

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

  it('(c4) legacy subscription_or_key with initial credentials-status failure then recovery settles on /setup/default without redirect loop', async () => {
    const user = userEvent.setup();
    server.state.teamModelsConfigured = false;
    server.state.credentialsStatusFailCount = 1;
    server.state.modelsExtra = {
      openai: { display_name: 'OpenAI', models: ['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth'] },
    };
    server.state.metadataExtra = {
      'gpt-6.2-astra-oauth': { display_name: 'GPT-6.2 Astra', provider: 'openai', access_type: 'api_key', intelligence: 5, speed: 3 },
      'gpt-6.2-sol-oauth': { display_name: 'GPT-6.2 Sol', provider: 'openai', access_type: 'api_key', intelligence: 5, speed: 3 },
    };
    server.state.userApiKeys = [
      { provider: 'openai', display_name: 'OpenAI', has_key: true, access_type: 'api_key', masked_key: '(server)' },
    ];
    server.state.availableModelsByProvider = {
      openai: { status: 'ok', ready: ['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth'] },
    };
    server.state.progress.models = 'subscription_or_key';
    server.state.progress.runs_on = 'cloud';
    server.state.progress.setup_on = 'browser';

    renderRealWizardApp('/dashboard');

    // First credentials/status fails -> lands on /setup/model -> retry/re-read succeeds ->
    // shared query updates routing and wizard together to /setup/default without bouncing back.
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/default'), { timeout: 5000 });
    const astraRadio = await screen.findByRole('radio', { name: /GPT-6\.2 Astra/i }, { timeout: 5000 });
    expect(astraRadio).toBeChecked();
    expect(screen.getByTestId('location')).toHaveTextContent('/setup/default');

    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/welcome'), { timeout: 5000 });
  }, 15_000);

  it('(c5) legacy subscription_or_key recovering from rejected (401/unauthorized) team credential via Try again moves to /setup/default without redirect loop', async () => {
    const user = userEvent.setup();
    server.state.teamModelsConfigured = false;
    server.state.modelsExtra = {
      openai: { display_name: 'OpenAI', models: ['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth'] },
    };
    server.state.metadataExtra = {
      'gpt-6.2-astra-oauth': { display_name: 'GPT-6.2 Astra', provider: 'openai', access_type: 'api_key', intelligence: 5, speed: 3 },
      'gpt-6.2-sol-oauth': { display_name: 'GPT-6.2 Sol', provider: 'openai', access_type: 'api_key', intelligence: 5, speed: 3 },
    };
    server.state.userApiKeys = [
      { provider: 'openai', display_name: 'OpenAI', has_key: true, access_type: 'api_key', masked_key: '(server)' },
    ];
    server.state.availableModelsByProvider = {
      openai: { status: 'fallback', reason: 'unauthorized', ready: ['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth'] },
    };
    server.state.progress.models = 'subscription_or_key';
    server.state.progress.runs_on = 'cloud';
    server.state.progress.setup_on = 'browser';

    renderRealWizardApp('/dashboard');

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/model'), { timeout: 5000 });
    expect(await screen.findByTestId('setup-model-team-models-empty', {}, { timeout: 5000 })).toBeInTheDocument();

    // Admin fixes the server key without changing providers list shape; Try again must
    // invalidate underlying queries and transition cleanly to /setup/default with no loop.
    server.state.availableModelsByProvider = {
      openai: { status: 'ok', ready: ['gpt-6.2-astra-oauth', 'gpt-6.2-sol-oauth'] },
    };
    await user.click(screen.getByRole('button', { name: 'Try again' }));

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/default'), { timeout: 5000 });
    const astraRadio = await screen.findByRole('radio', { name: /GPT-6\.2 Astra/i }, { timeout: 5000 });
    expect(astraRadio).toBeChecked();
    expect(screen.getByTestId('location')).toHaveTextContent('/setup/default');
  }, 15_000);

  it('(c3) normalizes legacy claude_code answers across AnswersStrip, DefaultStep, ComputerStep, and non-shared mode', async () => {
    // 1) Shared team models available -> DefaultStep chip shows Team-configured models, never Claude Code
    server.state.progress.models = 'claude_code';
    server.state.progress.runs_on = 'cloud';
    renderRealWizardApp('/dashboard');

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/default'), { timeout: 5000 });
    const defaultAnswers = await screen.findByTestId('setup-answers', {}, { timeout: 5000 });
    expect(defaultAnswers).toHaveTextContent('Team models');
    expect(document.body.textContent).not.toMatch(/Claude Code/i);

    // 2) Legacy claude_code + own_computer with SSH offered -> ComputerStep never exposes Claude Code chips or lead copy
    cleanup();
    server.state.computerProviders = [{ kind: 'docker', allowed: true }, { kind: 'ssh', allowed: true }];
    server.state.progress.models = 'claude_code';
    server.state.progress.runs_on = 'own_computer';
    renderRealWizardApp('/dashboard');

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/computer'), { timeout: 5000 });
    const computerAnswers = await screen.findByTestId('setup-answers', {}, { timeout: 5000 });
    expect(computerAnswers).toHaveTextContent('Team models');
    expect(document.body.textContent).not.toMatch(/Claude Code/i);

    // 3) Non-shared mode + legacy claude_code + no provider -> routes to /setup/model (not stuck in empty DefaultStep)
    cleanup();
    server.state.teamSharingEnabled = false;
    server.state.teamModelsConfigured = false;
    server.state.computerProviders = [{ kind: 'docker', allowed: true }];
    server.state.progress.models = 'claude_code';
    server.state.progress.runs_on = 'cloud';
    renderRealWizardApp('/dashboard');

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/model'), { timeout: 5000 });
    const modelAnswers = await screen.findByTestId('setup-answers', {}, { timeout: 5000 });
    expect(modelAnswers).toHaveTextContent('Subscription or key');
    expect(document.body.textContent).not.toMatch(/Claude Code/i);
  }, 15_000);

  it('(d) restores legacy mac_pending and app progress back to browser flow and enables Continue to complete', async () => {
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

    // Legacy setup_on='app' in Dalton browser also reclaims back to browser flow and enables Continue
    cleanup();
    server.state = makeInitialState();
    server.progressPatches = [];
    server.state.progress.models = 'langalpha_models';
    server.state.progress.runs_on = 'cloud';
    server.state.progress.setup_on = 'app';

    renderRealWizardApp('/dashboard');
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/default'), { timeout: 5000 });
    const appContinueBtn = await screen.findByRole('button', { name: 'Continue' }, { timeout: 5000 });
    await waitFor(() => expect(appContinueBtn).toBeEnabled());
    await waitFor(() => expect(server.progressPatches).toContainEqual({ setup_on: 'browser' }));

    await user.click(appContinueBtn);
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/welcome'), { timeout: 5000 });
    expect(server.state.progress.setup_completed_at).not.toBeNull();
  }, 15_000);

  it('(e) admin first login when team has no credentials yet can choose personal/team connection inside UseStep & ModelStep', async () => {
    const user = userEvent.setup();
    server.state.callerTier = 'admin';
    server.state.teamModelsConfigured = false;
    renderRealWizardApp('/dashboard');

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/use'), { timeout: 5000 });
    expect(screen.queryByTestId('setup-use-team-models-empty')).toBeNull();
    const subRadio = await screen.findByRole('radio', { name: /Another subscription or an API key/i }, { timeout: 5000 });
    await user.click(subRadio);
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/model'), { timeout: 5000 });
    expect(screen.queryByTestId('setup-model-team-models-empty')).toBeNull();
    expect(await screen.findByTestId('setup-sub-api-key', {}, { timeout: 5000 })).toBeInTheDocument();
  }, 15_000);

  it('(f) legacy /setup/method & /setup/defaults use useDaltonTeamModels(), block stale unavailable picks, and recover on Retry', async () => {
    const user = userEvent.setup();
    server.state.teamModelsConfigured = false;
    server.state.preferences = {
      model_preference: {
        preferred_model: 'stale-removed-model',
        preferred_background_model: 'stale-removed-bg',
      },
    };

    // On /setup/method, non-admin member sees contact-admin + Retry and cannot skip to /setup/defaults
    renderRealWizardApp('/setup/method');
    expect(await screen.findByTestId('setup-method-team-models-empty', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Skip to model selection/i })).toBeNull();

    // Direct visit to /setup/defaults with empty catalog & stale saved preferences:
    // must NOT show "team has already configured", must show contact-admin + Retry, and must keep Continue disabled
    cleanup();
    renderRealWizardApp('/setup/defaults');
    const defaultsEmpty = await screen.findByTestId('setup-defaults-team-models-empty', {}, { timeout: 5000 });
    expect(defaultsEmpty).toHaveTextContent(/contact your administrator/i);
    expect(screen.queryByText(/Your team has already configured shared models/i)).toBeNull();
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
    expect(server.lastPreferencesPut).toBeNull();

    // Admin configures team models -> clicking Try again populates Neon High + Hydrogen High and saves them
    server.state.teamModelsConfigured = true;
    await user.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.getByText(/Your team has already configured shared models/i)).toBeInTheDocument(), { timeout: 5000 });
    const continueBtn = screen.getByRole('button', { name: 'Continue' });
    await waitFor(() => expect(continueBtn).toBeEnabled());

    await user.click(continueBtn);
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/ready'), { timeout: 5000 });
    expect(server.lastPreferencesPut).toMatchObject({
      model_preference: expect.objectContaining({
        preferred_model: 'gemini-4-neon-high-oauth',
        preferred_background_model: 'gemini-4-hydrogen-low-oauth',
      }),
    });
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

  it('(A1) Retry on empty catalog passes refresh=1 to bypass server-cached provider lists and unlocks setup', async () => {
    const user = userEvent.setup();
    server.state.availableModelsByProvider = {
      'antigravity-oauth': ({ refresh }) => (
        refresh
          ? {
              status: 'ok',
              ready: ['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-low-oauth'],
              missing: [],
              cached: false,
            }
          : {
              status: 'ok',
              ready: [],
              missing: [
                'gemini-4-neon-high-oauth',
                'gemini-4-carbon-medium-oauth',
                'gemini-4-hydrogen-low-oauth',
                'gemini-4-hydrogen-high-oauth',
                'claude-opus-5-5-medium-oauth',
              ],
              cached: true,
            }
      ),
    };

    renderRealWizardApp('/setup/use');
    expect(await screen.findByTestId('setup-use-team-models-empty', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
    expect(server.state.availableModelsCalls.some((c) => c.provider === 'antigravity-oauth' && !c.refresh)).toBe(true);
    expect(server.state.availableModelsCalls.some((c) => c.provider === 'antigravity-oauth' && c.refresh)).toBe(false);

    await user.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByTestId('setup-use-team-models', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled();
    expect(server.state.availableModelsCalls.some((c) => c.provider === 'antigravity-oauth' && c.refresh)).toBe(true);
  }, 15_000);

  it('(A3) when none of the 5 curated candidates is available, shows contact-admin + Retry for members without falling back to uncurated models while admins can still connect', async () => {
    const user = userEvent.setup();
    server.state.teamModelsConfigured = false;
    server.state.modelsExtra = {
      openai: { display_name: 'OpenAI', models: ['gpt-5', 'gpt-5-mini'] },
    };
    server.state.metadataExtra = {
      'gpt-5': { display_name: 'GPT 5 Pro', provider: 'openai', access_type: 'api_key', intelligence: 5, speed: 3 },
      'gpt-5-mini': { display_name: 'GPT 5 Mini', provider: 'openai', access_type: 'api_key', intelligence: 3, speed: 5 },
    };
    server.state.userApiKeys = [
      { provider: 'openai', display_name: 'OpenAI', has_key: true, access_type: 'api_key', masked_key: '(server)' },
    ];
    server.state.availableModelsByProvider = {
      openai: { status: 'ok', ready: ['gpt-5', 'gpt-5-mini'] },
    };

    // Non-admin on /setup/use -> blocked with contact-admin + Retry
    renderRealWizardApp('/setup/use');
    const useEmpty = await screen.findByTestId('setup-use-team-models-empty', {}, { timeout: 5000 });
    expect(useEmpty).toHaveTextContent(/No recommended team models configured yet\. Please contact your administrator\./i);
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();

    // Non-admin on /setup/default -> never falls back to GPT 5 Pro; shows contact-admin + Retry
    cleanup();
    server.state.progress.models = 'langalpha_models';
    server.state.progress.runs_on = 'cloud';
    renderRealWizardApp('/setup/default');
    const defaultEmpty = await screen.findByTestId('setup-default-team-models-empty', {}, { timeout: 5000 });
    expect(defaultEmpty).toHaveTextContent(/No recommended team models configured yet\. Please contact your administrator\./i);
    expect(screen.queryByRole('radio', { name: /GPT 5 Pro/i })).toBeNull();
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();

    // Legacy /setup/defaults also blocks Continue even if uncurated models were previously saved
    cleanup();
    server.state.preferences = {
      model_preference: {
        preferred_model: 'gpt-5',
        preferred_background_model: 'gpt-5-mini',
      },
    };
    renderRealWizardApp('/setup/defaults');
    await waitFor(() => {
      expect(screen.getByTestId('setup-defaults-team-models-empty')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
    }, { timeout: 5000 });

    // Admin is NOT blocked on /setup/use and can proceed to /setup/model to connect an account
    cleanup();
    server.state.callerTier = 'admin';
    server.state.progress.models = null;
    server.state.progress.runs_on = null;
    renderRealWizardApp('/setup/use');
    expect(screen.queryByTestId('setup-use-team-models-empty')).toBeNull();
    const subRadio = await screen.findByRole('radio', { name: /Another subscription or an API key/i }, { timeout: 5000 });
    await user.click(subRadio);
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/model'), { timeout: 5000 });
    expect(await screen.findByTestId('setup-sub-api-key', {}, { timeout: 5000 })).toBeInTheDocument();
  }, 15_000);

  it('(A4) legacy /setup/method surfaces load-failure notice with Try again when reading team models fails and recovers on Retry', async () => {
    const user = userEvent.setup();
    server.state.modelsFailCount = 99;

    renderRealWizardApp('/setup/method');
    expect(await screen.findByTestId('setup-load-failed', {}, { timeout: 5000 })).toBeInTheDocument();
    const retryBtn = screen.getByRole('button', { name: 'Try again' });

    server.state.modelsFailCount = 0;
    await user.click(retryBtn);
    await waitFor(() => expect(screen.queryByTestId('setup-load-failed')).toBeNull(), { timeout: 5000 });
    expect(await screen.findByRole('button', { name: /Skip to models/i }, { timeout: 5000 })).toBeInTheDocument();
  }, 15_000);

  it('(A5) legacy /setup/defaults normalizes a saved Hydrogen High background when only Hydrogen Low is offered and keeps dropdown/save consistent', async () => {
    const user = userEvent.setup();
    server.state.antigravityModelsOverride = [
      'gemini-4-neon-high-oauth',
      'gemini-4-hydrogen-low-oauth',
    ];
    server.state.preferences = {
      model_preference: {
        preferred_model: 'gemini-4-neon-high-oauth',
        preferred_background_model: 'gemini-4-hydrogen-high-oauth',
      },
    };

    renderRealWizardApp('/setup/defaults');
    const bgSelect = await screen.findByLabelText(/Background model/i, {}, { timeout: 5000 }) as HTMLSelectElement;
    await waitFor(() => expect(bgSelect.value).toBe('gemini-4-hydrogen-low-oauth'), { timeout: 5000 });
    expect(bgSelect.selectedOptions[0]?.textContent).toContain('Gemini 4 Hydrogen');
    expect(bgSelect.selectedOptions[0]?.textContent).not.toMatch(/\bLow\b/i);

    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/setup/ready'), { timeout: 5000 });
    expect(server.lastPreferencesPut).toMatchObject({
      model_preference: expect.objectContaining({
        preferred_model: 'gemini-4-neon-high-oauth',
        preferred_background_model: 'gemini-4-hydrogen-low-oauth',
      }),
    });
  }, 15_000);
});
