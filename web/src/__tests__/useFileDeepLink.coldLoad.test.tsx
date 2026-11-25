/**
 * Cold-load deep-link tests (empty React Query cache):
 * 1. Rooted / sibling `?file=` links wait for workspace folder, sibling list,
 *    and computer-root queries to settle before consuming `?file=` so
 *    same-workspace and cross-workspace links resolve to the exact workspace.
 * 2. When folder queries fail, `?file=` unblocks and degrades to the current
 *    workspace path instead of stalling.
 * 3. Decoded `?file=` paths containing `#`, `?`, spaces, and Chinese flow from
 *    gateway encoding through `useFileDeepLink` → `usePanelLinkOpen` →
 *    `normalizeAgentPath` without truncating at `#` or `?`.
 */
import React, { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { Computer, ComputersResponse, Workspace, WorkspacesResponse } from '@/types/api';
import { useComputerFolders } from '@/pages/ChatAgent/hooks/useComputerFolders';
import { useComputerFoldersReady } from '@/pages/ChatAgent/hooks/useComputerFoldersReady';
import { useFileDeepLink } from '@/pages/ChatAgent/components/chatView/useFileDeepLink';
import { usePanelLinkOpen } from '@/pages/ChatAgent/components/chatView/usePanelLinks';
import type { UnsequencedTarget } from '@/pages/ChatAgent/components/filePanel/types';
import { normalizeAgentPath } from '@/pages/ChatAgent/utils/agentPaths';

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const WS_RESEARCH = '11111111-1111-4111-8111-111111111111';
const WS_NVDA = '22222222-2222-4222-8222-222222222222';
const THREAD_ID = '33333333-3333-4333-8333-333333333333';

const apiMocks = vi.hoisted(() => ({
  getWorkspace: vi.fn<(id: string) => Promise<Workspace>>(),
  getWorkspaces: vi.fn<() => Promise<WorkspacesResponse>>(),
  getComputers: vi.fn<() => Promise<ComputersResponse>>(),
}));

vi.mock('@/pages/ChatAgent/utils/api', async (importOriginal) => {
  const orig = await importOriginal<Record<string, unknown>>();
  return {
    ...orig,
    getWorkspace: (...args: [string]) => apiMocks.getWorkspace(...args),
    getWorkspaces: () => apiMocks.getWorkspaces(),
    getComputers: () => apiMocks.getComputers(),
  };
});

function makeWorkspace(overrides: Partial<Workspace> & { workspace_id: string; name: string }): Workspace {
  return {
    status: 'running',
    computer_id: 'c-1',
    dir_name: overrides.name,
    previous_dir_names: [],
    root_dir: '/home/workspace',
    ...overrides,
  } as Workspace;
}

function makeComputer(overrides: Partial<Computer> & { computer_id: string }): Computer {
  return {
    name: 'Primary',
    kind: 'docker',
    status: 'running',
    root_dir: '/home/workspace',
    ...overrides,
  } as Computer;
}

interface HarnessCapture {
  override: string | null;
  target: UnsequencedTarget | null;
  search: string;
  rootDir: string | null | undefined;
}

function DeepLinkHarness({
  workspaceId,
  capture,
}: {
  workspaceId: string;
  capture: HarnessCapture;
}) {
  const location = useLocation();
  const folders = useComputerFolders(workspaceId);
  const foldersReady = useComputerFoldersReady(workspaceId);
  const [override, setOverride] = useState<string | null>(null);
  const [target, setTarget] = useState<UnsequencedTarget | null>(null);

  const openFile = usePanelLinkOpen({
    workspaceId,
    folders,
    override,
    setOverride,
    land: setTarget,
    leave: (go) => go(),
  });

  useFileDeepLink(true, workspaceId, openFile, foldersReady);

  capture.override = override;
  capture.target = target;
  capture.search = location.search;
  capture.rootDir = folders?.rootDir;

  return null;
}

function renderHarness(initialEntry: string, workspaceId = WS_RESEARCH) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const capture: HarnessCapture = {
    override: null,
    target: null,
    search: '',
    rootDir: undefined,
  };
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <DeepLinkHarness workspaceId={workspaceId} capture={capture} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { view, capture, qc };
}

