import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// Hoisted mutable state — vary preferences / mutation per test. Stable refs
// (rebuilt in beforeEach) keep Settings' prefs-sync effect from looping.
const h = vi.hoisted(() => ({
  platformMode: false,
  daltonAuth: false, // DALTON-HOOK(hide-experiments)
  mutateAsync: vi.fn(async (_payload: unknown) => ({})),
  user: null as Record<string, unknown> | null,
  preferences: null as Record<string, unknown> | null,
  validModelNames: new Set<string>(),
}));

vi.mock('@/config/hostMode', () => ({
  get isPlatformMode() {
    return h.platformMode;
  },
}));

// DALTON-HOOK(hide-experiments): allow toggling Dalton auth mode in Settings tests
vi.mock('@/config/daltonAuth', () => ({
  get isDaltonAuth() {
    return h.daltonAuth;
  },
}));

// DALTON-HOOK(hide-experiments): stub useFeatures so ExperimentsTab renders without network
vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => ({ data: [], isLoading: false }),
  useSetFeatureOverride: () => ({ mutateAsync: vi.fn() }),
}));

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ logout: vi.fn() }),
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: h.user, isLoading: false }),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: h.preferences, isLoading: false }),
}));

const mutationStub = { mutateAsync: h.mutateAsync };
vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => mutationStub,
}));

vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'dark', preference: 'dark', setTheme: vi.fn() }),
}));

vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => ({
    models: {},
    modelAccessMap: {},
    systemDefaults: { fallback_models: [] },
    validModelNames: h.validModelNames,
    compactionProfiles: null,
    searchProviders: null,
    isLoading: false,
  }),
}));

vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock('@/hooks/useDebouncedSave', () => ({
  useDebouncedSave: (saveFn: () => Promise<void>) => ({
    trigger: () => { setTimeout(() => { void saveFn(); }, 0); },
    flush: () => { setTimeout(() => { void saveFn(); }, 0); },
    status: 'idle',
  }),
}));

vi.mock('@/components/model/ModelTierConfig', () => ({
  ModelTierConfig: () => <div data-testid="model-tier-config-stub" />,
}));

vi.mock('@/pages/Dashboard/utils/api', () => ({
  updateCurrentUser: vi.fn(async () => ({})),
  clearPreferences: vi.fn(async () => ({})),
  uploadAvatar: vi.fn(async () => ({ avatar_url: '' })),
  getUserApiKeys: vi.fn(async () => ({ providers: [] })),
  initiateCodexDevice: vi.fn(async () => ({})),
  pollCodexDevice: vi.fn(async () => ({})),
  getCodexOAuthStatus: vi.fn(async () => ({ connected: false })),
  disconnectCodexOAuth: vi.fn(async () => ({})),
  initiateClaudeOAuth: vi.fn(async () => ({})),
  submitClaudeCallback: vi.fn(async () => ({})),
  getClaudeOAuthStatus: vi.fn(async () => ({ connected: false })),
  disconnectClaudeOAuth: vi.fn(async () => ({})),
}));

vi.mock('@/api/tradingPermission', () => ({
  getTradingPermission: vi.fn(async () => ({
    level: 'approve_each',
    agreement_version: 1,
    agreed_at: null,
    updated_at: null,
  })),
  updateTradingPermission: vi.fn(async () => ({})),
}));

vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getHomeWorkspace: vi.fn(async () => ({ workspace_id: 'ws-home' })),
}));

// Onboarding — Settings renders replay/reset buttons; no provider in this harness.
vi.mock('@/pages/Onboarding', () => ({
  useOnboarding: () => ({ replayGuides: vi.fn(), resetOnboarding: vi.fn() }),
}));

// Import after mocks are registered.
import Settings from '../Settings';

function renderPreferencesTab() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/settings?tab=preferences']}>
        <Settings />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  h.platformMode = false;
  h.daltonAuth = false; // DALTON-HOOK(hide-experiments)
  h.validModelNames = new Set<string>();
  h.mutateAsync.mockClear();
  h.mutateAsync.mockResolvedValue({});
});

function setupAndRender(agentPreference: Record<string, unknown> = {}) {
  h.user = {
    id: 'u-1',
    email: 'tester@example.com',
    name: 'Tester',
    onboarding_completed: true,
  };
  h.preferences = { agent_preference: agentPreference };
  return renderPreferencesTab();
}

