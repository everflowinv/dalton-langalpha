import { fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';
import { createTestQueryClient, renderWithProviders } from '@/test/utils';
import type { CallerIdentity, CallerIdentitySnapshot } from '@/hooks/useCallerIdentity';
import type { Artifact, ShareLink, ThreadShareStatus } from '@/types/api';
import type { MonitorSummary } from '@/pages/Monitors/api';

const h = vi.hoisted(() => ({
  identity: {
    tier: 'admin',
    status: 'ready',
    authMode: 'local',
    isPlatform: false,
    isKnown: true,
    isConfirming: false,
    isMultiUserAuth: true,
    isSingleUserOss: false,
    isAdmin: true,
    isInternal: true,
    isExternal: false,
    canActAsOwner: true,
    canPublishPublic: true,
    canInvite: true,
    isTeamCredentialsShared: true,
    canManageCredentials: true,
    isPm: true,
    colleagueSharingEnabled: true,
  } as CallerIdentitySnapshot,
  revalidate: vi.fn(async () => undefined),
  shareState: {
    is_shared: true,
    share_token: 'thread-code',
    share_url: '/s/thread-code',
    permissions: { allow_files: true, allow_download: false, audience: 'public' as const },
    audiences: ['public', 'organization'] as Array<'public' | 'organization'>,
  } as ThreadShareStatus,
  sharedLinks: [] as ShareLink[],
  saveThreadSharing: vi.fn(),
  unshareArtifact: vi.fn(),
  patchShareLink: vi.fn(),
  copyText: vi.fn<(text: string) => Promise<void>>(async () => undefined),
  toast: vi.fn(),
  findArtifact: vi.fn<() => Promise<Artifact | null>>(),
  getMonitorByArtifact: vi.fn<() => Promise<MonitorSummary | null>>(),
}));

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: (): CallerIdentity => ({
    ...h.identity,
    getLatestIdentity: () => ({ ...h.identity }),
    isConfirmingNow: () => h.identity.isConfirming,
    revalidate: h.revalidate,
  }),
}));

vi.mock('@/components/ui/use-toast', () => ({
  toast: (...args: unknown[]) => h.toast(...args),
}));

vi.mock('@/lib/clipboard', () => ({
  copyText: (text: string) => h.copyText(text),
}));

vi.mock('@/hooks/useShareLink', () => ({
  useSharedLinks: (wsId: string | null | undefined) => ({
    data: wsId ? h.sharedLinks : undefined,
  }),
  useShareLinkMutations: () => ({
    patch: {
      isPending: false,
      mutate: (vars: unknown, opts?: { onError?: (err: unknown) => void }) => {
        h.patchShareLink(vars, opts);
      },
    },
  }),
}));

vi.mock('@/pages/ChatAgent/utils/api/artifacts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/ChatAgent/utils/api/artifacts')>();
  return {
    ...actual,
    findArtifact: () => h.findArtifact(),
    unshareArtifact: (artifactId: string) => h.unshareArtifact(artifactId),
  };
});

vi.mock('@/pages/Monitors/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/Monitors/api')>();
  return {
    ...actual,
    getMonitorByArtifact: () => h.getMonitorByArtifact(),
  };
});

vi.mock('../../hooks/useThreadShareStatus', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../hooks/useThreadShareStatus')>();
  return {
    ...actual,
    useThreadShareStatus: () => ({
      data: h.shareState,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    }),
    saveThreadSharing: (qc: unknown, threadId: string, body: unknown) =>
      h.saveThreadSharing(qc, threadId, body),
  };
});

vi.mock('@/components/organization/LibraryPublishRow', () => ({
  LibraryPublishRow: () => <div data-testid="library-publish-row" />,
}));

vi.mock('../viewers/html/useServedHtml', () => ({
  useServedHtml: () => ({ plainUrl: null, openUrl: null }),
}));

import { ThreadSharePanel } from '../ThreadSharePanel';
import ShareButton from '../ShareButton';
import FileHeaderActions from '../FileHeaderActions';

