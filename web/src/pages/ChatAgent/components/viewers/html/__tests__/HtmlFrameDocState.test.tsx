import React from 'react';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router';
import '@testing-library/jest-dom';

import { createTestQueryClient, renderWithProviders } from '@/test/utils';
import { ThemeProvider } from '@/contexts/ThemeContext';
import { BridgedFrame, type HtmlFrameDocState } from '../BridgedFrame';
import { useLiveFrame } from '../liveFrame';
import { usePageBridge, type PageBridge } from '../usePageBridge';
import { ArtifactFrames, type HtmlArtifactView } from '../ArtifactFrames';
import HtmlViewer from '../../HtmlViewer';
import FileHeaderActions from '../../../FileHeaderActions';
import { MonitorViewer } from '@/pages/Monitors/MonitorViewer';
import type { MonitorDetail } from '@/pages/Monitors/api';

const hoisted = vi.hoisted(() => ({
  isMobile: false,
  workspaceGrantError: null as Error | null,
  getMonitorDetail: vi.fn(),
  getPublicMonitorDetail: vi.fn(),
  mintMonitorGrant: vi.fn(),
}));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => hoisted.isMobile,
  getIsMobileSnapshot: () => hoisted.isMobile,
}));

vi.mock('@/hooks/useWorkspaceFileGrant', () => ({
  useWorkspaceFileGrant: (workspaceId: string | null) => ({
    data: workspaceId && !hoisted.workspaceGrantError ? { prefix: '/api/v1/wsfiles/g/grant-1/', expires_in: 3600 } : undefined,
    error: workspaceId ? hoisted.workspaceGrantError : null,
    refetch: vi.fn(),
  }),
}));

vi.mock('@/hooks/useOwnFileGrant', () => ({
  useOwnFileGrant: () => ({ data: undefined, error: null, refetch: vi.fn() }),
}));

vi.mock('@/hooks/useArtifact', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useArtifact')>();
  return {
    ...actual,
    useArtifactGrant: (artifactId: string | null) => ({
      data: artifactId
        ? { base: '/api/v1/artifacts/g/art-grant/', isolated: false, expires_in: 3600 }
        : undefined,
      error: null,
      refetch: vi.fn(),
    }),
    useOwnedArtifact: (workspaceId: string | null) => ({
      record: workspaceId ? { artifact_id: 'art-1', code: 'code123', url: '/a/code123' } : undefined,
      error: null,
    }),
    useArtifactByPath: () => ({ data: { artifact_id: 'art-1', shared: true, bound_thread_id: null } }),
    useArtifactBinding: () => ({ bind: { mutate: vi.fn() }, unbind: { mutate: vi.fn() } }),
    useArtifactComments: () => ({ data: [] }),
  };
});

vi.mock('@/pages/Monitors/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/Monitors/api')>();
  return {
    ...actual,
    getMonitorDetail: (...args: unknown[]) => hoisted.getMonitorDetail(...args),
    getPublicMonitorDetail: (...args: unknown[]) => hoisted.getPublicMonitorDetail(...args),
    mintMonitorGrant: (...args: unknown[]) => hoisted.mintMonitorGrant(...args),
  };
});

vi.mock('../comments/lazyCommentLayer', () => ({
  LazyCommentLayer: () => null,
}));

vi.mock('../../../SyntaxHighlighter', () => ({
  default: ({ children }: { children: string }) => <pre>{children}</pre>,
  oneDark: {},
  oneLight: {},
}));

vi.mock('@/hooks/useCapabilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCapabilities')>();
  return {
    ...actual,
    useCapabilities: () => ({
      profile: 'everflow',
      capabilities: actual.EVERFLOW_DEFAULT_CAPABILITY_SET,
      hasCapability: () => true,
      brandName: 'Everflow',
      fingerprint: null,
      tickerLogosEnabled: true,
      isLoading: false,
      isLoaded: true,
      isCoreBuild: false,
      isEverflowBuild: true,
    }),
  };
});

function postFromFrame(iframe: HTMLIFrameElement, data: Record<string, unknown>, origin = 'null') {
  act(() => {
    window.dispatchEvent(new MessageEvent('message', {
      data,
      origin,
      source: iframe.contentWindow,
    }));
  });
}

