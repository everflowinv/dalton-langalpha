import React from 'react';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router';
import type { TeamCredentialsStatus } from '@/hooks/useTeamCredentials';
import { AuthContext, type AuthContextValue } from '@/contexts/AuthContext';

const WS_ID = '11111111-1111-4111-8111-111111111111';
const THREAD_ID = '22222222-2222-4222-8222-222222222222';

const h = vi.hoisted(() => {
  const fetchStatus = vi.fn<(ctx?: { signal?: AbortSignal }) => Promise<TeamCredentialsStatus>>();
  const noop = () => {};
  const noopUnsub = () => () => {};
  const emptyMessages: Record<string, unknown>[] = [];
  const editDrafts = {
    get: () => undefined,
    put: noop,
    drop: noop,
    of: () => [],
    markFocus: noop,
    takeFocus: () => false,
    subscribe: noopUnsub,
  };
  const liveMessages = {
    get: () => emptyMessages,
    set: noop,
    subscribe: noopUnsub,
  };
  const chatResult = {
    messages: emptyMessages,
    liveMessages,
    isLoading: false,
    hasActiveSubagents: false,
    awaitingReportBack: false,
    reportBackOwed: false,
    turnStatus: { workspaceStarting: false as const, turnHeld: null, cliQueue: null },
    isCompacting: false as const,
    setIsCompacting: vi.fn(),
    queuedSend: null,
    isLoadingHistory: false,
    olderHistory: {
      snapshot: { status: 'end' as const, aligned: false, hasAhead: false },
      load: vi.fn(),
      backfill: vi.fn(),
    },
    isReconnecting: false,
    modelStatus: null as Record<string, unknown> | null,
    fallbackSuggestion: null,
    clearFallbackSuggestion: vi.fn(),
    messageError: null as string | null,
    returnedSteering: null as string | null,
    clearReturnedSteering: vi.fn(),
    returnedEdit: null as string | null,
    clearReturnedEdit: vi.fn(),
    editDrafts,
    watchCommentSends: vi.fn(() => () => {}),
    handleSendMessage: vi.fn(),
    stopWorkflow: vi.fn(),
    stopCompaction: vi.fn(),
    pendingInterrupt: null,
    handleApproveInterrupt: vi.fn(),
    handleRejectInterrupt: vi.fn(),
    handleAnswerQuestion: vi.fn(),
    handleSkipQuestion: vi.fn(),
    handleApproveCreateWorkspace: vi.fn(),
    handleRejectCreateWorkspace: vi.fn(),
    handleApproveStartQuestion: vi.fn(),
    handleRejectStartQuestion: vi.fn(),
    handleApprovePTCAgent: vi.fn(),
    handleRejectPTCAgent: vi.fn(),
    handleApproveSecretaryAction: vi.fn(),
    handleRejectSecretaryAction: vi.fn(),
    tokenUsage: null,
    threadId: '22222222-2222-4222-8222-222222222222',
    threadModels: [] as string[],
    marketWatch: null,
    isShared: false,
    insertNotification: vi.fn(),
    handleEditMessage: vi.fn(),
    handleRegenerate: vi.fn(),
    handleRetry: vi.fn(),
    handleThumbUp: vi.fn(),
    handleThumbDown: vi.fn(),
    feedbackByTurn: {} as Record<number, unknown>,
    reconnectIfStaleRun: vi.fn<() => Promise<boolean>>().mockResolvedValue(true),
    isOwnRun: () => false,
    getSubagentHistory: vi.fn().mockReturnValue(null),
    getTaskRuns: vi.fn().mockReturnValue([]),
    putTaskRun: vi.fn(),
    subagentHistoryEntries: {},
    resolveSubagentIdToAgentId: vi.fn((id: string) => id),
  };

  return {
    fetchStatus,
    capturedFilePanelProps: null as Record<string, unknown> | null,
    translationResult: {
      t: (key: string) => key,
      i18n: { language: 'en-US', resolvedLanguage: 'en-US' },
    },
    teamCredsResult: {
      isShared: true,
      canDisconnect: true,
      canManage: true,
      isPm: true,
      canInvite: true,
      callerTier: 'admin',
      authMode: 'local',
      callerTierPending: false,
      callerTierError: false,
      colleagueSharingEnabled: true,
      colleagueSharingKnown: true,
    },
    userResult: {
      user: { user_id: 'u-admin-1' },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    },
    workspaceResult: {
      data: {
        workspace_id: '11111111-1111-4111-8111-111111111111',
        user_id: 'u-admin-1',
        name: 'My Workspace',
        status: 'running',
        is_home: false,
      },
      error: null,
    },
    themeResult: { theme: 'light', setTheme: noop },
    workspaceFilesResult: {
      files: ['results/report.html'],
      loading: false,
      error: null,
      refresh: vi.fn(),
      refreshInBackground: vi.fn(),
    },
    navDataResult: {
      workspaces: [],
      workspaceThreads: {},
      expandWorkspace: vi.fn(),
      hasMore: false,
      loadAll: vi.fn(),
      loadMoreThreads: vi.fn(),
      reorderWorkspace: vi.fn(),
      canReorderWorkspaces: false,
      pinWorkspace: vi.fn(),
      renameWorkspace: vi.fn(),
    },
    chatResult,
  };
});

