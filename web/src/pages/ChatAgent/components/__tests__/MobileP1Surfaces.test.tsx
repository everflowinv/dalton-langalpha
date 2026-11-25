import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createTestQueryClient, renderWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import { CAPABILITY_IDS, type CapabilitiesApiResponse, type CapabilityId } from '@/hooks/useCapabilities';
import { PaletteField } from '@/components/CommandPalette/PaletteField';
import { GeneralPanel } from '@/pages/Settings/panels/GeneralPanel';
import FileHeaderActions from '../FileHeaderActions';
import CodeEditor from '../viewers/CodeEditor';
import { ArtifactVersionPicker } from '../viewers/html/ArtifactVersionPicker';
import { DaltonItemRow } from '@/components/dalton/primitives/DaltonKeyValueList';
import { FeishuReadScopesCard } from '@/pages/Settings/panels/dalton/FeishuReadScopesCard';
import { DaltonNotifyTargetsCard } from '@/pages/Settings/panels/dalton/DaltonNotifyTargetsCard';
import { DaltonBudgetCard } from '@/pages/Settings/panels/dalton/DaltonBudgetCard';

const diffOptionsCapture = vi.hoisted(() => ({
  lastOptions: null as Record<string, unknown> | null,
}));

vi.mock('@monaco-editor/react', () => ({
  default: () => <div data-testid="mock-monaco-editor" />,
  DiffEditor: ({ options }: { options?: Record<string, unknown> }) => {
    diffOptionsCapture.lastOptions = options ?? null;
    return <div data-testid="mock-monaco-diff" />;
  },
  loader: { config: vi.fn() },
}));

vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'dark', setTheme: vi.fn() }),
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: { user_id: 'u-1', name: 'Test', timezone: 'UTC' } }),
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  useTeamCredentials: () => ({
    isShared: true,
    isPm: true,
    canInvite: true,
    callerTier: 'admin',
    callerTierPending: false,
    callerTierError: false,
    colleagueSharingEnabled: true,
  }),
}));

vi.mock('@/pages/Onboarding', () => ({
  useOnboarding: () => ({
    replayGuides: () => true,
    resetOnboarding: () => true,
  }),
}));

vi.mock('@/pages/Settings/panels/AppearancePanel', () => ({
  AppearancePanel: () => <div data-testid="mock-appearance-panel" />,
}));

vi.mock('@/pages/Settings/panels/DesktopPanel', () => ({
  DesktopPanel: () => null,
}));

vi.mock('@/hooks/useReportsEnabled', () => ({
  useReportsEnabled: () => false,
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: { other_preference: { dalton_notify_enabled: true, dalton_notify_targets: [] } },
    isLoading: false,
    isLoaded: true,
  }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('@/hooks/useAllWorkspaces', () => ({
  useAllWorkspaces: () => ({ data: { workspaces: [] } }),
}));

vi.mock('@/pages/Settings/hooks/useOpenClaw', () => ({
  useDaltonBudget: () => ({ data: { daily_cap_usd: 10, default_task_cap_usd: 2, timezone: 'UTC', task_caps: [] }, isLoading: false, isError: false, isFetching: false, refetch: vi.fn() }),
  useDaltonBudgetToday: () => ({ data: { spent_usd: 1, cap_usd: 10, subscription_notional_usd: 0, budget_gate_enabled: true, runs: [] }, isLoading: false, isFetching: false, refetch: vi.fn() }),
  useUpdateDaltonBudget: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpsertDaltonTaskCap: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteDaltonTaskCap: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('@/pages/Settings/utils/daltonApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/Settings/utils/daltonApi')>();
  return {
    ...actual,
    fetchDaltonFeishuReadScopes: vi.fn().mockResolvedValue({ scopes: [{ space_id: '7398217398127398123', name: '投研知识库' }] }),
    fetchDaltonFeishuSpaces: vi.fn().mockResolvedValue({ spaces: [{ space_id: '7398217398127398123', name: '投研知识库' }] }),
    fetchDaltonChannelBindings: vi.fn().mockResolvedValue({
      user_tier: 'admin',
      bindings: {
        feishu: { linked: true, platform_user_id: 'ou_1234567890abcdef', platform_username: 'Alice' },
      },
    }),
  };
});

const mediaState = vi.hoisted(() => ({ mobile: false }));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => mediaState.mobile,
  getIsMobileSnapshot: () => mediaState.mobile,
}));

