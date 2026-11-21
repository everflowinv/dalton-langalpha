// DALTON-HOOK(dalton-setup): app-shell onboarding route coverage across all four Dalton team-model states
import type { ReactNode } from 'react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import type { ApiResponse, RequestConfig, api as ApiClient } from '@/api/client';
import type { AvailableModels } from '@/api/model';
import type { OnboardingProgress } from '@/api/onboarding';

const ok = <T,>(data: T): ApiResponse<T> => ({ data, status: 200, statusText: 'OK', headers: {} });

interface MockServerState {
  progress: OnboardingProgress;
  teamSharingEnabled: boolean;
  teamOauthConnected: boolean;
  userApiKeys: Array<{
    provider: string;
    display_name?: string;
    has_key: boolean;
    access_type?: string;
    masked_key?: string | null;
  }>;
  modelsError: boolean;
  apiKeysError: boolean;
  modelsPending: Promise<void> | null;
  availableModelsByProvider: Record<string, Partial<AvailableModels> | { httpStatus: number }>;
  requestedUrls: string[];
}

const makeInitialState = (): MockServerState => ({
  progress: {
    models: 'subscription_or_key',
    runs_on: 'cloud',
    plan_step_done_at: null,
    account_setup_completed_at: null,
    this_mac: 'unavailable',
    setup_on: 'browser',
    setup_step: null,
    setup_completed_at: null,
    created_at: '2026-11-21T00:00:00Z',
    updated_at: '2026-11-21T00:00:00Z',
  },
  teamSharingEnabled: true,
  teamOauthConnected: true,
  userApiKeys: [],
  modelsError: false,
  apiKeysError: false,
  modelsPending: null,
  availableModelsByProvider: {},
  requestedUrls: [],
});

const server = vi.hoisted(() => ({
  state: null as unknown as MockServerState,
}));

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  const { ApiError } = actual;
  const fail = (status: number, detail = 'error'): never => {
    throw new ApiError(
      `Request failed with status code ${status}`,
      status >= 400 && status < 500 ? 'ERR_BAD_REQUEST' : 'ERR_BAD_RESPONSE',
      { data: { detail }, status, statusText: 'Error', headers: {} },
    );
  };

  const get: typeof ApiClient.get = async <T,>(url: string, _config?: RequestConfig): Promise<ApiResponse<T>> => {
    const s = server.state;
    s.requestedUrls.push(url);

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
      return ok({} as T);
    }
    if (url === '/api/v1/users/me/account') {
      return ok({ policy: null } as T);
    }
    if (url === '/api/v1/dalton/credentials/status') {
      return ok({
        sharing: s.teamSharingEnabled ? 'team' : 'off',
        enabled: s.teamSharingEnabled,
        holder_user_id: 'u-admin-1',
        credential_user_id: s.teamSharingEnabled ? 'u-admin-1' : 'u-ext-1',
        is_pm: false,
        can_disconnect: false,
        can_invite: false,
        caller_tier: 'external',
      } as T);
    }
    if (url === '/api/v1/models') {
      if (s.modelsPending) await s.modelsPending;
      if (s.modelsError) fail(500, 'models unavailable');
      return ok({
        models: {
          'antigravity-oauth': {
            display_name: 'Antigravity',
            models: ['gemini-4-neon-high-oauth', 'gemini-4-hydrogen-high-oauth'],
          },
          openai: {
            display_name: 'OpenAI',
            models: ['gpt-5'],
          },
          platform: {
            display_name: 'Platform',
            models: ['credits-only-model'],
          },
        },
        model_metadata: {
          'gemini-4-neon-high-oauth': {
            display_name: 'Gemini 4 Neon High',
            provider: 'antigravity-oauth',
            access_type: 'oauth',
          },
          'gemini-4-hydrogen-high-oauth': {
            display_name: 'Gemini 4 Hydrogen High',
            provider: 'antigravity-oauth',
            access_type: 'oauth',
          },
          'gpt-5': {
            display_name: 'GPT 5',
            provider: 'openai',
            access_type: 'api_key',
          },
          'credits-only-model': {
            display_name: 'Credits Only',
            provider: 'platform',
            tier: 0,
          },
        },
        system_defaults: {},
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
      if (s.apiKeysError) fail(500, 'api keys unavailable');
      return ok({ providers: s.userApiKeys, byok_enabled: false } as T);
    }
    if (url.startsWith('/api/v1/oauth/') && url.endsWith('/status')) {
      const connected = url === '/api/v1/oauth/antigravity/status' && s.teamSharingEnabled && s.teamOauthConnected;
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
      const override = s.availableModelsByProvider[provider];
      if (override && 'httpStatus' in override && typeof override.httpStatus === 'number') {
        fail(override.httpStatus, 'unauthorized');
      }
      return ok({
        provider,
        status: 'ok',
        fetched_at: null,
        ready: provider === 'antigravity-oauth' ? ['gemini-4-neon-high-oauth'] : ['gpt-5'],
        missing: [],
        context_windows: {},
        others: [],
        total: 1,
        cached: false,
        ...override,
      } as T);
    }
    if (url === '/api/v1/computers') {
      return ok({ computers: [] } as T);
    }
    if (url === '/api/v1/threads' || url.startsWith('/api/v1/threads?')) {
      return ok({ threads: [] } as T);
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
    throw new Error(`Unhandled mocked API request: GET ${url}`);
  };

  return {
    ...actual,
    api: {
      ...actual.api,
      get,
    },
  };
});

