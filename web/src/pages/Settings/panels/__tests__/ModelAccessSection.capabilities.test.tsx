import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import type { ByokProvider, ProviderCatalogEntry } from '@/components/model/types';

const getOAuthStatusSpy = vi.fn();
const apiGetMock = vi.fn();

const REALISTIC_CATALOG: ProviderCatalogEntry[] = [
  {
    provider: 'openai',
    display_name: 'OpenAI',
    brand_key: 'openai',
    group: 'openai',
    group_display_name: 'OpenAI',
    category: 'closed_source',
    access_type: 'api_key',
    region: 'intl',
    base_url: 'https://api.openai.com/v1',
    byok_eligible: true,
  },
  {
    provider: 'z-ai',
    display_name: 'Zhipu AI',
    brand_key: 'z-ai',
    group: 'z-ai',
    group_display_name: 'Zhipu AI',
    category: 'open_source',
    access_type: 'api_key',
    region: 'intl',
    base_url: 'https://api.z.ai/api/paas/v4',
    byok_eligible: true,
  },
  {
    provider: 'z-ai-coding',
    display_name: 'GLM Coding Plan',
    brand_key: 'z-ai',
    group: 'z-ai',
    group_display_name: 'Zhipu AI',
    category: 'open_source',
    access_type: 'coding_plan',
    region: 'intl',
    base_url: 'https://api.z.ai/api/coding/paas/v4',
    byok_eligible: true,
    region_variants: [
      {
        provider: 'z-ai-cn-coding',
        display_name: 'GLM Coding Plan',
        region: 'cn',
        base_url: 'https://open.bigmodel.cn/api/coding/paas/v4',
      },
    ],
  },
];

let configuredApiKeyProviders: ByokProvider[] = [];

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      get: (...args: unknown[]) => apiGetMock(...args),
    },
  };
});

vi.mock('@/api/oauth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/oauth')>();
  return {
    ...actual,
    getOAuthStatus: (...args: unknown[]) => {
      getOAuthStatusSpy(...args);
      return Promise.resolve({ connected: false, state: 'disconnected', can_sign_in: true });
    },
  };
});

vi.mock('@/hooks/useApiKeys', () => ({
  useApiKeys: () => ({
    apiKeys: { providers: configuredApiKeyProviders, byok_enabled: true },
    isLoading: false,
    isError: false,
  }),
}));

vi.mock('@/hooks/useModels', () => ({
  useModels: () => ({
    models: { provider_catalog: REALISTIC_CATALOG, models: {} },
    isLoading: false,
  }),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: null,
    isLoading: false,
    isLoaded: true,
    unread: false,
    retry: vi.fn(),
  }),
}));

vi.mock('@/hooks/useProviderModels', () => ({
  useAvailableModels: () => ({
    data: { status: 'ok', ready: [] },
    isLoading: false,
  }),
}));

vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => ({
    metadata: {},
    allModels: [],
    isLoading: false,
  }),
}));

vi.mock('@/hooks/useClaudeCode', () => ({
  useClaudeCodeSetup: () => ({ setup: null, unread: false }),
  useClaudeCodeStatus: () => ({ enabled: false, runsHere: false, state: 'off', email: null, plan: null }),
}));

vi.mock('@/hooks/useClaudeCodeCustody', () => ({
  useClaudeCodeCustody: () => ({ custody: null, isLoading: false, unread: false }),
}));

vi.mock('@/hooks/useWorkspaceClaudeCode', () => ({
  useWorkspaceClaudeCode: () => ({ known: true, computerId: null }),
}));

vi.mock('@/hooks/useClaudeCodeSteps', () => ({
  stepsMissing: () => false,
  useClaudeCodeSteps: () => ({ status: 'none' }),
}));

vi.mock('@/hooks/useClaudeCodePlan', () => ({
  usePlanClock: () => ({ locale: 'en-US', zone: 'UTC', now: Date.now() }),
}));

vi.mock('@/components/modelAccess/tokenLine', () => ({
  linkedSheetAction: () => null,
  useClaudeCodeTokenRows: () => [],
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  TEAM_SHARED_CREDENTIALS_HINT: 'team-hint',
  useTeamCredentials: () => ({
    isShared: false,
    canDisconnect: true,
    canManage: true,
    callerTierPending: false,
    callerTierError: false,
  }),
}));

vi.mock('@/hooks/useAccount', () => ({
  useAccount: () => ({ policy: null }),
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: { access_tier: -1 }, isLoading: false }),
}));

vi.mock('@/pages/Setup/answers/useTeamModels', () => ({
  useDaltonTeamModels: () => ({
    empty: false,
    canManage: true,
    available: true,
    shared: false,
    unread: false,
    all: { unread: false, isLoading: false },
    retry: vi.fn(),
  }),
}));

