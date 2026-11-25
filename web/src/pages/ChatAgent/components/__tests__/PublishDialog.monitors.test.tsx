import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError } from '@/api/client';
import { renderWithProviders } from '@/test/utils';
import { runAuthResets } from '@/lib/authResets';
import { queryKeys } from '@/lib/queryKeys';
import type { CallerIdentity, CallerIdentitySnapshot } from '@/hooks/useCallerIdentity';
import type { Artifact, ArtifactStatus, Workspace } from '@/types/api';

const tierState = vi.hoisted(() => ({
  callerTier: 'internal' as 'external' | 'internal' | 'admin' | 'unknown',
  status: 'ready' as 'loading' | 'ready' | 'error',
  isConfirming: false,
  colleagueSharingEnabled: true,
  isMobile: false,
  revalidate: vi.fn(async () => undefined),
}));

function buildIdentitySnapshot(): CallerIdentitySnapshot {
  const ready = tierState.status === 'ready' && tierState.callerTier !== 'unknown';
  const tier = ready ? tierState.callerTier : 'unknown';
  return {
    tier,
    status: tierState.status,
    authMode: ready ? 'local' : null,
    isPlatform: false,
    isKnown: ready,
    isConfirming: tierState.isConfirming,
    isMultiUserAuth: ready,
    isSingleUserOss: false,
    isAdmin: ready && tier === 'admin',
    isInternal: ready && (tier === 'admin' || tier === 'internal'),
    isExternal: ready && tier === 'external',
    canActAsOwner: ready && (tier === 'admin' || tier === 'internal'),
    canPublishPublic: ready && tier === 'admin',
    canInvite: ready && (tier === 'admin' || tier === 'internal'),
    isTeamCredentialsShared: true,
    canManageCredentials: ready && tier === 'admin',
    isPm: ready && tier === 'admin',
    colleagueSharingEnabled: tierState.colleagueSharingEnabled,
  };
}

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: (): CallerIdentity => ({
    ...buildIdentitySnapshot(),
    getLatestIdentity: () => buildIdentitySnapshot(),
    isConfirmingNow: () => tierState.isConfirming,
    revalidate: tierState.revalidate,
  }),
}));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => tierState.isMobile,
  getIsMobileSnapshot: () => tierState.isMobile,
}));

const workspaceState = vi.hoisted(() => ({
  data: {
    status: 'running',
    name: 'ws-1',
    auto_publish_paused: false,
    auto_publish_pause_token: null as string | null,
  },
  requestedIds: [] as Array<string | null | undefined>,
}));

vi.mock('@/config/daltonAuth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/config/daltonAuth')>();
  return {
    ...actual,
    isDaltonAuth: true,
    DALTON_AUTH_MODE: 'local' as const,
  };
});

vi.mock('@/hooks/useTeamCredentials', () => ({
  useTeamCredentials: () => ({
    isShared: true,
    canDisconnect: tierState.callerTier === 'admin',
    canManage: tierState.callerTier === 'admin',
    isPm: tierState.callerTier === 'admin',
    canInvite: tierState.callerTier !== 'external',
    callerTier: tierState.callerTier,
    callerTierPending: false,
    callerTierError: false,
    colleagueSharingEnabled: tierState.colleagueSharingEnabled,
  }),
}));

vi.mock('@/components/ui/use-toast', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/components/ui/use-toast')>();
  return { ...orig, toast: vi.fn<typeof orig.toast>() };
});

vi.mock('../viewers/html/BridgedFrame', () => ({
  BridgedFrame: ({ onUnbridged }: { onUnbridged?: () => void }) => {
    React.useEffect(() => {
      onUnbridged?.();
    });
    return null;
  },
}));

vi.mock('@/pages/ChatAgent/utils/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/pages/ChatAgent/utils/api')>();
  const answer = vi.fn<typeof orig.readWorkspaceFile>(async () => ({
    content: '<html><body><h1>Monitor</h1></body></html>',
    mime: 'text/html',
    truncated: false,
    from: { kind: 'live' },
  }));
  return {
    ...orig,
    getWorkspace: vi.fn<typeof orig.getWorkspace>(),
    listWorkspaceFiles: vi.fn<typeof orig.listWorkspaceFiles>(async () => ({
      files: ['results/report.html'],
      from: { kind: 'live' },
    })),
    readWorkspaceFile: answer,
    readWorkspaceFileFull: answer as unknown as typeof orig.readWorkspaceFileFull,
    writeWorkspaceFile: vi.fn<typeof orig.writeWorkspaceFile>(),
    downloadWorkspaceFileAsArrayBuffer: vi.fn<typeof orig.downloadWorkspaceFileAsArrayBuffer>(),
    triggerFileDownload: vi.fn<typeof orig.triggerFileDownload>(),
    resolveWorkspaceFile: vi.fn<typeof orig.resolveWorkspaceFile>(),
    getPreviewUrl: vi.fn<typeof orig.getPreviewUrl>(),
  };
});
vi.mock('@/hooks/useWorkspace', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useWorkspace')>();
  return {
    ...actual,
    useWorkspace: (id: string | null | undefined) => {
      workspaceState.requestedIds.push(id);
      return actual.useWorkspace(id);
    },
  };
});
vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'dark' }),
}));
vi.mock('@/hooks/useWorkspaceFileGrant', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useWorkspaceFileGrant')>();
  type GrantRefetch = ReturnType<typeof actual.useWorkspaceFileGrant>['refetch'];
  return {
    useWorkspaceFileGrant: () => ({
      data: { prefix: '/api/v1/wsfiles/g/grant-1/', expires_in: 3600 },
      error: null,
      refetch: vi.fn<GrantRefetch>(),
    }),
  };
});
vi.mock('@/hooks/useShareLink', () => ({ useShareLink: () => ({ data: undefined }) }));
vi.mock('../FilePanelMemo', () => ({
  memoMimeForName: () => null,
  useAddToMemo: () => vi.fn<() => void>(),
  useWorkspaceMemoIndex: () => new Map(),
  useMemoStaleCheck: () => ({ status: null, sandboxText: null, refresh: () => {} }),
  MemoStaleBanner: () => null,
  MemoDiffModal: () => null,
}));
vi.mock('../SandboxSettingsPanel', () => ({ SandboxSettingsContent: () => null }));
vi.mock('../SyntaxHighlighter', () => ({
  default: ({ children }: { children: string }) => <pre>{children}</pre>,
  oneDark: {},
  oneLight: {},
}));