beforeEach(() => {
  apiMocks.getWorkspace.mockReset();
  apiMocks.getWorkspaces.mockReset();
  apiMocks.getComputers.mockReset();
});

describe('useFileDeepLink cold-load folder settlement', () => {
  it('waits for empty-cache workspace and sibling queries before consuming same-workspace deep link', async () => {
    const wsDef = deferred<Workspace>();
    const allDef = deferred<WorkspacesResponse>();
    const compDef = deferred<ComputersResponse>();
    apiMocks.getWorkspace.mockReturnValue(wsDef.promise);
    apiMocks.getWorkspaces.mockReturnValue(allDef.promise);
    apiMocks.getComputers.mockReturnValue(compDef.promise);

    const researchWs = makeWorkspace({ workspace_id: WS_RESEARCH, name: 'Research', dir_name: 'Research' });
    const nvdaWs = makeWorkspace({ workspace_id: WS_NVDA, name: 'NVDA', dir_name: 'NVDA' });

    const { capture } = renderHarness(
      `/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2FResearch%2Fresults%2Freport.md`,
    );

    // While queries are pending, `?file=` stays intact and nothing opens yet.
    expect(capture.target).toBeNull();
    expect(capture.search).toContain('file=');

    await act(async () => {
      wsDef.resolve(researchWs);
    });
    // Still waiting for sibling workspace list & computers list.
    expect(capture.target).toBeNull();
    expect(capture.search).toContain('file=');

    await act(async () => {
      allDef.resolve({ workspaces: [researchWs, nvdaWs], total: 2 });
      compDef.resolve({ computers: [makeComputer({ computer_id: 'c-1' })], total: 1 });
    });

    await waitFor(() => {
      expect(capture.target).toEqual({
        kind: 'file',
        path: 'results/report.md',
        location: null,
        pin: false,
      });
    });
    expect(capture.override).toBeNull();
    expect(capture.search).toBe('');
  });

  it('waits for empty-cache sibling list before consuming cross-workspace deep link (/home/workspace/NVDA/results/report.md selects NVDA)', async () => {
    const wsDef = deferred<Workspace>();
    const allDef = deferred<WorkspacesResponse>();
    const compDef = deferred<ComputersResponse>();
    apiMocks.getWorkspace.mockReturnValue(wsDef.promise);
    apiMocks.getWorkspaces.mockReturnValue(allDef.promise);
    apiMocks.getComputers.mockReturnValue(compDef.promise);

    const researchWs = makeWorkspace({ workspace_id: WS_RESEARCH, name: 'Research', dir_name: 'Research' });
    const nvdaWs = makeWorkspace({ workspace_id: WS_NVDA, name: 'NVDA', dir_name: 'NVDA' });

    const { capture } = renderHarness(
      `/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2FNVDA%2Fresults%2Freport.md`,
    );

    expect(capture.target).toBeNull();
    expect(capture.search).toContain('file=');

    await act(async () => {
      wsDef.resolve(researchWs);
    });
    // Sibling list has not settled yet: must NOT prematurely consume into Research.
    expect(capture.target).toBeNull();
    expect(capture.override).toBeNull();
    expect(capture.search).toContain('file=');

    await act(async () => {
      allDef.resolve({ workspaces: [researchWs, nvdaWs], total: 2 });
      compDef.resolve({ computers: [makeComputer({ computer_id: 'c-1' })], total: 1 });
    });

    await waitFor(() => {
      expect(capture.override).toBe(WS_NVDA);
      expect(capture.target).toEqual({
        kind: 'file',
        path: 'results/report.md',
        location: null,
        pin: false,
      });
    });
    expect(capture.search).toBe('');
  });

  it('falls back to current workspace relative path when folder queries fail', async () => {
    const notFound = Object.assign(new Error('Workspace unavailable'), { response: { status: 404 } });
    apiMocks.getWorkspace.mockRejectedValue(notFound);
    apiMocks.getWorkspaces.mockRejectedValue(notFound);
    apiMocks.getComputers.mockRejectedValue(notFound);

    const { capture } = renderHarness(
      `/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Freport.md`,
    );

    await waitFor(() => {
      expect(capture.target).toEqual({
        kind: 'file',
        path: '/home/workspace/results/report.md',
        location: null,
        pin: false,
      });
    });
    expect(capture.override).toBeNull();
    expect(normalizeAgentPath((capture.target as { path: string }).path)).toBe('results/report.md');
    expect(capture.search).toBe('');
  });

  it('opens filenames containing #, ?, spaces, and Chinese from gateway-encoded ?file= through useFileDeepLink and FilePanel normalization', async () => {
    const researchWs = makeWorkspace({
      workspace_id: WS_RESEARCH,
      name: 'Research',
      dir_name: 'Research',
      root_dir: '/home/someone/langalpha',
      previous_root_dirs: ['/srv/old'],
    } as Partial<Workspace> & { workspace_id: string; name: string });
    const nvdaWs = makeWorkspace({
      workspace_id: WS_NVDA,
      name: 'NVDA',
      dir_name: 'NVDA',
      root_dir: '/home/someone/langalpha',
    });

    const cases: Array<{ rawDeepLinkPath: string; expectedFilePanelPath: string; expectedWorkspaceOverride: string | null }> = [
      {
        rawDeepLinkPath: '/home/workspace/Research/results/issue#1.md',
        expectedFilePanelPath: 'results/issue#1.md',
        expectedWorkspaceOverride: null,
      },
      {
        rawDeepLinkPath: '/home/workspace/Research/results/中文 报告 #1?.md',
        expectedFilePanelPath: 'results/中文 报告 #1?.md',
        expectedWorkspaceOverride: null,
      },
      {
        rawDeepLinkPath: '/srv/old/Research/results/q?a#2.md',
        expectedFilePanelPath: 'results/q?a#2.md',
        expectedWorkspaceOverride: null,
      },
      {
        rawDeepLinkPath: '/home/workspace/NVDA/results/中文 #1?.md',
        expectedFilePanelPath: 'results/中文 #1?.md',
        expectedWorkspaceOverride: WS_NVDA,
      },
    ];

    for (const tc of cases) {
      apiMocks.getWorkspace.mockResolvedValue(researchWs);
      apiMocks.getWorkspaces.mockResolvedValue({ workspaces: [researchWs, nvdaWs], total: 2 });
      apiMocks.getComputers.mockResolvedValue({
        computers: [makeComputer({ computer_id: 'c-1', root_dir: '/home/someone/langalpha' })],
        total: 1,
      });

      // Exact same `?file=` encoding as gateway `encodeFileParam` (`URLSearchParams` + space as `%20`)
      const encoded = new URLSearchParams({ file: tc.rawDeepLinkPath })
        .toString()
        .slice('file='.length)
        .replace(/\+/g, '%20');

      const { view, capture } = renderHarness(`/chat/t/${THREAD_ID}?file=${encoded}`);
      await waitFor(() => {
        expect(capture.target).not.toBeNull();
      });

      expect(capture.override).toBe(tc.expectedWorkspaceOverride);
      const openedPath = (capture.target as { kind: 'file'; path: string }).path;
      expect(openedPath).toBe(tc.expectedFilePanelPath);
      expect(normalizeAgentPath(openedPath, capture.rootDir)).toBe(tc.expectedFilePanelPath);
      expect(capture.search).toBe('');
      view.unmount();
    }
  });
});
