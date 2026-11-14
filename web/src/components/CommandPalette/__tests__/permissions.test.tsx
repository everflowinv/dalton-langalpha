import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { queryOptions } from '@tanstack/react-query';
import { renderWithProviders } from '@/test/utils';
import { clientOs } from '@/lib/clientOs';
import type { Workspace } from '@/types/api';
import { CommandPaletteProvider } from '../CommandPaletteProvider';

const state = vi.hoisted(() => ({
  currentWorkspaceId: 'ws-own' as string | undefined,
  resolvingWorkspace: false,
  user: { user_id: 'u-self', email: 'self@dalton.test' } as { user_id: string; email: string } | null,
  userLoading: false,
  teamData: {
    members: [],
    caller_tier: 'internal' as 'admin' | 'internal' | 'external',
    colleague_dispatch_enabled: true,
  } as {
    members: Array<{
      user_id: string;
      email: string;
      display_name: string;
      workspaces: Array<{ workspace_id: string; user_id: string; owner_name: string; owner_email: string; name: string; web_access?: 'rw' | 'ro' | 'hidden'; status?: string }>;
    }>;
    caller_tier?: 'admin' | 'internal' | 'external';
    colleague_dispatch_enabled?: boolean;
  } | undefined,
  teamLoading: false,
  wsDetails: {} as Record<string, Workspace>,
  allWorkspaces: [] as Workspace[],
  recentThreads: [] as Array<Record<string, unknown>>,
}));

vi.mock('@/config/daltonAuth', () => ({ isDaltonAuth: true }));
vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: state.user, isLoading: state.userLoading, isError: false }),
}));
vi.mock('@/pages/ChatAgent/components/dalton/TeamSharing', () => ({
  useTeamWorkspaces: () => ({
    data: state.teamData,
    isPending: state.teamLoading,
    isLoading: state.teamLoading,
    isFetching: state.teamLoading,
  }),
}));
vi.mock('@/components/Sidebar/useChatRoute', () => ({
  useChatRoute: () => ({
    currentWorkspaceId: state.currentWorkspaceId,
    currentThreadId: undefined,
    resolvingWorkspace: state.resolvingWorkspace,
  }),
}));
vi.mock('@/hooks/useWorkspace', () => ({
  useWorkspace: (id: string | null | undefined) => ({
    data: id ? state.wsDetails[id] : undefined,
    isPending: false,
    isLoading: false,
  }),
}));
vi.mock('@/hooks/useAllWorkspaces', () => ({
  useAllWorkspaces: () => ({ data: { workspaces: state.allWorkspaces } }),
}));
vi.mock('@/hooks/useHomeWorkspace', () => ({
  homeWorkspaceQuery: () =>
    queryOptions({ queryKey: ['workspaces', 'home'], queryFn: async () => ({ workspace_id: 'home', is_home: true, name: 'Home' }) }),
}));
vi.mock('@/hooks/useSkills', () => ({
  useSkills: () => ({
    data: [{ name: 'earnings-preview', command: 'preview', description: 'Preview earnings', workspace_id: null }],
  }),
}));
vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getRecentThreads: vi.fn(async () => ({ threads: state.recentThreads })),
  getWorkspaceThreads: vi.fn(async () => ({ threads: [] })),
}));
vi.mock('@/api/search', () => ({
  searchConversations: vi.fn(async () => ({ threads: [], turns: [], files: [], next_cursor: { threads: null, turns: null, files: null }, failed: [] })),
  getSearchPreview: vi.fn(async () => {
    throw new Error('no preview');
  }),
}));
vi.mock('@/hooks/useSymbolSearch', () => ({ useSymbolSearch: () => ({ hits: [], loading: false }) }));
vi.mock('@/lib/quotes', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useQuotes: () => ({ quotes: {} }),
}));
vi.mock('@/contexts/ThemeContext', () => ({ useTheme: () => ({ theme: 'light' }) }));
vi.mock('@/hooks/useAppearance', () => ({ useAppearance: () => ({ pickTheme: vi.fn() }) }));
vi.mock('@/components/nav/useNavItems', () => ({ useNavItems: () => [] }));
vi.mock('@/pages/Settings/panels/usePreferenceWrite', () => ({ useSaveFailureToast: () => vi.fn() }));
vi.mock('@/pages/ChatAgent/hooks/useCreateWorkspace', () => ({ useCreateWorkspace: () => vi.fn() }));
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ userId: state.user?.user_id ?? null, logout: vi.fn() }) }));
vi.mock('@/pages/ChatAgent/hooks/useRemoveThread', () => ({ useRemoveThread: () => ({ removeThread: vi.fn() }) }));
vi.mock('@/pages/ChatAgent/components/threadArchiveAction', () => ({
  useArchiveThreadConfirm: () => ({ requestArchive: vi.fn(), dialog: null }),
}));