vi.mock('@/pages/ChatAgent/utils/api/artifacts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/ChatAgent/utils/api/artifacts')>();
  return {
    ...actual,
    findArtifact: vi.fn<typeof actual.findArtifact>(),
    ensureArtifact: vi.fn<typeof actual.ensureArtifact>(),
    getArtifactStatus: vi.fn<typeof actual.getArtifactStatus>(),
    listArtifactVersions: vi.fn<typeof actual.listArtifactVersions>(async () => []),
    unshareArtifact: vi.fn<typeof actual.unshareArtifact>(),
    setArtifactKeepUpdated: vi.fn<typeof actual.setArtifactKeepUpdated>(),
    setArtifactFileLeftOut: vi.fn<typeof actual.setArtifactFileLeftOut>(),
  };
});

vi.mock('@/pages/Monitors/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/Monitors/api')>();
  return {
    ...actual,
    getMonitorByArtifact: vi.fn<typeof actual.getMonitorByArtifact>(async () => null),
    listCompanyMembers: vi.fn<typeof actual.listCompanyMembers>(async () => [
      { user_id: 'u-colleague-1', email: 'bob@company.com', name: 'Bob', tier: 'internal' },
    ]),
    publishMonitor: vi.fn<typeof actual.publishMonitor>(),
    updateMonitor: vi.fn<typeof actual.updateMonitor>(),
  };
});

import { getWorkspace } from '@/pages/ChatAgent/utils/api';
import {
  ensureArtifact,
  findArtifact,
  getArtifactStatus,
  unshareArtifact,
} from '@/pages/ChatAgent/utils/api/artifacts';
import { toast } from '@/components/ui/use-toast';
import { getMonitorByArtifact, publishMonitor, updateMonitor } from '@/pages/Monitors/api';
import FilePanel from '../FilePanel';
import { usePanelFiles } from '../chatView/usePanelFiles';
import { fileTarget } from '../filePanel/types';
import PublishDialog from '../PublishDialog';

const mockGetWorkspace = vi.mocked(getWorkspace);
const mockFind = vi.mocked(findArtifact);
const mockEnsure = vi.mocked(ensureArtifact);
const mockStatus = vi.mocked(getArtifactStatus);
const mockUnshare = vi.mocked(unshareArtifact);
const mockGetMonitor = vi.mocked(getMonitorByArtifact);
const mockPublishMonitor = vi.mocked(publishMonitor);

const ARTIFACT: Artifact = {
  artifact_id: '6f1c2a7e-3b4d-4e5f-8a9b-0c1d2e3f4a5b',
  code: 'k7f2m9q1x4z8',
  url: '/a/k7f2m9q1x4z8',
  workspace_id: 'ws-1',
  path: 'results/report.html',
  name: 'report.html',
  shared: false,
  shared_at: null,
  keep_updated: false,
  latest_seq: 0,
  bound_thread_id: null,
  open_comments: 0,
  unread: 0,
  created_at: '2026-11-16T00:00:00Z',
};

const STATUS: ArtifactStatus = {
  latest_seq: 0,
  live_sha256: 'a'.repeat(64),
  page_sha256: 'c'.repeat(64),
  files: [{ path: 'results/report.html', size: 1024, reason: 'entry', optional: false }],
  left_out: [],
  total_size: 1024,
  changed: null,
  drift: null,
};

