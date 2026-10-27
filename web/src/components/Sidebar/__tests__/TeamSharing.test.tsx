import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { renderWithProviders, renderHookWithProviders, createTestQueryClient } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import { workspaceDetailQuery } from '@/pages/ChatAgent/utils/workspaceQueries';
import { createEditDraftStore } from '@/pages/ChatAgent/hooks/utils/editDrafts';
import type { useChatMessages as useChatMessagesType } from '@/pages/ChatAgent/hooks/useChatMessages';
import type { ChatMessage } from '@/types/chat';

vi.mock('@/components/ui/dropdown-menu', () => ({
  // upstream's context menu reads this class constant from the dropdown module
  DESTRUCTIVE_ITEM: 'text-destructive',
  DropdownMenu: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="dropdown-menu">{children}</div>
  ),
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="dropdown-menu-content">{children}</div>
  ),
  DropdownMenuItem: ({
    children,
    onSelect,
    ...props
  }: {
    children: React.ReactNode;
    onSelect?: (e: Event) => void;
    disabled?: boolean;
  }) => (
    <button
      type="button"
      onClick={() => onSelect?.(new Event('select'))}
      {...props}
    >
      {children}
    </button>
  ),
  DropdownMenuLabel: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuSeparator: () => <hr />,
  DropdownMenuSub: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuSubTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuSubContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const mockDaltonAuth = vi.hoisted(() => ({ isDaltonAuth: true }));

vi.mock('@/config/daltonAuth', () => ({
  get isDaltonAuth() {
    return mockDaltonAuth.isDaltonAuth;
  },
}));

const mockApiGet = vi.hoisted(() => vi.fn());
const mockApiPost = vi.hoisted(() => vi.fn());
const mockApiPut = vi.hoisted(() => vi.fn());
vi.mock('@/api/client', () => ({
  api: {
    defaults: { baseURL: 'http://localhost:8000' },
    get: (...args: unknown[]) => mockApiGet(...args),
    post: (...args: unknown[]) => mockApiPost(...args),
    put: (...args: unknown[]) => mockApiPut(...args),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

const mockGrantRefetch = vi.hoisted(() => vi.fn());
const mockGrantResult = vi.hoisted(() => ({
  data: undefined as { prefix: string; expiresAt: number } | undefined,
}));
const mockGrant = vi.hoisted(() => vi.fn());
vi.mock('@/hooks/useWorkspaceFileGrant', () => ({
  useWorkspaceFileGrant: (wsId: string | null | undefined) => {
    mockGrant(wsId);
    return { data: wsId ? mockGrantResult.data : undefined, error: null, refetch: mockGrantRefetch };
  },
}));

const mockShareLinkResult = vi.hoisted(() => ({
  data: undefined as { code: string } | undefined,
}));
const mockShareLink = vi.hoisted(() => vi.fn());
vi.mock('@/hooks/useShareLink', () => ({
  useShareLink: (wsId: string | null | undefined, target: unknown) => {
    mockShareLink(wsId, target);
    return { data: wsId ? mockShareLinkResult.data : undefined };
  },
}));

const STABLE_THEME = vi.hoisted(() => ({
  theme: 'dark' as const,
  setTheme: vi.fn(),
  toggleTheme: vi.fn(),
}));
vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => STABLE_THEME,
}));

const mockSendMessage = vi.hoisted(() => vi.fn());
const mockApproveCreateWorkspace = vi.hoisted(() => vi.fn());
const mockApprovePTCAgent = vi.hoisted(() => vi.fn());
const mockAnswerQuestion = vi.hoisted(() => vi.fn());
const mockMessages = vi.hoisted(() => ({ current: [] as ChatMessage[] }));
const capturedWorkflowOnStop = vi.hoisted(() => ({ current: undefined as (() => void) | undefined }));

const STABLE_CHAT_HELPERS = vi.hoisted(() => ({
  liveMessages: {
    get: () => mockMessages.current,
    subscribe: () => () => {},
  },
  olderHistory: { status: 'idle' as const, load: vi.fn(() => Promise.resolve()) },
  threadModels: [] as string[],
  stopWorkflow: vi.fn(() => Promise.resolve()),
  stopCompaction: vi.fn(() => Promise.resolve()),
  handleEditMessage: vi.fn(() => Promise.resolve()),
  handleRegenerate: vi.fn(() => Promise.resolve()),
  handleRetry: vi.fn(() => Promise.resolve()),
  clearReturnedSteering: vi.fn(),
  clearReturnedEdit: vi.fn(),
  handleRejectCreateWorkspace: vi.fn(() => Promise.resolve()),
  handleApproveStartQuestion: vi.fn(() => Promise.resolve()),
  handleRejectStartQuestion: vi.fn(() => Promise.resolve()),
  handleRejectPTCAgent: vi.fn(() => Promise.resolve()),
  handleApproveSecretaryAction: vi.fn(() => Promise.resolve()),
  handleRejectSecretaryAction: vi.fn(() => Promise.resolve()),
  handleResumeCreditPause: vi.fn(() => Promise.resolve()),
  handleApproveToolCall: vi.fn(() => Promise.resolve()),
  handleRejectToolCall: vi.fn(() => Promise.resolve()),
  handleSkipQuestion: vi.fn(() => Promise.resolve()),
  feedbackByTurn: {},
  handleThumbUp: vi.fn(() => Promise.resolve(null)),
  handleThumbDown: vi.fn(() => Promise.resolve(null)),
  clearFallbackSuggestion: vi.fn(),
  setIsCompacting: vi.fn(),
  reportBackOwedThreads: [] as import('@/pages/ChatAgent/utils/api/messages').ReportBackOwedThread[],
  reportBackOwedKinds: null as import('@/pages/ChatAgent/utils/api/messages').OwedReportKind[] | null,
  isOwnRun: () => false,
  reconnectIfStaleRun: vi.fn(),
  insertNotification: vi.fn(),
  getSubagentHistory: () => null,
  getTaskRuns: () => [],
  putTaskRun: vi.fn(),
  subagentHistoryEntries: {},
  resolveSubagentIdToAgentId: (id: string) => id,
  hydrateTaskTranscript: vi.fn(() => Promise.resolve(null)),
  sendSubagentInstruction: vi.fn(() => Promise.resolve()),
}));
const STABLE_EDIT_DRAFTS = createEditDraftStore(() => 'th-1');

vi.mock('@/pages/ChatAgent/hooks/useChatMessages', () => ({
  useChatMessages: (): ReturnType<typeof useChatMessagesType> => ({
    messages: mockMessages.current,
    liveMessages: STABLE_CHAT_HELPERS.liveMessages,
    isLoading: false,
    turnHeld: null,
    hasActiveSubagents: false,
    isLoadingHistory: false,
    historyLoadFailed: false,
    isLoadingThread: false,
    olderHistory: STABLE_CHAT_HELPERS.olderHistory,
    isReconnecting: false,
    messageError: null,
    threadId: 'th-1',
    isShared: true,
    threadModels: STABLE_CHAT_HELPERS.threadModels,
    marketWatch: null,
    handleSendMessage: mockSendMessage,
    stopWorkflow: STABLE_CHAT_HELPERS.stopWorkflow,
    stopCompaction: STABLE_CHAT_HELPERS.stopCompaction,
    handleEditMessage: STABLE_CHAT_HELPERS.handleEditMessage,
    handleRegenerate: STABLE_CHAT_HELPERS.handleRegenerate,
    handleRetry: STABLE_CHAT_HELPERS.handleRetry,
    returnedSteering: null,
    clearReturnedSteering: STABLE_CHAT_HELPERS.clearReturnedSteering,
    returnedEdit: null,
    clearReturnedEdit: STABLE_CHAT_HELPERS.clearReturnedEdit,
    editDrafts: STABLE_EDIT_DRAFTS,
    pendingInterrupt: null,
    handleApproveCreateWorkspace: mockApproveCreateWorkspace,
    handleRejectCreateWorkspace: STABLE_CHAT_HELPERS.handleRejectCreateWorkspace,
    handleApproveStartQuestion: STABLE_CHAT_HELPERS.handleApproveStartQuestion,
    handleRejectStartQuestion: STABLE_CHAT_HELPERS.handleRejectStartQuestion,
    handleApprovePTCAgent: mockApprovePTCAgent,
    handleRejectPTCAgent: STABLE_CHAT_HELPERS.handleRejectPTCAgent,
    handleApproveSecretaryAction: STABLE_CHAT_HELPERS.handleApproveSecretaryAction,
    handleRejectSecretaryAction: STABLE_CHAT_HELPERS.handleRejectSecretaryAction,
    handleResumeCreditPause: STABLE_CHAT_HELPERS.handleResumeCreditPause,
    handleApproveToolCall: STABLE_CHAT_HELPERS.handleApproveToolCall,
    handleRejectToolCall: STABLE_CHAT_HELPERS.handleRejectToolCall,
    handleAnswerQuestion: mockAnswerQuestion,
    handleSkipQuestion: STABLE_CHAT_HELPERS.handleSkipQuestion,
    feedbackByTurn: STABLE_CHAT_HELPERS.feedbackByTurn,
    handleThumbUp: STABLE_CHAT_HELPERS.handleThumbUp,
    handleThumbDown: STABLE_CHAT_HELPERS.handleThumbDown,
    workspaceStarting: false,
    fallbackSuggestion: null,
    clearFallbackSuggestion: STABLE_CHAT_HELPERS.clearFallbackSuggestion,
    modelStatus: null,
    isCompacting: false,
    compactionTokens: null,
    setIsCompacting: STABLE_CHAT_HELPERS.setIsCompacting,
    queuedSend: false,
    awaitingReportBack: false,
    reportBackOwed: false,
    reportBackOwedThreads: STABLE_CHAT_HELPERS.reportBackOwedThreads,
    reportBackOwedKinds: STABLE_CHAT_HELPERS.reportBackOwedKinds,
    isOwnRun: STABLE_CHAT_HELPERS.isOwnRun,
    reconnectIfStaleRun: STABLE_CHAT_HELPERS.reconnectIfStaleRun,
    tokenUsage: null,
    insertNotification: STABLE_CHAT_HELPERS.insertNotification,
    getSubagentHistory: STABLE_CHAT_HELPERS.getSubagentHistory,
    getTaskRuns: STABLE_CHAT_HELPERS.getTaskRuns,
    putTaskRun: STABLE_CHAT_HELPERS.putTaskRun,
    subagentHistoryEntries: STABLE_CHAT_HELPERS.subagentHistoryEntries,
    resolveSubagentIdToAgentId: STABLE_CHAT_HELPERS.resolveSubagentIdToAgentId,
    hydrateTaskTranscript: STABLE_CHAT_HELPERS.hydrateTaskTranscript,
    sendSubagentInstruction: STABLE_CHAT_HELPERS.sendSubagentInstruction,
  }),
}));

vi.mock('@/pages/ChatAgent/components/WorkflowRunDetail', () => ({
  default: ({ onStop }: { onStop?: () => void }) => {
    capturedWorkflowOnStop.current = onStop;
    return (
      <div data-testid="mock-workflow-run-detail">
        {onStop && <button data-testid="workflow-stop-btn" onClick={onStop}>Stop</button>}
      </div>
    );
  },
}));

const STABLE_CARD_STATE = vi.hoisted(() => ({
  cards: {
    'subagent-task:wf-1': {
      subagentData: {
        id: 'task:wf-1',
        taskId: 'wf-1',
        type: 'workflow',
        status: 'running',
        title: 'Workflow Run',
        messages: [],
      },
    },
  },
  updateTodoListCard: vi.fn(),
  updateSubagentCard: vi.fn(),
  finalizePendingTodos: vi.fn(),
  clearSubagentCards: vi.fn(),
}));

vi.mock('@/pages/ChatAgent/hooks/useCardState', () => ({
  useCardState: () => STABLE_CARD_STATE,
}));

import {
  TeamWorkspacesSection,
  TeamReadOnlyBanner,
  TeamReadOnlyComposer,
  useTeamReadOnly,
  type TeamWorkspacesResponse,
} from '@/pages/ChatAgent/components/dalton/TeamSharing';
import { WorkspaceSharingModal } from '@/pages/ChatAgent/components/dalton/WorkspaceSharingModal';
import { usePanelFiles } from '@/pages/ChatAgent/components/chatView/usePanelFiles';
import { usePanelTarget } from '@/pages/ChatAgent/components/filePanel/usePanelTarget';
import { PreviewPanes } from '@/pages/ChatAgent/components/filePanel/PreviewPanes';
import { PreviewCrumbs } from '@/pages/ChatAgent/components/filePanel/PreviewCrumbs';
import { usePreviews } from '@/pages/ChatAgent/components/filePanel/usePreviews';
import { StoreTab } from '@/pages/ChatAgent/components/filePanel/StoreTab';
import FilePanel from '@/pages/ChatAgent/components/FilePanel';
import FileHeaderActions from '@/pages/ChatAgent/components/FileHeaderActions';
import HtmlViewer from '@/pages/ChatAgent/components/viewers/HtmlViewer';
import ChatView from '@/pages/ChatAgent/components/ChatView';
import { useActiveThreadPublisher } from '@/lib/threadLifecycle/useActiveThreadPublisher';
import { applySnapshot, resetThreadLifecycle } from '@/lib/threadLifecycle/store';

const STABLE_WORKSPACE_FILES = vi.hoisted(() => ({
  files: ['results/nvda_memo.md', 'data/revenue.csv'],
  loading: false,
  error: null,
  refresh: vi.fn(),
}));

vi.mock('@/pages/ChatAgent/hooks/useWorkspaceFiles', () => ({
  useWorkspaceFiles: () => STABLE_WORKSPACE_FILES,
}));

const SAMPLE_TEAM_RESPONSE: TeamWorkspacesResponse = {
  caller_tier: 'internal',
  members: [
    {
      user_id: 'u-richard',
      email: 'richard.lu@dalton.test',
      display_name: 'Richard Lu',
      workspaces: [
        {
          workspace_id: 'ws-richard-1',
          user_id: 'u-richard',
          owner_name: 'Richard Lu',
          owner_email: 'richard.lu@dalton.test',
          name: 'NVDA Deep Dive',
          status: 'stopped',
          updated_at: '2026-04-20T12:00:00Z',
        },
        {
          workspace_id: 'ws-richard-2',
          user_id: 'u-richard',
          owner_name: 'Richard Lu',
          owner_email: 'richard.lu@dalton.test',
          name: 'META Q1 Review',
          status: 'running',
          updated_at: '2026-04-20T15:00:00Z',
        },
      ],
    },
    {
      user_id: 'u-rex',
      email: 'rex.yuan@dalton.test',
      display_name: 'Rex Yuan',
      workspaces: [
        {
          workspace_id: 'ws-rex-1',
          user_id: 'u-rex',
          owner_name: 'Rex Yuan',
          owner_email: 'rex.yuan@dalton.test',
          name: 'TSMC Supply Chain',
          status: 'stopped',
          updated_at: '2026-04-19T10:00:00Z',
        },
      ],
    },
  ],
};

describe('Dalton Team Sharing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetThreadLifecycle();
    mockDaltonAuth.isDaltonAuth = true;
    mockGrantResult.data = undefined;
    mockShareLinkResult.data = undefined;
    mockMessages.current = [];
    mockApiGet.mockResolvedValue({ data: SAMPLE_TEAM_RESPONSE });
    mockApiPost.mockResolvedValue({ data: {} });
    Element.prototype.scrollTo = vi.fn();
  });

  it('groups team workspaces by colleague and collapses each colleague group by default', async () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.workspaces.team(), SAMPLE_TEAM_RESPONSE);

    renderWithProviders(<TeamWorkspacesSection currentWorkspaceId={null} />, {
      queryClient,
    });

    expect(screen.getByTestId('team-workspaces-section')).toBeInTheDocument();
    expect(screen.getByText('团队')).toBeInTheDocument();

    const richardToggle = screen.getByTestId('team-member-toggle-u-richard');
    const rexToggle = screen.getByTestId('team-member-toggle-u-rex');
    expect(richardToggle).toHaveAttribute('aria-expanded', 'false');
    expect(rexToggle).toHaveAttribute('aria-expanded', 'false');

    // Workspaces are hidden while collapsed by default
    expect(screen.queryByText('NVDA Deep Dive')).toBeNull();
    expect(screen.queryByText('META Q1 Review')).toBeNull();
    expect(screen.queryByText('TSMC Supply Chain')).toBeNull();

    // Expand Richard Lu -> only Richard's workspaces appear; Rex's remain collapsed
    fireEvent.click(richardToggle);
    expect(richardToggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('NVDA Deep Dive')).toBeInTheDocument();
    expect(screen.getByText('META Q1 Review')).toBeInTheDocument();
    expect(screen.queryByText('TSMC Supply Chain')).toBeNull();

    // Collapse the entire Team section
    const sectionToggle = screen.getByTestId('team-section-toggle');
    fireEvent.click(sectionToggle);
    expect(sectionToggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByTestId('team-members-list')).toBeNull();
  });

  it('does not render team section when isDaltonAuth is false', () => {
    mockDaltonAuth.isDaltonAuth = false;
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.workspaces.team(), SAMPLE_TEAM_RESPONSE);

    renderWithProviders(<TeamWorkspacesSection currentWorkspaceId={null} />, {
      queryClient,
    });

    expect(screen.queryByTestId('team-workspaces-section')).toBeNull();
  });

  it('enters read-only mode on colleague workspace and disables write operations', () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.workspaces.team(), SAMPLE_TEAM_RESPONSE);
    queryClient.setQueryData(queryKeys.user.me(), {
      user_id: 'u-chao',
      email: 'chao.yu@dalton.test',
      name: 'Chao Yu',
    });

    const { result } = renderHookWithProviders(
      () =>
        useTeamReadOnly('ws-richard-1', {
          user_id: 'u-richard',
          name: 'NVDA Deep Dive',
        }),
      { queryClient },
    );

    expect(result.current.isReadOnly).toBe(true);
    expect(result.current.ownerName).toBe('Richard Lu');

    const { result: panelResult } = renderHookWithProviders(
      () =>
        usePanelFiles({
          isHome: false,
          workspaceId: 'ws-richard-1',
          workspaceName: 'NVDA Deep Dive',
          includeSystem: false,
          readOnly: result.current.isReadOnly,
        }),
      { queryClient },
    );
    expect(panelResult.current.panelAccess.readOnly).toBe(true);
    expect(panelResult.current.panelAccess.canShare).toBe(false);
    expect(panelResult.current.panelAccess.singleFileMode).toBe(false);

    renderWithProviders(
      <div>
        <TeamReadOnlyBanner ownerName={result.current.ownerName} />
        <TeamReadOnlyComposer ownerName={result.current.ownerName} />
      </div>,
      { queryClient },
    );

    expect(screen.getByTestId('team-readonly-banner')).toHaveTextContent(
      '只读 · 来自 Richard Lu',
    );
    const input = screen.getByLabelText('只读模式输入框') as HTMLInputElement;
    const sendBtn = screen.getByRole('button', { name: '发送' }) as HTMLButtonElement;
    expect(input.disabled).toBe(true);
    expect(sendBtn.disabled).toBe(true);
  });

  it('defaults useTeamReadOnly to read-only before workspace row returns', () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.workspaces.team(), { members: [] });
    queryClient.setQueryData(queryKeys.user.me(), {
      user_id: 'u-chao',
      email: 'chao.yu@dalton.test',
      name: 'Chao Yu',
    });

    // Workspace row not loaded yet -> fail-closed as read-only
    const { result: pendingResult } = renderHookWithProviders(
      () => useTeamReadOnly('ws-unknown-yet', undefined),
      { queryClient },
    );
    expect(pendingResult.current.isReadOnly).toBe(true);

    // Once workspace row loads and belongs to currentUser -> writable
    const { result: ownerResult } = renderHookWithProviders(
      () =>
        useTeamReadOnly('ws-own', {
          user_id: 'u-chao',
          name: 'My Own Workspace',
        }),
      { queryClient },
    );
    expect(ownerResult.current.isReadOnly).toBe(false);
  });

  it('renders HtmlViewer in colleague teamReadOnly mode via sandboxed srcDoc without requesting file-grant', () => {
    const queryClient = createTestQueryClient();
    renderWithProviders(
      <HtmlViewer
        content="<html><body><h1>Mirror Report</h1></body></html>"
        fileName="report.html"
        workspaceId="ws-richard-1"
        filePath="results/report.html"
        teamReadOnly={true}
      />,
      { queryClient },
    );

    // Owner-only file-grant and share-link hooks must receive null in colleague teamReadOnly mode
    expect(mockGrant).toHaveBeenCalledWith(null);
    expect(mockShareLink).toHaveBeenCalledWith(null, null);

    const iframe = screen.getByTitle('report.html') as HTMLIFrameElement;
    expect(iframe).toBeInTheDocument();
    expect(iframe.getAttribute('sandbox')).toContain('allow-scripts');
    expect(iframe.getAttribute('sandbox')).not.toContain('allow-same-origin');
    expect(iframe.getAttribute('srcdoc')).toContain('Mirror Report');
  });

  it('preserves owner file-grant, HTML open-in-new-tab, and PDF export when owner views sibling workspace file read-only', async () => {
    const queryClient = createTestQueryClient();
    mockGrantResult.data = { prefix: '/api/v1/wsfiles/sig-owner/ws-sibling', expiresAt: Date.now() + 60_000 };
    mockShareLinkResult.data = { code: 'share-sibling-html' };

    const { result: panelResult } = renderHookWithProviders(
      () =>
        usePanelFiles({
          isHome: true,
          workspaceId: 'ws-home',
          workspaceName: 'Home',
          includeSystem: false,
          readOnly: false,
        }),
      { queryClient },
    );
    act(() => {
      panelResult.current.setOverride('ws-sibling');
    });
    expect(panelResult.current.panelAccess.readOnly).toBe(true);
    expect(panelResult.current.panelAccess.teamReadOnly).toBe(false);

    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(new Blob(['%PDF-1.4']), { status: 200 }));
    const createObjectUrlSpy = vi
      .spyOn(URL, 'createObjectURL')
      .mockReturnValue('blob:http://localhost/report.pdf');
    const revokeObjectUrlSpy = vi
      .spyOn(URL, 'revokeObjectURL')
      .mockImplementation(() => {});

    try {
      renderWithProviders(
        <div>
          <HtmlViewer
            content="<html><body><h1>Owner Sibling Report</h1></body></html>"
            fileName="report.html"
            workspaceId="ws-sibling"
            filePath="results/report.html"
            teamReadOnly={panelResult.current.panelAccess.teamReadOnly}
          />
          <FileHeaderActions
            selectedFile="results/report.html"
            isEditing={false}
            workspaceId="ws-sibling"
            fileContent="<html><body><h1>Owner Sibling Report</h1></body></html>"
            fileMime="text/html"
            canEdit={false}
            onStartEdit={vi.fn()}
            onOpenExportModal={vi.fn()}
            triggerDownloadFn={vi.fn(() => Promise.resolve())}
            canDownload={true}
            readFileFullFn={vi.fn(() => Promise.resolve({ content: '' }))}
            canShare={false}
            teamReadOnly={panelResult.current.panelAccess.teamReadOnly}
            editorRef={{ current: null }}
            canUndo={false}
            canRedo={false}
            hasUnsavedChanges={false}
            showDiff={false}
            setShowDiff={vi.fn()}
            isSaving={false}
            saveError={null}
            onSave={vi.fn()}
            onCancelEdit={vi.fn()}
          />
        </div>,
        { queryClient },
      );

      // Owner viewing sibling workspace file still requests grant & share link
      expect(mockGrant).toHaveBeenCalledWith('ws-sibling');
      expect(mockShareLink).toHaveBeenCalledWith('ws-sibling', {
        kind: 'file',
        path: 'results/report.html',
      });

      // HTML open-in-new-tab button is rendered and opens the share link
      const openNewTabBtn = screen.getByTitle(/new tab|新标签/i);
      fireEvent.click(openNewTabBtn);
      expect(openSpy).toHaveBeenCalledWith(
        expect.stringContaining('/a/share-sibling-html'),
        '_blank',
        'noopener,noreferrer',
      );

      // Trigger Save as PDF (handleExportHtmlPdf -> exportServedPdf) and assert PDF export succeeds via grant URL
      const savePdfBtn = screen.getByRole('button', { name: /save as pdf|导出为 pdf/i });
      fireEvent.click(savePdfBtn);
      await waitFor(() => {
        expect(fetchSpy).toHaveBeenCalledWith(
          '/api/v1/wsfiles/sig-owner/ws-sibling/results/report.html?format=pdf',
          expect.objectContaining({ signal: expect.any(AbortSignal) }),
        );
      });
      expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
      expect(revokeObjectUrlSpy).toHaveBeenCalledWith('blob:http://localhost/report.pdf');
    } finally {
      openSpy.mockRestore();
      fetchSpy.mockRestore();
      createObjectUrlSpy.mockRestore();
      revokeObjectUrlSpy.mockRestore();
    }
  });

  it('calls previews.open when owner opens an app preview from a sibling workspace', () => {
    const queryClient = createTestQueryClient();
    const { result: panelResult } = renderHookWithProviders(
      () =>
        usePanelFiles({
          isHome: true,
          workspaceId: 'ws-home',
          workspaceName: 'Home',
          includeSystem: false,
          readOnly: false,
        }),
      { queryClient },
    );
    act(() => {
      panelResult.current.setOverride('ws-sibling');
    });
    expect(panelResult.current.panelAccess.readOnly).toBe(true);
    expect(panelResult.current.panelAccess.teamReadOnly).toBe(false);

    const openPreviewTab = vi.fn();
    const openPreviewApi = vi.fn();
    const registerPreviewApi = vi.fn();
    const ensurePreviewApi = vi.fn();
    const onTargetHandled = vi.fn();
    const previewTarget = {
      kind: 'preview' as const,
      port: 8080,
      title: 'Sibling App',
      command: 'python app.py',
      seq: 1,
    };

    renderHookWithProviders(() =>
      usePanelTarget({
        target: previewTarget,
        tabs: { openPreview: openPreviewTab } as any,
        previews: { open: openPreviewApi } as any,
        openFileRef: vi.fn() as any,
        cancelPending: vi.fn(),
        clearSearch: vi.fn(),
        setTreeOpen: vi.fn(),
        setScopeDir: vi.fn(),
        onTargetHandled,
        teamReadOnly: panelResult.current.panelAccess.teamReadOnly,
      }),
    );

    expect(openPreviewTab).toHaveBeenCalledWith(previewTarget);
    expect(openPreviewApi).toHaveBeenCalledWith(previewTarget);
    expect(onTargetHandled).toHaveBeenCalledWith(1);

    renderWithProviders(
      <PreviewPanes
        tabs={[{ id: 'preview:8080', kind: 'preview', port: 8080, title: 'Sibling App' }]}
        activeId="preview:8080"
        previews={{
          previews: [],
          byPort: new Map(),
          register: registerPreviewApi,
          ensure: ensurePreviewApi,
          open: openPreviewApi,
          refresh: vi.fn(),
        }}
        teamReadOnly={panelResult.current.panelAccess.teamReadOnly}
      />,
    );
    expect(registerPreviewApi).toHaveBeenCalledWith({
      port: 8080,
      title: 'Sibling App',
      path: undefined,
      command: undefined,
    });
    expect(ensurePreviewApi).toHaveBeenCalledWith(8080);
  });

  it('does not send POST /seen in colleague mode or before workspace ownership is confirmed', () => {
    applySnapshot({
      as_of_seq: 10,
      oldest_included_unseen_seq: 0,
      live: [],
      unseen: [
        {
          thread_id: 'th-colleague',
          workspace_id: 'ws-richard-1',
          status: 'completed',
          run_id: 'run-1',
          run_seq: 5,
          last_seen_run_seq: 0,
        },
      ],
    });

    let canMarkSeen = false;
    // 1. Ownership not yet confirmed or colleague mode (canMarkSeen: false) -> no POST /seen
    const { rerender } = renderHookWithProviders(() =>
      useActiveThreadPublisher('th-colleague', { canMarkSeen }),
    );
    expect(mockApiPost).not.toHaveBeenCalled();

    // 2. Once ownership is confirmed (canMarkSeen: true) -> POST /seen is sent
    canMarkSeen = true;
    rerender();
    expect(mockApiPost).toHaveBeenCalledWith(
      '/api/v1/threads/th-colleague/seen',
      { run_id: 'run-1' },
    );
  });

  it('does not start, register, or refresh preview sandbox in colleague teamReadOnly mode', () => {
    const openPreviewTab = vi.fn();
    const openPreviewApi = vi.fn();
    const ensurePreviewApi = vi.fn();
    const registerPreviewApi = vi.fn();
    const refreshPreviewApi = vi.fn();
    const onTargetHandled = vi.fn();

    renderHookWithProviders(() =>
      usePanelTarget({
        target: { kind: 'preview', port: 8080, title: 'App', seq: 1 },
        tabs: { openPreview: openPreviewTab } as any,
        previews: { open: openPreviewApi } as any,
        openFileRef: vi.fn() as any,
        cancelPending: vi.fn(),
        clearSearch: vi.fn(),
        setTreeOpen: vi.fn(),
        setScopeDir: vi.fn(),
        onTargetHandled,
        teamReadOnly: true,
      }),
    );

    expect(openPreviewTab).not.toHaveBeenCalled();
    expect(openPreviewApi).not.toHaveBeenCalled();
    expect(onTargetHandled).toHaveBeenCalledWith(1);

    renderWithProviders(
      <div>
        <PreviewCrumbs
          entry={{ port: 8080, title: 'App', url: '', loading: false, error: false, reloadToken: 0 }}
          onRefresh={refreshPreviewApi}
          refreshDisabled={true}
          workspaceId={null}
        />
        <PreviewPanes
          tabs={[{ id: 'preview:8080', kind: 'preview', port: 8080, title: 'App' }]}
          activeId="preview:8080"
          previews={{
            previews: [],
            byPort: new Map(),
            register: registerPreviewApi,
            ensure: ensurePreviewApi,
            open: openPreviewApi,
            refresh: refreshPreviewApi,
          }}
          teamReadOnly={true}
        />
      </div>,
    );
    expect(registerPreviewApi).not.toHaveBeenCalled();
    expect(ensurePreviewApi).not.toHaveBeenCalled();

    // Refresh button in PreviewCrumbs is disabled and does not call refresh
    const reloadBtn = screen.getByRole('button', { name: /reload app|刷新|重新加载/i });
    expect(reloadBtn).toBeDisabled();
    fireEvent.click(reloadBtn);
    expect(refreshPreviewApi).not.toHaveBeenCalled();

    // Preview request layer (usePreviews) blocks register/open/ensure/refresh in teamReadOnly mode
    mockApiPost.mockClear();
    const { result: previewsResult } = renderHookWithProviders(() =>
      usePreviews('ws-richard-1', { teamReadOnly: true }),
    );
    act(() => {
      previewsResult.current.register({ port: 8080, title: 'App', command: 'python app.py' });
      previewsResult.current.open({ port: 8080, title: 'App', command: 'python app.py' });
      previewsResult.current.ensure(8080);
      previewsResult.current.refresh(8080);
    });
    expect(previewsResult.current.byPort.size).toBe(0);
    expect(mockApiPost).not.toHaveBeenCalled();
  });

  it('blocks personal MemoPanel and hides Add to Memo in colleague teamReadOnly mode and while ownership is loading', async () => {
    const openMemoTab = vi.fn();
    const onTargetMemoHandled = vi.fn();

    // 1. Target routing (usePanelTarget) ignores memo target in teamReadOnly mode
    renderHookWithProviders(() =>
      usePanelTarget({
        target: { kind: 'memo', key: 'memo.md', seq: 2 },
        tabs: { openMemo: openMemoTab } as any,
        previews: {} as any,
        openFileRef: vi.fn() as any,
        cancelPending: vi.fn(),
        clearSearch: vi.fn(),
        setTreeOpen: vi.fn(),
        setScopeDir: vi.fn(),
        onTargetMemoHandled,
        teamReadOnly: true,
      }),
    );
    expect(openMemoTab).not.toHaveBeenCalled();
    expect(onTargetMemoHandled).toHaveBeenCalledWith(2);

    // 2. StoreTab renders unavailable notice instead of viewer's MemoPanel when teamReadOnly is true
    const onStoreMemoHandled = vi.fn();
    const { unmount: unmountStore } = renderWithProviders(
      <StoreTab
        kind="memo"
        workspaceId="ws-richard-1"
        target={{ kind: 'memo', key: 'memo.md', seq: 3 }}
        threadId={null}
        isActive={false}
        onTargetMemoHandled={onStoreMemoHandled}
        onOpenFile={null}
        marketWatch={null}
        teamReadOnly={true}
      />,
    );
    expect(screen.getByTestId('team-memo-unavailable')).toHaveTextContent('同事的备忘录不可查看');
    expect(onStoreMemoHandled).toHaveBeenCalledWith(3);
    unmountStore();

    // 3. File right-click context menu hides "Add to Memo" in colleague teamReadOnly mode
    const { unmount: unmountPanel } = renderWithProviders(
      <FilePanel
        workspaceId="ws-richard-1"
        onClose={vi.fn()}
        files={['nvda_memo.md']}
        readOnly={true}
        teamReadOnly={true}
      />,
    );
    const fileItem = await screen.findByText('nvda_memo.md');
    fireEvent.contextMenu(fileItem);
    expect(screen.getByRole('menu')).toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: /memo|备忘录/i })).toBeNull();
    unmountPanel();

    // 4. ?file=.agents/user/memo/memo.md deep link while workspace ownership is loading does not open MemoPanel
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.workspaces.team(), { members: [] });
    queryClient.setQueryData(queryKeys.user.me(), {
      user_id: 'u-chao',
      email: 'chao.yu@dalton.test',
      name: 'Chao Yu',
    });
    // Workspace row is deliberately unset (loading state -> useTeamReadOnly fails closed to true)
    const { unmount: unmountChat } = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/chat/t/th-1?file=.agents/user/memo/memo.md']}>
          <ChatView
            workspaceId="ws-loading-colleague"
            threadId="th-1"
            onBack={vi.fn()}
            workspaceName="Loading Workspace"
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.queryByRole('tab', { name: /memo|备忘录/i })).toBeNull();
    unmountChat();
  });

  it('renders real ChatView and real MessageList in read-only mode without interactive HITL buttons, auto-send, or subagent stop', () => {
    vi.useFakeTimers();
    try {
      const queryClient = createTestQueryClient();
      queryClient.setQueryData(queryKeys.workspaces.team(), SAMPLE_TEAM_RESPONSE);
      queryClient.setQueryData(queryKeys.user.me(), {
        user_id: 'u-chao',
        email: 'chao.yu@dalton.test',
        name: 'Chao Yu',
      });
      queryClient.setQueryData(workspaceDetailQuery('ws-richard-1').queryKey, {
        workspace_id: 'ws-richard-1',
        user_id: 'u-richard',
        name: 'NVDA Deep Dive',
        status: 'running',
        is_home: false,
      });

      mockMessages.current = [
        {
          id: 'msg-user-1',
          role: 'user',
          content: 'Analyze NVDA and AMD',
          contentSegments: [{ type: 'text', content: 'Analyze NVDA and AMD', order: 0 }],
          turnIndex: 0,
          timestamp: new Date('2026-04-20T12:00:00Z'),
        } as unknown as ChatMessage,
        {
          id: 'msg-asst-1',
          role: 'assistant',
          content: 'Please review the following items:',
          contentSegments: [
            { type: 'text', content: 'Please review the following items:', order: 0 },
            { type: 'user_question', questionId: 'q-1', order: 1 },
            { type: 'create_workspace', proposalId: 'cw-1', order: 2 },
            { type: 'ptc_agent', proposalId: 'ptc-1', order: 3 },
          ],
          userQuestions: {
            'q-1': {
              interruptId: 'int-q-1',
              question: 'Which valuation multiple should we prioritize?',
              options: ['Forward P/E', 'EV/EBITDA'],
              allow_multiple: false,
              status: 'pending',
            },
          },
          workspaceProposals: {
            'cw-1': {
              interruptId: 'int-cw-1',
              workspace_name: 'AMD Competitor Study',
              workspace_description: 'Deep dive on MI350',
              status: 'pending',
            },
          },
          ptcAgentProposals: {
            'ptc-1': {
              interruptId: 'int-ptc-1',
              action_type: 'ptc_agent',
              workspace_name: 'AMD Competitor Study',
              question: 'Build AMD DCF model',
              report_back: true,
              status: 'pending',
            },
          },
          turnIndex: 0,
          timestamp: new Date('2026-04-20T12:01:00Z'),
        } as unknown as ChatMessage,
      ];

      capturedWorkflowOnStop.current = undefined;

      const { unmount } = render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter
            initialEntries={[
              {
                pathname: '/chat/t/th-1',
                state: { initialMessage: 'Should not auto-send in read-only mode' },
              },
            ]}
          >
            <ChatView
              workspaceId="ws-richard-1"
              threadId="th-1"
              initialTaskId="wf-1"
              onBack={vi.fn()}
              workspaceName="NVDA Deep Dive"
            />
          </MemoryRouter>
        </QueryClientProvider>,
      );

      // Advance past the 100ms initialMessage timer before asserting auto-send did not fire
      act(() => {
        vi.advanceTimersByTime(150);
      });
      expect(mockSendMessage).not.toHaveBeenCalled();

      // Banner and disabled composer are rendered
      expect(screen.getByTestId('team-readonly-banner')).toBeInTheDocument();
      expect(screen.getByTestId('team-readonly-composer')).toBeInTheDocument();
      // Subagent/workflow stop callback is undefined in read-only mode
      expect(screen.getByTestId('mock-workflow-run-detail')).toBeInTheDocument();
      expect(capturedWorkflowOnStop.current).toBeUndefined();
      expect(screen.queryByTestId('workflow-stop-btn')).toBeNull();
      unmount();

      // Render main agent view with real MessageList / MessageContentSegments
      renderWithProviders(
        <ChatView
          workspaceId="ws-richard-1"
          threadId="th-1"
          onBack={vi.fn()}
          workspaceName="NVDA Deep Dive"
        />,
        { queryClient },
      );

      // Card contents are visible in read-only mode
      expect(screen.getByText('Which valuation multiple should we prioritize?')).toBeInTheDocument();
      expect(screen.getByText('Forward P/E')).toBeInTheDocument();
      expect(screen.getByText('EV/EBITDA')).toBeInTheDocument();
      expect(screen.getAllByText('AMD Competitor Study').length).toBeGreaterThanOrEqual(1);
      expect(screen.getByText('Build AMD DCF model')).toBeInTheDocument();

      // Interactive HITL buttons / inputs (including "Always allow" that writes viewer prefs) are NOT rendered
      expect(screen.queryByPlaceholderText('Or type a custom answer...')).toBeNull();
      expect(screen.queryByRole('button', { name: /Skip/i })).toBeNull();
      expect(screen.queryByRole('switch')).toBeNull();
      expect(screen.queryByTitle(/alwaysApproveHint|Always allow|始终允许/i)).toBeNull();

      // Clicking an option in read-only UserQuestionCard does not trigger onAnswer
      fireEvent.click(screen.getByText('Forward P/E'));
      expect(mockAnswerQuestion).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('supports 3-state useTeamReadOnly (owner / rw / ro) and renders access badges in TeamWorkspacesSection', () => {
    const queryClient = createTestQueryClient();
    const teamWithLevels: TeamWorkspacesResponse = {
      caller_tier: 'internal',
      members: [
        {
          user_id: 'u-richard',
          email: 'richard.lu@dalton.test',
          display_name: 'Richard Lu',
          workspaces: [
            {
              workspace_id: 'ws-ro-1',
              user_id: 'u-richard',
              owner_name: 'Richard Lu',
              owner_email: 'richard.lu@dalton.test',
              name: 'NVDA ReadOnly',
              status: 'running',
              access: 'ro',
              web_access: 'ro',
              cos_access: 'ro',
            },
            {
              workspace_id: 'ws-rw-1',
              user_id: 'u-richard',
              owner_name: 'Richard Lu',
              owner_email: 'richard.lu@dalton.test',
              name: 'TSM ReadWrite',
              status: 'running',
              access: 'rw',
              web_access: 'rw',
              cos_access: 'rw',
            },
          ],
        },
      ],
    };
    queryClient.setQueryData(queryKeys.workspaces.team(), teamWithLevels);
    queryClient.setQueryData(queryKeys.user.me(), {
      user_id: 'u-chao',
      email: 'chao.yu@dalton.test',
      name: 'Chao Yu',
    });
    queryClient.setQueryData(workspaceDetailQuery('ws-own-1').queryKey, {
      workspace_id: 'ws-own-1',
      user_id: 'u-chao',
      name: 'My Own Workspace',
      status: 'running',
      is_home: false,
    });

    // 1. Owner state
    const { result: ownerRes } = renderHookWithProviders(
      () =>
        useTeamReadOnly('ws-own-1', {
          user_id: 'u-chao',
          name: 'My Own Workspace',
        }),
      { queryClient },
    );
    expect(ownerRes.current.access).toBe('owner');
    expect(ownerRes.current.isColleague).toBe(false);
    expect(ownerRes.current.isReadOnly).toBe(false);
    expect(ownerRes.current.canWriteFiles).toBe(true);

    // 2. Colleague RO state
    const { result: roRes } = renderHookWithProviders(() => useTeamReadOnly('ws-ro-1'), {
      queryClient,
    });
    expect(roRes.current.access).toBe('ro');
    expect(roRes.current.isColleague).toBe(true);
    expect(roRes.current.isReadOnly).toBe(true);
    expect(roRes.current.canWriteFiles).toBe(false);

    // 3. Colleague RW state (canWriteFiles=true, isReadOnly=false)
    const { result: rwRes } = renderHookWithProviders(() => useTeamReadOnly('ws-rw-1'), {
      queryClient,
    });
    expect(rwRes.current.access).toBe('rw');
    expect(rwRes.current.isColleague).toBe(true);
    expect(rwRes.current.isReadOnly).toBe(false);
    expect(rwRes.current.canWriteFiles).toBe(true);

    // 3b. usePanelFiles for rw colleague keeps teamReadOnly=true and canShare=false while readOnly=false
    const { result: rwPanelRes } = renderHookWithProviders(
      () =>
        usePanelFiles({
          isHome: false,
          workspaceId: 'ws-rw-1',
          workspaceName: 'TSM RW',
          includeSystem: false,
          readOnly: !rwRes.current.canWriteFiles,
          isColleague: rwRes.current.isColleague,
        }),
      { queryClient },
    );
    expect(rwPanelRes.current.panelAccess.readOnly).toBe(false);
    expect(rwPanelRes.current.panelAccess.teamReadOnly).toBe(true);
    expect(rwPanelRes.current.panelAccess.canShare).toBe(false);

    // 4. Sidebar badges for RO and RW (expand member group first)
    renderWithProviders(<TeamWorkspacesSection currentWorkspaceId="ws-rw-1" />, {
      queryClient,
    });
    fireEvent.click(screen.getByTestId('team-member-toggle-u-richard'));
    expect(screen.getByTestId('team-workspace-access-ws-ro-1')).toHaveTextContent('只读');
    expect(screen.getByTestId('team-workspace-access-ws-rw-1')).toHaveTextContent('可读写');
  });

  it('renders WorkspaceSharingModal via portal under document.body with token overlay, supports Esc/backdrop dismiss, and saves sharing settings', async () => {
    const { default: i18n } = await import('@/i18n');
    await i18n.changeLanguage('zh-CN');
    onTestFinished(async () => {
      await i18n.changeLanguage('en-US');
    });
    const wsId = '11111111-1111-4111-8111-111111111111';
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dalton.sharing(wsId), {
      workspace_id: wsId,
      cos_access: 'ro',
      web_access: 'hidden',
      is_home: false,
      can_edit: true,
      updated_by: 'u-chao',
      updated_at: '2026-04-20T12:00:00Z',
    });
    mockApiPut.mockResolvedValueOnce({
      data: {
        workspace_id: wsId,
        cos_access: 'rw',
        web_access: 'ro',
        is_home: false,
        can_edit: true,
        updated_by: 'u-chao',
        updated_at: '2026-04-20T12:05:00Z',
      },
    });

    renderWithProviders(
      <div className="enter-fade-up" data-testid="transformed-header-wrapper">
        <WorkspaceSharingModal workspaceId={wsId} />
      </div>,
      { queryClient },
    );

    const control = screen.getByTestId('workspace-sharing-control');
    const trigger = screen.getByTestId('workspace-sharing-button');
    expect(trigger).toHaveTextContent('共享设置');
    fireEvent.click(trigger);

    // 1. Portaled directly under document.body, outside the transformed parent
    const roleDialog = screen.getByRole('dialog');
    const dialog = screen.getByTestId('workspace-sharing-modal');
    expect(dialog).toBeInTheDocument();
    expect(control.contains(roleDialog)).toBe(false);
    const backdrop = roleDialog.parentElement as HTMLElement;
    expect(backdrop).not.toBeNull();
    expect(backdrop.parentElement).toBe(document.body);

    // 2. Backdrop uses the design token (--color-bg-overlay-strong) instead of hardcoded bg-black/50
    expect(backdrop.style.backgroundColor).toBe('var(--color-bg-overlay-strong)');
    expect(backdrop.className).not.toContain('bg-black/50');

    // 3. Escape closes the dialog
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();

    // 4. Backdrop click closes the dialog
    fireEvent.click(trigger);
    const reopenedBackdrop = screen.getByRole('dialog').parentElement as HTMLElement;
    fireEvent.mouseDown(reopenedBackdrop, { button: 0, clientX: 12, clientY: 12 });
    fireEvent.mouseUp(reopenedBackdrop, { button: 0, clientX: 12, clientY: 12 });
    fireEvent.click(reopenedBackdrop, { button: 0, clientX: 12, clientY: 12 });
    expect(screen.queryByRole('dialog')).toBeNull();

    // 5. Re-open, verify all two 3-option radio groups reflect initial state, edit, and save
    fireEvent.click(trigger);
    expect(screen.getByText('别人的研究总监')).toBeInTheDocument();
    expect(screen.getByText('别人的网页界面')).toBeInTheDocument();
    expect(screen.getByTestId('sharing-home-note')).toHaveTextContent(
      'Home 工作区始终不对他人开放',
    );

    const cosRw = screen.getByTestId('sharing-cos-rw') as HTMLInputElement;
    const cosRo = screen.getByTestId('sharing-cos-ro') as HTMLInputElement;
    const cosHidden = screen.getByTestId('sharing-cos-hidden') as HTMLInputElement;
    const webRw = screen.getByTestId('sharing-web-rw') as HTMLInputElement;
    const webRo = screen.getByTestId('sharing-web-ro') as HTMLInputElement;
    const webHidden = screen.getByTestId('sharing-web-hidden') as HTMLInputElement;

    expect(cosRo.checked).toBe(true);
    expect(cosRw.checked).toBe(false);
    expect(cosHidden.checked).toBe(false);
    expect(webHidden.checked).toBe(true);
    expect(webRw.checked).toBe(false);
    expect(webRo.checked).toBe(false);

    fireEvent.click(cosRw);
    fireEvent.click(webRo);
    expect(cosRw.checked).toBe(true);
    expect(webRo.checked).toBe(true);
    fireEvent.click(screen.getByTestId('sharing-save-btn'));

    await waitFor(() => {
      expect(mockApiPut).toHaveBeenCalledWith(
        `/api/v1/dalton/workspaces/${wsId}/sharing`,
        { cos_access: 'rw', web_access: 'ro' },
      );
    });
  });

  it('does not render team section when caller_tier is external', () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.workspaces.team(), {
      ...SAMPLE_TEAM_RESPONSE,
      caller_tier: 'external',
    });

    renderWithProviders(<TeamWorkspacesSection currentWorkspaceId={null} />, {
      queryClient,
    });

    expect(screen.queryByTestId('team-workspaces-section')).toBeNull();
  });

  it('does not render team section while the team query is still loading', () => {
    // Never-resolving GET keeps useQuery in the loading state (no caller_tier yet).
    mockApiGet.mockReturnValue(new Promise(() => {}));
    const queryClient = createTestQueryClient();

    renderWithProviders(<TeamWorkspacesSection currentWorkspaceId={null} />, {
      queryClient,
    });

    expect(screen.queryByTestId('team-workspaces-section')).toBeNull();
  });

  it('does not render team section when the team query fails', async () => {
    mockApiGet.mockRejectedValueOnce(new Error('network down'));
    const queryClient = createTestQueryClient();

    renderWithProviders(<TeamWorkspacesSection currentWorkspaceId={null} />, {
      queryClient,
    });

    // Wait for React Query to actually settle into the error state
    // (`useTeamWorkspaces` has retry: false) before asserting on rendering.
    await waitFor(() => {
      expect(queryClient.getQueryState(queryKeys.workspaces.team())?.status).toBe(
        'error',
      );
    });
    expect(screen.queryByTestId('team-workspaces-section')).toBeNull();
  });

  it('shows the admin-only notice instead of options for external-owned workspaces', async () => {
    const { default: i18n } = await import('@/i18n');
    await i18n.changeLanguage('zh-CN');
    onTestFinished(async () => {
      await i18n.changeLanguage('en-US');
    });
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dalton.sharing('ws-ext-1'), {
      workspace_id: 'ws-ext-1',
      cos_access: 'ro',
      web_access: 'ro',
      is_home: false,
      can_edit: false,
      owner_external: true,
      updated_by: 'u-ext',
      updated_at: '2026-10-26T12:00:00Z',
    });

    renderWithProviders(<WorkspaceSharingModal workspaceId="ws-ext-1" />, {
      queryClient,
    });

    fireEvent.click(screen.getByTestId('workspace-sharing-button'));

    expect(screen.getByTestId('sharing-external-owner-note')).toHaveTextContent(
      '外部成员的工作区仅管理员可查看',
    );
    expect(screen.queryByTestId('sharing-cos-fieldset')).toBeNull();
    expect(screen.queryByTestId('sharing-web-fieldset')).toBeNull();
    expect(screen.queryByTestId('sharing-save-btn')).toBeNull();
  });
});
