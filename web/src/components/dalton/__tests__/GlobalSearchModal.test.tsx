import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation, useParams } from 'react-router';
import GlobalSearchModal, {
  closeGlobalSearch,
  GlobalSearchHost,
  renderHighlightedSnippet,
} from '../GlobalSearchModal';
import AppSidebar from '@/components/Sidebar/AppSidebar';
import BottomTabBar from '@/components/BottomTabBar/BottomTabBar';
import ThreadGallery from '@/pages/ChatAgent/components/ThreadGallery';
import type { GlobalSearchResponse } from '@/lib/dalton/searchApi';
import type { LocationState } from '@/pages/ChatAgent/components/chatView/types';
import type { PanelTarget } from '@/pages/ChatAgent/components/filePanel/types';

// DALTON-HOOK(brand-name): AppSidebar now mounts upstream's Getting Started gate, which needs
// the onboarding provider these brand/search harnesses do not mount.
vi.mock('@/pages/Onboarding/GettingStartedGate', () => ({ default: () => null, GettingStartedGate: () => null }));

// Valid v4 UUIDs: usePanelLinkOpen ignores a link whose workspace id fails isValidUuid.
const WS_NVDA = '11111111-1111-4111-8111-111111111111';
const WS_AMD = '22222222-2222-4222-8222-222222222222';
const WS_HOME = '99999999-9999-4999-8999-999999999999';

const h = vi.hoisted(() => ({
  fetchGlobalSearch: vi.fn<(q: string, limit?: number, signal?: AbortSignal) => Promise<GlobalSearchResponse>>(),
  openedTargets: [] as Array<{ host: string; workspaceId: string; target: PanelTarget | null }>,
}));

vi.mock('@/lib/dalton/searchApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/dalton/searchApi')>();
  return {
    ...actual,
    fetchGlobalSearch: (q: string, limit?: number, signal?: AbortSignal) =>
      h.fetchGlobalSearch(q, limit, signal),
  };
});

const STABLE_THEME = {
  theme: 'dark' as const,
  setTheme: vi.fn(),
  toggleTheme: vi.fn(),
};
vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => STABLE_THEME,
}));

const mockResolveChatWorkspace = vi.fn(() => Promise.resolve(WS_HOME));
vi.mock('@/hooks/useHomeWorkspace', () => ({
  useHomeWorkspaceId: () => WS_HOME,
  // Lazy: vi.mock is hoisted above the const, so read it at call time.
  resolveChatWorkspace: (...args: unknown[]) => mockResolveChatWorkspace(...(args as [])),
  HomeRowIcon: () => <span data-testid="mock-home-icon" />,
}));

const workspaceById = new Map<string, unknown>();
vi.mock('@/hooks/useWorkspace', () => ({
  useWorkspace: (wsId: string) => {
    const cached = workspaceById.get(wsId);
    if (cached) return cached;
    const value = {
      data: { workspace_id: wsId, name: wsId === WS_NVDA ? 'NVDA' : 'Home', status: 'running' },
      error: null,
    };
    workspaceById.set(wsId, value);
    return value;
  },
}));

const STABLE_EMPTY_THREADS = { threads: [], total: 0 };
const mockGetWorkspaceThreads = vi.fn(() => Promise.resolve(STABLE_EMPTY_THREADS));
const mockDeleteThread = vi.fn();
const mockUpdateThreadTitle = vi.fn();
const mockUpdateThread = vi.fn();
vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getWorkspaceThreads: () => mockGetWorkspaceThreads(),
  deleteThread: () => mockDeleteThread(),
  updateThreadTitle: () => mockUpdateThreadTitle(),
  updateThread: () => mockUpdateThread(),
}));

vi.mock('@/pages/ChatAgent/hooks/useComputerFolders', () => ({
  useComputerFolders: () => null,
}));