beforeAll(async () => {
  Object.defineProperty(Element.prototype, 'scrollIntoView', { value: vi.fn(), writable: true });
  await import('../CommandPalette');
}, 30_000);

beforeEach(() => {
  state.currentWorkspaceId = 'ws-own';
  state.resolvingWorkspace = false;
  state.user = { user_id: 'u-self', email: 'self@dalton.test' };
  state.userLoading = false;
  state.teamLoading = false;
  state.allWorkspaces = [
    { workspace_id: 'ws-own', user_id: 'u-self', name: 'Own Semis', status: 'running', is_home: false, created_at: '', updated_at: '' },
  ];
  state.wsDetails = {};
  state.teamData = {
    members: [
      {
        user_id: 'u-colleague',
        email: 'colleague@dalton.test',
        display_name: 'Colleague',
        workspaces: [
          { workspace_id: 'ws-colleague-ro', user_id: 'u-colleague', owner_name: 'Colleague', owner_email: 'colleague@dalton.test', name: 'Colleague RO', web_access: 'ro', status: 'running' },
        ],
      },
    ],
    caller_tier: 'internal',
    colleague_dispatch_enabled: true,
  };
  state.recentThreads = [
    { thread_id: 't-own', title: 'Own Thread', workspace_id: 'ws-own', user_id: 'u-self', updated_at: '2026-11-10T00:00:00Z' },
    { thread_id: 't-colleague', title: 'Colleague Thread', workspace_id: 'ws-colleague-ro', user_id: 'u-colleague', workspace_owner_id: 'u-colleague', updated_at: '2026-11-11T00:00:00Z' },
  ];
});

afterEach(() => {
  vi.clearAllMocks();
});

function openPalette() {
  renderWithProviders(
    <CommandPaletteProvider>
      <div />
    </CommandPaletteProvider>,
  );
  fireEvent.keyDown(document.activeElement ?? window, {
    key: 'k',
    shiftKey: true,
    ...(clientOs() === 'windows' ? { ctrlKey: true } : { metaKey: true }),
  });
}

describe('CommandPalette permissions & loading gate', () => {
  it('hides colleague workspaces and colleague recent threads for external members', async () => {
    state.teamData = {
      ...state.teamData!,
      caller_tier: 'external',
    };
    openPalette();
    const dialog = await screen.findByRole('dialog');
    const input = within(dialog).getByRole('combobox');

    await waitFor(() => {
      expect(within(dialog).getByText('Own Thread')).toBeInTheDocument();
    });
    expect(within(dialog).queryByText('Colleague Thread')).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: '@Colleague' } });
    await waitFor(() => {
      expect(within(dialog).queryByText('Colleague RO')).not.toBeInTheDocument();
    });
  });

  it('disables/hides create actions when viewing a read-only colleague workspace', async () => {
    state.currentWorkspaceId = 'ws-colleague-ro';
    state.wsDetails['ws-colleague-ro'] = {
      workspace_id: 'ws-colleague-ro',
      user_id: 'u-colleague',
      name: 'Colleague RO',
      status: 'running',
      is_home: false,
      created_at: '',
      updated_at: '',
      effective_web_access: 'ro',
      colleague_dispatch_enabled: true,
    } as unknown as Workspace;

    openPalette();
    const dialog = await screen.findByRole('dialog');
    const input = within(dialog).getByRole('combobox');

    expect(within(dialog).queryByText('New thread')).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: 'preview' } });
    await waitFor(() => {
      expect(
        within(dialog).getByText(
          (_, node) =>
            Boolean(node?.classList?.contains('command-palette-title')) &&
            node?.textContent === 'earnings-preview',
        ),
      ).toBeInTheDocument();
    });
    // Ask row must not be offered for a read-only colleague workspace
    expect(within(dialog).queryByText(/Ask about/i)).not.toBeInTheDocument();
  });

  it('hides colleague entries and disables create actions while identity/team permissions load', async () => {
    state.user = null;
    state.userLoading = true;
    state.teamData = undefined;
    state.teamLoading = true;

    openPalette();
    const dialog = await screen.findByRole('dialog');
    const input = within(dialog).getByRole('combobox');

    await waitFor(() => {
      expect(within(dialog).getByText('Switch to dark theme')).toBeInTheDocument();
    });
    expect(within(dialog).queryByText('New thread')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Colleague Thread')).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: 'thesis' } });
    expect(within(dialog).queryByText(/Ask about/i)).not.toBeInTheDocument();
  });
});