let Loaded: {
  QueryClient: typeof import('@tanstack/react-query').QueryClient;
  QueryClientProvider: typeof import('@tanstack/react-query').QueryClientProvider;
  AuthContext: typeof import('@/contexts/AuthContext').AuthContext;
  _localDevValue: typeof import('@/contexts/AuthContext')._localDevValue;
  useOnboardingRoute: typeof import('../useOnboardingRoute').useOnboardingRoute;
  shellTarget: typeof import('@/pages/Setup/answers/gate').shellTarget;
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
    { AuthContext, _localDevValue },
    { useOnboardingRoute },
    { shellTarget },
  ] = await Promise.all([
    import('@tanstack/react-query'),
    import('@/contexts/AuthContext'),
    import('../useOnboardingRoute'),
    import('@/pages/Setup/answers/gate'),
  ]);

  Loaded = { QueryClient, QueryClientProvider, AuthContext, _localDevValue, useOnboardingRoute, shellTarget };
});

afterAll(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

const activeClients: Array<{ clear: () => void; cancelQueries: () => Promise<void> }> = [];

function renderOnboardingRoute() {
  const { QueryClient, QueryClientProvider, AuthContext, _localDevValue, useOnboardingRoute } = Loaded;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  activeClients.push(qc);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <AuthContext value={{ ..._localDevValue, userId: 'u-ext-1' }}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </AuthContext>
  );
  return renderHook(() => useOnboardingRoute(), { wrapper });
}

