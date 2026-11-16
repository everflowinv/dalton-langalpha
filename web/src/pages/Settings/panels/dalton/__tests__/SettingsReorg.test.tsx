import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Settings from '../../../Settings';
import { LEGACY_ANCHORS, readSection, sectionShown, searchableSettings, settingAt } from '../../../sections';
import { DaltonBudgetCard } from '../DaltonBudgetCard';

const h = vi.hoisted(() => ({
  isDaltonAuth: true,
  callerTier: null as string | null,
  callerTierPending: false,
  callerTierError: false,
  isPm: false,
  dailyCapUsd: 25 as number,
  coverageEnabled: false,
  coveragePending: false,
  workspaces: [
    { workspace_id: 'ws-semi', name: 'Semiconductors', is_home: false },
    { workspace_id: 'ws-home', name: 'Home', is_home: true },
  ],
  upsertCap: vi.fn<(p: unknown) => Promise<void>>(),
  deleteCap: vi.fn<(p: { scopeKind: string; scopeId: string }) => Promise<void>>(),
  preferences: {
    risk_preference: { risk_level: 'balanced' },
    investment_preference: { horizon: 'medium_term', focus_custom: 'ai' },
    model_preference: {},
    other_preference: {},
  } as Record<string, unknown>,
}));

vi.mock('@/config/daltonAuth', () => ({
  get isDaltonAuth() {
    return h.isDaltonAuth;
  },
}));

vi.mock('@/config/hostMode', () => ({
  isPlatformMode: false,
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({
    user: { id: 'u-1', email: 'analyst@dalton.test', name: 'Analyst', timezone: 'Asia/Shanghai' },
    isLoading: false,
  }),
}));

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 'u-1', email: 'analyst@dalton.test' },
    signOut: vi.fn(async () => undefined),
  }),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: h.preferences,
    isLoading: false,
    isLoaded: true,
  }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  PREFERENCE_MUTATION_KEY: ['prefs'],
  useUpdatePreferences: () => ({ mutateAsync: vi.fn(async () => ({})), isPending: false }),
  useClearPreferences: () => ({ mutateAsync: vi.fn(async () => undefined), isPending: false }),
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  TEAM_SHARED_CREDENTIALS_HINT: '',
  useTeamCredentials: () => ({
    isShared: true,
    canManage: h.callerTier === 'admin' || (!h.isDaltonAuth && h.isPm),
    canDisconnect: h.callerTier === 'admin' || (!h.isDaltonAuth && h.isPm),
    isPm: h.callerTier === 'admin' || h.isPm,
    canInvite: h.callerTier === 'admin' || h.callerTier === 'internal',
    callerTier: h.callerTier,
    callerTierPending: h.callerTierPending,
    callerTierError: h.callerTierError,
  }),
}));

vi.mock('@/lib/dalton/coverage', () => ({
  useDaltonCoverageUnits: () => ({
    data: h.coverageEnabled ? { enabled: true, units: [], unclassified: [] } : { enabled: false },
    isLoading: h.coveragePending,
    isFetching: false,
    isError: false,
    refetch: vi.fn(async () => ({ data: { enabled: h.coverageEnabled } })),
  }),
  useDaltonCoverageUnitDetail: () => ({ data: undefined, isLoading: false, isError: false }),
  splitCoverageUnits: () => ({ sectors: [], standaloneCompanies: [] }),
  coverageUnitErrorCode: () => null,
}));

vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: () => ({ data: { workspaces: h.workspaces } }),
}));

vi.mock('@/hooks/useAllWorkspaces', () => ({
  useAllWorkspaces: () => ({ data: { workspaces: h.workspaces, total: h.workspaces.length } }),
}));