vi.mock('@/config/brand', () => ({
  isDaltonBuild: true,
  DALTON_PROFILE: 'everflow' as const,
  isCoreProfileBuild: false,
  BRAND_NAME: 'Everflow',
  BRAND_MARK: 'everflow',
  APP_TAGLINE: '',
  isBetaBuild: false,
}));

vi.mock('@/config/hostMode', () => ({
  isPlatformMode: false,
  HOST_MODE: 'oss',
  APP_ENTRY_PATH: '/',
}));

vi.mock('@/config/daltonAuth', () => ({
  isDaltonAuth: true,
  isDaltonLocalAuth: true,
  isDaltonSupabaseAuth: false,
  DALTON_AUTH_MODE: 'local' as const,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => h.translationResult,
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  fetchTeamCredentialsStatus: (ctx?: { signal?: AbortSignal }) => h.fetchStatus(ctx),
  useTeamCredentials: () => h.teamCredsResult,
}));

vi.mock('@/lib/framer', async () => {
  const ReactMod = await import('react');
  const FRAMER_ONLY_PROPS = new Set([
    'initial', 'animate', 'exit', 'transition', 'variants',
    'whileHover', 'whileTap', 'whileInView', 'layout', 'layoutId',
    'onAnimationComplete', 'onAnimationStart', 'drag', 'dragConstraints',
    'dragElastic', 'onDragEnd',
  ]);
  const cache = new Map<React.ElementType | string, React.ElementType>();
  const make = (Comp: React.ElementType | string): React.ElementType => {
    const hit = cache.get(Comp);
    if (hit) return hit;
    const Stub = function MotionStub({
      children,
      ...props
    }: { children?: React.ReactNode } & Record<string, unknown>) {
      const domProps: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(props)) {
        if (!FRAMER_ONLY_PROPS.has(k)) domProps[k] = v;
      }
      return (ReactMod.createElement as (type: unknown, p?: unknown, ...c: unknown[]) => React.ReactElement)(
        Comp,
        domProps,
        children,
      );
    };
    cache.set(Comp, Stub);
    return Stub;
  };
  return {
    motion: new Proxy({} as Record<string, unknown>, {
      get: (_t, key: string) => (key === 'create' ? make : make(key)),
    }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      ReactMod.createElement(ReactMod.Fragment, null, children),
    animate: () => ({ stop: () => {} }),
    useMotionValue: (v: unknown) => ({ get: () => v, set: () => {}, on: () => () => {}, onChange: () => () => {} }),
    useTransform: () => ({ get: () => 0, set: () => {}, on: () => () => {} }),
    useSpring: (v: unknown) => ({ get: () => v, set: () => {}, on: () => () => {} }),
    useReducedMotion: () => false,
    useIsPresent: () => true,
    cubicBezier: () => (t: number) => t,
  };
});

