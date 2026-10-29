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
  canInvite: false, // DALTON-HOOK(settings-section)
  mutateAsync: vi.fn(async (_payload: unknown) => ({})),
  user: null as Record<string, unknown> | null,
  preferences: null as Record<string, unknown> | null,
  validModelNames: new Set<string>(),
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  TEAM_SHARED_CREDENTIALS_HINT: '',
  useTeamCredentials: () => ({
    isShared: true,
    canDisconnect: h.canInvite,
    canManage: h.canInvite,
    isPm: h.canInvite,
    canInvite: h.canInvite,
  }),
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
    payers: {},
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
import { queryKeys } from '@/lib/queryKeys';
import Settings from '../Settings';
import { readSection, searchableSettings, sectionShown } from '../sections';

function renderPreferencesTab(queryClient?: QueryClient) {
  const client = queryClient ?? new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/settings?tab=agent']}>
        <Settings />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  h.platformMode = false;
  h.daltonAuth = false; // DALTON-HOOK(hide-experiments)
  h.canInvite = false; // DALTON-HOOK(settings-section)
  h.validModelNames = new Set<string>();
  h.mutateAsync.mockClear();
  h.mutateAsync.mockResolvedValue({});
});

function setupAndRender(
  agentPreference: Record<string, unknown> = {},
  queryClient?: QueryClient,
) {
  h.user = {
    id: 'u-1',
    email: 'tester@example.com',
    name: 'Tester',
    onboarding_completed: true,
  };
  h.preferences = { agent_preference: agentPreference };
  return renderPreferencesTab(queryClient);
}

const lastWrite = () => (h.mutateAsync.mock.calls.at(-1)![0] as { agent_preference: Record<string, unknown> }).agent_preference;
const ranked = () => screen.getAllByTestId(/^output-format-/).map((row) => row.dataset.testid!.replace('output-format-', ''));

describe('Settings: output format', () => {
  it('shows the forms in the server order, left to the agent, when none is ranked', async () => {
    setupAndRender();
    await screen.findByText('Output format');
    expect(ranked()).toEqual(['visual', 'markdown', 'text']);
    expect(screen.getByText('The agent decides for now. Reorder to set your preference.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Let the agent decide' })).not.toBeInTheDocument();
  });

  it('shows a saved ranking in its order', async () => {
    setupAndRender({ output_format: ['text', 'visual', 'markdown'] });
    await screen.findByText('Output format');
    expect(ranked()).toEqual(['text', 'visual', 'markdown']);
  });

  it('fades the forms a partial ranking leaves out, which the agent gets no rank for', async () => {
    setupAndRender({ output_format: ['text'] });
    await screen.findByText('Output format');
    expect(ranked()).toEqual(['text', 'visual', 'markdown']);
    expect(screen.getAllByTestId(/^output-format-/).map((row) => row.dataset.ranked === 'true')).toEqual([true, false, false]);
  });

  it('reads a row from before the ranking as the server migrated it', async () => {
    setupAndRender({ output_format: 'html', deliverable: 'text' });
    await screen.findByText('Output format');
    expect(ranked()).toEqual(['visual', 'markdown', 'text']);
    expect(screen.getByRole('button', { name: 'Let the agent decide' })).toBeInTheDocument();
  });

  it('reads a list with a form outside the ranking as the user\'s words, not a ranking', async () => {
    setupAndRender({ output_format: ['visual', 'pdf'] });
    expect(await screen.findByText('visual, pdf')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Let the agent decide' })).not.toBeInTheDocument();
  });

  it('saves the whole order when a form moves, with only the keys it owns', async () => {
    setupAndRender({ tone: 'terse' });
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chat up' }));
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1));
    expect(lastWrite()).toEqual({ output_format: ['visual', 'text', 'markdown'], deliverable: null });
  });

  it('cannot move the first form up or the last one down', async () => {
    setupAndRender({ output_format: ['visual', 'markdown', 'text'] });
    expect(await screen.findByRole('button', { name: 'Move Visual up' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Move Chat down' })).toBeDisabled();
  });

  it('hands the choice back to the agent', async () => {
    setupAndRender({ output_format: ['markdown', 'text', 'visual'] });
    fireEvent.click(await screen.findByRole('button', { name: 'Let the agent decide' }));
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1));
    expect(lastWrite()).toEqual({ output_format: null, deliverable: null });
  });
});