function makeCommentUi() {
  return {
    modeTab: null,
    listTab: null,
    focus: null,
    setMode: vi.fn(),
    setList: vi.fn(),
    requestFocus: vi.fn(),
    focusHandled: vi.fn(),
    takesWords: () => false,
    commentOnWords: vi.fn(),
    registerWordsTaker: () => () => {},
    askInChat: null,
    docOf: (_tabId: string, fallback: string) => fallback,
    showDoc: vi.fn(),
    sends: {
      get: () => undefined,
      sending: vi.fn(),
      taken: vi.fn(),
      refused: vi.fn(),
      clear: vi.fn(),
      release: vi.fn(() => false),
    },
  } as unknown as HtmlArtifactView['ui'];
}

function makeArtifactView(version: number | null = null): HtmlArtifactView {
  return {
    tabId: 'tab-1',
    ui: makeCommentUi(),
    active: true,
    versionView: {
      record: {
        artifact_id: 'art-1',
        code: 'code123',
        url: '/a/code123',
        workspace_id: 'ws-1',
        path: 'reports/a.html',
        name: 'a.html',
        shared: true,
        shared_at: '2026-11-20T00:00:00Z',
        shared_seq: 2,
        keep_updated: false,
        latest_seq: 2,
        bound_thread_id: null,
        open_comments: 0,
        unread: 0,
        created_at: '2026-11-20T00:00:00Z',
      },
      version,
      updatedTo: null,
      versions: [
        {
          seq: 2,
          entry_path: 'reports/a.html',
          origin: 'owner',
          title: 'Report A',
          label: 'v2',
          byte_len: 512,
          published_at: '2026-11-20T00:00:00Z',
        },
      ],
      entry: version === 2
        ? {
            seq: 2,
            entry_path: 'reports/a.html',
            origin: 'owner',
            title: 'Report A',
            label: 'v2',
            byte_len: 512,
            published_at: '2026-11-20T00:00:00Z',
          }
        : null,
      missing: false,
      failed: false,
      retry: vi.fn(),
      pick: vi.fn(),
    },
  };
}