// Stable per-workspace result, like the real hook: a fresh object (and fresh
// vi.fn()s) on every render re-fires ThreadGallery's effects in a render loop.
const panelFilesByWorkspace = new Map<string, unknown>();
vi.mock('@/pages/ChatAgent/components/chatView/usePanelFiles', () => ({
  usePanelFiles: ({ workspaceId }: { workspaceId: string }) => {
    const cached = panelFilesByWorkspace.get(workspaceId);
    if (cached) return cached;
    const value = {
      override: null,
      setOverride: vi.fn(),
      shownWorkspaceId: workspaceId,
      files: ['reports/nvda_margin_model.md'],
      loading: false,
      error: null,
      refresh: vi.fn(),
      mentionFiles: ['reports/nvda_margin_model.md'],
      panelAccess: {
        readOnly: false,
        teamReadOnly: false,
        singleFileMode: false,
        onReturnHome: undefined,
        returnLabel: undefined,
      },
    };
    panelFilesByWorkspace.set(workspaceId, value);
    return value;
  },
}));

vi.mock('@/pages/ChatAgent/components/FilePanel', () => ({
  SYSTEM_DIR_PREFIXES: ['.agents', 'code', 'tools'],
  default: ({
    workspaceId,
    target,
  }: {
    workspaceId: string;
    target: PanelTarget | null;
  }) => {
    if (target) {
      h.openedTargets.push({ host: 'ThreadGallery', workspaceId, target });
    }
    return (
      <div data-testid="mock-file-panel" data-workspace-id={workspaceId}>
        <span data-testid="file-panel-target-kind">{target?.kind ?? ''}</span>
        <span data-testid="file-panel-target-path">
          {target?.kind === 'file'
            ? (target.path ?? '')
            : target?.kind === 'memo'
              ? target.key
              : ''}
        </span>
      </div>
    );
  },
}));

vi.mock('@/components/ui/chat-input', () => ({
  default: React.forwardRef(() => <div data-testid="mock-chat-input" />),
}));

vi.mock('@/pages/ChatAgent/components/NavigationPanel', () => ({
  default: () => <div data-testid="mock-nav-panel" />,
}));

vi.mock('@/pages/ChatAgent/components/NavDisplayOptions', () => ({
  default: () => <div data-testid="mock-nav-display-options" />,
}));

const mockCreateWorkspace = vi.fn();
vi.mock('@/pages/ChatAgent/hooks/useCreateWorkspace', () => ({
  useCreateWorkspace: () => mockCreateWorkspace,
}));

const STABLE_NAV_TREE_PROPS = {};
vi.mock('@/pages/ChatAgent/hooks/useNavTreeProps', () => ({
  useNavTreeProps: () => STABLE_NAV_TREE_PROPS,
}));

vi.mock('@/components/Sidebar/AccountMenu', () => ({
  default: () => <div data-testid="mock-account-menu" />,
}));

const STABLE_TEAM_READ_ONLY = { isReadOnly: false, ownerName: null };
vi.mock('@/pages/ChatAgent/components/dalton/TeamSharing', () => ({
  TeamWorkspacesSection: () => <div data-testid="mock-team-workspaces" />,
  TeamReadOnlyBanner: () => null,
  TeamReadOnlyComposer: () => <div data-testid="mock-readonly-composer" />,
  useTeamReadOnly: () => STABLE_TEAM_READ_ONLY,
}));

vi.mock('@/pages/ChatAgent/components/dalton/WorkspaceTrackingToggle', () => ({
  WorkspaceTrackingToggle: () => null,
}));

