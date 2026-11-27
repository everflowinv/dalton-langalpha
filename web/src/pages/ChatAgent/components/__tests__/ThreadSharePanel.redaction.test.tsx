import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import i18n from '@/i18n';
import { renderWithProviders } from '@/test/utils';
import type { CallerIdentity, CallerIdentitySnapshot } from '@/hooks/useCallerIdentity';
import type { ShareRedactionSummary, ThreadShareStatus } from '@/types/api';
import type { getThreadShareStatus as GetThreadShareStatusFn } from '../../utils/api/threads';

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
  getThreadShareStatus: vi.fn<typeof GetThreadShareStatusFn>(),
}));

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: (): CallerIdentity => ({
    ...h.identity,
    getLatestIdentity: () => ({ ...h.identity }),
    isConfirmingNow: () => h.identity.isConfirming,
    revalidate: vi.fn<() => Promise<void>>(async () => undefined),
  }),
}));

vi.mock('@/hooks/useShareLink', () => ({
  useSharedLinks: () => ({ data: [] }),
  useShareLinkMutations: () => ({ patch: { isPending: false, mutate: vi.fn<() => void>() } }),
}));

vi.mock('@/components/organization/LibraryPublishRow', () => ({
  LibraryPublishRow: () => <div data-testid="library-publish-row" />,
}));

vi.mock('../../utils/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/api')>();
  return {
    ...actual,
    getThreadShareStatus: (...args: Parameters<typeof GetThreadShareStatusFn>) =>
      h.getThreadShareStatus(...args),
  };
});

import { ThreadSharePanel } from '../ThreadSharePanel';
import ShareButton from '../ShareButton';

function makeShareStatus(overrides: Partial<ThreadShareStatus> = {}): ThreadShareStatus {
  return {
    is_shared: true,
    share_token: 'tok-abc',
    share_url: '/s/tok-abc',
    permissions: { allow_files: false, allow_download: false, audience: 'public' },
    audiences: ['public', 'organization'],
    ...overrides,
  };
}

