import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createTestQueryClient, renderWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import type { Artifact, ArtifactComment, ArtifactStatus, ArtifactVersion } from '@/types/api';
import FileHeaderActions from '../FileHeaderActions';
import { ArtifactCrumbs } from '../filePanel/ArtifactCrumbs';
import { PinPageButton } from '../filePanel/PinPageButton';
import { CommentTools } from '../viewers/html/comments/CommentTools';

const mediaState = vi.hoisted(() => ({ mobile: true }));
const sendBatchesMock = vi.hoisted(() => vi.fn());
const publishArtifactMock = vi.hoisted(() => vi.fn());
const getArtifactStatusMock = vi.hoisted(() => vi.fn());
const commentSenderState = vi.hoisted(() => ({ besideThreadId: null as string | null }));
const commentDestState = vi.hoisted(() => ({ canBind: false }));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => mediaState.mobile,
  getIsMobileSnapshot: () => mediaState.mobile,
}));

vi.mock('../viewers/html/usePageScout', () => ({
  usePageScout: () => ({
    holding: false,
    failed: false,
    retry: vi.fn(),
    reads: true,
    element: null,
    follow: vi.fn(),
  }),
}));

vi.mock('../../utils/api/artifacts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/api/artifacts')>();
  return {
    ...actual,
    publishArtifact: (...args: unknown[]) => publishArtifactMock(...args),
    getArtifactStatus: (...args: unknown[]) => getArtifactStatusMock(...args),
  };
});

vi.mock('../artifactComments/sendContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../artifactComments/sendContext')>();
  return {
    ...actual,
    useArtifactCommentSender: () => ({
      beside: commentSenderState.besideThreadId ? { threadId: commentSenderState.besideThreadId } : null,
      running: false,
      send: vi.fn(),
    }),
  };
});

vi.mock('../artifactComments/useCommentDestinations', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../artifactComments/useCommentDestinations')>();
  return {
    ...actual,
    useCommentDestinations: (workspaceId: string | null | undefined, docPath: { kind: 'file' | 'chart'; path: string } | null) => {
      if (commentDestState.canBind && workspaceId && docPath) {
        return {
          choices: [
            {
              key: 'thread:th-old',
              label: 'Old Thread',
              target: { threadId: 'th-old' },
              isDefault: true,
            },
          ],
          canBind: true,
          targetFor: () => ({ threadId: 'th-old' }),
        };
      }
      return actual.useCommentDestinations(workspaceId, docPath);
    },
  };
});

vi.mock('../artifactComments/commentSends', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../artifactComments/commentSends')>();
  return {
    ...actual,
    sendBatches: (...args: unknown[]) => sendBatchesMock(...args),
  };
});

function makeArtifact(overrides: Partial<Artifact>): Artifact {
  return {
    kind: 'file' as const,
    artifact_id: 'art-a',
    code: 'code-a',
    url: '/a/code-a',
    workspace_id: 'ws-1',
    path: 'reports/a.html',
    name: 'a.html',
    shared: true,
    shared_at: '2026-11-20T00:00:00Z',
    shared_seq: 2,
    keep_updated: false,
    latest_seq: 3,
    bound_thread_id: null,
    open_comments: 0,
    unread: 0,
    created_at: '2026-11-20T00:00:00Z',
    ...overrides,
  };
}

function makeStatus(overrides: Partial<ArtifactStatus>): ArtifactStatus {
  return {
    latest_seq: 3,
    live_sha256: 'a'.repeat(64),
    page_sha256: 'a'.repeat(64),
    files: [{ path: 'reports/a.html', size: 1024, reason: 'entry', optional: false }],
    left_out: [],
    total_size: 1024,
    changed: false,
    drift: null,
    ...overrides,
  };
}

function makeCommentUi() {
  return {
    modeTab: null,
    listTab: null,
    setMode: vi.fn(),
    setList: vi.fn(),
    docOf: (_tabId: string, fallback: string) => fallback,
    sends: {
      get: () => undefined,
      sending: vi.fn(),
      taken: vi.fn(),
      refused: vi.fn(),
      clear: vi.fn(),
      release: vi.fn(() => false),
    },
  } as unknown as import('../viewers/html/comments/useCommentUi').CommentUi;
}

function baseProps(overrides: Record<string, unknown> = {}) {
  return {
    selectedFile: 'reports/a.html',
    artifactDoc: 'reports/a.html',
    onOpenDocAsTab: vi.fn(),
    isEditing: false,
    workspaceId: 'ws-1',
    fileContent: '<h1>Report</h1>',
    fileMime: 'text/html',
    canEdit: true,
    onStartEdit: vi.fn(),
    onOpenExportModal: vi.fn(),
    triggerDownloadFn: vi.fn().mockResolvedValue(undefined),
    canDownload: true,
    readFileFullFn: vi.fn().mockResolvedValue({ content: '' }),
    access: { kind: 'owner' as const },
    openPublish: vi.fn(),
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
    ...overrides,
  };
}