describe('PublishDialog 4-tier monitor visibility (Checklist 8)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    runAuthResets();
    tierState.callerTier = 'internal';
    tierState.status = 'ready';
    tierState.isConfirming = false;
    tierState.colleagueSharingEnabled = true;
    workspaceState.data = {
      status: 'running',
      name: 'ws-1',
      auto_publish_paused: false,
      auto_publish_pause_token: null,
    };
    workspaceState.requestedIds = [];
    mockGetWorkspace.mockImplementation(async () => ({
      workspace_id: 'ws-1',
      is_home: false,
      ...workspaceState.data,
    }));
    mockEnsure.mockResolvedValue(ARTIFACT);
    mockFind.mockResolvedValue(ARTIFACT);
    mockStatus.mockResolvedValue(STATUS);
    mockGetMonitor.mockResolvedValue(null);
    mockPublishMonitor.mockResolvedValue({
      monitor: {
        monitor_id: 'm-1',
        slug: 'report',
        artifact_id: ARTIFACT.artifact_id,
        artifact_code: ARTIFACT.code,
        workspace_id: 'ws-1',
        entry_path: 'results/report.html',
        owner_user_id: 'u-1',
        owner_name: 'Alice',
        owner_email: 'alice@company.com',
        title: 'report',
        category: 'other',
        sort_order: 0,
        visibility: 'own',
        member_user_ids: [],
        published_seq: 1,
        keep_updated: false,
        source_kind: 'workspace',
        source_ref: 'ws-1:results/report.html',
        acl_revision: 1,
        last_publish_error: null,
        last_publish_failed_at: null,
        data_as_of: null,
        created_at: '2026-11-16T12:00:00Z',
        updated_at: '2026-11-16T12:00:00Z',
        can_manage: true,
      },
      versions: [],
      monitor_grant: 'm1.tok',
      frame_base: '/api/v1/dalton/monitors/g/m1.tok/',
      data_base: '/api/v1/dalton/monitors/g/m1.tok/data',
      expires_in: 43200,
      cut: true,
    });
  });

  it('external member only sees Only me visibility option', async () => {
    tierState.callerTier = 'external';
    renderWithProviders(<PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Only me' })).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Selected colleagues' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Company' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Anyone/i })).not.toBeInTheDocument();
  });

  it('internal member sees own, members, and company, but NOT public', async () => {
    tierState.callerTier = 'internal';
    renderWithProviders(<PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Only me' })).toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Selected colleagues' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Company' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Anyone/i })).not.toBeInTheDocument();

    // Selecting members opens company member multi-select
    await userEvent.click(screen.getByRole('button', { name: 'Selected colleagues' }));
    const bobBtn = await screen.findByRole('button', { name: /Bob/i });
    await userEvent.click(bobBtn);

    const publishBtn = screen.getByRole('button', { name: 'Publish' });
    await waitFor(() => expect(publishBtn).toBeEnabled());
    await userEvent.click(publishBtn);

    await waitFor(() => expect(mockPublishMonitor).toHaveBeenCalledTimes(1));
    expect(mockPublishMonitor.mock.calls[0][0]).toMatchObject({
      visibility: 'members',
      member_user_ids: ['u-colleague-1'],
    });
  });

  it('admin sees public tier and requires secondary confirmation before publishing publicly', async () => {
    tierState.callerTier = 'admin';
    renderWithProviders(<PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />);

    const publicBtn = await screen.findByRole('button', { name: /Anyone \(Public link\)/i });
    await userEvent.click(publicBtn);

    const publishBtn = screen.getByRole('button', { name: 'Publish' });
    await waitFor(() => expect(publishBtn).toBeEnabled());
    await userEvent.click(publishBtn);

    // Must not submit yet before confirmation
    expect(mockPublishMonitor).not.toHaveBeenCalled();

    const confirmBtn = await screen.findByRole('button', { name: 'Publish publicly' });
    await userEvent.click(confirmBtn);

    await waitFor(() => expect(mockPublishMonitor).toHaveBeenCalledTimes(1));
    expect(mockPublishMonitor.mock.calls[0][0]).toMatchObject({
      visibility: 'public',
    });
  });

  it('external member opens PublishDialog from real FilePanel preview entry and publishes Only me monitor', async () => {
    tierState.callerTier = 'external';
    renderWithProviders(
      <FilePanel
        workspaceId="ws-1"
        callerTier="external"
        onClose={() => {}}
        files={['results/report.html']}
        target={fileTarget('results/report.html', {}, 1)}
        onTargetHandled={() => {}}
      />,
    );

    const headerPublishBtn = await screen.findByRole('button', { name: 'Publish' });
    fireEvent.click(headerPublishBtn);

    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Only me' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Selected colleagues' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Company' })).not.toBeInTheDocument();

    const submitBtns = screen.getAllByRole('button', { name: 'Publish' });
    const dialogPublishBtn = submitBtns[submitBtns.length - 1];
    await waitFor(() => expect(dialogPublishBtn).toBeEnabled());
    await userEvent.click(dialogPublishBtn);

    await waitFor(() => expect(mockPublishMonitor).toHaveBeenCalledTimes(1));
    expect(mockPublishMonitor.mock.calls[0][0]).toMatchObject({
      workspace_id: 'ws-1',
      path: 'results/report.html',
      visibility: 'own',
      member_user_ids: [],
    });
  });

  it('non-admin opening an existing public monitor must explicitly reselect visibility before updating, keeps Unshare visible, and non-monitor shared file keeps Unshare button', async () => {
    tierState.callerTier = 'internal';
    mockGetMonitor.mockResolvedValue({
      monitor_id: 'm-pub',
      slug: 'pub-mon',
      artifact_id: ARTIFACT.artifact_id,
      artifact_code: ARTIFACT.code,
      workspace_id: 'ws-1',
      entry_path: 'results/report.html',
      owner_user_id: 'u-admin',
      owner_name: 'Admin',
      owner_email: 'admin@company.com',
      title: 'Public Monitor',
      category: 'overview',
      sort_order: 0,
      visibility: 'public',
      member_user_ids: [],
      published_seq: 1,
      keep_updated: false,
      source_kind: 'workspace',
      source_ref: 'ws-1:results/report.html',
      acl_revision: 1,
      last_publish_error: null,
      last_publish_failed_at: null,
      data_as_of: null,
      created_at: '2026-11-16T12:00:00Z',
      updated_at: '2026-11-16T12:00:00Z',
      can_manage: true,
    });

    const { unmount } = renderWithProviders(
      <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
    );
    expect(
      await screen.findByText('This monitor currently uses a public link. Select a visibility scope before updating.'),
    ).toBeInTheDocument();
    const updateBtn = screen.getByRole('button', { name: 'Update' });
    expect(updateBtn).toBeDisabled();
    // Normal mode also exposes Unshare for an existing public monitor.
    expect(screen.getByRole('button', { name: 'Unshare' })).toBeEnabled();

    // Explicitly selecting Company re-enables Update and submits company visibility.
    await userEvent.click(screen.getByRole('button', { name: 'Company' }));
    await waitFor(() => expect(updateBtn).toBeEnabled());
    await userEvent.click(updateBtn);
    await waitFor(() => expect(mockPublishMonitor).toHaveBeenCalledTimes(1));
    expect(mockPublishMonitor.mock.calls[0][0]).toMatchObject({ visibility: 'company' });
    unmount();

    // Non-monitor shared legacy artifact keeps Unshare button
    mockGetMonitor.mockResolvedValue(null);
    mockEnsure.mockResolvedValue({
      ...ARTIFACT,
      shared: true,
      shared_at: '2026-11-16T12:00:00Z',
      latest_seq: 2,
    });
    mockFind.mockResolvedValue({
      ...ARTIFACT,
      shared: true,
      shared_at: '2026-11-16T12:00:00Z',
      latest_seq: 2,
    });
    mockUnshare.mockResolvedValue({ ...ARTIFACT, shared: false, latest_seq: 2 });

    renderWithProviders(
      <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
    );
    const unshareBtn = await screen.findByRole('button', { name: 'Unshare' });
    await userEvent.click(unshareBtn);
    await waitFor(() => expect(mockUnshare).toHaveBeenCalledWith(ARTIFACT.artifact_id));
  });

  // DALTON-HOOK(colleague-access): verify auto-publish pause hint, confirm_pause_token submission, and server pause notice
  it('shows auto-publish paused hint, submits confirm_pause_token, and surfaces auto_publish_pause_notice', async () => {
    tierState.callerTier = 'internal';
    workspaceState.data = {
      status: 'running',
      name: 'ws-1',
      auto_publish_paused: true,
      auto_publish_pause_token: 'tok-pause-1',
    };

    const notice = '仍有进行中的同事协作轮次，结束后再确认';
    const publishedMonitor = {
      monitor_id: 'm-1',
      slug: 'report',
      artifact_id: ARTIFACT.artifact_id,
      artifact_code: ARTIFACT.code,
      workspace_id: 'ws-1',
      entry_path: 'results/report.html',
      owner_user_id: 'u-1',
      owner_name: 'Alice',
      owner_email: 'alice@company.com',
      title: 'report',
      category: 'other' as const,
      sort_order: 0,
      visibility: 'own' as const,
      member_user_ids: [],
      published_seq: 1,
      keep_updated: true,
      source_kind: 'workspace' as const,
      source_ref: 'ws-1:results/report.html',
      acl_revision: 1,
      last_publish_error: null,
      last_publish_failed_at: null,
      data_as_of: null,
      created_at: '2026-11-16T12:00:00Z',
      updated_at: '2026-11-16T12:00:00Z',
      can_manage: true,
    };
    mockPublishMonitor
      .mockImplementationOnce(async () => {
        workspaceState.data = {
          ...workspaceState.data,
          auto_publish_paused: true,
          auto_publish_pause_token: 'tok-pause-2',
        };
        mockGetMonitor.mockResolvedValue(publishedMonitor);
        return {
          monitor: publishedMonitor,
          versions: [],
          monitor_grant: 'm1.tok',
          frame_base: '/api/v1/dalton/monitors/g/m1.tok/',
          data_base: '/api/v1/dalton/monitors/g/m1.tok/data',
          expires_in: 43200,
          cut: true,
          auto_publish_paused: true,
          auto_publish_pause_token: 'tok-pause-2',
          auto_publish_pause_notice: notice,
        };
      })
      .mockImplementationOnce(async () => {
        workspaceState.data = {
          ...workspaceState.data,
          auto_publish_paused: false,
          auto_publish_pause_token: null,
        };
        return {
          monitor: publishedMonitor,
          versions: [],
          monitor_grant: 'm1.tok',
          frame_base: '/api/v1/dalton/monitors/g/m1.tok/',
          data_base: '/api/v1/dalton/monitors/g/m1.tok/data',
          expires_in: 43200,
          cut: true,
        };
      });

    renderWithProviders(<PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />);

    expect(
      await screen.findByText(
        "Auto-update is paused because this workspace read a colleague's content. Review the files and visibility, then click Publish to resume auto-update.",
      ),
    ).toBeInTheDocument();

    const publishBtn = screen.getByRole('button', { name: 'Publish' });
    await waitFor(() => expect(publishBtn).toBeEnabled());
    await userEvent.click(publishBtn);

    await waitFor(() => expect(mockPublishMonitor).toHaveBeenCalledTimes(1));
    expect(mockPublishMonitor.mock.calls[0][0]).toMatchObject({
      workspace_id: 'ws-1',
      confirm_pause_token: 'tok-pause-1',
    });
    expect(await screen.findByText(notice)).toBeInTheDocument();
    expect(
      await screen.findByText(
        "Auto-update is paused because this workspace read a colleague's content. Review the files and visibility, then click Update to resume auto-update.",
      ),
    ).toBeInTheDocument();

    // Next publish carries the refreshed token returned by the server and clears the pause hint when resumed
    const updateBtn = await screen.findByRole('button', { name: 'Update' });
    await waitFor(() => expect(updateBtn).toBeEnabled());
    await userEvent.click(updateBtn);

    await waitFor(() => expect(mockPublishMonitor).toHaveBeenCalledTimes(2));
    expect(mockPublishMonitor.mock.calls[1][0]).toMatchObject({
      confirm_pause_token: 'tok-pause-2',
    });
    await waitFor(() => {
      expect(screen.queryByText(notice)).not.toBeInTheDocument();
      expect(
        screen.queryByText(
          "Auto-update is paused because this workspace read a colleague's content. Review the files and visibility, then click Update to resume auto-update.",
        ),
      ).not.toBeInTheDocument();
    });
  });

  // DALTON-HOOK(colleague-access): when colleague sharing switch is OFF, skip workspace refetch & invalidation
  it('skips workspace detail fetch, refetch, and workspace query invalidations when colleague sharing is off', async () => {
    tierState.callerTier = 'internal';
    tierState.colleagueSharingEnabled = false;
    workspaceState.data = {
      status: 'running',
      name: 'ws-1',
      auto_publish_paused: true,
      auto_publish_pause_token: 'tok-stale',
    };

    const { queryClient } = renderWithProviders(
      <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
    );
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    const publishBtn = await screen.findByRole('button', { name: 'Publish' });
    await waitFor(() => expect(publishBtn).toBeEnabled());

    expect(workspaceState.requestedIds.every((id) => id === null)).toBe(true);
    expect(mockGetWorkspace).not.toHaveBeenCalled();
    expect(
      screen.queryByText(/Auto-update is paused because this workspace read a colleague's content/),
    ).not.toBeInTheDocument();

    await userEvent.click(publishBtn);
    await waitFor(() => expect(mockPublishMonitor).toHaveBeenCalledTimes(1));
    expect(mockPublishMonitor.mock.calls[0][0]).not.toHaveProperty('confirm_pause_token');

    const invalidatedRoots = invalidateSpy.mock.calls.map((c) => {
      const arg = c[0] as { queryKey?: unknown[] } | undefined;
      return arg?.queryKey?.[0];
    });
    expect(invalidatedRoots).not.toContain('workspaces');
  });

  // DALTON-HOOK(colleague-access): refreshed workspace detail updates pause state while notice persists until next submit
  it('prefers freshly refetched workspace detail for pause state while keeping the publish notice until the next submit', async () => {
    tierState.callerTier = 'internal';
    tierState.colleagueSharingEnabled = true;
    workspaceState.data = {
      status: 'running',
      name: 'ws-1',
      auto_publish_paused: true,
      auto_publish_pause_token: 'tok-1',
    };

    const publishedMonitor = {
      monitor_id: 'm-1',
      slug: 'report',
      artifact_id: ARTIFACT.artifact_id,
      artifact_code: ARTIFACT.code,
      workspace_id: 'ws-1',
      entry_path: 'results/report.html',
      owner_user_id: 'u-1',
      owner_name: 'Alice',
      owner_email: 'alice@company.com',
      title: 'report',
      category: 'other' as const,
      sort_order: 0,
      visibility: 'own' as const,
      member_user_ids: [],
      published_seq: 1,
      keep_updated: true,
      source_kind: 'workspace' as const,
      source_ref: 'ws-1:results/report.html',
      acl_revision: 1,
      last_publish_error: null,
      last_publish_failed_at: null,
      data_as_of: null,
      created_at: '2026-11-16T12:00:00Z',
      updated_at: '2026-11-16T12:00:00Z',
      can_manage: true,
    };
    mockPublishMonitor.mockImplementationOnce(async () => {
      workspaceState.data = {
        ...workspaceState.data,
        auto_publish_paused: true,
        auto_publish_pause_token: 'tok-2',
      };
      mockGetMonitor.mockResolvedValue(publishedMonitor);
      return {
        monitor: publishedMonitor,
        versions: [],
        monitor_grant: 'm1.tok',
        frame_base: '/api/v1/dalton/monitors/g/m1.tok/',
        data_base: '/api/v1/dalton/monitors/g/m1.tok/data',
        expires_in: 43200,
        cut: true,
        auto_publish_paused: true,
        auto_publish_pause_token: 'tok-2',
        auto_publish_pause_notice: '仍有进行中的同事协作轮次，结束后再确认',
      };
    });

    const { queryClient } = renderWithProviders(
      <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
    );

    expect(
      await screen.findByText(/Auto-update is paused because this workspace read a colleague's content/),
    ).toBeInTheDocument();
    const publishBtn = await screen.findByRole('button', { name: 'Publish' });
    await waitFor(() => expect(publishBtn).toBeEnabled());
    await userEvent.click(publishBtn);
    expect(await screen.findByText('仍有进行中的同事协作轮次，结束后再确认')).toBeInTheDocument();

    // Another tab clears the pause and this dialog receives a fresher workspace detail
    workspaceState.data = {
      status: 'running',
      name: 'ws-1',
      auto_publish_paused: false,
      auto_publish_pause_token: null,
    };
    act(() => {
      queryClient.setQueryData<Workspace>(queryKeys.workspaces.detail('ws-1'), {
        workspace_id: 'ws-1',
        is_home: false,
        ...workspaceState.data,
      });
    });

    await waitFor(() => {
      expect(
        screen.queryByText(/Auto-update is paused because this workspace read a colleague's content/),
      ).not.toBeInTheDocument();
    });
    expect(screen.getByText('仍有进行中的同事协作轮次，结束后再确认')).toBeInTheDocument();

    const updateBtn = await screen.findByRole('button', { name: 'Update' });
    await waitFor(() => expect(updateBtn).toBeEnabled());
    await userEvent.click(updateBtn);
    await waitFor(() => expect(mockPublishMonitor).toHaveBeenCalledTimes(2));
    expect(mockPublishMonitor.mock.calls[1][0]).not.toHaveProperty('confirm_pause_token');
    await waitFor(() => {
      expect(screen.queryByText('仍有进行中的同事协作轮次，结束后再确认')).not.toBeInTheDocument();
    });
  });

  it('renders 2x2 visibility grid and inline file reason on mobile while keeping desktop SegmentedControl', async () => {
    const originalMatchMedia = window.matchMedia;
    const prevTier = tierState.callerTier;
    tierState.callerTier = 'admin';
    mockStatus.mockResolvedValue({
      ...STATUS,
      left_out: [{ path: 'results/notes.md', size: 128, reason: 'markdown', optional: true }],
    });
    const setMedia = (mobile: boolean) => {
      tierState.isMobile = mobile;
      Object.defineProperty(window, 'matchMedia', {
        writable: true,
        configurable: true,
        value: vi.fn().mockImplementation((query: string) => ({
          matches:
            query === '(max-width: 767px)' || query === '(pointer: coarse)' ? mobile : false,
          media: query,
          onchange: null,
          addListener: vi.fn(),
          removeListener: vi.fn(),
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
          dispatchEvent: vi.fn(),
        })),
      });
    };

    try {
      setMedia(true);
      const { unmount } = renderWithProviders(
        <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
      );
      const grid = await screen.findByTestId('publish-visibility-grid');
      expect(grid.className).toContain('grid-cols-2');
      expect(grid.querySelectorAll('button')).toHaveLength(4);
      const inlineReasons = await screen.findAllByTestId('publish-file-inline-reason');
      expect(inlineReasons.length).toBeGreaterThan(0);
      unmount();

      setMedia(false);
      renderWithProviders(
        <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
      );
      await screen.findByRole('button', { name: 'Publish' });
      expect(screen.queryByTestId('publish-visibility-grid')).toBeNull();
      expect(screen.queryByTestId('publish-file-inline-reason')).toBeNull();
    } finally {
      tierState.callerTier = prevTier;
      tierState.isMobile = false;
      Object.defineProperty(window, 'matchMedia', {
        writable: true,
        configurable: true,
        value: originalMatchMedia,
      });
    }
  });

  it('clears public selection on live admin -> internal downgrade without rebounding to existing public monitor', async () => {
    tierState.callerTier = 'admin';
    tierState.status = 'ready';
    tierState.isConfirming = false;
    vi.mocked(getMonitorByArtifact).mockResolvedValue({
      monitor_id: 'mon-pub',
      slug: 'pub-slug',
      artifact_id: ARTIFACT.artifact_id,
      workspace_id: 'ws-1',
      title: 'Public Monitor',
      category: 'other',
      visibility: 'public',
      published_seq: 1,
      keep_updated: false,
      member_user_ids: [],
    } as never);

    const { rerender } = renderWithProviders(
      <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
    );

    const updateBtn = await screen.findByRole('button', { name: 'Update' });
    await waitFor(() => expect(updateBtn).toBeEnabled());

    // Live downgrade admin -> internal while dialog is open.
    tierState.callerTier = 'internal';
    rerender(<PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Update' })).toBeDisabled();
    });
    expect(screen.queryByRole('button', { name: /Public|公开/i })).toBeNull();
    // Selecting company explicitly re-enables Update.
    await userEvent.click(screen.getByRole('button', { name: /Company|全公司/i }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Update' })).toBeEnabled());
  });

  it('blocks external owner republish on non-own monitor and offers standalone downgrade-to-own action', async () => {
    tierState.callerTier = 'external';
    tierState.status = 'ready';
    tierState.isConfirming = false;
    vi.mocked(getMonitorByArtifact).mockResolvedValue({
      monitor_id: 'mon-co',
      slug: 'co-slug',
      artifact_id: ARTIFACT.artifact_id,
      workspace_id: 'ws-1',
      title: 'Company Monitor',
      category: 'other',
      visibility: 'company',
      published_seq: 1,
      keep_updated: false,
      member_user_ids: [],
    } as never);
    vi.mocked(updateMonitor).mockResolvedValue({
      monitor: {
        monitor_id: 'mon-co',
        slug: 'co-slug',
        artifact_id: ARTIFACT.artifact_id,
        workspace_id: 'ws-1',
        title: 'Company Monitor',
        category: 'other',
        visibility: 'own',
        published_seq: 1,
        keep_updated: false,
        member_user_ids: [],
      },
    } as never);

    renderWithProviders(
      <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
    );

    const downgradeBtn = await screen.findByRole('button', { name: /Change to Only me|改为仅自己可见/i });
    expect(screen.getByRole('button', { name: 'Update' })).toBeDisabled();
    // External caller never sees copy link button or footer monitor link.
    expect(screen.queryByRole('button', { name: /^Copy$/i })).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();

    await userEvent.click(downgradeBtn);
    await waitFor(() =>
      expect(updateMonitor).toHaveBeenCalledWith('co-slug', { visibility: 'own', member_user_ids: [] }),
    );
    expect(publishMonitor).not.toHaveBeenCalled();
  });

  it('unshareOnly uniquely calls POST /artifacts/{id}/unshare, invalidates 3 cache groups, hides link/copy, and stays clickable while confirming', async () => {
    tierState.callerTier = 'unknown';
    tierState.status = 'loading';
    tierState.isConfirming = true;
    vi.mocked(getMonitorByArtifact).mockResolvedValue({
      monitor_id: 'mon-pub',
      slug: 'pub-slug',
      artifact_id: ARTIFACT.artifact_id,
      workspace_id: 'ws-1',
      title: 'Admin Taken Over Monitor',
      category: 'other',
      visibility: 'public',
      published_seq: 1,
      keep_updated: false,
      member_user_ids: [],
    } as never);
    vi.mocked(unshareArtifact).mockResolvedValue({
      ...ARTIFACT,
      shared: false,
      latest_seq: 1,
    });

    const { queryClient } = renderWithProviders(
      <PublishDialog open unshareOnly workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
    );
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    const unshareBtn = await screen.findByRole('button', { name: /Unshare|停止分享/i });
    expect(unshareBtn).toBeEnabled();
    expect(screen.queryByRole('button', { name: /^Copy$/i })).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();

    await userEvent.click(unshareBtn);
    await waitFor(() => expect(unshareArtifact).toHaveBeenCalledWith(ARTIFACT.artifact_id));
    expect(unshareArtifact).toHaveBeenCalledTimes(1);
    const keys = invalidateSpy.mock.calls.map((c) => c[0]?.queryKey);
    expect(keys).toContainEqual(queryKeys.daltonMonitors.all);
    expect(keys).toContainEqual(queryKeys.artifacts.pathsIn('ws-1'));
    expect(keys).toContainEqual(queryKeys.shareLinks.byWorkspace('ws-1'));
  });

  it('displays formatted 403 detail and triggers revalidate() on publish 403', async () => {
    tierState.callerTier = 'internal';
    tierState.status = 'ready';
    tierState.isConfirming = false;
    tierState.revalidate.mockClear();
    vi.mocked(toast).mockClear();
    vi.mocked(publishMonitor).mockRejectedValueOnce(
      new ApiError('Request failed with status code 403', 'ERR_BAD_REQUEST', {
        status: 403,
        statusText: 'Forbidden',
        headers: {},
        data: {
          detail: { code: 'external_member_forbidden', message: '外部成员无法发布公司监控' },
        },
      }),
    );

    renderWithProviders(
      <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
    );
    const publishBtn = await screen.findByRole('button', { name: 'Publish' });
    await waitFor(() => expect(publishBtn).toBeEnabled());
    await userEvent.click(publishBtn);

    await waitFor(() => expect(tierState.revalidate).toHaveBeenCalledTimes(1));
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        description: '外部成员无法发布公司监控',
        variant: 'destructive',
      }),
    );
  });

  it('fails closed when existing monitor query is pending or errors so external members cannot publish over an unknown monitor', async () => {
    tierState.callerTier = 'external';
    tierState.status = 'ready';
    tierState.isConfirming = false;

    let rejectMonitor!: (err: Error) => void;
    mockGetMonitor
      .mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            rejectMonitor = reject;
          }),
      )
      .mockResolvedValueOnce(null);

    renderWithProviders(
      <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
    );

    // While existingMonitorQuery is pending, Publish stays disabled.
    const publishBtn = await screen.findByRole('button', { name: 'Publish' });
    await waitFor(() => expect(mockGetMonitor).toHaveBeenCalledTimes(1));
    expect(publishBtn).toBeDisabled();

    // When existingMonitorQuery errors, Publish stays disabled and a retryable error banner shows.
    act(() => {
      rejectMonitor(new Error('monitor lookup failed'));
    });
    expect(await screen.findByText('Failed to load monitors')).toBeInTheDocument();
    expect(publishBtn).toBeDisabled();

    // Retrying and resolving null re-enables Publish.
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(publishBtn).toBeEnabled());
  });

  it('hides keep_updated for external members, forces keep_updated=false on publish, and blocks stale toggleKeep closures', async () => {
    tierState.callerTier = 'external';
    tierState.status = 'ready';
    tierState.isConfirming = false;
    mockEnsure.mockResolvedValue({ ...ARTIFACT, keep_updated: true });
    mockGetMonitor.mockResolvedValue({
      monitor_id: 'mon-own',
      slug: 'own-slug',
      artifact_id: ARTIFACT.artifact_id,
      workspace_id: 'ws-1',
      title: 'Own Monitor',
      category: 'other',
      visibility: 'own',
      published_seq: 1,
      keep_updated: true,
      member_user_ids: [],
    } as never);

    const { unmount } = renderWithProviders(
      <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
    );

    const updateBtn = await screen.findByRole('button', { name: 'Update' });
    await waitFor(() => expect(updateBtn).toBeEnabled());
    expect(screen.queryByRole('switch', { name: 'Auto-update when files change' })).toBeNull();

    await userEvent.click(updateBtn);
    await waitFor(() => expect(mockPublishMonitor).toHaveBeenCalledTimes(1));
    expect(mockPublishMonitor.mock.calls[0][0]).toMatchObject({
      visibility: 'own',
      keep_updated: false,
    });
    unmount();

    // Stale internal closure downgraded to external synchronously before click cannot toggle keep_updated.
    tierState.callerTier = 'internal';
    vi.mocked(updateMonitor).mockClear();
    renderWithProviders(
      <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
    );
    const keepSwitch = await screen.findByRole('switch', { name: 'Auto-update when files change' });
    await waitFor(() => expect(keepSwitch).toBeEnabled());

    tierState.callerTier = 'external';
    fireEvent.click(keepSwitch);
    expect(updateMonitor).not.toHaveBeenCalled();
  });

  it('hides selectable link box during isConfirming, keeps normal-mode Unshare clickable, and prevents footer monitor link click on stale identity', async () => {
    tierState.callerTier = 'internal';
    tierState.status = 'ready';
    tierState.isConfirming = true;
    mockGetMonitor.mockResolvedValue({
      monitor_id: 'mon-pub',
      slug: 'pub-slug',
      artifact_id: ARTIFACT.artifact_id,
      workspace_id: 'ws-1',
      title: 'Public Monitor',
      category: 'other',
      visibility: 'public',
      published_seq: 1,
      keep_updated: false,
      member_user_ids: [],
    } as never);
    mockUnshare.mockResolvedValue({ ...ARTIFACT, shared: false, latest_seq: 1 });

    const { rerender } = renderWithProviders(
      <PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />,
    );

    const unshareBtn = await screen.findByRole('button', { name: 'Unshare' });
    expect(unshareBtn).toBeEnabled();
    expect(screen.queryByRole('button', { name: /^Copy$/i })).toBeNull();
    expect(screen.queryByText(/\/m\/pub-slug/)).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();

    await userEvent.click(unshareBtn);
    await waitFor(() => expect(mockUnshare).toHaveBeenCalledWith(ARTIFACT.artifact_id));

    // When settled, footer link renders; if identity flips to confirming before click, click is prevented.
    tierState.isConfirming = false;
    rerender(<PublishDialog open workspaceId="ws-1" path="results/report.html" onClose={() => {}} />);
    const footerLink = await screen.findByRole('link', { name: /Open in new window/i });
    tierState.isConfirming = true;
    const clickEvent = new MouseEvent('click', { bubbles: true, cancelable: true });
    footerLink.dispatchEvent(clickEvent);
    expect(clickEvent.defaultPrevented).toBe(true);
  });

  it('FilePanel treats isConfirming with status=ready as unknown tier, hiding owner publish while keeping stop-share for public monitors', async () => {
    tierState.callerTier = 'admin';
    tierState.status = 'ready';
    tierState.isConfirming = true;
    mockFind.mockResolvedValue({ ...ARTIFACT, shared: false });
    mockGetMonitor.mockResolvedValue({
      monitor_id: 'mon-pub',
      slug: 'pub-slug',
      artifact_id: ARTIFACT.artifact_id,
      workspace_id: 'ws-1',
      title: 'Public Monitor',
      category: 'other',
      visibility: 'public',
      published_seq: 1,
      keep_updated: false,
      member_user_ids: [],
    } as never);

    renderWithProviders(
      <FilePanel
        workspaceId="ws-1"
        onClose={() => {}}
        files={['results/report.html']}
        target={fileTarget('results/report.html', {}, 1)}
        onTargetHandled={() => {}}
      />,
    );

    const stopBtn = await screen.findByRole('button', { name: /Stop sharing|Unshare/i });
    expect(stopBtn).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Publish' })).toBeNull();
  });

  it('usePanelFiles -> FilePanel production callerTier path drops owner comment/pin/publish on the same tick isConfirming flips true, while keeping stop-share for public monitors and shared files', async () => {
    tierState.callerTier = 'admin';
    tierState.status = 'ready';
    tierState.isConfirming = false;
    mockFind.mockResolvedValue({ ...ARTIFACT, shared: false });
    mockGetMonitor.mockResolvedValue({
      monitor_id: 'mon-pub',
      slug: 'pub-slug',
      artifact_id: ARTIFACT.artifact_id,
      workspace_id: 'ws-1',
      title: 'Public Monitor',
      category: 'other',
      visibility: 'public',
      published_seq: 1,
      keep_updated: false,
      member_user_ids: [],
    } as never);

    function PanelHarness({ filePath }: { filePath: string }) {
      const { panelAccess } = usePanelFiles({
        isHome: false,
        workspaceId: 'ws-1',
        includeSystem: false,
      });
      return (
        <FilePanel
          workspaceId="ws-1"
          onClose={() => {}}
          files={[filePath]}
          target={fileTarget(filePath, {}, 1)}
          onTargetHandled={() => {}}
          {...panelAccess}
        />
      );
    }

    const { rerender, unmount } = renderWithProviders(
      <PanelHarness filePath="results/report.html" />,
    );

    // Ready admin sees owner controls: Publish, Comment, and Pin to dashboard.
    expect(await screen.findByRole('button', { name: 'Publish' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Comment' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Set as dashboard' })).toBeInTheDocument();

    // Same-tick revalidation: status stays 'ready' with cached 'admin', isConfirming flips true.
    tierState.isConfirming = true;
    rerender(<PanelHarness filePath="results/report.html" />);

    expect(screen.queryByRole('button', { name: 'Publish' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Comment' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Set as dashboard' })).toBeNull();
    expect(await screen.findByRole('button', { name: /Stop sharing|Unshare/i })).toBeInTheDocument();
    unmount();

    // Also verify existing public shared file (without public monitor) retains stop-share during isConfirming.
    tierState.isConfirming = true;
    mockFind.mockResolvedValue({ ...ARTIFACT, shared: true });
    mockGetMonitor.mockResolvedValue(null);

    renderWithProviders(<PanelHarness filePath="results/report.html" />);
    expect(await screen.findByRole('button', { name: /Stop sharing|Unshare/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Publish' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Comment' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Set as dashboard' })).toBeNull();
  });
});