function setTier(
  tier: 'admin' | 'internal' | 'external' | 'unknown',
  overrides: Partial<CallerIdentitySnapshot> = {},
) {
  h.identity = {
    tier,
    status: tier === 'unknown' ? 'loading' : 'ready',
    authMode: tier === 'unknown' ? null : 'local',
    isPlatform: false,
    isKnown: tier !== 'unknown',
    isConfirming: tier === 'unknown',
    isMultiUserAuth: tier !== 'unknown',
    isSingleUserOss: false,
    isAdmin: tier === 'admin',
    isInternal: tier === 'admin' || tier === 'internal',
    isExternal: tier === 'external',
    canActAsOwner: tier === 'admin' || tier === 'internal',
    canPublishPublic: tier === 'admin',
    canInvite: tier === 'admin' || tier === 'internal',
    isTeamCredentialsShared: true,
    canManageCredentials: tier === 'admin',
    isPm: tier === 'admin',
    colleagueSharingEnabled: true,
    ...overrides,
  };
}

describe('ThreadSharePanel, ShareButton, and FileHeaderActions permissions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setTier('admin');
    h.shareState = {
      is_shared: true,
      share_token: 'thread-code',
      share_url: '/s/thread-code',
      permissions: { allow_files: true, allow_download: false, audience: 'public' },
      audiences: ['public', 'organization'],
    };
    h.sharedLinks = [];
    h.saveThreadSharing.mockResolvedValue(undefined);
    h.unshareArtifact.mockResolvedValue({
      artifact_id: 'art-1',
      code: 'c1',
      url: '/a/c1',
      workspace_id: 'ws-1',
      path: 'results/report.html',
      name: 'report.html',
      shared: false,
      latest_seq: 1,
      published_seq: 1,
      keep_updated: false,
      created_at: '2026-01-01T00:00:00Z',
    });
    h.findArtifact.mockResolvedValue(null);
    h.getMonitorByArtifact.mockResolvedValue(null);
  });

  it('sends only { is_shared: false } without permissions when stopping a share, even for external or confirming callers', async () => {
    setTier('external');
    renderWithProviders(<ThreadSharePanel threadId="th-1" workspaceId="ws-1" />);

    const toggle = screen.getByRole('switch');
    expect(toggle).not.toBeDisabled();
    fireEvent.click(toggle);

    await waitFor(() => expect(h.saveThreadSharing).toHaveBeenCalledTimes(1));
    expect(h.saveThreadSharing.mock.calls[0][2]).toEqual({ is_shared: false });
    expect(h.saveThreadSharing.mock.calls[0][2]).not.toHaveProperty('permissions');
  });

  it('hides share URL, copy button, audience picker, file checkboxes, library row, and SharedInWorkspace copy for external members', () => {
    setTier('external');
    h.sharedLinks = [
      {
        code: 'mon-code',
        kind: 'file',
        artifact_id: 'art-1',
        workspace_id: 'ws-1',
        path: 'results/report.html',
        title: 'Historical Public Monitor',
        shared_at: null,
        created_at: '2026-01-01T00:00:00Z',
      } as ShareLink,
    ];
    renderWithProviders(<ThreadSharePanel threadId="th-1" workspaceId="ws-1" />);

    expect(screen.queryByText(/\/s\/thread-code/)).toBeNull();
    expect(screen.queryByTitle(/Copy link|复制链接/)).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.queryByTestId('library-publish-row')).toBeNull();
    expect(screen.getByText('Historical Public Monitor')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Stop|停止/ })).toBeInTheDocument();
  });

  it('keeps stop clickable during unknown/confirming state while disabling enable and file checkboxes', async () => {
    setTier('unknown', { status: 'loading', isConfirming: true });
    renderWithProviders(<ThreadSharePanel threadId="th-1" workspaceId="ws-1" />);

    const toggle = screen.getByRole('switch');
    expect(toggle).not.toBeDisabled();
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(h.saveThreadSharing).toHaveBeenCalledWith(expect.anything(), 'th-1', { is_shared: false }),
    );
  });

  it('uniquely calls POST /artifacts/{id}/unshare for SharedInWorkspace file links (including shared_at=null monitors) and invalidates artifacts, shareLinks, and daltonMonitors caches', async () => {
    setTier('external');
    h.sharedLinks = [
      {
        code: 'mon-code',
        kind: 'file',
        artifact_id: 'art-1',
        workspace_id: 'ws-1',
        path: 'results/report.html',
        title: 'Legacy Monitor',
        shared_at: null,
        created_at: '2026-01-01T00:00:00Z',
      } as ShareLink,
    ];
    const client = createTestQueryClient();
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');
    renderWithProviders(<ThreadSharePanel threadId="th-1" workspaceId="ws-1" />, {
      queryClient: client,
    });

    fireEvent.click(screen.getByRole('button', { name: /Stop|停止/ }));

    await waitFor(() => expect(h.unshareArtifact).toHaveBeenCalledWith('art-1'));
    expect(h.patchShareLink).not.toHaveBeenCalled();

    const invalidatedKeys = invalidateSpy.mock.calls.map((c) => c[0]?.queryKey);
    expect(invalidatedKeys).toContainEqual(queryKeys.daltonMonitors.all);
    expect(invalidatedKeys).toContainEqual(queryKeys.artifacts.pathsIn('ws-1'));
    expect(invalidatedKeys).toContainEqual(queryKeys.shareLinks.byWorkspace('ws-1'));
  });

  it('surfaces formatApiErrorDetail and triggers identity.revalidate() on 403 save failure', async () => {
    setTier('internal');
    h.shareState = {
      is_shared: false,
      share_token: '',
      share_url: '',
      permissions: { allow_files: false, allow_download: false },
    };
    h.saveThreadSharing.mockRejectedValueOnce(
      new ApiError('Request failed with status code 403', 'ERR_BAD_REQUEST', {
        status: 403,
        statusText: 'Forbidden',
        headers: {},
        data: {
          detail: { code: 'external_member_forbidden', message: '外部成员无法开启公开分享' },
        },
      }),
    );

    renderWithProviders(<ThreadSharePanel threadId="th-1" workspaceId="ws-1" />);
    fireEvent.click(screen.getByRole('switch'));

    await waitFor(() => expect(h.revalidate).toHaveBeenCalledTimes(1));
    expect(h.toast).toHaveBeenCalledWith(
      expect.objectContaining({
        description: '外部成员无法开启公开分享',
        variant: 'destructive',
      }),
    );
  });

  it('hides ShareButton for external member without active share and disables it for unknown identity without active share', () => {
    h.shareState = { is_shared: false, share_token: '', share_url: '', permissions: {} };
    h.sharedLinks = [];

    setTier('external');
    const { unmount } = renderWithProviders(
      <ShareButton threadId="th-1" initialIsShared={false} workspaceId="ws-1" />,
    );
    expect(screen.queryByRole('button')).toBeNull();
    unmount();

    setTier('unknown', { status: 'loading', isConfirming: true });
    renderWithProviders(
      <ShareButton threadId="th-1" initialIsShared={false} workspaceId="ws-1" />,
    );
    expect(screen.getByRole('button')).toBeDisabled();
  });

  it('shows stop-share entry in FileHeaderActions when a historical public monitor exists despite artifact.shared=false', async () => {
    const openUnshareOnly = vi.fn();
    h.findArtifact.mockResolvedValue({
      artifact_id: 'art-legacy',
      code: 'legacy-code',
      url: '/a/legacy-code',
      workspace_id: 'ws-1',
      path: 'results/report.html',
      name: 'report.html',
      shared: false,
      shared_at: null,
      latest_seq: 2,
      keep_updated: false,
      bound_thread_id: null,
      open_comments: 0,
      unread: 0,
      created_at: '2026-01-01T00:00:00Z',
    });
    h.getMonitorByArtifact.mockResolvedValue({
      monitor_id: 'mon-1',
      slug: 'legacy-mon',
      artifact_id: 'art-legacy',
      workspace_id: 'ws-1',
      title: 'Legacy Public Monitor',
      category: 'other',
      visibility: 'public',
      published_seq: 2,
      keep_updated: false,
      member_user_ids: [],
    } as unknown as MonitorSummary);

    renderWithProviders(
      <FileHeaderActions
        selectedFile="results/report.html"
        isEditing={false}
        workspaceId="ws-1"
        fileContent="<html></html>"
        fileMime="text/html"
        canEdit={false}
        onStartEdit={vi.fn()}
        onOpenExportModal={vi.fn()}
        triggerDownloadFn={vi.fn()}
        canDownload={false}
        readFileFullFn={vi.fn()}
        access={{ kind: 'readOnly' }}
        openUnshareOnly={openUnshareOnly}
        htmlSource={false}
        onToggleHtmlSource={null}
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
      />,
    );

    const stopBtn = await screen.findByRole('button', { name: /Stop sharing|停止分享|Unshare/i });
    fireEvent.click(stopBtn);
    expect(openUnshareOnly).toHaveBeenCalledWith('results/report.html');
  });
});
