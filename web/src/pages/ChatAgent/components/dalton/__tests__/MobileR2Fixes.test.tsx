import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { queryOptions } from '@tanstack/react-query';
import { createTestQueryClient, renderWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import { CAPABILITY_IDS, type CapabilitiesApiResponse, type CapabilityId } from '@/hooks/useCapabilities';
import { CommandPaletteProvider, useCommandPalette } from '@/components/CommandPalette/CommandPaletteProvider';
import BottomTabBar from '@/components/BottomTabBar/BottomTabBar';
import { MobileNavDrawer } from '../../chatView/MobileNavDrawer';

const mediaState = vi.hoisted(() => ({ mobile: true }));
const teamFetchMock = vi.hoisted(() => vi.fn());

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => mediaState.mobile,
  getIsMobileSnapshot: () => mediaState.mobile,
}));

vi.mock('@/config/daltonAuth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/config/daltonAuth')>();
  return {
    ...actual,
    isDaltonAuth: true,
    DALTON_AUTH_MODE: 'local' as const,
  };
});

vi.mock('../TeamSharing', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../TeamSharing')>();
  return {
    ...actual,
    TeamWorkspacesSection: () => {
      teamFetchMock();
      return <div data-testid="team-workspaces-section">Team Workspaces</div>;
    },
  };
});

vi.mock('../CoverageTreeSection', () => ({
  CoverageTreeSection: () => <div data-testid="coverage-tree-section">Coverage Tree</div>,
}));

vi.mock('../../NavigationPanel', () => ({
  default: ({ extraSections }: { extraSections?: React.ReactNode }) => (
    <div data-testid="mock-nav-panel">{extraSections}</div>
  ),
}));

vi.mock('../../NavDisplayOptions', () => ({
  default: () => null,
}));

vi.mock('../../../hooks/useNavTreeProps', () => ({
  useNavTreeProps: () => ({
    onNavigateThread: vi.fn(),
    onNewThread: vi.fn(),
    onSelectAgent: vi.fn(),
  }),
}));

vi.mock('@/hooks/useWorkspaceLabel', () => ({
  useWorkspaceLabel: () => () => 'Workspace',
}));

// Stubs for CommandPaletteProvider
vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getRecentThreads: vi.fn(async () => ({ threads: [] })),
  getWorkspaceThreads: vi.fn(async () => ({ threads: [] })),
}));
vi.mock('@/api/search', () => ({
  searchConversations: vi.fn(async () => ({
    threads: [],
    turns: [],
    files: [],
    next_cursor: { threads: null, turns: null, files: null },
    failed: [],
  })),
  getSearchPreview: vi.fn(async () => {
    throw new Error('no preview');
  }),
}));
vi.mock('@/lib/dalton/searchApi', () => ({
  fetchGlobalSearch: vi.fn(async () => ({ conversations: [], documents: [] })),
}));
vi.mock('@/hooks/useAllWorkspaces', () => ({
  useAllWorkspaces: () => ({
    data: { workspaces: [{ workspace_id: 'ws1', name: 'Semis', status: 'running', is_home: false }] },
  }),
}));
vi.mock('@/hooks/useSkills', () => ({ useSkills: () => ({ data: [] }) }));
vi.mock('@/hooks/useHomeWorkspace', () => ({
  homeWorkspaceQuery: () =>
    queryOptions({
      queryKey: ['workspaces', 'home'],
      queryFn: async () => ({ workspace_id: 'home', is_home: true, name: 'Home' }),
    }),
  useHomeWorkspaceId: () => 'home',
  resolveChatWorkspace: vi.fn(async () => 'home'),
}));
vi.mock('@/hooks/useSymbolSearch', () => ({ useSymbolSearch: () => ({ hits: [], loading: false }) }));
vi.mock('@/lib/quotes', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useQuotes: () => ({ quotes: {} }),
}));
vi.mock('@/contexts/ThemeContext', () => ({ useTheme: () => ({ theme: 'light' }) }));
vi.mock('@/hooks/useAppearance', () => ({ useAppearance: () => ({ pickTheme: vi.fn() }) }));
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ userId: null, logout: vi.fn() }) }));
vi.mock('@/components/nav/useNavItems', () => ({ useNavItems: () => [] }));
vi.mock('@/pages/Settings/panels/usePreferenceWrite', () => ({ useSaveFailureToast: () => vi.fn() }));
vi.mock('@/pages/ChatAgent/hooks/useCreateWorkspace', () => ({ useCreateWorkspace: () => vi.fn() }));

function seedCapabilities(caps: readonly CapabilityId[]) {
  const qc = createTestQueryClient();
  qc.setQueryData<CapabilitiesApiResponse>(queryKeys.dalton.capabilities(), {
    profile: caps.length > 0 ? 'everflow' : 'core',
    capabilities: [...caps],
  });
  return qc;
}