describe('Settings: research depth and reaching out', () => {
  it('shows each saved choice with its own line, and Agent decides when there is none', async () => {
    setupAndRender({ research_depth: 'thorough' });
    const depth = (await screen.findByRole('combobox', { name: 'Research depth' })) as HTMLSelectElement;
    expect(depth.value).toBe('thorough');
    expect(screen.getByText('Data and evidence checked before every answer. Takes longer and uses more credits.')).toBeInTheDocument();
    const reaching = screen.getByRole('combobox', { name: 'Reaching out' }) as HTMLSelectElement;
    expect(reaching.value).toBe('');
    expect(screen.getByText('When the agent gets in touch without being asked.')).toBeInTheDocument();
  });

  it('writes a pick, and null for Agent decides', async () => {
    setupAndRender({ proactivity: 'proactive' });
    fireEvent.change(await screen.findByRole('combobox', { name: 'Research depth' }), { target: { value: 'light' } });
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1));
    expect(lastWrite()).toEqual({ research_depth: 'light' });
    fireEvent.change(screen.getByRole('combobox', { name: 'Reaching out' }), { target: { value: '' } });
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(2));
    expect(lastWrite()).toEqual({ proactivity: null });
  });
});

describe("Settings: the user's instructions", () => {
  it('lists the keys the agent reads as the user\'s words, and none of the ones that steer it', async () => {
    setupAndRender({
      tone: 'plain words',
      research_depth: 'deep dives please',
      proactivity: 'on_request',
      output_format: ['visual'],
      avoid: ['jargon', 'emoji'],
    });
    expect(await screen.findByText('Your instructions')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Tone' })).toHaveValue('plain words');
    // A value outside its vocabulary reaches the agent as written, so it is one
    // too, but only to remove: the server refuses words as an edit to it.
    expect(screen.getByText('deep dives please')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Research depth' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Remove Research depth' })).toBeInTheDocument();
    expect(screen.getByText('jargon, emoji')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove Proactivity' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove Output format' })).not.toBeInTheDocument();
  });

  it('lists an old deliverable beside an output format in the user\'s words, which the agent reads as written', async () => {
    setupAndRender({ output_format: 'pdf', deliverable: 'visual' });
    expect(await screen.findByRole('button', { name: 'Remove Deliverable' })).toBeInTheDocument();
    expect(screen.getByText('visual')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Let the agent decide' })).not.toBeInTheDocument();
  });

  it('lists an old deliverable beside a list in the user\'s words too', async () => {
    setupAndRender({ output_format: ['visual', 'pdf'], deliverable: 'text' });
    expect(await screen.findByRole('button', { name: 'Remove Deliverable' })).toBeInTheDocument();
  });

  it('saves an edit on leaving the field, and removes one with its button', async () => {
    setupAndRender({ tone: 'plain words', avoid: ['jargon'] });
    const tone = await screen.findByRole('textbox', { name: 'Tone' });
    fireEvent.change(tone, { target: { value: 'terse' } });
    fireEvent.blur(tone);
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1));
    expect(lastWrite()).toEqual({ tone: 'terse' });
    fireEvent.click(screen.getByRole('button', { name: 'Remove Avoid' }));
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(2));
    expect(lastWrite()).toEqual({ avoid: null });
  });

  it('puts an emptied instruction back rather than saving it blank', async () => {
    setupAndRender({ tone: 'plain words' });
    const tone = await screen.findByRole('textbox', { name: 'Tone' });
    fireEvent.change(tone, { target: { value: '  ' } });
    fireEvent.blur(tone);
    expect(tone).toHaveValue('plain words');
    expect(h.mutateAsync).not.toHaveBeenCalled();
  });

  it('draws no group when there are none', async () => {
    setupAndRender({ research_depth: 'light', output_format: ['text'] });
    await screen.findByText('Output format');
    expect(screen.queryByText('Your instructions')).not.toBeInTheDocument();
  });

  // DALTON-HOOK(hide-trading): TradingPermissionSection is hidden while AutoApproveSettings stays
  it('hides TradingPermissionSection in Preferences while keeping Approvals (AutoApproveSettings)', async () => {
    setupAndRender({});

    await screen.findByText('Output format');
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

  // DALTON-HOOK(hide-trading): trading and experiments stay closed in registry, search, and deep links
  it('closes trading and experiments across registry, search, and deep links', () => {
    expect(sectionShown('trading')).toBe(false);
    expect(sectionShown('experiments')).toBe(false);

    const searchableAnchors = searchableSettings(true).map((entry) => entry.anchor);
    expect(searchableAnchors).not.toContain('trading-permission');
    expect(searchableAnchors).not.toContain('experiments');

    expect(
      readSection(new URLSearchParams('tab=trading'), '#trading-permission'),
    ).toBe('account');
    expect(
      readSection(new URLSearchParams('tab=experiments'), '#experiments'),
    ).toBe('account');
  });

  // DALTON-HOOK(settings-section): legacy Settings deep links and Dalton Chinese search keywords
  it('resolves legacy tab params and hashes to their new sections', () => {
    expect(readSection(new URLSearchParams('tab=userInfo'), '')).toBe('account');
    expect(readSection(new URLSearchParams('tab=models'), '')).toBe('model');
    expect(readSection(new URLSearchParams('tab=dalton'), '')).toBe('model');
    expect(readSection(new URLSearchParams('tab=preferences'), '#card-style')).toBe('appearance');
    expect(readSection(new URLSearchParams('tab=preferences'), '#output-format')).toBe('agent');
    expect(readSection(new URLSearchParams(), '#dalton-budget')).toBe('model');
    expect(readSection(new URLSearchParams(), '#dalton-cos-sharing')).toBe('model');
    expect(readSection(new URLSearchParams(), '#dalton-reviewer-models')).toBe('model');
  });

  it('finds Dalton settings in search by Chinese keywords ("预算", "审稿", "研究总监")', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    client.setQueryData(queryKeys.dalton.userSharing(), {
      user_id: 'u-int',
      cos_rw_enabled: true,
      cos_access: 'rw',
      can_edit: true,
      caller_tier: 'internal',
      colleague_dispatch_enabled: true,
      updated_at: null,
    });
    setupAndRender({}, client);

    const searchInput = await screen.findByRole('combobox', { name: /Search settings/i });

    fireEvent.change(searchInput, { target: { value: '预算' } });
    expect(await screen.findByText('Daily budget & spending limits')).toBeInTheDocument();

    fireEvent.change(searchInput, { target: { value: '审稿' } });
    expect(await screen.findByText('Independent reviewer candidates')).toBeInTheDocument();

    fireEvent.change(searchInput, { target: { value: '研究总监' } });
    expect(await screen.findByText('Chief of Staff workspace sharing')).toBeInTheDocument();
  });

  // DALTON-HOOK(settings-section): filter Dalton invite and Feishu scope settings in search by canInvite
  it('hides invite and Feishu scope settings in search for non-admins and shows them for admins', async () => {
    h.canInvite = false;
    const { unmount } = setupAndRender({});
    const nonAdminSearch = await screen.findByRole('combobox', { name: /Search settings/i });

    fireEvent.change(nonAdminSearch, { target: { value: '邀请' } });
    expect(screen.queryByText('Team invitations & member access')).not.toBeInTheDocument();
    expect(screen.getByText('No settings match.')).toBeInTheDocument();

    fireEvent.change(nonAdminSearch, { target: { value: '飞书' } });
    expect(screen.queryByText('Feishu read scopes')).not.toBeInTheDocument();
    // Notification targets is every member's own card, so it still answers "飞书".
    expect(await screen.findByText('Notification targets')).toBeInTheDocument();
    unmount();

    h.canInvite = true;
    const { unmount: unmountAdmin } = setupAndRender({});
    const adminSearch = await screen.findByRole('combobox', { name: /Search settings/i });

    fireEvent.change(adminSearch, { target: { value: '邀请' } });
    expect(await screen.findByText('Team invitations & member access')).toBeInTheDocument();

    fireEvent.change(adminSearch, { target: { value: '飞书' } });
    expect(await screen.findByText('Feishu read scopes')).toBeInTheDocument();
    unmountAdmin();
  });

  // DALTON-HOOK(settings-section): filter Chief of Staff sharing in search for external members and when dispatch is off
  it('hides Chief of Staff workspace sharing in search for external members and shows it for internal members when dispatch is enabled', async () => {
    const extClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    extClient.setQueryData(queryKeys.dalton.userSharing(), {
      user_id: 'u-ext',
      cos_rw_enabled: false,
      cos_access: 'hidden',
      can_edit: false,
      caller_tier: 'external',
      colleague_dispatch_enabled: true,
      updated_at: null,
    });
    const { unmount: unmountExt } = setupAndRender({}, extClient);
    const extSearch = await screen.findByRole('combobox', { name: /Search settings/i });

    fireEvent.change(extSearch, { target: { value: '研究总监' } });
    expect(screen.queryByText('Chief of Staff workspace sharing')).not.toBeInTheDocument();
    expect(screen.getByText('No settings match.')).toBeInTheDocument();
    unmountExt();

    const intClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    intClient.setQueryData(queryKeys.dalton.userSharing(), {
      user_id: 'u-int',
      cos_rw_enabled: true,
      cos_access: 'rw',
      can_edit: true,
      caller_tier: 'internal',
      colleague_dispatch_enabled: true,
      updated_at: null,
    });
    const { unmount: unmountInt } = setupAndRender({}, intClient);
    const intSearch = await screen.findByRole('combobox', { name: /Search settings/i });

    fireEvent.change(intSearch, { target: { value: '研究总监' } });
    expect(await screen.findByText('Chief of Staff workspace sharing')).toBeInTheDocument();
    unmountInt();
  });

  // DALTON-HOOK(settings-section): until the sharing settings arrive, neither
  // the card nor its search entry shows, so nothing flashes for a user it is not for
  it('keeps Chief of Staff workspace sharing out of search until the sharing settings arrive', async () => {
    setupAndRender({});
    const search = await screen.findByRole('combobox', { name: /Search settings/i });

    fireEvent.change(search, { target: { value: '研究总监' } });
    expect(screen.queryByText('Chief of Staff workspace sharing')).not.toBeInTheDocument();
    expect(screen.getByText('No settings match.')).toBeInTheDocument();
  });
});
