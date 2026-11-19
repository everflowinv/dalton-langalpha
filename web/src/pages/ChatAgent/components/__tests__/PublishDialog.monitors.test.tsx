import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import type { Artifact, ArtifactStatus } from '@/types/api';

const tierState = vi.hoisted(() => ({
  callerTier: 'internal' as 'external' | 'internal' | 'admin',
}));

vi.mock('@/config/daltonAuth', () => ({
  isDaltonAuth: true,
}));

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
    readWorkspaceFile: answer,
    readWorkspaceFileFull: answer as unknown as typeof orig.readWorkspaceFileFull,
    writeWorkspaceFile: vi.fn<typeof orig.writeWorkspaceFile>(),
    downloadWorkspaceFileAsArrayBuffer: vi.fn<typeof orig.downloadWorkspaceFileAsArrayBuffer>(),
    triggerFileDownload: vi.fn<typeof orig.triggerFileDownload>(),
    resolveWorkspaceFile: vi.fn<typeof orig.resolveWorkspaceFile>(),
    getPreviewUrl: vi.fn<typeof orig.getPreviewUrl>(),
  };
});
vi.mock('@/hooks/useWorkspace', () => ({
  useWorkspace: () => ({ data: { status: 'running', name: 'ws-1' } }),
}));
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

import {
  ensureArtifact,
  findArtifact,
  getArtifactStatus,
  unshareArtifact,
} from '@/pages/ChatAgent/utils/api/artifacts';
import { getMonitorByArtifact, publishMonitor } from '@/pages/Monitors/api';
import FilePanel from '../FilePanel';
import { fileTarget } from '../filePanel/types';
import PublishDialog from '../PublishDialog';

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

  it('non-admin opening an existing public monitor sees downgrade hint, and non-monitor shared file keeps Unshare button', async () => {
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
      await screen.findByText('You do not have public link permission; publishing will set visibility to Company.'),
    ).toBeInTheDocument();
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
});