beforeEach(() => {
  server.state = makeInitialState();
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

describe('useOnboardingRoute in Dalton build (lazy team-model availability)', () => {
  it('routes to /setup/default when team models are available (normalizing legacy subscription_or_key)', async () => {
    const { result } = renderOnboardingRoute();
    await waitFor(() => expect(result.current.kind).not.toBe('loading'));
    expect(result.current).toEqual({
      kind: 'answers',
      next: 'default',
      nextPath: '/setup/default',
      owner: 'browser',
      unpaired: false,
    });
    expect(Loaded.shellTarget(result.current, { desktop: false, skipped: false })).toEqual({
      kind: 'path',
      to: '/setup/default',
    });
  });

  it('routes to /setup/model when team sharing is active with zero available models', async () => {
    server.state.teamOauthConnected = false;
    const { result } = renderOnboardingRoute();
    await waitFor(() => expect(result.current.kind).not.toBe('loading'));
    expect(result.current).toEqual({
      kind: 'answers',
      next: 'model',
      nextPath: '/setup/model',
      owner: 'browser',
      unpaired: false,
    });
    expect(Loaded.shellTarget(result.current, { desktop: false, skipped: false })).toEqual({
      kind: 'path',
      to: '/setup/model',
    });
  });

  it('routes to /setup/model when team sharing only has a rejected (401/unauthorized) credential', async () => {
    server.state.teamOauthConnected = false;
    server.state.userApiKeys = [
      { provider: 'openai', display_name: 'OpenAI', has_key: true, access_type: 'api_key', masked_key: '(server)' },
    ];
    server.state.availableModelsByProvider = {
      openai: { status: 'fallback', reason: 'unauthorized', ready: ['gpt-5'] },
    };
    const { result } = renderOnboardingRoute();
    await waitFor(() => expect(result.current.kind).not.toBe('loading'));
    expect(result.current).toEqual({
      kind: 'answers',
      next: 'model',
      nextPath: '/setup/model',
      owner: 'browser',
      unpaired: false,
    });
  });

  it('routes to /setup/model when team sharing only exposes models outside the 5 curated candidates', async () => {
    server.state.teamOauthConnected = false;
    server.state.userApiKeys = [
      { provider: 'openai', display_name: 'OpenAI', has_key: true, access_type: 'api_key', masked_key: '(server)' },
    ];
    server.state.availableModelsByProvider = {
      openai: { status: 'ok', ready: ['gpt-5'] },
    };
    const { result } = renderOnboardingRoute();
    await waitFor(() => expect(result.current.kind).not.toBe('loading'));
    expect(result.current).toEqual({
      kind: 'answers',
      next: 'model',
      nextPath: '/setup/model',
      owner: 'browser',
      unpaired: false,
    });
  });

  it('stays in loading while team model catalog read is still in flight, then settles', async () => {
    let releaseModels!: () => void;
    server.state.modelsPending = new Promise<void>((resolve) => {
      releaseModels = resolve;
    });

    const { result } = renderOnboardingRoute();
    // Wait until /api/v1/models has actually been requested so all other shell reads have settled.
    await waitFor(() => expect(server.state.requestedUrls).toContain('/api/v1/models'));
    expect(result.current).toEqual({ kind: 'loading' });

    releaseModels();
    await waitFor(() => expect(result.current.kind).not.toBe('loading'));
    expect(result.current).toEqual({
      kind: 'answers',
      next: 'default',
      nextPath: '/setup/default',
      owner: 'browser',
      unpaired: false,
    });
  });

  it('returns done (does not redirect shell) when reading team models fails', async () => {
    server.state.modelsError = true;
    const { result } = renderOnboardingRoute();
    await waitFor(() => expect(result.current.kind).not.toBe('loading'));
    expect(result.current).toEqual({ kind: 'done' });
    expect(Loaded.shellTarget(result.current, { desktop: false, skipped: false })).toBeNull();
  });

  it('returns done when reading configured providers fails under team sharing', async () => {
    server.state.apiKeysError = true;
    const { result } = renderOnboardingRoute();
    await waitFor(() => expect(result.current.kind).not.toBe('loading'));
    expect(result.current).toEqual({ kind: 'done' });
  });

  it('skips model catalog and team credential fetches entirely once setup is completed', async () => {
    server.state.progress.setup_completed_at = '2026-11-21T01:00:00Z';
    const { result } = renderOnboardingRoute();
    await waitFor(() => expect(result.current.kind).not.toBe('loading'));
    expect(result.current).toEqual({ kind: 'done' });
    expect(server.state.requestedUrls).not.toContain('/api/v1/models');
    expect(server.state.requestedUrls).not.toContain('/api/v1/dalton/credentials/status');
  });
});
