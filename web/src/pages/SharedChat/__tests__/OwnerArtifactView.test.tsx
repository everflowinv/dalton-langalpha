import { fireEvent, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithProviders } from '@/test/utils';
import type { CallerIdentity, CallerIdentitySnapshot } from '@/hooks/useCallerIdentity';
import type { MonitorSummary } from '@/pages/Monitors/api';
import type { SharedOwnerFileMetadata } from '../api';

const h = vi.hoisted(() => ({
  identity: {
    tier: 'internal',
    status: 'ready',
    authMode: 'local',
    isPlatform: false,
    isKnown: true,
    isConfirming: false,
    isMultiUserAuth: true,
    isSingleUserOss: false,
    isAdmin: false,
    isInternal: true,
    isExternal: false,
    canActAsOwner: true,
    canPublishPublic: false,
    canInvite: true,
    isTeamCredentialsShared: true,
    canManageCredentials: false,
    isPm: false,
    colleagueSharingEnabled: true,
  } as CallerIdentitySnapshot,
  record: {
    artifact_id: 'art-1',
    code: 'c1',
    path: 'results/report.html',
    shared: false,
    latest_seq: 1,
  },
  monitor: null as MonitorSummary | null,
}));

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: (): CallerIdentity => ({
    ...h.identity,
    getLatestIdentity: () => ({ ...h.identity }),
    isConfirmingNow: () => h.identity.isConfirming,
    revalidate: vi.fn(async () => undefined),
  }),
}));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => false,
}));

vi.mock('@/lib/threadLifecycle/ThreadLifecycleFeed', () => ({
  ThreadLifecycleFeed: () => null,
}));

vi.mock('@/pages/Monitors/api', () => ({
  getMonitorByArtifact: vi.fn(async () => h.monitor),
}));

vi.mock('../../ChatAgent/components/PublishDialog', () => ({
  default: ({ unshareOnly }: { unshareOnly?: boolean }) => (
    <div data-testid="publish-dialog" data-unshare-only={String(Boolean(unshareOnly))} />
  ),
}));

vi.mock('../../ChatAgent/components/filePanel/useArtifactVersionView', () => ({
  useArtifactVersionView: () => ({
    record: h.record,
    version: null,
    versions: [],
    onView: vi.fn(),
  }),
  versionReducer: (s: unknown) => s,
}));

vi.mock('../../ChatAgent/components/viewers/HtmlViewer', () => ({
  default: ({ servePrefix }: { servePrefix?: string }) => (
    <div data-testid="readonly-html-viewer" data-serve-prefix={servePrefix ?? ''} />
  ),
}));

vi.mock('../../ChatAgent/components/viewers/html/ArtifactFrames', () => ({
  ArtifactFrames: () => <div data-testid="owner-artifact-frames" />,
}));

vi.mock('../../ChatAgent/components/viewers/html/ArtifactVersionPicker', () => ({
  ArtifactVersionPicker: () => <div data-testid="artifact-version-picker" />,
}));

vi.mock('../../ChatAgent/components/viewers/html/comments/CommentTools', () => ({
  CommentTools: () => <div data-testid="comment-tools" />,
}));

import OwnerArtifactView from '../OwnerArtifactView';

const METADATA: SharedOwnerFileMetadata = {
  kind: 'file',
  workspace_id: 'ws-1',
  path: 'results/report.html',
  name: 'report.html',
  frame_base: '/api/v1/wsfiles/g/grant-1/',
  shared: false,
} as SharedOwnerFileMetadata;

function setIdentity(overrides: Partial<CallerIdentitySnapshot>) {
  h.identity = {
    ...h.identity,
    ...overrides,
  };
}

