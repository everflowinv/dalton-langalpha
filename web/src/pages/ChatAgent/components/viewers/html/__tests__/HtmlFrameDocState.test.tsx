import React from 'react';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router';
import '@testing-library/jest-dom';

import { createTestQueryClient, renderWithProviders } from '@/test/utils';
import { ThemeProvider } from '@/contexts/ThemeContext';
import type { Artifact, ArtifactComment, ArtifactStatus } from '@/types/api';
import { BridgedFrame, type HtmlFrameDocState } from '../BridgedFrame';
import { useLiveFrame } from '../liveFrame';
import { usePageBridge, type PageBridge } from '../usePageBridge';
import { ArtifactFrames, type HtmlArtifactView } from '../ArtifactFrames';
import { CommentTools } from '../comments/CommentTools';
import HtmlViewer from '../../HtmlViewer';
import FileHeaderActions from '../../../FileHeaderActions';
import { ArtifactCrumbs } from '../../../filePanel/ArtifactCrumbs';
import { ArtifactCommentSendContext, type ArtifactCommentSender } from '../../../artifactComments/sendContext';
import { MonitorViewer } from '@/pages/Monitors/MonitorViewer';
import type { MonitorDetail } from '@/pages/Monitors/api';

const hoisted = vi.hoisted(() => ({
  isMobile: false,
  workspaceGrantError: null as Error | null,
  getMonitorDetail: vi.fn(),
  getPublicMonitorDetail: vi.fn(),
  mintMonitorGrant: vi.fn(),
  artifactsByPath: {} as Record<string, Partial<Artifact>>,
  commentsByArtifactId: {} as Record<string, ArtifactComment[]>,
  artifactStatus: null as ArtifactStatus | null,
  targetFor: (() => ({ kind: 'send', to: { threadId: 'th-1' } })) as unknown,
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
    useArtifactByPath: (_workspaceId: string | null | undefined, path: string | null | undefined) => {
      if (!path) return { data: undefined };
      const mapped = hoisted.artifactsByPath[path];
      return {
        data: mapped ?? { artifact_id: 'art-1', shared: true, bound_thread_id: null },
      };
    },
    useArtifactBinding: () => ({ bind: { mutate: vi.fn() }, unbind: { mutate: vi.fn() } }),
    useArtifactComments: (artifactId: string | null | undefined) => ({
      data: artifactId ? (hoisted.commentsByArtifactId[artifactId] ?? []) : [],
    }),
    useArtifactStatus: () => ({ data: hoisted.artifactStatus ?? undefined }),
  };
});

vi.mock('../../../artifactComments/useCommentDestinations', () => ({
  useCommentDestinations: () => ({
    choices: [],
    canBind: false,
    targetFor: hoisted.targetFor,
  }),
}));

vi.mock('../ArtifactVersionPicker', () => ({
  ArtifactVersionPicker: () => <div data-testid="artifact-version-picker" />,
}));

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
    const event = new Event('message');
    Object.defineProperties(event, {
      data: { value: data },
      origin: { value: origin },
      source: { value: iframe.contentWindow },
    });
    window.dispatchEvent(event);
  });
}