describe('ThreadSharePanel redaction summary, boundary notice, and audience previews', () => {
  beforeEach(async () => {
    h.getThreadShareStatus.mockReset();
    await i18n.changeLanguage('zh-CN');
  });

  afterEach(async () => {
    await i18n.changeLanguage('en-US');
  });

  it('omits include_summary while closed (and on ShareButton) and requests include_summary=true only when opened', async () => {
    h.getThreadShareStatus.mockImplementation(async (_tid, opts) =>
      makeShareStatus({
        redaction_summary: opts?.includeSummary
          ? { widgets: 1, tool_calls: 2, images: 0, attachments: 0 }
          : null,
      }),
    );

    renderWithProviders(<ShareButton threadId="th-btn" workspaceId="ws-1" />);
    await waitFor(() => expect(h.getThreadShareStatus).toHaveBeenCalledTimes(1));
    expect(h.getThreadShareStatus).toHaveBeenCalledWith('th-btn');

    h.getThreadShareStatus.mockClear();

    const { rerender } = renderWithProviders(
      <ThreadSharePanel threadId="th-panel" workspaceId="ws-1" open={false} />,
    );
    await waitFor(() => expect(h.getThreadShareStatus).toHaveBeenCalledTimes(1));
    expect(h.getThreadShareStatus).toHaveBeenCalledWith('th-panel');

    rerender(<ThreadSharePanel threadId="th-panel" workspaceId="ws-1" open={true} />);
    await waitFor(() =>
      expect(h.getThreadShareStatus).toHaveBeenCalledWith('th-panel', { includeSummary: true }),
    );
  });

  it('displays boundary line and exact non-zero, all-zero, and allow_files=true summary copy', async () => {
    const nonZero: ShareRedactionSummary = { widgets: 2, tool_calls: 9, images: 3, attachments: 1 };
    h.getThreadShareStatus.mockResolvedValueOnce(
      makeShareStatus({
        permissions: { allow_files: false, allow_download: false, audience: 'public' },
        redaction_summary: nonZero,
      }),
    );

    const view1 = renderWithProviders(<ThreadSharePanel threadId="th-nonzero" open={true} />);
    expect(await screen.findByText('分享内容包括模型回答的全部文字')).toBeInTheDocument();
    expect(
      screen.getByText('将自动隐藏本对话中的 2 个交互组件（显示通用占位）、9 次工具调用与思考过程、3 张图片及 1 个附件'),
    ).toBeInTheDocument();
    view1.unmount();

    // Zero counts are omitted from partial summaries
    h.getThreadShareStatus.mockResolvedValueOnce(
      makeShareStatus({
        permissions: { allow_files: false, allow_download: false, audience: 'public' },
        redaction_summary: { widgets: 0, tool_calls: 4, images: 0, attachments: 2 },
      }),
    );
    const viewPartial = renderWithProviders(<ThreadSharePanel threadId="th-partial" open={true} />);
    expect(
      await screen.findByText('将自动隐藏本对话中的 4 次工具调用与思考过程及 2 个附件'),
    ).toBeInTheDocument();
    viewPartial.unmount();

    // All-zero summary shows generic all-hidden description
    h.getThreadShareStatus.mockResolvedValueOnce(
      makeShareStatus({
        permissions: { allow_files: false, allow_download: false, audience: 'public' },
        redaction_summary: { widgets: 0, tool_calls: 0, images: 0, attachments: 0 },
      }),
    );
    const viewZero = renderWithProviders(<ThreadSharePanel threadId="th-zero" open={true} />);
    expect(
      await screen.findByText('工具调用、思考过程、交互组件、图片与工作区文件均自动隐藏'),
    ).toBeInTheDocument();
    viewZero.unmount();

    // allow_files=true switches to full-file sharing description
    h.getThreadShareStatus.mockResolvedValueOnce(
      makeShareStatus({
        permissions: { allow_files: true, allow_download: false, audience: 'public' },
        redaction_summary: nonZero,
      }),
    );
    const viewFiles = renderWithProviders(<ThreadSharePanel threadId="th-files" open={true} />);
    expect(
      await screen.findByText('已允许访问工作区文件，交互组件、工具调用、图片与工作区文件将一并展示'),
    ).toBeInTheDocument();
    viewFiles.unmount();

    // English boundary copy
    await i18n.changeLanguage('en-US');
    h.getThreadShareStatus.mockResolvedValueOnce(
      makeShareStatus({
        permissions: { allow_files: false, allow_download: false, audience: 'public' },
        redaction_summary: { widgets: 0, tool_calls: 0, images: 0, attachments: 0 },
      }),
    );
    renderWithProviders(<ThreadSharePanel threadId="th-en" open={true} />);
    expect(
      await screen.findByText('Shared content includes the full text of model responses.'),
    ).toBeInTheDocument();
  });

  it('renders audience-specific preview links with target=_blank and rel=noopener noreferrer', async () => {
    // Public audience -> visitor view with ?as=visitor
    h.getThreadShareStatus.mockResolvedValueOnce(
      makeShareStatus({
        share_url: '/s/tok-pub',
        permissions: { allow_files: false, allow_download: false, audience: 'public' },
      }),
    );
    const viewPub = renderWithProviders(<ThreadSharePanel threadId="th-pub" open={true} />);
    const visitorLink = await screen.findByRole('link', { name: '预览访客视角' });
    expect(visitorLink).toHaveAttribute('href', '/s/tok-pub?as=visitor');
    expect(visitorLink).toHaveAttribute('target', '_blank');
    expect(visitorLink).toHaveAttribute('rel', 'noopener noreferrer');
    viewPub.unmount();

    // Organization audience -> member view without ?as=visitor
    h.getThreadShareStatus.mockResolvedValueOnce(
      makeShareStatus({
        share_url: '/s/tok-org',
        permissions: { allow_files: false, allow_download: false, audience: 'organization' },
      }),
    );
    renderWithProviders(<ThreadSharePanel threadId="th-org" open={true} />);
    const memberLink = await screen.findByRole('link', { name: '预览成员视角' });
    expect(memberLink).toHaveAttribute('href', '/s/tok-org');
    expect(memberLink).toHaveAttribute('target', '_blank');
    expect(memberLink).toHaveAttribute('rel', 'noopener noreferrer');
  });
});