vi.mock('@/lib/supabase', () => ({ supabase: null }));
vi.mock('@/hooks/useUser', () => ({
  useUser: () => h.userResult,
}));
vi.mock('@/hooks/useWorkspace', () => ({
  useWorkspace: () => h.workspaceResult,
}));
vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => h.themeResult,
}));
vi.mock('../../Markdown', () => ({
  default: ({ content }: { content: string }) => <div data-testid="markdown-content">{content}</div>,
}));
vi.mock('../../../../../components/ui/chat-input', () => ({
  default: () => <textarea data-testid="chat-input-stub" />,
}));
vi.mock('../../../hooks/useChatMessages', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useChatMessages: () => h.chatResult,
}));
vi.mock('../../../hooks/useWorkspaceFiles', () => ({
  useWorkspaceFiles: () => h.workspaceFilesResult,
}));
vi.mock('../../../hooks/useNavigationData', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useNavigationData: () => h.navDataResult,
}));
vi.mock('../../../utils/api', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getWorkspace: vi.fn().mockResolvedValue({ workspace_id: '11111111-1111-4111-8111-111111111111', name: 'My Workspace', status: 'running' }),
  getThreadShareStatus: vi.fn().mockResolvedValue({ is_shared: false }),
  getSubagentTaskStatus: vi.fn().mockResolvedValue({}),
  getSkills: vi.fn().mockResolvedValue([]),
  getModelMetadata: vi.fn().mockResolvedValue({}),
  getWorkspaceThreads: vi.fn().mockResolvedValue({ threads: [], total: 0 }),
}));
vi.mock('../../FilePanel', () => ({
  default: (props: Record<string, unknown>) => {
    h.capturedFilePanelProps = props;
    return (
      <div
        data-testid="captured-file-panel"
        data-caller-tier={String(props.callerTier)}
        data-can-share={String(props.canShare)}
      />
    );
  },
  SYSTEM_DIR_PREFIXES: [],
}));
vi.mock('../../SandboxSettingsPanel', () => ({
  default: () => null,
  SandboxSettingsContent: () => null,
}));

import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { queryKeys } from '@/lib/queryKeys';
import { usePanelFiles } from '../../chatView/usePanelFiles';
import ChatView from '../../ChatView';
import ThreadGallery from '../../ThreadGallery';

const ADMIN_STATUS: TeamCredentialsStatus = {
  sharing: 'team',
  enabled: true,
  holder_user_id: 'u-admin-1',
  credential_user_id: 'u-admin-1',
  is_pm: true,
  can_disconnect: true,
  can_invite: true,
  caller_tier: 'admin',
  colleague_sharing_enabled: true,
  auth_mode: 'local',
};

const TEST_AUTH: AuthContextValue = {
  userId: 'u-admin-1',
  isInitialized: true,
  isLoggedIn: true,
  loginWithEmail: async () => {},
  signupWithEmail: async () => {},
  loginWithProvider: async () => {},
  logout: async () => {},
  sendMagicLink: async () => {},
  sendPasswordReset: async () => {},
  resendConfirmation: async () => {},
  verifyEmailOtp: async () => {},
  updatePassword: async () => {},
};

function makeClient() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  client.setQueryData(queryKeys.dalton.capabilities(), {
    profile: 'core',
    capabilities: [],
    ticker_logos: false,
  });
  return client;
}

function IdentityBridge({
  onReady,
}: {
  onReady: (api: { revalidate: () => Promise<void> }) => void;
}) {
  const identity = useCallerIdentity();
  React.useLayoutEffect(() => {
    onReady({ revalidate: identity.revalidate });
  }, [identity.revalidate, onReady]);
  return (
    <div
      data-testid="identity-bridge"
      data-status={identity.status}
      data-confirming={String(identity.isConfirming)}
      data-tier={identity.tier}
    />
  );
}

beforeAll(() => {
  if (!Element.prototype.scrollTo) {
    Element.prototype.scrollTo = (() => {}) as typeof Element.prototype.scrollTo;
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = (() => {}) as typeof Element.prototype.scrollIntoView;
  }
});

beforeEach(() => {
  vi.clearAllMocks();
  h.capturedFilePanelProps = null;
  h.fetchStatus.mockResolvedValue(ADMIN_STATUS);
});