describe('Settings — Output format', () => {
  it('defaults to Default (Markdown) when output_format is absent', async () => {
    setupAndRender({});

    const defaultBtn = await screen.findByRole('button', { name: 'Default' });
    const htmlBtn = screen.getByRole('button', { name: 'HTML' });
    // The active segment uses the accent color; the inactive one uses tertiary.
    expect(defaultBtn).toHaveStyle({ color: 'var(--color-accent-primary)' });
    expect(htmlBtn).toHaveStyle({ color: 'var(--color-text-tertiary)' });
  });

  it('reflects the saved html value as the active segment', async () => {
    setupAndRender({ output_format: 'html' });

    const defaultBtn = await screen.findByRole('button', { name: 'Default' });
    const htmlBtn = screen.getByRole('button', { name: 'HTML' });
    expect(htmlBtn).toHaveStyle({ color: 'var(--color-accent-primary)' });
    expect(defaultBtn).toHaveStyle({ color: 'var(--color-text-tertiary)' });
  });

  it('selecting HTML saves output_format: "html" through updatePreferences', async () => {
    setupAndRender({});

    const htmlBtn = await screen.findByRole('button', { name: 'HTML' });
    fireEvent.click(htmlBtn);

    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalled());
    const payload = h.mutateAsync.mock.calls.at(-1)![0] as {
      agent_preference: Record<string, unknown>;
    };
    expect(payload.agent_preference).toMatchObject({ output_format: 'html' });
  });

  it('selecting Default writes output_format: null to delete the key', async () => {
    setupAndRender({ output_format: 'html' });

    const defaultBtn = await screen.findByRole('button', { name: 'Default' });
    fireEvent.click(defaultBtn);

    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalled());
    const payload = h.mutateAsync.mock.calls.at(-1)![0] as {
      agent_preference: Record<string, unknown>;
    };
    expect(payload.agent_preference.output_format).toBeNull();
  });

  it('preserves other agent_preference keys when changing output format', async () => {
    setupAndRender({ tone: 'concise' });

    const htmlBtn = await screen.findByRole('button', { name: 'HTML' });
    fireEvent.click(htmlBtn);

    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalled());
    const payload = h.mutateAsync.mock.calls.at(-1)![0] as {
      agent_preference: Record<string, unknown>;
    };
    expect(payload.agent_preference).toMatchObject({ tone: 'concise', output_format: 'html' });
  });

  it('does not duplicate output_format as a read-only row', async () => {
    setupAndRender({ output_format: 'html' });

    await screen.findByRole('button', { name: 'HTML' });
    // The generic key/value loop renders rows like "Output Format:"; the
    // dedicated control replaces it, so that raw row must not appear.
    expect(screen.queryByText('Output Format:')).not.toBeInTheDocument();
  });

  // DALTON-HOOK(hide-trading): TradingPermissionSection is hidden while AutoApproveSettings stays
  it('hides TradingPermissionSection in Preferences while keeping Approvals (AutoApproveSettings)', async () => {
    setupAndRender({});

    await screen.findByRole('button', { name: 'Default' });
    expect(screen.getByText('Approvals')).toBeInTheDocument();
    expect(screen.queryByText('Trading permissions')).not.toBeInTheDocument();
    expect(screen.queryByRole('radiogroup', { name: 'Trading permissions' })).not.toBeInTheDocument();
  });

  // DALTON-HOOK(hide-experiments): Experiments tab is hidden in Dalton mode and falls back to default tab
  it('hides Experiments tab button and ExperimentsTab in Dalton mode and falls back to userInfo on ?tab=experiments', async () => {
    h.daltonAuth = true;
    h.user = {
      id: 'u-1',
      email: 'tester@example.com',
      name: 'Tester',
      onboarding_completed: true,
    };
    h.preferences = { agent_preference: {} };

    // 1. Direct URL ?tab=experiments falls back to default 'userInfo' tab
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/settings?tab=experiments']}>
          <Settings />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.queryByRole('button', { name: /Experiments|实验功能/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/Early-access features still in development/i)).not.toBeInTheDocument();
    expect(await screen.findByDisplayValue('Tester')).toBeInTheDocument();
    unmount();
  });
});