const originalMatchMedia = window.matchMedia;

function setMobileMedia(mobile: boolean) {
  mediaState.mobile = mobile;
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query === '(max-width: 767px)' ? mobile : false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

function seedCapabilities(caps: readonly CapabilityId[]) {
  const qc = createTestQueryClient();
  qc.setQueryData<CapabilitiesApiResponse>(queryKeys.dalton.capabilities(), {
    profile: caps.length > 0 ? 'everflow' : 'core',
    capabilities: [...caps],
  });
  return qc;
}

describe('Mobile P1 surfaces: PaletteField, GeneralPanel /help, FileHeaderActions, CodeEditor diff, Dalton settings capabilities', () => {
  afterEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      configurable: true,
      value: originalMatchMedia,
    });
    diffOptionsCapture.lastOptions = null;
  });

  it('PaletteField renders Back and Cancel touch buttons when provided and invokes callbacks', () => {
    const onBack = vi.fn();
    const onCancel = vi.fn();
    renderWithProviders(
      <PaletteField
        query=""
        onText={vi.fn()}
        onKeyDown={vi.fn()}
        placeholder="Search"
        label="Search"
        chips={[]}
        onLeave={vi.fn()}
        trailing="Actions"
        busy={false}
        selected={false}
        combobox={{}}
        onBack={onBack}
        onCancel={onCancel}
      />,
    );

    fireEvent.click(screen.getByTestId('command-palette-back'));
    expect(onBack).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('command-palette-cancel'));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('GeneralPanel renders /help documentation links only on mobile and keeps desktop unchanged', () => {
    setMobileMedia(true);
    const { unmount } = renderWithProviders(<GeneralPanel />);
    const linksBox = screen.getByTestId('settings-mobile-help-links');
    expect(linksBox).toBeInTheDocument();
    const anchors = linksBox.querySelectorAll('a[target="_blank"]');
    expect(anchors.length).toBeGreaterThanOrEqual(3);
    unmount();

    setMobileMedia(false);
    renderWithProviders(<GeneralPanel />);
    expect(screen.queryByTestId('settings-mobile-help-links')).toBeNull();
  });

  it('FileHeaderActions collapses HTML owner actions into More menu using active artifactDoc path while keeping flat buttons on desktop', async () => {
    const user = userEvent.setup();
    const openPublish = vi.fn();
    const setMode = vi.fn();
    const setList = vi.fn();
    const mockCommentUi = {
      modeTab: null,
      listTab: null,
      setMode,
      setList,
      sends: new Map(),
    } as unknown as import('../viewers/html/comments/useCommentUi').CommentUi;

    const baseRecord = {
      artifact_id: 'art-entry',
      code: 'ent',
      url: '/a/ent',
      workspace_id: 'ws-1',
      path: 'reports/entry.html',
      name: 'entry.html',
      shared: true,
      shared_at: '2026-11-20T00:00:00Z',
      shared_seq: 2,
      keep_updated: false,
      latest_seq: 2,
      bound_thread_id: null,
      open_comments: 0,
      unread: 0,
      created_at: '2026-11-20T00:00:00Z',
    };

    const commonProps = {
      selectedFile: 'reports/entry.html',
      artifactDoc: 'reports/entry.html',
      isEditing: false,
      workspaceId: 'ws-1',
      fileContent: '<h1>Report</h1>',
      fileMime: 'text/html',
      canEdit: false,
      onStartEdit: vi.fn(),
      onOpenExportModal: vi.fn(),
      triggerDownloadFn: vi.fn().mockResolvedValue(undefined),
      canDownload: true,
      readFileFullFn: vi.fn().mockResolvedValue({ content: '' }),
      access: { kind: 'owner' as const },
      openPublish,
      onToggleHtmlSource: vi.fn(),
      editorRef: { current: null },
      canUndo: false,
      canRedo: false,
      hasUnsavedChanges: false,
      showDiff: false,
      setShowDiff: vi.fn(),
      isSaving: false,
      saveError: null,
      onSave: vi.fn(),
      onCancelEdit: vi.fn(),
      mobileHtmlTools: {
        tabId: 'tab-1',
        ui: mockCommentUi,
        threadId: 'th-1',
        modeDisabled: false,
        versionView: {
          record: baseRecord,
          version: null,
          updatedTo: null,
          versions: [
            {
              seq: 2,
              published_at: '2026-11-20T00:00:00Z',
              label: 'v2',
              origin: 'owner' as const,
              entry_path: 'reports/entry.html',
              title: 'entry.html',
              byte_len: 1024,
            },
          ],
          entry: null,
          missing: false,
          failed: false,
          retry: vi.fn(),
          pick: vi.fn(),
        },
      },
    };

    setMobileMedia(true);
    const { unmount } = renderWithProviders(<FileHeaderActions {...commonProps} />);
    const moreBtn = screen.getByTestId('file-header-mobile-more');
    expect(moreBtn).toBeInTheDocument();
    await user.click(moreBtn);
    const menu = await screen.findByTestId('file-header-mobile-more-menu');
    expect(menu).toBeInTheDocument();
    expect(screen.getByTestId('file-header-mobile-pin-dashboard')).toBeInTheDocument();
    expect(screen.getByTestId('file-header-mobile-versions')).toBeInTheDocument();
    await user.click(screen.getByTestId('file-header-mobile-publish'));
    expect(openPublish).toHaveBeenCalledWith('reports/entry.html');
    unmount();

    setMobileMedia(false);
    renderWithProviders(<FileHeaderActions {...commonProps} />);
    expect(screen.queryByTestId('file-header-mobile-more')).toBeNull();
  });

  it('ArtifactVersionPicker preserves full accessible names (live vs snapshot) and status markers on mobile while keeping desktop unchanged', () => {
    const baseRecord = {
      artifact_id: 'art-1',
      code: 'abc',
      url: '/a/abc',
      workspace_id: 'ws-1',
      path: 'report.html',
      name: 'report.html',
      shared: true,
      shared_at: '2026-11-20T00:00:00Z',
      shared_seq: 3,
      keep_updated: false,
      latest_seq: 3,
      bound_thread_id: null,
      open_comments: 0,
      unread: 0,
      created_at: '2026-11-20T00:00:00Z',
    };

    const baseView = {
      record: baseRecord,
      version: null as number | null,
      updatedTo: null as number | null,
      versions: [],
      entry: null,
      missing: false,
      failed: false,
      retry: vi.fn(),
      pick: vi.fn(),
    };

    setMobileMedia(true);
    const { rerender, unmount } = renderWithProviders(
      <ArtifactVersionPicker variant="bar" view={baseView} />,
    );
    const pubBtn = document.querySelector('[data-artifact-version-picker]') as HTMLElement;
    expect(pubBtn).toHaveAttribute('data-version-status', 'published');
    expect(pubBtn.textContent).toMatch(/Live v3|已发布 v3/);
    // Accessible name explicitly indicates live file + published version
    expect(pubBtn.getAttribute('aria-label')).toMatch(/Live file, Published v3|实时文件，已发布 v3/);

    rerender(
      <ArtifactVersionPicker
        variant="chip"
        view={{
          ...baseView,
          record: { ...baseRecord, shared: false, shared_seq: undefined },
        }}
      />,
    );
    const unsharedBtn = document.querySelector('[data-artifact-version-picker]') as HTMLElement;
    expect(unsharedBtn).toHaveAttribute('data-version-status', 'unshared');
    expect(unsharedBtn.textContent).toMatch(/Private v3|未分享 v3/);
    expect(unsharedBtn.getAttribute('aria-label')).toMatch(/Live file, Not shared|实时文件，未分享/);

    // Historical snapshot retains full "Version 2" / "版本 2" accessible name (never short "v2")
    rerender(
      <ArtifactVersionPicker
        variant="bar"
        view={{ ...baseView, version: 2, updatedTo: null }}
      />,
    );
    const snapBtn = document.querySelector('[data-artifact-version-picker]') as HTMLElement;
    expect(snapBtn).toHaveAttribute('data-version-status', 'snapshot');
    expect(snapBtn.getAttribute('aria-label')).toMatch(/Version 2|版本 2/);

    rerender(
      <ArtifactVersionPicker
        variant="bar"
        view={{ ...baseView, version: 3, updatedTo: 3 }}
      />,
    );
    const updatedBtn = document.querySelector('[data-artifact-version-picker]') as HTMLElement;
    expect(updatedBtn).toHaveAttribute('data-version-status', 'updated');
    expect(updatedBtn.textContent).toMatch(/Updated v3|已更新 v3/);
    expect(updatedBtn.getAttribute('aria-label')).toMatch(/Updated to v3|已更新到 v3/);
    unmount();

    setMobileMedia(false);
    renderWithProviders(
      <ArtifactVersionPicker variant="bar" view={baseView} />,
    );
    const desktopBtn = document.querySelector('[data-artifact-version-picker]') as HTMLElement;
    expect(desktopBtn).not.toHaveAttribute('data-version-status');
    expect(desktopBtn.textContent).toMatch(/Published v3|已发布 v3/);
  });

  it('DaltonItemRow scopes trailing wrapping and width constraints strictly to max-md', () => {
    const { container } = renderWithProviders(
      <DaltonItemRow title="Row" trailing={<button type="button">Action</button>} />,
    );
    const trailingWrap = container.querySelector('button')?.parentElement;
    expect(trailingWrap?.className).toContain('md:shrink-0');
    expect(trailingWrap?.className).toContain('max-md:flex-wrap');
    expect(trailingWrap?.className).toContain('max-md:min-w-0');
    expect(trailingWrap?.className).toContain('max-md:max-w-full');
  });

  it('CodeEditor uses single-column inline diff on mobile and side-by-side diff on desktop', () => {
    setMobileMedia(true);
    const { unmount } = renderWithProviders(
      <CodeEditor value="new" originalValue="old" diffMode fileName="app.ts" />,
    );
    expect(diffOptionsCapture.lastOptions?.renderSideBySide).toBe(false);
    unmount();

    setMobileMedia(false);
    renderWithProviders(
      <CodeEditor value="new" originalValue="old" diffMode fileName="app.ts" />,
    );
    expect(diffOptionsCapture.lastOptions?.renderSideBySide).toBe(true);
  });

  it('Dalton settings cards respect useCapabilities() all-on and all-off states and apply responsive full-width mobile classes', async () => {
    setMobileMedia(true);

    // Capabilities ALL OFF: feishu scopes & notify cards stay hidden; budget card renders with responsive inputs
    const qcOff = seedCapabilities([]);
    const { unmount } = renderWithProviders(
      <>
        <FeishuReadScopesCard />
        <DaltonNotifyTargetsCard />
        <DaltonBudgetCard />
      </>,
      { queryClient: qcOff },
    );
    expect(screen.queryByTestId('dalton-feishu-scopes-card')).toBeNull();
    expect(screen.queryByTestId('dalton-notify-targets-card')).toBeNull();
    expect(screen.getByTestId('dalton-budget-card')).toBeInTheDocument();
    const dailyCapInput = document.getElementById('dalton-budget-daily-cap-input');
    expect(dailyCapInput?.className).toContain('w-full');
    expect(dailyCapInput?.className).toContain('md:w-36');
    unmount();

    // Capabilities ALL ON: feishu scopes & notify cards mount with responsive wrapping / widths
    const qcOn = seedCapabilities(CAPABILITY_IDS);
    renderWithProviders(
      <>
        <FeishuReadScopesCard />
        <DaltonNotifyTargetsCard allowManualInput />
      </>,
      { queryClient: qcOn },
    );
    expect(await screen.findByTestId('dalton-feishu-scopes-card')).toBeInTheDocument();
    expect(await screen.findByTestId('dalton-notify-targets-admin-card')).toBeInTheDocument();
    const manualInput = screen.getByTestId('dalton-notify-manual-input');
    expect(manualInput.className).toContain('w-full');
    expect(manualInput.className).toContain('md:w-72');
  });
});