describe('ChatView / ThreadGallery / usePanelFiles drop callerTier to unknown on the same tick revalidate() sets isConfirming', () => {
  it('usePanelFiles falls back to unknown and canShare=false synchronously when identity.revalidate() begins', async () => {
    const client = makeClient();
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthContext.Provider value={TEST_AUTH}>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </AuthContext.Provider>
    );

    const { result } = renderHook(
      () => ({
        identity: useCallerIdentity(),
        panel: usePanelFiles({
          isHome: false,
          workspaceId: WS_ID,
          includeSystem: false,
        }),
      }),
      { wrapper },
    );

    await waitFor(() => {
      expect(result.current.identity.status).toBe('ready');
      expect(result.current.panel.panelAccess.callerTier).toBe('admin');
      expect(result.current.panel.panelAccess.canShare).toBe(true);
    });

    // Keep the revalidation request pending so we inspect the synchronous confirming tick.
    h.fetchStatus.mockImplementationOnce(() => new Promise<TeamCredentialsStatus>(() => {}));
    act(() => {
      void result.current.identity.revalidate();
    });

    expect(result.current.identity.status).toBe('ready');
    expect(result.current.identity.isConfirming).toBe(true);
    expect(result.current.panel.panelAccess.callerTier).toBe('unknown');
    expect(result.current.panel.panelAccess.canShare).toBe(false);
  });

  it('ThreadGallery passes callerTier="unknown" to FilePanel on the same tick revalidate() begins', async () => {
    const client = makeClient();
    let triggerRevalidate: () => Promise<void> = async () => {};

    render(
      <AuthContext.Provider value={TEST_AUTH}>
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={[`/chat/${WS_ID}?file=results/report.html`]}>
            <IdentityBridge onReady={({ revalidate }) => { triggerRevalidate = revalidate; }} />
            <Routes>
              <Route
                path="/chat/:workspaceId"
                element={
                  <ThreadGallery
                    workspaceId={WS_ID}
                    onBack={vi.fn()}
                    onThreadSelect={vi.fn()}
                  />
                }
              />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('captured-file-panel')).toHaveAttribute('data-caller-tier', 'admin');
    });

    h.fetchStatus.mockImplementationOnce(() => new Promise<TeamCredentialsStatus>(() => {}));
    act(() => {
      void triggerRevalidate();
    });

    expect(screen.getByTestId('identity-bridge')).toHaveAttribute('data-status', 'ready');
    expect(screen.getByTestId('identity-bridge')).toHaveAttribute('data-confirming', 'true');
    expect(screen.getByTestId('captured-file-panel')).toHaveAttribute('data-caller-tier', 'unknown');
  });

  it('ChatView passes callerTier="unknown" and canShare=false to FilePanel on the same tick revalidate() begins', async () => {
    const client = makeClient();
    let triggerRevalidate: () => Promise<void> = async () => {};

    const { container } = render(
      <AuthContext.Provider value={TEST_AUTH}>
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={[`/chat/t/${THREAD_ID}`]}>
            <IdentityBridge onReady={({ revalidate }) => { triggerRevalidate = revalidate; }} />
            <ChatView
              workspaceId={WS_ID}
              threadId={THREAD_ID}
              onBack={vi.fn()}
              workspaceName="My Workspace"
            />
          </MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('identity-bridge')).toHaveAttribute('data-status', 'ready');
    });

    const toggleBtn = container.querySelector('[data-panel-toggle="header"]');
    expect(toggleBtn).not.toBeNull();
    fireEvent.click(toggleBtn!);

    await waitFor(() => {
      const panel = screen.getByTestId('captured-file-panel');
      expect(panel).toHaveAttribute('data-caller-tier', 'admin');
      expect(panel).toHaveAttribute('data-can-share', 'true');
    });

    h.fetchStatus.mockImplementationOnce(() => new Promise<TeamCredentialsStatus>(() => {}));
    act(() => {
      void triggerRevalidate();
    });

    expect(screen.getByTestId('identity-bridge')).toHaveAttribute('data-status', 'ready');
    expect(screen.getByTestId('identity-bridge')).toHaveAttribute('data-confirming', 'true');
    const panel = screen.getByTestId('captured-file-panel');
    expect(panel).toHaveAttribute('data-caller-tier', 'unknown');
    expect(panel).toHaveAttribute('data-can-share', 'false');
  });
});