const SAMPLE_SEARCH_RESPONSE: GlobalSearchResponse = {
  conversations: [
    {
      kind: 'conversation',
      thread_id: 'th-101',
      workspace_id: WS_NVDA,
      title: 'NVDA Q3 Gross Margin Review',
      workspace_name: 'NVDA',
      timestamp: '2026-10-26T10:00:00+00:00',
      snippet: 'The gross margin expanded by 340 bps in Q3.',
      highlights: [[10, 16]],
    },
    {
      kind: 'conversation',
      thread_id: 'th-102',
      workspace_id: WS_AMD,
      title: 'AMD Data Center Margin',
      workspace_name: 'AMD',
      timestamp: '2026-10-26T09:00:00+00:00',
      snippet: 'Checking MI300 margin contribution.',
      highlights: [[15, 21]],
    },
  ],
  documents: [
    {
      kind: 'workspace_file',
      memo_key: null,
      workspace_id: WS_NVDA,
      path: 'reports/nvda_margin_model.md',
      title: 'nvda_margin_model.md',
      workspace_name: 'NVDA',
      timestamp: '2026-10-26T09:30:00+00:00',
      snippet: ' Gross margin bridge for FY27.',
      highlights: [[7, 13]],
    },
    {
      kind: 'memo',
      memo_key: 'semi-notes.md',
      workspace_id: null,
      path: '.agents/user/memo/semi-notes.md',
      title: 'Semiconductor Notes.md',
      workspace_name: '',
      timestamp: '2026-10-26T08:00:00+00:00',
      snippet: 'Personal memo on wafer margin.',
      highlights: [[23, 29]],
    },
  ],
};

const mockNoop = vi.fn();

function LocationProbe() {
  const location = useLocation();
  const state = (location.state as LocationState | null) ?? null;
  return (
    <div data-testid="location-probe">
      <span data-testid="location-pathname">{location.pathname}</span>
      <span data-testid="location-open-file">{state?.openFilePath ?? ''}</span>
    </div>
  );
}

function RoutedThreadGallery() {
  const { workspaceId = '' } = useParams<{ workspaceId: string }>();
  return (
    <ThreadGallery
      workspaceId={workspaceId}
      onBack={mockNoop}
      onThreadSelect={mockNoop}
    />
  );
}

/**
 * Mirrors `AuthenticatedShell` (`App.tsx` L300-319): `AppSidebar` mounts on
 * desktop (`!isMobile`), `BottomTabBar` mounts on mobile (`isMobile`), and a
 * single persistent `GlobalSearchHost` stays mounted at the shell level across
 * viewport transitions.
 */
function ResponsiveShellHarness({ isMobile }: { isMobile: boolean }) {
  return (
    <>
      {!isMobile && (
        <AppSidebar
          collapsed={false}
          onToggleCollapse={mockNoop}
          width={260}
          onWidthChange={mockNoop}
        />
      )}
      {isMobile && <BottomTabBar />}
      <GlobalSearchHost />
    </>
  );
}

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
}