describe('Mobile HTML More menu R5: linked-doc separation, inline versions & PDF options, a11y, draft guard, and Send all status', () => {
  beforeEach(() => {
    mediaState.mobile = true;
    sendBatchesMock.mockReset();
    publishArtifactMock.mockReset();
    getArtifactStatusMock.mockReset();
    commentSenderState.besideThreadId = null;
    commentDestState.canBind = false;
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('shows ONLY "Open this report in its own tab" and comment actions when viewing linked doc B inside tab A, then shows full B menu once B opens as its own tab', async () => {
    const user = userEvent.setup();
    const openPublish = vi.fn();
    const onOpenDocAsTab = vi.fn();
    const recordA = makeArtifact({ artifact_id: 'art-a', path: 'reports/a.html', name: 'a.html', shared: true, latest_seq: 2, shared_seq: 2 });
    const recordB = makeArtifact({ artifact_id: 'art-b', path: 'reports/b.html', name: 'b.html', shared: true, latest_seq: 3, shared_seq: 3 });

    const qc = createTestQueryClient();
    qc.setQueryData(queryKeys.artifacts.byPath('ws-1', 'reports/a.html'), recordA);
    qc.setQueryData(queryKeys.artifacts.byPath('ws-1', 'reports/b.html'), recordB);
    qc.setQueryData(queryKeys.artifacts.statusWith('art-a', [], []), makeStatus({ changed: true }));
    qc.setQueryData(queryKeys.artifacts.statusWith('art-b', [], []), makeStatus({ changed: true }));

    // 1. Inside tab A, iframe navigated to linked report B -> hide all document-dependent actions
    const { rerender } = renderWithProviders(
      <FileHeaderActions
        {...baseProps({
          selectedFile: 'reports/a.html',
          artifactDoc: 'reports/b.html',
          onOpenDocAsTab,
          openPublish,
          openUnshareOnly: vi.fn(),
          mobileHtmlTools: {
            tabId: 'tab-1',
            ui: makeCommentUi(),
            threadId: 'th-1',
            versionView: {
              record: recordA,
              version: null,
              updatedTo: null,
              versions: [],
              entry: null,
              missing: false,
              failed: false,
              retry: vi.fn(),
              pick: vi.fn(),
            },
            modeDisabled: false,
            readAt: 100,
          },
        })}
      />,
      { queryClient: qc },
    );

    await user.click(screen.getByTestId('file-header-mobile-more'));
    await screen.findByTestId('file-header-mobile-more-menu');

    // Only "Open this report in its own tab" and comment controls appear
    const openTabItem = screen.getByTestId('file-header-mobile-open-doc-tab');
    expect(openTabItem).toBeInTheDocument();
    expect(screen.getByTestId('file-header-mobile-comment')).toBeInTheDocument();
    expect(screen.getByTestId('file-header-mobile-comment-list')).toBeInTheDocument();

    // All document-dependent actions are hidden while tab A displays linked doc B
    expect(screen.queryByTestId('file-header-mobile-publish')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-update')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-unshare')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-versions')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-pin-dashboard')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-source')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-open-url')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-edit')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-download')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-save-pdf')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-pdf-options')).toBeNull();

    // Clicking "Open this report in its own tab" opens B
    await user.click(openTabItem);
    expect(onOpenDocAsTab).toHaveBeenCalledWith('reports/b.html');

    // 2. Once B is opened as its own tab (selectedFile === artifactDoc === B), full menu targets B
    qc.setQueryData(queryKeys.artifacts.byPath('ws-1', 'reports/b.html'), recordB);
    qc.setQueryData(
      queryKeys.artifacts.statusWith('art-b', [], []),
      makeStatus({ changed: true, drift: { added: ['reports/chart.js'], changed: [], removed: [] } }),
    );
    rerender(
      <FileHeaderActions
        {...baseProps({
          selectedFile: 'reports/b.html',
          artifactDoc: 'reports/b.html',
          onOpenDocAsTab,
          openPublish,
          mobileHtmlTools: {
            tabId: 'tab-2',
            ui: makeCommentUi(),
            threadId: 'th-1',
            versionView: {
              record: recordB,
              version: null,
              updatedTo: null,
              versions: [],
              entry: null,
              missing: false,
              failed: false,
              retry: vi.fn(),
              pick: vi.fn(),
            },
            modeDisabled: false,
            readAt: 100,
          },
        })}
      />,
    );

    await user.click(screen.getByTestId('file-header-mobile-more'));
    await screen.findByTestId('file-header-mobile-more-menu');
    expect(screen.queryByTestId('file-header-mobile-open-doc-tab')).toBeNull();
    expect(screen.getByTestId('file-header-mobile-versions')).toBeInTheDocument();
    expect(screen.getByTestId('file-header-mobile-download')).toBeInTheDocument();
    expect(screen.getByTestId('file-header-mobile-pdf-options')).toBeInTheDocument();

    await user.click(screen.getByTestId('file-header-mobile-update'));
    expect(openPublish).toHaveBeenCalledWith('reports/b.html');
  });

  it('invalidates active tab document publish status when readAt changes', () => {
    const recordA = makeArtifact({ artifact_id: 'art-a', path: 'reports/a.html', shared: true, latest_seq: 2, shared_seq: 2 });
    const qc = createTestQueryClient();
    qc.setQueryData(queryKeys.artifacts.byPath('ws-1', 'reports/a.html'), recordA);
    qc.setQueryData(queryKeys.artifacts.statusWith('art-a', [], []), makeStatus({ changed: false }));
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');

    const versionView = {
      record: recordA,
      version: null,
      updatedTo: null,
      versions: [],
      entry: null,
      missing: false,
      failed: false,
      retry: vi.fn(),
      pick: vi.fn(),
    };

    const props1 = baseProps({
      mobileHtmlTools: {
        tabId: 'tab-1',
        ui: makeCommentUi(),
        threadId: 'th-1',
        versionView,
        modeDisabled: false,
        readAt: 1000,
      },
    });
    const { rerender } = renderWithProviders(<FileHeaderActions {...props1} />, { queryClient: qc });
    expect(invalidateSpy).not.toHaveBeenCalled();

    rerender(
      <FileHeaderActions
        {...baseProps({
          mobileHtmlTools: {
            tabId: 'tab-1',
            ui: makeCommentUi(),
            threadId: 'th-1',
            versionView,
            modeDisabled: false,
            readAt: 2000,
          },
        })}
      />,
    );
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.artifacts.status('art-a') });
  });

  it('desktop ArtifactCrumbs re-queries status when switching between two published reports and returning to the earlier readAt', () => {
    const recordA = makeArtifact({ artifact_id: 'art-a', path: 'reports/a.html', shared: true, latest_seq: 2, shared_seq: 2 });
    const recordB = makeArtifact({ artifact_id: 'art-b', path: 'reports/b.html', shared: true, latest_seq: 1, shared_seq: 1 });
    const qc = createTestQueryClient();
    qc.setQueryData(queryKeys.artifacts.statusWith('art-a', [], []), makeStatus({ changed: false }));
    qc.setQueryData(queryKeys.artifacts.statusWith('art-b', [], []), makeStatus({ changed: false }));
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');

    const makeView = (record: Artifact) => ({
      record,
      version: null,
      updatedTo: null,
      versions: [],
      entry: null,
      missing: false,
      failed: false,
      retry: vi.fn(),
      pick: vi.fn(),
    });

    // 1. Open report A (readAt = 1000) -> initial mount does not invalidate
    const { rerender } = renderWithProviders(
      <ArtifactCrumbs view={makeView(recordA)} onReview={vi.fn()} readAt={1000} />,
      { queryClient: qc },
    );
    expect(invalidateSpy).not.toHaveBeenCalled();

    // 2. Switch to report B (readAt = 2000) -> invalidates B
    rerender(<ArtifactCrumbs view={makeView(recordB)} onReview={vi.fn()} readAt={2000} />);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.artifacts.status('art-b') });

    // 3. Switch back to previously-read report A (cached readAt = 1000) -> re-queries A's status as on main
    invalidateSpy.mockClear();
    rerender(<ArtifactCrumbs view={makeView(recordA)} onReview={vi.fn()} readAt={1000} />);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.artifacts.status('art-a') });
  });

  it('still invalidates tab A publish status when A is re-read while previewing linked doc B and user returns to A', () => {
    const recordA = makeArtifact({ artifact_id: 'art-a', path: 'reports/a.html', shared: true, latest_seq: 2, shared_seq: 2 });
    const recordB = makeArtifact({ artifact_id: 'art-b', path: 'reports/b.html', shared: true, latest_seq: 1, shared_seq: 1 });
    const qc = createTestQueryClient();
    qc.setQueryData(queryKeys.artifacts.byPath('ws-1', 'reports/a.html'), recordA);
    qc.setQueryData(queryKeys.artifacts.byPath('ws-1', 'reports/b.html'), recordB);
    qc.setQueryData(queryKeys.artifacts.statusWith('art-a', [], []), makeStatus({ changed: false }));
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');

    const versionView = {
      record: recordA,
      version: null,
      updatedTo: null,
      versions: [],
      entry: null,
      missing: false,
      failed: false,
      retry: vi.fn(),
      pick: vi.fn(),
    };

    // 1. Initial render on A with readAt = 1000
    const { rerender } = renderWithProviders(
      <FileHeaderActions
        {...baseProps({
          selectedFile: 'reports/a.html',
          artifactDoc: 'reports/a.html',
          mobileHtmlTools: {
            tabId: 'tab-1',
            ui: makeCommentUi(),
            threadId: 'th-1',
            versionView,
            modeDisabled: false,
            readAt: 1000,
          },
        })}
      />,
      { queryClient: qc },
    );
    expect(invalidateSpy).not.toHaveBeenCalled();

    // 2. Navigate in-frame to linked B; agent edits A while user views B (readAt bumps to 2000)
    rerender(
      <FileHeaderActions
        {...baseProps({
          selectedFile: 'reports/a.html',
          artifactDoc: 'reports/b.html',
          mobileHtmlTools: {
            tabId: 'tab-1',
            ui: makeCommentUi(),
            threadId: 'th-1',
            versionView,
            modeDisabled: false,
            readAt: 2000,
          },
        })}
      />,
    );
    expect(invalidateSpy).not.toHaveBeenCalled();

    // 3. Return from linked B back to A -> A's status MUST now be invalidated
    rerender(
      <FileHeaderActions
        {...baseProps({
          selectedFile: 'reports/a.html',
          artifactDoc: 'reports/a.html',
          mobileHtmlTools: {
            tabId: 'tab-1',
            ui: makeCommentUi(),
            threadId: 'th-1',
            versionView,
            modeDisabled: false,
            readAt: 2000,
          },
        })}
      />,
    );
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.artifacts.status('art-a') });
  });

  it('renders inline versions & inline PDF options, full aria-label, data-artifact-version-picker, and announces Send all outcome in role="status"', async () => {
    const user = userEvent.setup();
    const record = makeArtifact({
      artifact_id: 'art-a',
      path: 'reports/a.html',
      shared: true,
      latest_seq: 3,
      shared_seq: 2,
    });
    const versions: ArtifactVersion[] = [
      { seq: 3, published_at: '2026-11-22T00:00:00Z', label: 'Draft v3', origin: 'owner', entry_path: 'reports/a.html', title: 'a.html', byte_len: 1024 },
      { seq: 2, published_at: '2026-11-20T00:00:00Z', label: 'Shared v2', origin: 'owner', entry_path: 'reports/a.html', title: 'a.html', byte_len: 1024 },
    ];
    const queuedComment = {
      comment_id: 'c-1',
      artifact_id: 'art-a',
      status: 'open',
      delivery: 'queued',
      thread_hidden: false,
      body: 'Check margin',
      thread_id: 'th-1',
      created_at: '2026-11-22T00:00:00Z',
    } as unknown as ArtifactComment;

    const qc = createTestQueryClient();
    qc.setQueryData(queryKeys.artifacts.byPath('ws-1', 'reports/a.html'), record);
    qc.setQueryData(queryKeys.artifacts.statusWith('art-a', [], []), makeStatus({ changed: true }));
    qc.setQueryData(queryKeys.artifacts.comments('art-a'), [queuedComment]);

    sendBatchesMock.mockResolvedValue([
      {
        batch: { to: { threadId: 'th-1' }, comments: [queuedComment] },
        threadId: 'th-1',
        reason: null,
      },
    ]);

    const pick = vi.fn();
    renderWithProviders(
      <FileHeaderActions
        {...baseProps({
          mobileHtmlTools: {
            tabId: 'tab-1',
            ui: makeCommentUi(),
            threadId: 'th-1',
            modeDisabled: false,
            readAt: 100,
            versionView: {
              record,
              version: null,
              updatedTo: null,
              versions,
              entry: null,
              missing: false,
              failed: false,
              retry: vi.fn(),
              pick,
            },
          },
        })}
      />,
      { queryClient: qc },
    );

    const moreBtn = screen.getByTestId('file-header-mobile-more');
    expect(moreBtn).toHaveAttribute('data-artifact-version-picker');
    await user.click(moreBtn);

    const menu = await screen.findByTestId('file-header-mobile-more-menu');
    expect(menu).toHaveAttribute('data-artifact-version-picker');

    // Versions trigger carries full accessible name including live status, published v2, and changed-since note
    const versionsBtn = screen.getByTestId('file-header-mobile-versions');
    expect(versionsBtn).toHaveAttribute('aria-expanded', 'false');
    expect(versionsBtn.getAttribute('aria-label')).toMatch(/Live file, Published v2|实时文件，已发布 v2/);
    expect(versionsBtn.getAttribute('aria-label')).toMatch(/changed since|之后有改动/);

    // Clicking expands inline version list inside the same menu with Latest and Visitors see this tags
    await user.click(versionsBtn);
    expect(versionsBtn).toHaveAttribute('aria-expanded', 'true');
    const inlineList = await screen.findByTestId('file-header-mobile-versions-list');
    expect(menu.contains(inlineList)).toBe(true);
    expect(inlineList.textContent).toMatch(/Latest|最新/);
    expect(inlineList.textContent).toMatch(/Visitors see this|访问者看到的版本/);

    // Clicking PDF options expands inline inside the same menu (no sideways submenu)
    const pdfBtn = screen.getByTestId('file-header-mobile-pdf-options');
    expect(pdfBtn).toHaveAttribute('aria-expanded', 'false');
    await user.click(pdfBtn);
    expect(pdfBtn).toHaveAttribute('aria-expanded', 'true');
    const inlinePdfList = await screen.findByTestId('file-header-mobile-pdf-options-list');
    expect(menu.contains(inlinePdfList)).toBe(true);
    expect(inlinePdfList.textContent).toMatch(/100%/);

    // Triggering Send all announces the outcome in a persistent role="status" live region
    const sendAllItem = screen.getByTestId('file-header-mobile-send-all');
    await user.click(sendAllItem);
    const statusRegion = screen.getByTestId('file-header-mobile-send-all-status');
    expect(statusRegion).toHaveAttribute('role', 'status');
    await waitFor(() => {
      expect(statusRegion.textContent?.trim().length).toBeGreaterThan(0);
    });
  });

  it('announces legacyRefused and updatedTo accessible states in the mobile version trigger and live region', async () => {
    const user = userEvent.setup();
    const refusedRecord = makeArtifact({
      artifact_id: 'art-ref',
      path: 'reports/a.html',
      shared: true,
      latest_seq: 1,
      shared_seq: 1,
      legacy_refused: 'too_many_files',
    });
    const qc = createTestQueryClient();
    qc.setQueryData(queryKeys.artifacts.byPath('ws-1', 'reports/a.html'), refusedRecord);

    const { rerender } = renderWithProviders(
      <FileHeaderActions
        {...baseProps({
          mobileHtmlTools: {
            tabId: 'tab-1',
            ui: makeCommentUi(),
            threadId: 'th-1',
            modeDisabled: false,
            readAt: 100,
            versionView: {
              record: refusedRecord,
              version: null,
              updatedTo: null,
              versions: [],
              entry: null,
              missing: false,
              failed: false,
              retry: vi.fn(),
              pick: vi.fn(),
            },
          },
        })}
      />,
      { queryClient: qc },
    );

    await user.click(screen.getByTestId('file-header-mobile-more'));
    const versionsBtn = await screen.findByTestId('file-header-mobile-versions');
    expect(versionsBtn.getAttribute('aria-label')).toMatch(/Visitors can't open it|访问者无法打开/);
    expect(screen.getByTestId('file-header-mobile-update').textContent).toMatch(/Visitors can't open it|访问者无法打开/);

    // Updated to v3 notice reflected in both trigger aria-label and role="status" live region
    const updatedRecord = { ...refusedRecord, legacy_refused: null, latest_seq: 3, shared_seq: 3 };
    qc.setQueryData(queryKeys.artifacts.byPath('ws-1', 'reports/a.html'), updatedRecord);
    rerender(
      <FileHeaderActions
        {...baseProps({
          mobileHtmlTools: {
            tabId: 'tab-1',
            ui: makeCommentUi(),
            threadId: 'th-1',
            modeDisabled: false,
            readAt: 100,
            versionView: {
              record: updatedRecord,
              version: 3,
              updatedTo: 3,
              versions: [],
              entry: null,
              missing: false,
              failed: false,
              retry: vi.fn(),
              pick: vi.fn(),
            },
          },
        })}
      />,
    );

    expect(screen.getByTestId('file-header-mobile-versions').getAttribute('aria-label')).toMatch(/Updated to v3|已更新到 v3/);
    expect(screen.getByTestId('file-header-mobile-version-status').textContent).toMatch(/Updated to v3|已更新到 v3/);
  });

  it('uses desktop ArtifactCrumbs Update flow on mobile: publishes in-place when no files were added, opens Publish dialog when files were added', async () => {
    const user = userEvent.setup();
    const openPublish = vi.fn();
    const recordA = makeArtifact({
      artifact_id: 'art-a',
      path: 'reports/a.html',
      shared: true,
      latest_seq: 2,
      shared_seq: 2,
    });
    const contentOnlyStatus = makeStatus({
      changed: true,
      drift: null,
      files: [{ path: 'reports/a.html', size: 1024, reason: 'entry', optional: false }],
      page_sha256: 'b'.repeat(64),
    });
    getArtifactStatusMock.mockResolvedValue(contentOnlyStatus);
    publishArtifactMock.mockResolvedValue({
      version: {
        seq: 3,
        published_at: '2026-11-24T00:00:00Z',
        label: null,
        origin: 'owner',
        entry_path: 'reports/a.html',
        title: 'a.html',
        byte_len: 1024,
      },
      cut: true,
    });

    const qc = createTestQueryClient();
    qc.setQueryData(queryKeys.artifacts.byPath('ws-1', 'reports/a.html'), recordA);
    qc.setQueryData(queryKeys.artifacts.statusWith('art-a', [], []), contentOnlyStatus);

    const versionView = {
      record: recordA,
      version: null,
      updatedTo: null,
      versions: [],
      entry: null,
      missing: false,
      failed: false,
      retry: vi.fn(),
      pick: vi.fn(),
    };

    const { rerender } = renderWithProviders(
      <FileHeaderActions
        {...baseProps({
          openPublish,
          mobileHtmlTools: {
            tabId: 'tab-1',
            ui: makeCommentUi(),
            threadId: 'th-1',
            versionView,
            modeDisabled: false,
            readAt: 100,
          },
        })}
      />,
      { queryClient: qc },
    );

    // 1. Content-only change (no added files): publishes in-place via UpdateReview without opening Publish dialog
    await user.click(screen.getByTestId('file-header-mobile-more'));
    await user.click(await screen.findByTestId('file-header-mobile-update'));

    await waitFor(() => {
      expect(publishArtifactMock).toHaveBeenCalledWith(
        'art-a',
        expect.objectContaining({
          files: ['reports/a.html'],
          page_sha256: 'b'.repeat(64),
        }),
      );
    });
    expect(openPublish).not.toHaveBeenCalled();

    // 2. Drift with added file: delegates to openPublish for owner review, matching desktop ArtifactCrumbs
    publishArtifactMock.mockClear();
    const addedFileStatus = makeStatus({
      changed: true,
      drift: { added: ['reports/extra.js'], changed: [], removed: [] },
    });
    qc.setQueryData(queryKeys.artifacts.statusWith('art-a', [], []), addedFileStatus);
    rerender(
      <FileHeaderActions
        {...baseProps({
          openPublish,
          mobileHtmlTools: {
            tabId: 'tab-1',
            ui: makeCommentUi(),
            threadId: 'th-1',
            versionView,
            modeDisabled: false,
            readAt: 100,
          },
        })}
      />,
    );

    await user.click(screen.getByTestId('file-header-mobile-more'));
    await user.click(await screen.findByTestId('file-header-mobile-update'));
    expect(openPublish).toHaveBeenCalledWith('reports/a.html');
    expect(publishArtifactMock).not.toHaveBeenCalled();
  });

  it('matches desktop toolbar + crumbs action set 1-to-1 across owner (full controls), shared non-owner, and unshared non-owner inputs', async () => {
    const user = userEvent.setup();
    const recordSharedOwner = makeArtifact({
      artifact_id: 'art-a',
      path: 'reports/a.html',
      shared: true,
      latest_seq: 2,
      shared_seq: 2,
      bound_thread_id: 'th-old',
    });
    const recordSharedNonOwner = makeArtifact({
      artifact_id: 'art-a',
      path: 'reports/a.html',
      shared: true,
      latest_seq: 2,
      shared_seq: 2,
      bound_thread_id: null,
    });
    const recordUnshared = makeArtifact({
      artifact_id: 'art-a',
      path: 'reports/a.html',
      shared: false,
      latest_seq: 0,
      shared_seq: 0,
      bound_thread_id: null,
    });
    const queuedComment = {
      comment_id: 'c-q1',
      artifact_id: 'art-a',
      status: 'open',
      delivery: 'queued',
      thread_hidden: false,
      body: 'Queued comment',
      thread_id: 'th-old',
      created_at: '2026-11-22T00:00:00Z',
    } as unknown as ArtifactComment;

    type ActionKey =
      | 'comment'
      | 'commentList'
      | 'sendAll'
      | 'pinDashboard'
      | 'versions'
      | 'update'
      | 'publish'
      | 'bind'
      | 'unbind'
      | 'unshare'
      | 'source'
      | 'openUrl'
      | 'download'
      | 'savePdf'
      | 'pdfOptions'
      | 'pdfBranding'
      | 'pdfPageNumbers'
      | 'pdfScale80'
      | 'pdfScale100'
      | 'pdfScale125'
      | 'edit';

    const collectDesktopActions = async (ownerHasMenu: boolean): Promise<ActionKey[]> => {
      const keys = new Set<ActionKey>();
      if (screen.queryByRole('button', { name: /^comment$|评论/i })) keys.add('comment');
      if (screen.queryByRole('button', { name: /open comment|comments|评论列表/i })) keys.add('commentList');
      if (screen.queryByRole('button', { name: /send all|全部发送/i })) keys.add('sendAll');
      if (screen.queryByRole('button', { name: /pin.*dashboard|set as dashboard|设为看板|钉成仪表盘|已钉/i })) keys.add('pinDashboard');
      if (screen.queryByRole('button', { name: /live file|published v|实时文件|已发布/i })) keys.add('versions');
      if (screen.queryByRole('button', { name: /^update$|更新/i })) keys.add('update');
      if (screen.queryByRole('button', { name: /stop sharing|停止分享/i })) keys.add('unshare');
      if (screen.queryByRole('button', { name: /view source|view preview|查看源码|查看预览/i })) keys.add('source');
      if (screen.queryByRole('button', { name: /open in new tab|在新标签页打开/i })) keys.add('openUrl');
      if (screen.queryByTitle(/edit file|编辑文件/i)) keys.add('edit');

      const publishBtn = screen.queryByRole('button', { name: /^publish$|发布/i });
      if (publishBtn) {
        keys.add('publish');
        if (ownerHasMenu) {
          await user.click(publishBtn);
          if (screen.queryByRole('menuitem', { name: /bind to this thread|bind comments here|绑定到此会话/i })) keys.add('bind');
          if (screen.queryByRole('menuitem', { name: /unbind|解除绑定/i })) keys.add('unbind');
          await user.keyboard('{Escape}');
        }
      }

      const downloadBtn = screen.queryByRole('button', { name: /download options|下载选项/i });
      if (downloadBtn) {
        await user.click(downloadBtn);
        if (screen.queryByRole('menuitem', { name: /^download$|下载/i })) keys.add('download');
        if (screen.queryByRole('menuitem', { name: /^save as pdf$|另存为 pdf/i })) keys.add('savePdf');
        const pdfSubTrigger = screen.queryByRole('menuitem', { name: /pdf options|pdf 选项/i });
        if (pdfSubTrigger) {
          keys.add('pdfOptions');
          await user.hover(pdfSubTrigger);
          await user.click(pdfSubTrigger);
          if (await screen.findByRole('menuitem', { name: /^footer$|页脚/i })) keys.add('pdfBranding');
          if (screen.queryByRole('menuitem', { name: /^page numbers$|页码/i })) keys.add('pdfPageNumbers');
          if (screen.queryByRole('menuitem', { name: /^80%$/ })) keys.add('pdfScale80');
          if (screen.queryByRole('menuitem', { name: /^100%$/ })) keys.add('pdfScale100');
          if (screen.queryByRole('menuitem', { name: /^125%$/ })) keys.add('pdfScale125');
        }
        await user.keyboard('{Escape}');
      }

      return [...keys].sort();
    };

    const collectMobileActions = async (): Promise<ActionKey[]> => {
      const keys: ActionKey[] = [];
      if (screen.queryByTestId('file-header-mobile-comment')) keys.push('comment');
      if (screen.queryByTestId('file-header-mobile-comment-list')) keys.push('commentList');
      if (screen.queryByTestId('file-header-mobile-send-all')) keys.push('sendAll');
      if (screen.queryByTestId('file-header-mobile-pin-dashboard')) keys.push('pinDashboard');
      if (screen.queryByTestId('file-header-mobile-versions')) keys.push('versions');
      if (screen.queryByTestId('file-header-mobile-update')) keys.push('update');
      if (screen.queryByTestId('file-header-mobile-publish')) keys.push('publish');
      if (screen.queryByTestId('file-header-mobile-bind')) keys.push('bind');
      if (screen.queryByTestId('file-header-mobile-unbind')) keys.push('unbind');
      if (screen.queryByTestId('file-header-mobile-unshare')) keys.push('unshare');
      if (screen.queryByTestId('file-header-mobile-source')) keys.push('source');
      if (screen.queryByTestId('file-header-mobile-open-url')) keys.push('openUrl');
      if (screen.queryByTestId('file-header-mobile-download')) keys.push('download');
      if (screen.queryByTestId('file-header-mobile-save-pdf')) keys.push('savePdf');
      const pdfOptionsBtn = screen.queryByTestId('file-header-mobile-pdf-options');
      if (pdfOptionsBtn) {
        keys.push('pdfOptions');
        await user.click(pdfOptionsBtn);
        const pdfList = await screen.findByTestId('file-header-mobile-pdf-options-list');
        if (within(pdfList).queryByRole('menuitem', { name: /^footer$|页脚/i })) keys.push('pdfBranding');
        if (within(pdfList).queryByRole('menuitem', { name: /^page numbers$|页码/i })) keys.push('pdfPageNumbers');
        if (within(pdfList).queryByRole('menuitem', { name: /^80%$/ })) keys.push('pdfScale80');
        if (within(pdfList).queryByRole('menuitem', { name: /^100%$/ })) keys.push('pdfScale100');
        if (within(pdfList).queryByRole('menuitem', { name: /^125%$/ })) keys.push('pdfScale125');
      }
      if (screen.queryByTestId('file-header-mobile-edit')) keys.push('edit');
      return keys.sort();
    };

    const ownerUi = makeCommentUi();
    const ownerVersionView = {
      record: recordSharedOwner,
      version: null,
      updatedTo: null,
      versions: [],
      entry: null,
      missing: false,
      failed: false,
      retry: vi.fn(),
      pick: vi.fn(),
    };

    const scenarios = [
      {
        name: 'owner-full',
        record: recordSharedOwner,
        hasQueued: true,
        canBind: true,
        ownerTools: true,
        props: baseProps({
          access: { kind: 'owner' as const },
          openPublish: vi.fn(),
          openUnshareOnly: null,
          canDownload: true,
          canEdit: true,
          mobileHtmlTools: {
            tabId: 'tab-1',
            ui: ownerUi,
            threadId: 'th-beside',
            modeDisabled: false,
            readAt: 100,
            versionView: ownerVersionView,
          },
        }),
      },
      {
        name: 'shared-non-owner',
        record: recordSharedNonOwner,
        hasQueued: false,
        canBind: false,
        ownerTools: false,
        props: baseProps({
          access: { kind: 'readOnly' as const },
          openPublish: vi.fn(),
          openUnshareOnly: vi.fn(),
          canDownload: true,
          canEdit: false,
          mobileHtmlTools: null,
        }),
      },
      {
        name: 'unshared-non-owner',
        record: recordUnshared,
        hasQueued: false,
        canBind: false,
        ownerTools: false,
        props: baseProps({
          access: { kind: 'readOnly' as const },
          openPublish: null,
          openUnshareOnly: vi.fn(),
          canDownload: true,
          canEdit: false,
          mobileHtmlTools: null,
        }),
      },
    ];

    for (const scenario of scenarios) {
      commentSenderState.besideThreadId = scenario.canBind ? 'th-beside' : null;
      commentDestState.canBind = scenario.canBind;

      const qc = createTestQueryClient();
      qc.setQueryData(queryKeys.artifacts.byPath('ws-1', 'reports/a.html'), scenario.record);
      qc.setQueryData(
        queryKeys.artifacts.statusWith('art-a', [], []),
        makeStatus({ changed: scenario.record.shared }),
      );
      qc.setQueryData(
        queryKeys.artifacts.comments('art-a'),
        scenario.hasQueued ? [queuedComment] : [],
      );

      // 1. Desktop: crumbs (CommentTools + PinPageButton + ArtifactCrumbs) + flat FileHeaderActions
      mediaState.mobile = false;
      const { unmount: unmountDesktop } = renderWithProviders(
        <>
          {scenario.ownerTools && (
            <>
              <CommentTools tabId="tab-1" workspaceId="ws-1" target={{ kind: 'file', path: 'reports/a.html' }} ui={ownerUi} variant="crumb" />
              <PinPageButton workspaceId="ws-1" path="reports/a.html" threadId="th-beside" />
              <ArtifactCrumbs view={ownerVersionView} onReview={vi.fn()} readAt={100} />
            </>
          )}
          <FileHeaderActions {...scenario.props} />
        </>,
        { queryClient: qc },
      );
      const desktopSet = await collectDesktopActions(scenario.ownerTools);
      unmountDesktop();

      // 2. Mobile: single More menu carrying the same inputs
      mediaState.mobile = true;
      const { unmount: unmountMobile } = renderWithProviders(
        <FileHeaderActions {...scenario.props} />,
        { queryClient: qc },
      );
      await user.click(screen.getByTestId('file-header-mobile-more'));
      await screen.findByTestId('file-header-mobile-more-menu');
      const mobileSet = await collectMobileActions();

      expect(mobileSet, `mobile vs desktop parity failed for ${scenario.name}`).toEqual(desktopSet);
      if (scenario.name === 'owner-full') {
        expect(mobileSet).toEqual(
          expect.arrayContaining([
            'comment',
            'commentList',
            'sendAll',
            'pinDashboard',
            'versions',
            'update',
            'publish',
            'bind',
            'unbind',
            'source',
            'download',
            'savePdf',
            'pdfOptions',
            'pdfBranding',
            'pdfPageNumbers',
            'pdfScale80',
            'pdfScale100',
            'pdfScale125',
            'edit',
          ]),
        );
      }
      unmountMobile();
    }
  });

  it('in read-only preview on a linked doc B, shows ONLY "Open this report in its own tab" and hides tab A actions', async () => {
    const user = userEvent.setup();
    const onOpenDocAsTab = vi.fn();
    const recordA = makeArtifact({ artifact_id: 'art-a', path: 'reports/a.html', shared: true });
    const qc = createTestQueryClient();
    qc.setQueryData(queryKeys.artifacts.byPath('ws-1', 'reports/a.html'), recordA);

    mediaState.mobile = true;
    renderWithProviders(
      <FileHeaderActions
        {...baseProps({
          selectedFile: 'reports/a.html',
          artifactDoc: 'reports/b.html',
          onOpenDocAsTab,
          access: { kind: 'readOnly' as const },
          openPublish: null,
          openUnshareOnly: vi.fn(),
          canDownload: true,
          canEdit: false,
          mobileHtmlTools: null,
        })}
      />,
      { queryClient: qc },
    );

    await user.click(screen.getByTestId('file-header-mobile-more'));
    await screen.findByTestId('file-header-mobile-more-menu');
    const openTabItem = screen.getByTestId('file-header-mobile-open-doc-tab');
    expect(openTabItem).toBeInTheDocument();
    expect(screen.queryByTestId('file-header-mobile-unshare')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-publish')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-source')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-download')).toBeNull();
    expect(screen.queryByTestId('file-header-mobile-comment')).toBeNull();

    await user.click(openTabItem);
    expect(onOpenDocAsTab).toHaveBeenCalledWith('reports/b.html');
  });
});
