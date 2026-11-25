import { screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithProviders } from '@/test/utils';
import type { CallerIdentity, CallerIdentitySnapshot } from '@/hooks/useCallerIdentity';
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

vi.mock('../../ChatAgent/components/PublishDialog', () => ({
  default: () => <div data-testid="publish-dialog" />,
}));

vi.mock('../../ChatAgent/components/filePanel/useArtifactVersionView', () => ({
  useArtifactVersionView: () => ({
    record: { artifact_id: 'art-1', code: 'c1', path: 'results/report.html', shared: false, latest_seq: 1 },
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
});