function renderWithQueryClient(ui: React.ReactElement) {
  const qc = createTestQueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe('GlobalSearchModal, AppSidebar, Mobile BottomTabBar & ThreadGallery FilePanel', () => {
  beforeEach(() => {
    closeGlobalSearch();
    h.fetchGlobalSearch.mockReset();
    h.fetchGlobalSearch.mockResolvedValue(SAMPLE_SEARCH_RESPONSE);
    h.openedTargets.length = 0;
  });

  it('opens via sidebar search button and via ⌘K shortcut (including inside an input)', async () => {
    renderWithQueryClient(
      <MemoryRouter initialEntries={['/dashboard']}>
        <input data-testid="external-page-input" />
        <AppSidebar
          collapsed={false}
          onToggleCollapse={mockNoop}
          width={260}
          onWidthChange={mockNoop}
        />
        <GlobalSearchHost />
      </MemoryRouter>,
    );

    const searchBtn = screen.getByRole('button', { name: /Search|搜索/i });
    fireEvent.click(searchBtn);

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toBeInTheDocument();

    const modalInput = within(dialog).getByRole('textbox');
    fireEvent.keyDown(modalInput, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });

    const externalInput = screen.getByTestId('external-page-input');
    externalInput.focus();
    fireEvent.keyDown(externalInput, { key: 'k', metaKey: true });

    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });

  it('keeps working after switching from desktop width (AppSidebar mounted) to mobile width (AppSidebar unmounted, BottomTabBar mounted)', async () => {
    const qc = createTestQueryClient();
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/dashboard']}>
          <ResponsiveShellHarness isMobile={false} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // 1. Desktop width: open via sidebar search button, then close
    const desktopSearchBtn = screen.getByRole('button', { name: /Search|搜索/i });
    fireEvent.click(desktopSearchBtn);
    const desktopDialog = await screen.findByRole('dialog');
    expect(desktopDialog).toBeInTheDocument();

    fireEvent.keyDown(within(desktopDialog).getByRole('textbox'), { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });

    // 2. Switch to mobile width: AppSidebar unmounts, BottomTabBar mounts, single GlobalSearchHost stays mounted
    rerender(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/dashboard']}>
          <ResponsiveShellHarness isMobile={true} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // 3. Mobile BottomTabBar button opens the modal
    const mobileSearchBtn = screen.getByRole('button', { name: /^Search$|^搜索$/i });
    fireEvent.click(mobileSearchBtn);
    expect(await screen.findByRole('dialog')).toBeInTheDocument();

    act(() => {
      closeGlobalSearch();
    });
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });

    // 4. ⌘K / Ctrl+K shortcut still opens the modal after AppSidebar unmounted
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });

  it('debounces input by 250ms, renders both sections, and highlights matched ranges', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderWithQueryClient(
        <MemoryRouter initialEntries={['/dashboard']}>
          <GlobalSearchModal open onClose={mockNoop} />
        </MemoryRouter>,
      );

      const input = screen.getByRole('textbox');
      fireEvent.change(input, { target: { value: 'ma' } });
      fireEvent.change(input, { target: { value: 'mar' } });
      fireEvent.change(input, { target: { value: 'margin' } });

      expect(h.fetchGlobalSearch).not.toHaveBeenCalled();

      await act(async () => {
        vi.advanceTimersByTime(260);
      });

      await waitFor(() => {
        expect(h.fetchGlobalSearch).toHaveBeenCalledTimes(1);
      });
      expect(h.fetchGlobalSearch).toHaveBeenCalledWith('margin', 20, expect.anything());

      const convSection = await screen.findByTestId('search-section-conversations');
      const docSection = await screen.findByTestId('search-section-documents');

      expect(within(convSection).getByText('NVDA Q3 Gross Margin Review')).toBeInTheDocument();
      expect(within(convSection).getByText('AMD Data Center Margin')).toBeInTheDocument();
      expect(within(docSection).getByText('nvda_margin_model.md')).toBeInTheDocument();
      expect(within(docSection).getByText('Semiconductor Notes.md')).toBeInTheDocument();

      const highlights = screen.getAllByTestId('search-highlight');
      expect(highlights.length).toBe(4);
      for (const mark of highlights) {
        expect(mark.textContent?.toLowerCase()).toBe('margin');
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it('ignores Enter and ArrowDown/ArrowUp during IME composition and scrolls active option into view', async () => {
    const scrollSpy = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollSpy,
    });

    const onClose = vi.fn();
    renderWithQueryClient(
      <MemoryRouter initialEntries={['/dashboard']}>
        <Routes>
          <Route
            path="*"
            element={
              <>
                <GlobalSearchModal open onClose={onClose} />
                <LocationProbe />
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'margin' } });

    await screen.findByText('NVDA Q3 Gross Margin Review');
    const options = screen.getAllByRole('option');
    expect(options).toHaveLength(4);
    expect(options[0]).toHaveAttribute('aria-selected', 'true');

    // IME composing events must be ignored
    fireEvent.keyDown(input, { key: 'ArrowDown', isComposing: true });
    expect(options[0]).toHaveAttribute('aria-selected', 'true');

    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 });
    expect(onClose).not.toHaveBeenCalled();

    // Non-composing ArrowDown moves selection and calls scrollIntoView({ block: 'nearest' })
    scrollSpy.mockClear();
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(options[1]).toHaveAttribute('aria-selected', 'true');
    expect(scrollSpy).toHaveBeenCalledWith({ block: 'nearest' });

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(options[2]).toHaveAttribute('aria-selected', 'true');

    // Non-composing Enter navigates
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('location-pathname').textContent).toBe(`/chat/${WS_NVDA}`);
  });

  it('opens clicked workspace file and memo results in ThreadGallery FilePanel and navigates conversation results to /chat/t/:threadId', async () => {
    renderWithQueryClient(
      <MemoryRouter initialEntries={['/dashboard']}>
        <GlobalSearchModal open onClose={mockNoop} />
        <Routes>
          <Route path="/dashboard" element={<div data-testid="dashboard-page" />} />
          <Route
            path="/chat/:workspaceId"
            element={
              <>
                <RoutedThreadGallery />
                <LocationProbe />
              </>
            }
          />
          <Route
            path="/chat/t/:threadId"
            element={
              <>
                <div data-testid="thread-route-page" />
                <LocationProbe />
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'margin' } });

    // 1. Clicking a workspace file result opens FilePanel on that workspace's ThreadGallery with kind='file'
    const fileOption = await screen.findByText('nvda_margin_model.md');
    fireEvent.click(fileOption);

    const filePanel = await screen.findByTestId('mock-file-panel');
    expect(filePanel).toHaveAttribute('data-workspace-id', WS_NVDA);
    expect(screen.getByTestId('file-panel-target-kind').textContent).toBe('file');
    expect(screen.getByTestId('file-panel-target-path').textContent).toBe(
      'reports/nvda_margin_model.md',
    );
    await waitFor(() => {
      expect(screen.getByTestId('location-open-file').textContent).toBe('');
    });

    // 2. Clicking a memo result opens FilePanel on Home's ThreadGallery with kind='memo' and key='semi-notes.md'
    const memoOption = await screen.findByText('Semiconductor Notes.md');
    fireEvent.click(memoOption);

    await waitFor(() => {
      expect(screen.getByTestId('mock-file-panel')).toHaveAttribute('data-workspace-id', WS_HOME);
      expect(screen.getByTestId('file-panel-target-kind').textContent).toBe('memo');
      expect(screen.getByTestId('file-panel-target-path').textContent).toBe('semi-notes.md');
    });
    await waitFor(() => {
      expect(screen.getByTestId('location-open-file').textContent).toBe('');
    });

    // 3. Clicking a conversation result navigates to /chat/t/:threadId
    const convOption = await screen.findByText('NVDA Q3 Gross Margin Review');
    fireEvent.click(convOption);

    expect(await screen.findByTestId('thread-route-page')).toBeInTheDocument();
    expect(screen.getByTestId('location-pathname').textContent).toBe('/chat/t/th-101');
  });

  it('renderHighlightedSnippet slices by Unicode code points (emoji and supplementary characters do not shift offsets)', () => {
    // '🚀' and '🔥' are astral-plane characters (1 Unicode code point each, 2 UTF-16 code units each).
    // In code points: '🚀'(0) '🔥'(1) ' '(2) 'L'(3) 'a'(4) 'u'(5) 'n'(6) 'c'(7) 'h'(8) ' '(9) 'N'(10) 'V'(11) 'D'(12) 'A'(13)
    const { container } = render(
      <div>{renderHighlightedSnippet('🚀🔥 Launch NVDA 研报', [[10, 14]])}</div>,
    );
    const mark = container.querySelector('mark[data-testid="search-highlight"]');
    expect(mark).not.toBeNull();
    expect(mark?.textContent).toBe('NVDA');
  });
});