describe('Mobile R2 fixes: colleague_sharing capability gate & CommandPalette keyboard flow', () => {
  beforeAll(async () => {
    await import('@/components/CommandPalette/CommandPalette');
  }, 30_000);

  beforeEach(() => {
    mediaState.mobile = true;
    teamFetchMock.mockReset();
  });

  afterEach(() => {
    mediaState.mobile = true;
  });

  it('MobileNavDrawer gates TeamWorkspacesSection strictly on hasCapability(colleague_sharing)', () => {
    const qcOff = seedCapabilities([]);
    const { unmount } = renderWithProviders(
      <MobileNavDrawer
        visible
        onMinimize={vi.fn()}
        isActive
        workspaceId="ws-1"
        threadId="th-1"
        agents={{ active: 'main', subagents: [], workflows: [] } as unknown as Parameters<typeof MobileNavDrawer>[0]['agents']}
        workspaceName="Semis"
      />,
      { queryClient: qcOff },
    );

    expect(screen.getByTestId('coverage-tree-section')).toBeInTheDocument();
    expect(screen.queryByTestId('team-workspaces-section')).toBeNull();
    expect(teamFetchMock).not.toHaveBeenCalled();
    unmount();

    const qcOn = seedCapabilities(CAPABILITY_IDS);
    renderWithProviders(
      <MobileNavDrawer
        visible
        onMinimize={vi.fn()}
        isActive
        workspaceId="ws-1"
        threadId="th-1"
        agents={{ active: 'main', subagents: [], workflows: [] } as unknown as Parameters<typeof MobileNavDrawer>[0]['agents']}
        workspaceName="Semis"
      />,
      { queryClient: qcOn },
    );

    expect(screen.getByTestId('team-workspaces-section')).toBeInTheDocument();
    expect(teamFetchMock).toHaveBeenCalledTimes(1);
  });

  it('restores focus to BottomTabBar More trigger after opening Search from More menu and closing via keyboard', async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <CommandPaletteProvider>
        <BottomTabBar />
      </CommandPaletteProvider>,
      { route: '/chat' },
    );

    const moreBtn = screen.getByTestId('bottom-tab-more');
    moreBtn.focus();
    await user.keyboard('{Enter}');
    const searchItem = await screen.findByRole('menuitem', { name: /Commands & jump|命令与跳转/i });
    await user.click(searchItem);

    const input = await screen.findByRole('combobox');
    await waitFor(() => expect(input).toHaveFocus());

    // Tab to Cancel and activate via Enter -> focus must return to the persistent More trigger button
    await user.tab();
    const cancelBtn = screen.getByTestId('command-palette-cancel');
    expect(cancelBtn).toHaveFocus();
    await user.keyboard('{Enter}');

    await waitFor(() => expect(screen.queryByRole('combobox')).not.toBeInTheDocument());
    await waitFor(() => expect(moreBtn).toHaveFocus());
  });

  it('CommandPalette lets Tab/Shift+Tab reach Cancel & Back on mobile and restores focus to input after keyboard Back', async () => {
    const user = userEvent.setup();
    function OpenBtn() {
      const palette = useCommandPalette();
      return (
        <button type="button" onClick={() => palette?.open()}>
          Open Search
        </button>
      );
    }
    renderWithProviders(
      <CommandPaletteProvider>
        <OpenBtn />
        <textarea aria-label="Composer" />
      </CommandPaletteProvider>,
    );

    // Open CommandPalette through CommandPaletteProvider
    await user.click(screen.getByRole('button', { name: 'Open Search' }));
    const input = await screen.findByRole('combobox');
    await waitFor(() => expect(input).toHaveFocus());

    // Tab moves focus from the search input to the mobile Cancel button (not trapped)
    await user.tab();
    const cancelBtn = screen.getByTestId('command-palette-cancel');
    expect(cancelBtn).toHaveFocus();

    // Shift+Tab returns to search input
    await user.tab({ shift: true });
    expect(input).toHaveFocus();

    // Enter a scoped view ($ -> Tickers) so the mobile Back button mounts
    fireEvent.change(input, { target: { value: '$NVDA' } });
    const backBtn = await screen.findByTestId('command-palette-back');

    // Shift+Tab moves from search input backward to Back button
    await user.tab({ shift: true });
    expect(backBtn).toHaveFocus();

    // Activating Back via keyboard steps back AND returns focus to search input (never document.body)
    await user.keyboard('{Enter}');
    await waitFor(() => expect(screen.queryByTestId('command-palette-back')).toBeNull());
    expect(input).toHaveFocus();
    expect(document.activeElement).not.toBe(document.body);

    // Tab to Cancel and press Escape -> closes the palette cleanly
    await user.tab();
    expect(screen.getByTestId('command-palette-cancel')).toHaveFocus();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('combobox')).not.toBeInTheDocument());
  });
});