vi.mock('../../../hooks/useOpenClaw', () => ({
  useNativeModelsStatus: () => ({ data: null, isLoading: false, isError: false }),
  useNativeModelsCheck: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useNativeModelsSync: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useNativeModelsRetire: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useNativeModelsRestore: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDaltonBudget: () => ({
    data: {
      daily_cap_usd: h.dailyCapUsd,
      default_task_cap_usd: 5,
      timezone: 'Asia/Shanghai',
      configured: true,
      task_caps: [
        { scope_kind: 'workspace', scope_id: 'ws-semi', scope_name: 'Semiconductors', cap_usd: 10 },
      ],
    },
    isLoading: false,
    isFetching: false,
    isError: false,
    refetch: vi.fn(),
  }),
  useDaltonBudgetToday: () => ({
    data: {
      day: '2026-11-16',
      cap_usd: h.dailyCapUsd,
      settled_usd: 1.5,
      open_reserved_usd: 0,
      expired_conservative_usd: 0,
      unreserved_usage_usd: 0,
      spent_usd: 1.5,
      remaining_usd: Math.max(0, h.dailyCapUsd - 1.5),
      subscription_notional_usd: 0.4,
      budget_gate_enabled: true,
      runs: [],
    },
    isLoading: false,
    isFetching: false,
    isError: false,
    refetch: vi.fn(),
  }),
  useUpdateDaltonBudget: () => ({ mutateAsync: vi.fn(async () => ({})), isPending: false }),
  useUpsertDaltonTaskCap: () => ({
    mutateAsync: (p: unknown) => h.upsertCap(p),
    isPending: false,
  }),
  useDeleteDaltonTaskCap: () => ({
    mutateAsync: (p: { scopeKind: string; scopeId: string }) => h.deleteCap(p),
    isPending: false,
  }),
  useDaltonSystemJobs: () => ({ data: { ok: true, jobs: [] }, isLoading: false, isError: false }),
  useToggleDaltonSystemJob: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRunDaltonSystemJobNow: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('../DaltonTeamModelsPanel', () => ({
  DaltonTeamModelsPanel: () => (
    <div data-testid="mounted-team-models-panel">Team Models Mounted</div>
  ),
}));

vi.mock('@/pages/ChatAgent/components/ComputersDialogHost', () => ({
  default: () => null,
}));

function LocationSpy() {
  const loc = useLocation();
  return <div data-testid="location-spy">{`${loc.pathname}${loc.search}${loc.hash}`}</div>;
}

function renderSettings(initialUrl: string, client?: QueryClient) {
  const qc =
    client ??
    new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialUrl]}>
        <Routes>
          <Route
            path="/settings"
            element={
              <>
                <LocationSpy />
                <Settings />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Settings reorganization (groups, minTier guards, legacy links, dropdowns)', () => {
  beforeEach(() => {
    h.isDaltonAuth = true;
    h.callerTier = 'admin';
    h.callerTierPending = false;
    h.callerTierError = false;
    h.isPm = false;
    h.dailyCapUsd = 25;
    h.coverageEnabled = false;
    h.coveragePending = false;
    h.workspaces = [
      { workspace_id: 'ws-semi', name: 'Semiconductors', is_home: false },
      { workspace_id: 'ws-home', name: 'Home', is_home: true },
    ];
    h.upsertCap.mockReset().mockResolvedValue(undefined);
    h.deleteCap.mockReset().mockResolvedValue(undefined);
  });

  it('filters rail sections and search results uniformly by minTier and coverageUnitsEnabled', () => {
    expect(sectionShown('teamModels', { callerTier: 'admin' })).toBe(true);
    expect(sectionShown('teamModels', { callerTier: 'internal' })).toBe(false);
    expect(sectionShown('teamMembers', { callerTier: 'internal' })).toBe(true);
    expect(sectionShown('teamMembers', { callerTier: 'external' })).toBe(false);
    expect(sectionShown('teamChannels', { callerTier: null })).toBe(false);
    expect(sectionShown('coverage', { callerTier: 'internal', coverageUnitsEnabled: false })).toBe(false);
    expect(sectionShown('coverage', { callerTier: 'internal', coverageUnitsEnabled: true })).toBe(true);

    const internalSearch = searchableSettings(false, { callerTier: 'internal' }).map((s) => s.anchor);
    expect(internalSearch).not.toContain('dalton-providers');
    expect(internalSearch).toContain('dalton-invites');
    expect(internalSearch).toContain('dalton-system-jobs');

    const failClosedSearch = searchableSettings(false, { callerTier: null }).map((s) => s.anchor);
    expect(failClosedSearch).not.toContain('dalton-invites');
    expect(failClosedSearch).not.toContain('dalton-system-jobs');
    expect(failClosedSearch).not.toContain('dalton-providers');
  });

  it('holds skeleton while callerTierPending is true without kicking user or dropping hash', () => {
    h.callerTier = null;
    h.callerTierPending = true;

    renderSettings('/settings?tab=teamModels#dalton-providers');

    expect(screen.getByTestId('location-spy')).toHaveTextContent(
      '/settings?tab=teamModels#dalton-providers',
    );
    expect(screen.queryByTestId('mounted-team-models-panel')).not.toBeInTheDocument();
  });

  it('shows admin-required notice, never mounts admin panel, and replace-redirects to account when unauthorized', async () => {
    h.callerTier = 'internal';
    h.callerTierPending = false;

    renderSettings('/settings?tab=teamModels#dalton-providers');

    expect(screen.queryByTestId('mounted-team-models-panel')).not.toBeInTheDocument();
    expect(await screen.findByTestId('settings-permission-denied')).toHaveTextContent(
      /Admin permission required|需要管理员权限/,
    );
    await waitFor(() => {
      expect(screen.getByTestId('location-spy')).toHaveTextContent('/settings?tab=account');
    });
  });

  it('resolves #connected-accounts, #dalton-weixin-channel, and legacy merged tabs cleanly', () => {
    expect(LEGACY_ANCHORS['connected-accounts']).toEqual({
      section: 'model',
      targetAnchor: 'model-access',
    });
    expect(LEGACY_ANCHORS['dalton-weixin-channel']).toEqual({
      section: 'integrations',
      targetAnchor: 'dalton-weixin-bind',
    });
    expect(settingAt('#connected-accounts')?.section).toBe('model');
    expect(settingAt('#dalton-weixin-channel')?.section).toBe('integrations');
    expect(readSection(new URLSearchParams('tab=appearance'), '')).toBe('general');
    expect(readSection(new URLSearchParams('tab=desktop'), '')).toBe('general');
    expect(readSection(new URLSearchParams('tab=agent'), '')).toBe('chat');
    expect(readSection(new URLSearchParams('tab=plan'), '')).toBe('budget');
  });

  it('fails closed in rail and route guard on callerTierError even when userSharing reports admin', async () => {
    h.isDaltonAuth = true;
    h.callerTier = 'internal';
    h.callerTierError = true;
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    qc.setQueryData(['dalton', 'userSharing'], {
      cos_rw_enabled: true,
      caller_tier: 'admin',
      colleague_dispatch_enabled: true,
    });

    renderSettings('/settings?tab=teamModels#dalton-providers', qc);

    expect(screen.queryByTestId('mounted-team-models-panel')).not.toBeInTheDocument();
    expect(await screen.findByTestId('settings-permission-denied')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId('location-spy')).toHaveTextContent('/settings?tab=account');
    });
  });

  it('preserves admin & coverage visibility in OSS mode while hiding daltonAuth-only Team Members', async () => {
    h.isDaltonAuth = false;
    h.callerTier = null;
    h.isPm = true;
    h.coverageEnabled = true;

    expect(sectionShown('teamMembers', { callerTier: 'admin' })).toBe(false);
    expect(sectionShown('teamModels', { callerTier: 'admin' })).toBe(true);
    expect(sectionShown('teamChannels', { callerTier: 'admin' })).toBe(true);
    expect(sectionShown('coverage', { callerTier: 'admin', coverageUnitsEnabled: true })).toBe(true);

    renderSettings('/settings?tab=teamModels#dalton-providers');
    expect(await screen.findByTestId('mounted-team-models-panel')).toBeInTheDocument();
  });

  it('shows Dalton Team Edition notice rather than internal-member prompt when OSS user visits teamMembers', async () => {
    h.isDaltonAuth = false;
    h.callerTier = null;
    h.isPm = true;

    renderSettings('/settings?tab=teamMembers');

    const banner = await screen.findByTestId('settings-permission-denied');
    expect(banner).toHaveTextContent(/Everflow 团队版|Everflow Team Edition/);
    expect(banner).not.toHaveTextContent(/Team member permission required|需要团队成员权限/);
    await waitFor(() => {
      expect(screen.getByTestId('location-spy')).toHaveTextContent('/settings?tab=account');
    });
  });

  it('translates known investing profile preference keys while falling back on custom keys', async () => {
    renderSettings('/settings?tab=preferences');
    expect(await screen.findByText('Risk level')).toBeInTheDocument();
    expect(screen.getByText('Investment horizon')).toBeInTheDocument();
    expect(screen.getByText('Focus Custom')).toBeInTheDocument();
  });

  it('renders DaltonBudgetCard workspace dropdown excluding Home and confirms before deleting a cap', async () => {
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <DaltonBudgetCard />
      </QueryClientProvider>,
    );

    const select = (await screen.findByTestId(
      'dalton-budget-workspace-select',
    )) as HTMLSelectElement;
    const optionTexts = [...select.options].map((o) => o.textContent);
    expect(optionTexts).toContain('Semiconductors');
    expect(optionTexts).not.toContain('Home');

    fireEvent.click(screen.getByTestId('dalton-budget-cap-delete-ws-semi'));
    expect(h.deleteCap).not.toHaveBeenCalled();

    const confirmButtons = await screen.findAllByRole('button', { name: /Delete|删除/ });
    fireEvent.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() => {
      expect(h.deleteCap).toHaveBeenCalledWith({
        scopeKind: 'workspace',
        scopeId: 'ws-semi',
      });
    });
  });

  it('renders $0.00 (never ∞) for 0 daily cap and enforces positive task caps', async () => {
    h.dailyCapUsd = 0;
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <DaltonBudgetCard />
      </QueryClientProvider>,
    );

    const cashToday = await screen.findByTestId('dalton-budget-cash-today');
    expect(cashToday).toHaveTextContent('$1.50 / $0.00');
    expect(cashToday).not.toHaveTextContent('∞');

    // Empty workspace cap shows dedicated empty error
    fireEvent.change(screen.getByTestId('dalton-budget-workspace-select'), {
      target: { value: 'ws-semi' },
    });
    fireEvent.click(screen.getByTestId('dalton-budget-workspace-cap-save'));
    expect(await screen.findByText(/Enter a workspace cap in USD/)).toBeInTheDocument();

    // Non-positive workspace cap is rejected
    fireEvent.change(screen.getByTestId('dalton-budget-workspace-cap-input'), {
      target: { value: '0' },
    });
    fireEvent.click(screen.getByTestId('dalton-budget-workspace-cap-save'));
    expect(await screen.findByText(/greater than 0/)).toBeInTheDocument();
    expect(h.upsertCap).not.toHaveBeenCalled();
  });

  it('populates DaltonBudgetCard workspace selector beyond 100 workspaces via useAllWorkspaces', async () => {
    h.workspaces = Array.from({ length: 125 }, (_, i) => ({
      workspace_id: `ws-${i + 1}`,
      name: `Workspace ${i + 1}`,
      is_home: false,
    }));
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <DaltonBudgetCard />
      </QueryClientProvider>,
    );

    const select = (await screen.findByTestId(
      'dalton-budget-workspace-select',
    )) as HTMLSelectElement;
    const optionTexts = [...select.options].map((o) => o.textContent);
    expect(optionTexts).toContain('Workspace 1');
    expect(optionTexts).toContain('Workspace 125');
    expect(select.options.length).toBeGreaterThan(100);
  });
});