function stubFrameWindow(
  iframe: HTMLIFrameElement,
  locationSpec: { throwSecurityError: true } | { href: string },
) {
  const postMessage = vi.fn();
  const fakeWin: Record<string, unknown> = { postMessage };
  if ('throwSecurityError' in locationSpec) {
    Object.defineProperty(fakeWin, 'location', {
      get() {
        throw new DOMException(
          'Blocked a frame with origin "null" from accessing a cross-origin frame.',
          'SecurityError',
        );
      },
    });
  } else {
    fakeWin.location = { href: locationSpec.href };
  }
  Object.defineProperty(iframe, 'contentWindow', {
    value: fakeWin,
    configurable: true,
  });
  return { postMessage };
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
  hoisted.artifactsByPath = {};
  hoisted.commentsByArtifactId = {};
  hoisted.artifactStatus = null;
  hoisted.targetFor = () => ({ kind: 'send', to: { threadId: 'th-1' } });
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
  it('rejects self-declared root: true on normal file frames and maps invalid/conflicting/missing-root payloads to unbridged with cleared bridge.path', () => {
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

    // 3. Normal file frame MUST reject self-declared `root: true` (treated as unrecognized -> unbridged).
    postFromFrame(iframe, { type: 'page:ready', v: 1, root: true });
    expect(docStates.at(-1)).toEqual({ kind: 'unbridged', currentUrl: null });
    expect(latestBridge.current?.path).toBeNull();

    // 4. All conflicting, invalid, and missing-root messages clear bridge.path and enter { kind: 'unbridged', currentUrl: null }.
    const badPayloads: Record<string, unknown>[] = [
      { type: 'page:ready', v: 1, root: true },
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

  it('accepts root: true in an opaque sandbox only with a matching server rootProof and rejects forged or redirected root: true', () => {
    vi.useFakeTimers();
    const docStates: HtmlFrameDocState[] = [];
    const onUnbridged = vi.fn();
    const expectedProof = 'server_root_proof_1234567890abcdef';

    const { container } = renderWithProviders(
      <BridgedFrame
        source={{
          key: 'mon-pub-1:v4',
          src: '/api/v1/dalton/monitors/public/public-pulse/v/4/?root_nonce=nonce-12345678901234',
          sandbox: 'allow-scripts',
          rootProof: expectedProof,
        }}
        title="Public Pulse"
        groundKey="mon-pub-1"
        expectedDocKey={null}
        onDocStateChange={(state) => docStates.push(state)}
        onUnbridged={onUnbridged}
      />,
    );

    const iframe = container.querySelector('iframe')!;
    const pings: number[] = [];
    const { postMessage } = stubFrameWindow(iframe, { throwSecurityError: true });
    postMessage.mockImplementation((msg: unknown) => {
      if (msg && typeof msg === 'object' && (msg as { type?: string }).type === 'page:ping') {
        pings.push((msg as { seq: number }).seq);
      }
    });

    // Host-initiated initial navigation to opaque version root with matching server proof -> enters bridged_root.
    postFromFrame(iframe, { type: 'page:ready', v: 1, docId: 'doc-root-1', root: true, rootProof: expectedProof });
    expect(docStates.at(-1)).toEqual({ kind: 'bridged_root' });

    // Same document finishes load and echoes page:pong with matching docId -> remains bridged_root.
    fireEvent.load(iframe);
    expect(pings).toHaveLength(1);
    postFromFrame(iframe, { type: 'page:pong', v: 1, seq: pings[0], docId: 'doc-root-1' });
    expect(docStates.at(-1)).toEqual({ kind: 'bridged_root' });
    act(() => { vi.advanceTimersByTime(3000); });
    expect(onUnbridged).not.toHaveBeenCalled();

    // Navigating away and later forging root: true (even with the old proof) without host navigation is rejected.
    postFromFrame(iframe, { type: 'page:ready', v: 1, docId: 'doc-away', invalidPath: true });
    expect(docStates.at(-1)).toEqual({ kind: 'unbridged', currentUrl: null });
    postFromFrame(iframe, { type: 'page:ready', v: 1, docId: 'doc-forged', root: true, rootProof: expectedProof });
    expect(docStates.at(-1)).toEqual({ kind: 'unbridged', currentUrl: null });
  });

  it('rejects root: true in an opaque sandbox when rootProof is missing or mismatched (initial navigation redirected)', () => {
    // Case 1: Opaque sandbox with NO server rootProof (cannot read contentWindow.location) -> must NOT default to true.
    const noProofStates: HtmlFrameDocState[] = [];
    const { container: noProofContainer, unmount: unmountNoProof } = renderWithProviders(
      <BridgedFrame
        source={{
          key: 'mon-pub-noproof:v4',
          src: '/api/v1/dalton/monitors/public/public-pulse/v/4/',
          sandbox: 'allow-scripts',
        }}
        title="Public Pulse No Proof"
        groundKey="mon-pub-noproof"
        expectedDocKey={null}
        onDocStateChange={(state) => noProofStates.push(state)}
      />,
    );
    const noProofIframe = noProofContainer.querySelector('iframe')!;
    stubFrameWindow(noProofIframe, { throwSecurityError: true });
    postFromFrame(noProofIframe, { type: 'page:ready', v: 1, docId: 'doc-unverified', root: true });
    expect(noProofStates.at(-1)).toEqual({ kind: 'unbridged', currentUrl: null });
    unmountNoProof();

    // Case 2: Opaque sandbox with expected server rootProof, but initial navigation redirected to a target
    // that posts root: true without the server proof (or with a forged proof) -> stays unbridged.
    for (const redirectedPayload of [
      { type: 'page:ready', v: 1, docId: 'doc-redir-1', root: true },
      { type: 'page:ready', v: 1, docId: 'doc-redir-2', root: true, rootProof: 'wrong_forged_proof_1234567890' },
    ]) {
      const states: HtmlFrameDocState[] = [];
      const { container, unmount } = renderWithProviders(
        <BridgedFrame
          source={{
            key: `mon-pub-redir:${String(redirectedPayload.docId)}`,
            src: '/api/v1/dalton/monitors/public/public-pulse/v/4/?root_nonce=nonce-12345678901234',
            sandbox: 'allow-scripts',
            rootProof: 'server_root_proof_1234567890abcdef',
          }}
          title="Public Pulse Redirected"
          groundKey="mon-pub-redir"
          expectedDocKey={null}
          onDocStateChange={(state) => states.push(state)}
        />,
      );
      const iframe = container.querySelector('iframe')!;
      stubFrameWindow(iframe, { throwSecurityError: true });
      postFromFrame(iframe, redirectedPayload);
      expect(states.at(-1)).toEqual({ kind: 'unbridged', currentUrl: null });
      unmount();
    }
  });

  it('verifies same-origin readable location for version roots without rootProof and rejects redirected paths', () => {
    const okStates: HtmlFrameDocState[] = [];
    const { container: okContainer, unmount: unmountOk } = renderWithProviders(
      <BridgedFrame
        source={{
          key: 'same-origin-root:v4',
          src: '/api/v1/dalton/monitors/public/public-pulse/v/4/',
          sandbox: 'allow-scripts allow-same-origin',
        }}
        title="Same Origin Root"
        groundKey="same-origin-root"
        expectedDocKey={null}
        onDocStateChange={(state) => okStates.push(state)}
      />,
    );
    const okIframe = okContainer.querySelector('iframe')!;
    stubFrameWindow(okIframe, { href: `${window.location.origin}/api/v1/dalton/monitors/public/public-pulse/v/4/` });
    postFromFrame(okIframe, { type: 'page:ready', v: 1, docId: 'doc-so-1', root: true }, window.location.origin);
    expect(okStates.at(-1)).toEqual({ kind: 'bridged_root' });
    unmountOk();

    const badStates: HtmlFrameDocState[] = [];
    const { container: badContainer } = renderWithProviders(
      <BridgedFrame
        source={{
          key: 'same-origin-redir:v4',
          src: '/api/v1/dalton/monitors/public/public-pulse/v/4/',
          sandbox: 'allow-scripts allow-same-origin',
        }}
        title="Same Origin Redirected"
        groundKey="same-origin-redir"
        expectedDocKey={null}
        onDocStateChange={(state) => badStates.push(state)}
      />,
    );
    const badIframe = badContainer.querySelector('iframe')!;
    stubFrameWindow(badIframe, { href: `${window.location.origin}/api/v1/dalton/monitors/public/public-pulse/v/4/other.html` });
    postFromFrame(badIframe, { type: 'page:ready', v: 1, docId: 'doc-so-2', root: true }, window.location.origin);
    expect(badStates.at(-1)).toEqual({ kind: 'unbridged', currentUrl: null });
  });

  it('guards timeout callbacks by navigation sequence, pairs ready/pong by docId, and keeps B bridged when B sends ready during A wait before B loads', () => {
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
    const pings: number[] = [];
    vi.spyOn(iframe.contentWindow!, 'postMessage').mockImplementation((msg: unknown) => {
      if (msg && typeof msg === 'object' && (msg as { type?: string }).type === 'page:ping') {
        pings.push((msg as { seq: number }).seq);
      }
    });

    // 1. Same navigation: document A sends page:ready(doc-a), finishes load, and echoes page:pong(doc-a) -> stays current.
    postFromFrame(iframe, { type: 'page:ready', v: 1, docId: 'doc-a', path: 'reports/a.html' });
    fireEvent.load(iframe);
    expect(docStates.at(-1)).toEqual({ kind: 'pending' });
    expect(pings).toHaveLength(1);
    postFromFrame(iframe, { type: 'page:pong', v: 1, seq: pings[0], docId: 'doc-a' });
    expect(docStates.at(-1)).toEqual({ kind: 'current', path: 'reports/a.html' });
    act(() => { vi.advanceTimersByTime(3000); });
    expect(onUnbridged).not.toHaveBeenCalled();

    // 2. While previous load A is still waiting, new document B sends page:ready(doc-b) BEFORE B's load fires.
    //    B's subsequent load MUST ping B and confirm doc-b via page:pong(doc-b), never misclassifying B as unbridged.
    fireEvent.load(iframe);
    expect(docStates.at(-1)).toEqual({ kind: 'pending' });
    act(() => { vi.advanceTimersByTime(1000); });
    postFromFrame(iframe, { type: 'page:ready', v: 1, docId: 'doc-b', path: 'reports/b.html' });
    expect(docStates.at(-1)).toEqual({ kind: 'linked', path: 'reports/b.html' });

    // B finishes loading -> triggers page:ping -> B echoes page:pong with doc-b -> stays linked.
    fireEvent.load(iframe);
    expect(docStates.at(-1)).toEqual({ kind: 'pending' });
    expect(pings).toHaveLength(2);
    postFromFrame(iframe, { type: 'page:pong', v: 1, seq: pings[1], docId: 'doc-b' });
    expect(docStates.at(-1)).toEqual({ kind: 'linked', path: 'reports/b.html' });
    act(() => { vi.advanceTimersByTime(3000); });
    expect(onUnbridged).not.toHaveBeenCalled();
    expect(docStates.at(-1)).toEqual({ kind: 'linked', path: 'reports/b.html' });

    // 3. Redirect before load finishes with mismatched docId on page:pong -> rejected and times out to unbridged.
    postFromFrame(iframe, { type: 'page:ready', v: 1, docId: 'doc-a2', path: 'reports/a.html' });
    expect(docStates.at(-1)).toEqual({ kind: 'current', path: 'reports/a.html' });
    expect(latestBridge.current?.path).toBe('reports/a.html');

    fireEvent.load(iframe);
    expect(docStates.at(-1)).toEqual({ kind: 'pending' });
    expect(pings).toHaveLength(3);
    // A pong with a different docId (or missing docId) must NOT confirm doc-a2.
    postFromFrame(iframe, { type: 'page:pong', v: 1, seq: pings[2], docId: 'doc-other' });
    postFromFrame(iframe, { type: 'page:pong', v: 1, seq: pings[2] });
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
  const PUBLIC_ROOT_NONCE = 'nonce-public-pulse-123456';
  const PUBLIC_ROOT_PROOF = 'proof_public_pulse_abcdef123456';

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
      root_nonce: PUBLIC_ROOT_NONCE,
      root_proof: PUBLIC_ROOT_PROOF,
    };
  }

  it('enters bridged_root in opaque sandbox on matching rootProof and enters unbridged (hiding open-in-new-window) on conflicting/invalid/unrecognized page:ready', async () => {
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
    stubFrameWindow(iframe, { throwSecurityError: true });
    const host = container.querySelector('.monitors-frame-host')!;
    expect(iframe.getAttribute('src')).toBe(
      `/api/v1/dalton/monitors/public/public-pulse/v/4/?root_nonce=${PUBLIC_ROOT_NONCE}`,
    );

    // Confirmed pathless version root with matching server proof -> bridged_root, open-in-new-window stays visible.
    postFromFrame(iframe, { type: 'page:ready', v: 1, docId: 'mon-doc-1', root: true, rootProof: PUBLIC_ROOT_PROOF });
    expect(host).toHaveAttribute('data-doc-state', 'bridged_root');
    expect(screen.getByRole('link', { name: /openInNewWindow|Open in new window|新窗口打开/i })).toBeInTheDocument();

    // Conflicting { path: undefined, root: true } -> unbridged, open-in-new-window hidden.
    postFromFrame(iframe, { type: 'page:ready', v: 1, path: undefined, root: true });
    expect(host).toHaveAttribute('data-doc-state', 'unbridged');
    expect(screen.queryByRole('link', { name: /openInNewWindow|Open in new window|新窗口打开/i })).toBeNull();

    // Forged root: true after leaving the host-initiated version root navigation stays rejected (unbridged).
    postFromFrame(iframe, { type: 'page:ready', v: 1, docId: 'mon-doc-2', root: true, rootProof: PUBLIC_ROOT_PROOF });
    expect(host).toHaveAttribute('data-doc-state', 'unbridged');
    expect(screen.queryByRole('link', { name: /openInNewWindow|Open in new window|新窗口打开/i })).toBeNull();
  });

  it('rejects redirected opaque-sandbox public monitor root without matching rootProof and hides open-in-new-window', async () => {
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
    stubFrameWindow(iframe, { throwSecurityError: true });
    const host = container.querySelector('.monitors-frame-host')!;

    // Redirected target posts root: true without server-signed rootProof -> must NOT enter bridged_root.
    postFromFrame(iframe, { type: 'page:ready', v: 1, docId: 'mon-redir', root: true });
    expect(host).toHaveAttribute('data-doc-state', 'unbridged');
    expect(screen.queryByRole('link', { name: /openInNewWindow|Open in new window|新窗口打开/i })).toBeNull();
  });

  it('maps missing root: true on a pathless public monitor to unbridged and hides open-in-new-window', async () => {
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
    stubFrameWindow(iframe, { throwSecurityError: true });
    const host = container.querySelector('.monitors-frame-host')!;

    postFromFrame(iframe, { type: 'page:ready', v: 1 });
    expect(host).toHaveAttribute('data-doc-state', 'unbridged');
    expect(screen.queryByRole('link', { name: /openInNewWindow|Open in new window|新窗口打开/i })).toBeNull();
  });
});

describe('Desktop pending ArtifactCrumbs and linked-doc CommentTools parity', () => {
  it('disables desktop ArtifactCrumbs Update button and callback while pending (changed and legacy_refused)', () => {
    const onReview = vi.fn();
    hoisted.artifactStatus = {
      latest_seq: 2,
      live_sha256: 'a'.repeat(64),
      page_sha256: 'b'.repeat(64),
      files: [],
      left_out: [],
      total_size: 100,
      changed: true,
      drift: { added: ['reports/extra.js'], removed: [], changed: [] },
    };

    const view = makeArtifactView(null).versionView;
    const { rerender } = renderWithProviders(
      <ArtifactCrumbs view={view} onReview={onReview} readAt={1} updateDisabled />,
    );

    const changedBtn = screen.getByRole('button', { name: /artifact\.update|Update|更新/i });
    expect(changedBtn).toBeDisabled();
    fireEvent.click(changedBtn);
    expect(onReview).not.toHaveBeenCalled();

    // Legacy refused variant is also disabled while pending.
    const refusedView = {
      ...view,
      record: { ...view.record!, legacy_refused: 'too_many_files' as const },
    };
    rerender(<ArtifactCrumbs view={refusedView} onReview={onReview} readAt={1} updateDisabled />);
    const refusedBtn = screen.getByRole('button', { name: /artifact\.update|Update|更新/i });
    expect(refusedBtn).toBeDisabled();
    fireEvent.click(refusedBtn);
    expect(onReview).not.toHaveBeenCalled();

    // Once doc state settles (updateDisabled: false), Update becomes enabled and invokes onReview.
    rerender(<ArtifactCrumbs view={refusedView} onReview={onReview} readAt={1} updateDisabled={false} />);
    const enabledBtn = screen.getByRole('button', { name: /artifact\.update|Update|更新/i });
    expect(enabledBtn).toBeEnabled();
    fireEvent.click(enabledBtn);
    expect(onReview).toHaveBeenCalledTimes(1);
  });

  it('targets linked document B for comment counts and Send all on both desktop and mobile even in a version frame without CommentLayer', async () => {
    const user = userEvent.setup();
    const makeComment = (id: string, artifactId: string, body: string): ArtifactComment => ({
      comment_id: id,
      artifact_id: artifactId,
      author_id: 'user-1',
      status: 'open',
      delivery: 'queued',
      thread_id: 'th-1',
      thread_title: 'Main',
      thread_hidden: false,
      anchor: { kind: 'element', path: 'h1', quote: body },
      body,
      replies: [],
      created_at: '2026-11-25T00:00:00Z',
      updated_at: '2026-11-25T00:00:00Z',
    } as unknown as ArtifactComment);

    const cmtA = makeComment('cmt-a-1', 'art-a', 'Comment on A');
    const cmtB1 = makeComment('cmt-b-1', 'art-b', 'First comment on B');
    const cmtB2 = makeComment('cmt-b-2', 'art-b', 'Second comment on B');

    hoisted.artifactsByPath = {
      'reports/a.html': { artifact_id: 'art-a', path: 'reports/a.html', shared: true, bound_thread_id: null },
      'reports/b.html': { artifact_id: 'art-b', path: 'reports/b.html', shared: true, bound_thread_id: null },
    };
    hoisted.commentsByArtifactId = {
      'art-a': [cmtA],
      'art-b': [cmtB1, cmtB2],
    };

    // Simulate a version frame where the hidden live CommentLayer still maps tab-1's opened file A -> A.
    const ui = {
      ...makeCommentUi(),
      docOf: (tabId: string, opened: string) =>
        tabId === 'tab-1' && opened === 'reports/a.html' ? 'reports/a.html' : opened,
    } as unknown as HtmlArtifactView['ui'];

    const desktopSend = vi.fn().mockResolvedValue({ threadId: 'th-1' });
    const desktopSender: ArtifactCommentSender = {
      besideThreadId: 'th-1',
      running: false,
      send: desktopSend,
    };

    // 1. Desktop: FilePanel passes htmlDocState.path ('reports/b.html') to CommentTools when linked.
    const { unmount } = renderWithProviders(
      <ArtifactCommentSendContext.Provider value={desktopSender}>
        <CommentTools
          tabId="tab-1"
          workspaceId="ws-1"
          path="reports/b.html"
          ui={ui}
          variant="crumb"
          modeDisabled
        />
      </ArtifactCommentSendContext.Provider>,
    );

    const sendAllDesktop = screen.getByRole('button', { name: /sendAll|Send all|全部发送/i });
    expect(screen.getByRole('button', { name: /2/ })).toBeInTheDocument();
    await user.click(sendAllDesktop);
    await waitFor(() => expect(desktopSend).toHaveBeenCalledTimes(1));
    expect(desktopSend.mock.calls[0][0].map((c: ArtifactComment) => c.comment_id)).toEqual(['cmt-b-1', 'cmt-b-2']);
    unmount();

    // 2. Mobile: FileHeaderActions (MobileHtmlOverflowMenu) targets linked doc B for open count and Send all.
    hoisted.isMobile = true;
    const mobileSend = vi.fn().mockResolvedValue({ threadId: 'th-1' });
    const mobileSender: ArtifactCommentSender = {
      besideThreadId: 'th-1',
      running: false,
      send: mobileSend,
    };
    renderWithProviders(
      <ArtifactCommentSendContext.Provider value={mobileSender}>
        <FileHeaderActions
          selectedFile="reports/a.html"
          isEditing={false}
          workspaceId="ws-1"
          fileContent="<h1>A</h1>"
          fileMime="text/html"
          canEdit={false}
          onStartEdit={vi.fn()}
          onOpenExportModal={vi.fn()}
          triggerDownloadFn={vi.fn().mockResolvedValue(undefined)}
          canDownload
          readFileFullFn={vi.fn().mockResolvedValue({ content: '' })}
          access={{ kind: 'owner' }}
          htmlDocState={{ kind: 'linked', path: 'reports/b.html' }}
          onOpenDocAsTab={vi.fn()}
          mobileHtmlTools={{
            tabId: 'tab-1',
            ui,
            versionView: makeArtifactView(2).versionView,
            modeDisabled: true,
            readAt: 1,
          }}
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
      </ArtifactCommentSendContext.Provider>,
    );

    await user.click(screen.getByTestId('file-header-mobile-more'));
    const listItem = await screen.findByTestId('file-header-mobile-comment-list');
    expect(listItem).toHaveTextContent('(2)');
    const sendAllMobile = screen.getByTestId('file-header-mobile-send-all');
    expect(sendAllMobile).toHaveTextContent('(2)');
    await user.click(sendAllMobile);
    await waitFor(() => expect(mobileSend).toHaveBeenCalledTimes(1));
    expect(mobileSend.mock.calls[0][0].map((c: ArtifactComment) => c.comment_id)).toEqual(['cmt-b-1', 'cmt-b-2']);
  });
});