describe('OwnerArtifactView permissions', () => {
  beforeEach(() => {
    h.record = {
      artifact_id: 'art-1',
      code: 'c1',
      path: 'results/report.html',
      shared: false,
      latest_seq: 1,
    };
    h.monitor = null;
    setIdentity({
      tier: 'internal',
      status: 'ready',
      isKnown: true,
      isConfirming: false,
      isAdmin: false,
      isInternal: true,
      isExternal: false,
      canActAsOwner: true,
      canPublishPublic: false,
    });
  });

  it('renders CommentTools and ArtifactFrames for internal/admin owners', () => {
    renderWithProviders(<OwnerArtifactView code="c1" metadata={METADATA} />);
    expect(screen.getByTestId('comment-tools')).toBeInTheDocument();
    expect(screen.getByTestId('owner-artifact-frames')).toBeInTheDocument();
    expect(screen.queryByTestId('readonly-html-viewer')).toBeNull();
  });

  it('hides CommentTools and renders read-only HtmlViewer frame for external members', () => {
    setIdentity({
      tier: 'external',
      status: 'ready',
      isKnown: true,
      isConfirming: false,
      isInternal: false,
      isExternal: true,
      canActAsOwner: false,
    });
    renderWithProviders(<OwnerArtifactView code="c1" metadata={METADATA} />);
    expect(screen.queryByTestId('comment-tools')).toBeNull();
    expect(screen.queryByTestId('owner-artifact-frames')).toBeNull();
    expect(screen.queryByTestId('artifact-version-picker')).toBeNull();
    expect(screen.getByTestId('readonly-html-viewer')).toHaveAttribute(
      'data-serve-prefix',
      '/api/v1/wsfiles/g/grant-1/',
    );
  });

  it('hides CommentTools and renders read-only HtmlViewer frame while identity is unknown/loading', () => {
    setIdentity({
      tier: 'unknown',
      status: 'loading',
      isKnown: false,
      isConfirming: true,
      isInternal: false,
      isExternal: false,
      canActAsOwner: false,
    });
    renderWithProviders(<OwnerArtifactView code="c1" metadata={METADATA} />);
    expect(screen.queryByTestId('comment-tools')).toBeNull();
    expect(screen.queryByTestId('owner-artifact-frames')).toBeNull();
    expect(screen.getByTestId('readonly-html-viewer')).toBeInTheDocument();
  });

  it('keeps standalone stop-sharing action enabled for shared files and historical public monitors during identity error and revalidation, opening unshareOnly PublishDialog', async () => {
    // 1. Shared file with identity query error: Publish is hidden, standalone stop-sharing stays enabled and opens unshareOnly dialog.
    h.record = { ...h.record, shared: true };
    setIdentity({
      tier: 'unknown',
      status: 'error',
      isKnown: false,
      isConfirming: false,
      isInternal: false,
      isExternal: false,
      canActAsOwner: false,
    });
    const view1 = renderWithProviders(
      <OwnerArtifactView code="c1" metadata={{ ...METADATA, shared: true }} />,
    );
    expect(screen.queryByRole('button', { name: /^Publish$/i })).toBeNull();
    const stopBtn1 = screen.getByRole('button', { name: /Stop sharing|Unshare/i });
    expect(stopBtn1).toBeEnabled();
    fireEvent.click(stopBtn1);
    expect(screen.getByTestId('publish-dialog')).toHaveAttribute('data-unshare-only', 'true');
    view1.unmount();

    // 2. Historical public monitor (shared=false, monitor.visibility='public') while identity is confirming:
    // Publish is disabled, standalone stop-sharing stays enabled and opens unshareOnly dialog.
    h.record = { ...h.record, shared: false };
    h.monitor = {
      monitor_id: 'mon-pub',
      slug: 'pub-slug',
      artifact_id: 'art-1',
      workspace_id: 'ws-1',
      title: 'Public Monitor',
      category: 'other',
      visibility: 'public',
      published_seq: 1,
      keep_updated: false,
      member_user_ids: [],
    } as unknown as MonitorSummary;
    setIdentity({
      tier: 'admin',
      status: 'ready',
      isKnown: true,
      isConfirming: true,
      isAdmin: true,
      isInternal: true,
      isExternal: false,
      canActAsOwner: true,
      canPublishPublic: true,
    });
    renderWithProviders(<OwnerArtifactView code="c1" metadata={METADATA} />);
    expect(screen.getByRole('button', { name: /^Publish$/i })).toBeDisabled();
    const stopBtn2 = await screen.findByRole('button', { name: /Stop sharing|Unshare/i });
    expect(stopBtn2).toBeEnabled();
    fireEvent.click(stopBtn2);
    expect(screen.getByTestId('publish-dialog')).toHaveAttribute('data-unshare-only', 'true');
  });
});