beforeEach(() => {
  hoisted.isMobile = false;
  hoisted.workspaceGrantError = null;
  hoisted.getMonitorDetail.mockReset();
  hoisted.getPublicMonitorDetail.mockReset();
  hoisted.mintMonitorGrant.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('usePageBridge tri-state path clearing and frameKey', () => {
  it('clears bridge.path immediately on invalid/unrecognized page:ready and via clearPath()', () => {
    const iframe = document.createElement('iframe');
    document.body.appendChild(iframe);
    vi.spyOn(iframe.contentWindow!, 'postMessage').mockImplementation(() => {});
    const frameRef = { current: iframe };

    const { result } = renderHook(() =>
      usePageBridge({ frameRef, origin: 'null', frameKey: 'view:ws:a.html:' }),
    );
    expect(result.current.frameKey).toBe('view:ws:a.html:');
    expect(result.current.path).toBeNull();

    // Valid path sets bridge.path and pathState: 'path'.
    postFromFrame(iframe, { type: 'page:ready', v: 1, path: 'reports/a.html' });
    expect(result.current.path).toBe('reports/a.html');
    expect(result.current.pathState).toBe('path');

    // Conflicting { path: undefined, root: true } immediately clears stale bridge.path -> null.
    postFromFrame(iframe, { type: 'page:ready', v: 1, path: undefined, root: true });
    expect(result.current.path).toBeNull();
    expect(result.current.pathState).toBe('invalid');

    // Valid path again, then explicit clearPath() (unbridged timeout).
    postFromFrame(iframe, { type: 'page:ready', v: 1, path: 'reports/b.html' });
    expect(result.current.path).toBe('reports/b.html');
    act(() => result.current.clearPath?.());
    expect(result.current.path).toBeNull();

    iframe.remove();
  });
});

describe('BridgedFrame unified HtmlFrameDocState & race guards', () => {
  it('transitions pending -> current -> linked -> bridged_root, and maps invalid/conflicting/missing-root payloads to unbridged with cleared bridge.path', () => {
    const docStates: HtmlFrameDocState[] = [];
    const latestBridge: { current: PageBridge | null } = { current: null };
    const onBridge = vi.fn((bridge: PageBridge, _key: string | null) => {
      latestBridge.current = bridge;
    });

    const { container } = renderWithProviders(
      <BridgedFrame
        source={{
          key: 'view:ws:reports/a.html:',
          src: '/api/v1/public/shared/tok-1/files/serve/reports/a.html?inject=view',
          sandbox: 'allow-scripts',
        }}
        title="Report A"
        groundKey="reports/a.html"
        expectedDocKey="reports/a.html"
        onBridge={onBridge}
        onDocStateChange={(state) => docStates.push(state)}
      />,
    );

    const iframe = container.querySelector('iframe')!;
    expect(docStates.at(-1)).toEqual({ kind: 'pending' });
    expect(onBridge).toHaveBeenCalledWith(expect.objectContaining({ frameKey: 'view:ws:reports/a.html:' }), 'view:ws:reports/a.html:');

    // 1. Current document A.
    postFromFrame(iframe, { type: 'page:ready', v: 1, path: 'reports/a.html' });
    expect(docStates.at(-1)).toEqual({ kind: 'current', path: 'reports/a.html' });
    expect(latestBridge.current?.path).toBe('reports/a.html');

    // 2. In-frame link to document B.
    postFromFrame(iframe, { type: 'page:ready', v: 1, path: 'reports/b.html' });
    expect(docStates.at(-1)).toEqual({ kind: 'linked', path: 'reports/b.html' });
    expect(latestBridge.current?.path).toBe('reports/b.html');

    // 3. Confirmed pathless version root -> bridged_root.
    postFromFrame(iframe, { type: 'page:ready', v: 1, root: true });
    expect(docStates.at(-1)).toEqual({ kind: 'bridged_root' });
    expect(latestBridge.current?.path).toBeNull();

    // 4. All conflicting, invalid, and missing-root messages clear bridge.path and enter { kind: 'unbridged', currentUrl: null }.
    const badPayloads: Record<string, unknown>[] = [
      { type: 'page:ready', v: 1, path: undefined, root: true },
      { type: 'page:ready', v: 1, path: null, root: true },
      { type: 'page:ready', v: 1, path: 'a.html', root: true },
      { type: 'page:ready', v: 1, path: undefined },
      { type: 'page:ready', v: 1, invalidPath: true },
      { type: 'page:ready', v: 1 },
    ];
    for (const payload of badPayloads) {
      postFromFrame(iframe, { type: 'page:ready', v: 1, path: 'reports/a.html' });
      expect(latestBridge.current?.path).toBe('reports/a.html');
      postFromFrame(iframe, payload);
      expect(latestBridge.current?.path).toBeNull();
      expect(docStates.at(-1)).toEqual({ kind: 'unbridged', currentUrl: null });
    }
  });

  it('guards timeout callbacks by navigation sequence and clears stale bridge.path on real timeout', () => {
    vi.useFakeTimers();
    const docStates: HtmlFrameDocState[] = [];
    const latestBridge: { current: PageBridge | null } = { current: null };
    const onUnbridged = vi.fn();

    const { container } = renderWithProviders(
      <BridgedFrame
        source={{
          key: 'view:ws:reports/a.html:',
          src: '/api/v1/public/shared/tok-1/files/serve/reports/a.html?inject=view',
          sandbox: 'allow-scripts',
        }}
        title="Report A"
        groundKey="reports/a.html"
        expectedDocKey="reports/a.html"
        onBridge={(bridge) => { latestBridge.current = bridge; }}
        onDocStateChange={(state) => docStates.push(state)}
        onUnbridged={onUnbridged}
      />,
    );

    const iframe = container.querySelector('iframe')!;
    // Initial document A loads and says hello.
    postFromFrame(iframe, { type: 'page:ready', v: 1, path: 'reports/a.html' });
    fireEvent.load(iframe);
    expect(docStates.at(-1)).toEqual({ kind: 'current', path: 'reports/a.html' });

    // Load #1 starts waiting; before timeout expires, Load #2 arrives and gets page:ready for B.
    fireEvent.load(iframe);
    expect(docStates.at(-1)).toEqual({ kind: 'pending' });
    act(() => { vi.advanceTimersByTime(1500); });
    postFromFrame(iframe, { type: 'page:ready', v: 1, path: 'reports/b.html' });
    expect(docStates.at(-1)).toEqual({ kind: 'linked', path: 'reports/b.html' });

    // Advancing past Load #1's original deadline must NOT fire unbridged.
    act(() => { vi.advanceTimersByTime(3000); });
    expect(onUnbridged).not.toHaveBeenCalled();
    expect(docStates.at(-1)).toEqual({ kind: 'linked', path: 'reports/b.html' });

    // Now simulate navigating to an unbridged page (no page:ready within grace period).
    fireEvent.load(iframe);
    expect(docStates.at(-1)).toEqual({ kind: 'pending' });
    act(() => { vi.advanceTimersByTime(2500); });
    expect(onUnbridged).toHaveBeenCalledTimes(1);
    expect(latestBridge.current?.path).toBeNull();
    expect(docStates.at(-1)).toEqual({ kind: 'unbridged', currentUrl: null });
  });
});

describe('useLiveFrame cross-tab frameKey isolation', () => {
  it('immediately resets to pending/null on tab switch and drops stale frameKey callbacks', () => {
    const { result, rerender } = renderHook(
      ({ filePath }: { filePath: string }) =>
        useLiveFrame({ workspaceId: 'ws-1', filePath, prefix: '/api/v1/wsfiles/g/g1/', inject: 'view' }),
      { initialProps: { filePath: 'reports/a.html' } },
    );

    const keyA = result.current.source!.key;
    const bridgeA = {
      ready: true,
      session: 1,
      path: 'reports/a.html',
      frameKey: keyA,
      post: vi.fn(),
      subscribe: () => () => {},
    } as unknown as PageBridge;

    act(() => {
      result.current.onBridge(bridgeA, keyA);
      result.current.onDocStateChange({ kind: 'current', path: 'reports/a.html' }, keyA);
    });
    expect(result.current.bridge).toBe(bridgeA);
    expect(result.current.docState).toEqual({ kind: 'current', path: 'reports/a.html' });

    // Switch tab to B -> same render immediately exposes bridge: null and docState: pending.
    rerender({ filePath: 'reports/b.html' });
    expect(result.current.bridge).toBeNull();
    expect(result.current.docState).toEqual({ kind: 'pending' });

    // Late callback carrying tab A's frameKey is rejected.
    act(() => {
      result.current.onBridge(bridgeA, keyA);
      result.current.onDocStateChange({ kind: 'linked', path: 'reports/x.html' }, keyA);
    });
    expect(result.current.bridge).toBeNull();
    expect(result.current.docState).toEqual({ kind: 'pending' });
  });
});

describe('ArtifactFrames and HtmlViewer onShownDocChange forwarding', () => {
  it('reports from live BridgedFrame when version is null and switches to version BridgedFrame when viewing a version', () => {
    const reported: HtmlFrameDocState[] = [];
    const { container, rerender } = renderWithProviders(
      <ThemeProvider>
        <ArtifactFrames
          revision="r1"
          fileName="a.html"
          workspaceId="ws-1"
          filePath="reports/a.html"
          view={makeArtifactView(null)}
          onShownDocChange={(state) => reported.push(state)}
        />
      </ThemeProvider>,
    );

    const liveIframe = container.querySelector('iframe')!;
    postFromFrame(liveIframe, { type: 'page:ready', v: 1, path: 'reports/linked.html' });
    expect(reported.at(-1)).toEqual({ kind: 'linked', path: 'reports/linked.html' });

    // Switch to published version 2 -> active layer becomes the version frame (starts pending).
    rerender(
      <ThemeProvider>
        <ArtifactFrames
          revision="r1"
          fileName="a.html"
          workspaceId="ws-1"
          filePath="reports/a.html"
          view={makeArtifactView(2)}
          onShownDocChange={(state) => reported.push(state)}
        />
      </ThemeProvider>,
    );
    expect(reported.at(-1)).toEqual({ kind: 'pending' });

    const frames = container.querySelectorAll('iframe');
    expect(frames).toHaveLength(2);
    const versionIframe = frames[1];
    // Version frame reporting a valid doc updates onShownDocChange.
    postFromFrame(versionIframe, { type: 'page:ready', v: 1, path: 'reports/a.html' });
    expect(reported.at(-1)).toEqual({ kind: 'current', path: 'reports/a.html' });

    // Version frame reporting invalidPath -> unbridged.
    postFromFrame(versionIframe, { type: 'page:ready', v: 1, invalidPath: true });
    expect(reported.at(-1)).toEqual({ kind: 'unbridged', currentUrl: null });
  });

  it('forwards onShownDocChange from HtmlViewer in both read-only ViewFrame and owner ArtifactFrames', () => {
    const reported: HtmlFrameDocState[] = [];
    const { container } = render(
      <ThemeProvider>
        <HtmlViewer
          content="<html><body>Hi</body></html>"
          fileName="a.html"
          workspaceId="ws-1"
          filePath="reports/a.html"
          servePrefix="/api/v1/public/shared/tok-1/files/serve/"
          onShownDocChange={(state) => reported.push(state)}
        />
      </ThemeProvider>,
    );

    const iframe = container.querySelector('iframe')!;
    expect(reported.at(-1)).toEqual({ kind: 'pending' });
    postFromFrame(iframe, { type: 'page:ready', v: 1, path: 'reports/b.html' });
    expect(reported.at(-1)).toEqual({ kind: 'linked', path: 'reports/b.html' });
  });
});

describe('FileHeaderActions desktop & mobile parity across HtmlFrameDocState', () => {
  function headerProps(overrides: Record<string, unknown> = {}) {
    return {
      selectedFile: 'reports/a.html',
      isEditing: false,
      workspaceId: 'ws-1',
      fileContent: '<h1>A</h1>',
      fileMime: 'text/html',
      canEdit: true,
      onStartEdit: vi.fn(),
      onOpenExportModal: vi.fn(),
      triggerDownloadFn: vi.fn().mockResolvedValue(undefined),
      canDownload: true,
      readFileFullFn: vi.fn().mockResolvedValue({ content: '' }),
      access: { kind: 'share' as const, servePrefix: '/api/v1/public/shared/tok-1/files/serve/' },
      onToggleHtmlSource: vi.fn(),
      onOpenDocAsTab: vi.fn(),
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

  it('keeps actions on current/bridged_root, disables write/export on pending, shows only Open-in-tab on linked, and hides all actions + Open-in-new-tab on unbridged (desktop)', () => {
    hoisted.isMobile = false;
    const onOpenDocAsTab = vi.fn();
    const qc = createTestQueryClient();

    // 1. pending -> write/export/openUrl disabled, View source still enabled.
    const { rerender } = renderWithProviders(
      <FileHeaderActions {...headerProps({ onOpenDocAsTab, htmlDocState: { kind: 'pending' } })} />,
      { queryClient: qc },
    );
    expect(screen.getByRole('button', { name: /viewSource|View source|查看源代码/i })).toBeEnabled();
    expect(screen.getByRole('button', { name: /openInNewTab|Open in new tab|在新标签页打开/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /downloadOptions|Download options|下载选项/i })).toBeDisabled();

    // 2. bridged_root -> treated as valid root view (all primary actions enabled).
    rerender(
      <FileHeaderActions {...headerProps({ onOpenDocAsTab, htmlDocState: { kind: 'bridged_root' } })} />,
    );
    expect(screen.getByRole('button', { name: /openInNewTab|Open in new tab|在新标签页打开/i })).toBeEnabled();
    expect(screen.getByRole('button', { name: /downloadOptions|Download options|下载选项/i })).toBeEnabled();

    // 3. linked -> hides doc A actions and original openUrl, shows "Open this report in its own tab" for B.
    rerender(
      <FileHeaderActions {...headerProps({ onOpenDocAsTab, htmlDocState: { kind: 'linked', path: 'reports/b.html' } })} />,
    );
    expect(screen.queryByRole('button', { name: /viewSource|View source|查看源代码/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /openInNewTab|Open in new tab|在新标签页打开/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /downloadOptions|Download options|下载选项/i })).toBeNull();
    fireEvent.click(screen.getByTestId('file-header-open-doc-tab'));
    expect(onOpenDocAsTab).toHaveBeenCalledWith('reports/b.html');

    // 4. unbridged (currentUrl: null) -> hides ALL doc A actions AND hides Open-in-new-tab & Open-in-tab.
    rerender(
      <FileHeaderActions {...headerProps({ onOpenDocAsTab, htmlDocState: { kind: 'unbridged', currentUrl: null } })} />,
    );
    expect(screen.queryByTestId('file-header-open-doc-tab')).toBeNull();
    expect(screen.queryByRole('button', { name: /viewSource|View source|查看源代码/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /openInNewTab|Open in new tab|在新标签页打开/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /downloadOptions|Download options|下载选项/i })).toBeNull();
  });

  it('hides mobile overflow trigger completely on unbridged (currentUrl: null) and shows it on bridged_root', () => {
    hoisted.isMobile = true;
    const qc = createTestQueryClient();
    const { rerender } = renderWithProviders(
      <FileHeaderActions {...headerProps({ htmlDocState: { kind: 'bridged_root' } })} />,
      { queryClient: qc },
    );
    expect(screen.getByTestId('file-header-mobile-more')).toBeInTheDocument();

    rerender(
      <FileHeaderActions {...headerProps({ htmlDocState: { kind: 'unbridged', currentUrl: null } })} />,
    );
    expect(screen.queryByTestId('file-header-mobile-more')).toBeNull();
  });

  it('keeps original file actions visible and clickable for plain previews (non-HTML, unattached/failed HTML, current, bridged_root) and hides them only on unbridged', () => {
    hoisted.isMobile = false;
    const openPublish = vi.fn();
    const qc = createTestQueryClient();

    // 1. Non-HTML file preview (Markdown) keeps Publish, Download, and Edit visible and clickable.
    const { rerender, unmount } = renderWithProviders(
      <FileHeaderActions
        {...headerProps({
          selectedFile: 'notes/summary.md',
          fileContent: '# Summary',
          fileMime: 'text/markdown',
          access: { kind: 'readOnly' },
          openPublish,
          htmlDocState: null,
        })}
      />,
      { queryClient: qc },
    );
    const mdPublishBtn = screen.getByRole('button', { name: /artifact\.publish|Publish|发布/i });
    expect(mdPublishBtn).toBeEnabled();
    fireEvent.click(mdPublishBtn);
    expect(openPublish).toHaveBeenCalledWith('notes/summary.md');
    expect(screen.getByRole('button', { name: /downloadOptions|Download options|下载选项/i })).toBeEnabled();
    expect(screen.getByTitle(/editFile|Edit file|编辑文件/i)).toBeEnabled();

    // 2. HTML preview before any bridged frame reports (htmlDocState: null) keeps Publish clickable (e.g. external member preview entry).
    openPublish.mockClear();
    rerender(
      <FileHeaderActions
        {...headerProps({
          selectedFile: 'reports/a.html',
          fileContent: '<h1>A</h1>',
          fileMime: 'text/html',
          access: { kind: 'readOnly' },
          openPublish,
          htmlDocState: null,
        })}
      />,
    );
    const plainHtmlPublishBtn = screen.getByRole('button', { name: /artifact\.publish|Publish|发布/i });
    expect(plainHtmlPublishBtn).toBeEnabled();
    fireEvent.click(plainHtmlPublishBtn);
    expect(openPublish).toHaveBeenCalledWith('reports/a.html');
    expect(screen.getByRole('button', { name: /downloadOptions|Download options|下载选项/i })).toBeEnabled();

    // 3. Bridged HTML preview reporting current or bridged_root keeps Publish clickable.
    openPublish.mockClear();
    rerender(
      <FileHeaderActions
        {...headerProps({
          selectedFile: 'reports/a.html',
          access: { kind: 'readOnly' },
          openPublish,
          htmlDocState: { kind: 'current', path: 'reports/a.html' },
        })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /artifact\.publish|Publish|发布/i }));
    expect(openPublish).toHaveBeenCalledWith('reports/a.html');

    openPublish.mockClear();
    rerender(
      <FileHeaderActions
        {...headerProps({
          selectedFile: 'reports/a.html',
          access: { kind: 'readOnly' },
          openPublish,
          htmlDocState: { kind: 'bridged_root' },
        })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /artifact\.publish|Publish|发布/i }));
    expect(openPublish).toHaveBeenCalledWith('reports/a.html');

    // 4. Only unbridged (from invalid/unrecognized/timeout) hides Publish and original file actions.
    rerender(
      <FileHeaderActions
        {...headerProps({
          selectedFile: 'reports/a.html',
          access: { kind: 'readOnly' },
          openPublish,
          htmlDocState: { kind: 'unbridged', currentUrl: null },
        })}
      />,
    );
    expect(screen.queryByRole('button', { name: /artifact\.publish|Publish|发布/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /downloadOptions|Download options|下载选项/i })).toBeNull();
    unmount();

    // 5. HtmlViewer with a failed file grant (no bridged frame mounted) does not push pending/unbridged state.
    hoisted.workspaceGrantError = new Error('grant failed');
    const reported: HtmlFrameDocState[] = [];
    render(
      <ThemeProvider>
        <HtmlViewer
          content="<html><body>Hi</body></html>"
          fileName="a.html"
          workspaceId="ws-1"
          filePath="reports/a.html"
          onShownDocChange={(state) => reported.push(state)}
        />
      </ThemeProvider>,
    );
    expect(reported).toEqual([]);
  });
});

describe('MonitorViewer bridged_root and unbridged handling', () => {
  function makePublicMonitorDetail(): MonitorDetail {
    return {
      monitor: {
        monitor_id: 'mon-pub-1',
        slug: 'public-pulse',
        owner_name: null,
        owner_email: null,
        title: 'Public Pulse',
        category: 'us',
        sort_order: 0,
        visibility: 'public',
        member_user_ids: [],
        published_seq: 4,
        keep_updated: true,
        source_kind: 'workspace',
        source_ref: 'ws-1:index.html',
        acl_revision: 1,
        last_publish_error: null,
        last_publish_failed_at: null,
        data_as_of: null,
        created_at: '2026-11-20T00:00:00Z',
        updated_at: '2026-11-20T00:00:00Z',
        can_manage: false,
      },
      versions: [],
      monitor_grant: '',
      frame_base: '/api/v1/dalton/monitors/public/public-pulse/',
      data_base: '/api/v1/dalton/monitors/public/public-pulse/data',
      expires_in: 0,
    };
  }

  it('enters bridged_root on { root: true } and enters unbridged (hiding open-in-new-window) on conflicting/invalid/unrecognized page:ready', async () => {
    hoisted.getPublicMonitorDetail.mockResolvedValue(makePublicMonitorDetail());

    const { container } = renderWithProviders(
      <Routes>
        <Route path="/m/:slug" element={<MonitorViewer slug="public-pulse" publicMode />} />
      </Routes>,
      { route: '/m/public-pulse' },
    );

    await waitFor(() => {
      expect(container.querySelector('iframe.bridged-frame-doc')).toBeTruthy();
    });
    const iframe = container.querySelector('iframe.bridged-frame-doc') as HTMLIFrameElement;
    const host = container.querySelector('.monitors-frame-host')!;
    expect(iframe.getAttribute('src')).toBe('/api/v1/dalton/monitors/public/public-pulse/v/4/');

    // Confirmed pathless version root -> bridged_root, open-in-new-window stays visible.
    postFromFrame(iframe, { type: 'page:ready', v: 1, root: true });
    expect(host).toHaveAttribute('data-doc-state', 'bridged_root');
    expect(screen.getByRole('link', { name: /openInNewWindow|Open in new window|新窗口打开/i })).toBeInTheDocument();

    // Conflicting { path: undefined, root: true } -> unbridged, open-in-new-window hidden.
    postFromFrame(iframe, { type: 'page:ready', v: 1, path: undefined, root: true });
    expect(host).toHaveAttribute('data-doc-state', 'unbridged');
    expect(screen.queryByRole('link', { name: /openInNewWindow|Open in new window|新窗口打开/i })).toBeNull();

    // Back to root: true -> bridged_root restored.
    postFromFrame(iframe, { type: 'page:ready', v: 1, root: true });
    expect(host).toHaveAttribute('data-doc-state', 'bridged_root');

    // Missing root: true ({ type: 'page:ready', v: 1 }) -> unrecognized -> unbridged.
    postFromFrame(iframe, { type: 'page:ready', v: 1 });
    expect(host).toHaveAttribute('data-doc-state', 'unbridged');
    expect(screen.queryByRole('link', { name: /openInNewWindow|Open in new window|新窗口打开/i })).toBeNull();
  });
});