afterEach(() => {
  configuredApiKeyProviders = [];
  getOAuthStatusSpy.mockReset();
  apiGetMock.mockReset();
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe('ModelAccessSection, MethodStep & SubscriptionSummary native_oauth_sync gating', () => {
  it('hides OAuth subscriptions in Core while keeping API-key coding plans visible and selectable without calling OAuth status', async () => {
    vi.stubEnv('VITE_DALTON_PROFILE', 'core');
    apiGetMock.mockResolvedValue({
      data: { profile: 'core', capabilities: [] },
    });
    configuredApiKeyProviders = [
      {
        provider: 'z-ai-coding',
        display_name: 'GLM Coding Plan',
        access_type: 'coding_plan',
        brand_key: 'z-ai',
        has_key: true,
        masked_key: 'sk-...1234',
        base_url: 'https://api.z.ai/api/coding/paas/v4',
      },
    ];
    vi.resetModules();
    const { renderWithProviders } = await import('@/test/utils');
    const { ModelAccessSection } = await import('../ModelAccessSection');
    const { default: MethodStep } = await import('@/pages/Setup/steps/MethodStep');
    const { SubscriptionSummary } = await import('@/components/modelAccess/SubscriptionSummary');

    const { unmount } = renderWithProviders(<ModelAccessSection />);
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith('/api/v1/dalton/capabilities');
    });

    // OAuth subscription section & button stay hidden in Core
    expect(screen.queryByText(/Connect a subscription|连接订阅/i)).toBeNull();
    expect(screen.queryByText(/^Subscriptions$|^订阅$/i)).toBeNull();

    // Configured API-key coding plan (GLM Coding Plan) remains visible in Settings
    expect(screen.getByText('GLM Coding Plan')).toBeInTheDocument();

    // Opening "Add API key" offers GLM Coding Plan from the real provider catalog
    const addKeyButton = screen.getByRole('button', { name: /Add API key|添加 API 密钥/i });
    fireEvent.click(addKeyButton);
    expect(screen.getAllByText('GLM Coding Plan').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('OpenAI')).toBeInTheDocument();
    expect(getOAuthStatusSpy).not.toHaveBeenCalled();
    unmount();

    const { container, unmount: unmountMethod } = renderWithProviders(<MethodStep />);
    expect(screen.queryByText(/Use a subscription|使用订阅/i)).toBeNull();
    // The connected API-key provider keeps its logo; no image may sit in a subscription row.
    for (const img of Array.from(container.querySelectorAll('img'))) {
      expect(img.closest('button, label, li, div')?.textContent ?? '').not.toMatch(/subscription|订阅/i);
    }
    expect(screen.getByText(/I have an API key|我有 API 密钥/i)).toBeInTheDocument();
    expect(getOAuthStatusSpy).not.toHaveBeenCalled();
    unmountMethod();

    // Wizard SubscriptionSummary skips OAuth status queries in Core
    renderWithProviders(<SubscriptionSummary provider="codex-oauth" />);
    expect(getOAuthStatusSpy).not.toHaveBeenCalled();
  });

  it('keeps Subscriptions group, Connect Subscription button, MethodStep OAuth, and OAuth queries unchanged in Everflow even when native_oauth_sync is false', async () => {
    vi.stubEnv('VITE_DALTON_PROFILE', 'everflow');
    apiGetMock.mockResolvedValue({
      data: {
        profile: 'everflow',
        capabilities: [], // native_oauth_sync=false
      },
    });
    vi.resetModules();
    const { renderWithProviders } = await import('@/test/utils');
    const { ModelAccessSection } = await import('../ModelAccessSection');
    const { default: MethodStep } = await import('@/pages/Setup/steps/MethodStep');

    const { unmount } = renderWithProviders(<ModelAccessSection />);
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith('/api/v1/dalton/capabilities');
    });
    expect(screen.getByText(/^Subscriptions$|^订阅$/i)).toBeInTheDocument();
    expect(screen.getByText(/Connect a subscription|连接订阅/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(getOAuthStatusSpy).toHaveBeenCalled();
    });

    // In Everflow, Add API key keeps coding plans in Subscriptions rather than duplicating into API key picker
    fireEvent.click(screen.getByRole('button', { name: /Add API key|添加 API 密钥/i }));
    expect(screen.getByText('OpenAI')).toBeInTheDocument();
    expect(screen.queryByText('GLM Coding Plan')).toBeNull();
    unmount();

    const { container } = renderWithProviders(<MethodStep />);
    await waitFor(() => {
      expect(screen.getByText(/Use a subscription|使用订阅/i)).toBeInTheDocument();
    });
    expect(container.querySelectorAll('img').length).toBeGreaterThan(0);
  });
});
