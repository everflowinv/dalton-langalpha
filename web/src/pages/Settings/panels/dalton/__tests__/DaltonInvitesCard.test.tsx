import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import { DaltonInvitesCard } from '../DaltonInvitesCard';
import type {
  DaltonInviteCreateResponse,
  DaltonInviteItem,
  DaltonInviteListResponse,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  canInvite: true,
  callerTier: 'admin' as string | null,
  isConfirming: false,
  revalidate: vi.fn(async () => undefined),
  fetchInvites: vi.fn<() => Promise<DaltonInviteListResponse>>(),
  createInvite: vi.fn<() => Promise<DaltonInviteCreateResponse>>(),
  revokeInvite: vi.fn<(inviteId: string) => Promise<DaltonInviteItem>>(),
  copyText: vi.fn<(text: string) => Promise<void>>(),
}));

vi.mock('@/config/daltonAuth', () => ({
  isDaltonAuth: true,
  isDaltonLocalAuth: false,
  isDaltonSupabaseAuth: true,
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  useTeamCredentials: () => ({
    canInvite: h.canInvite,
    callerTier: h.callerTier,
    callerTierPending: false,
    callerTierError: false,
  }),
}));

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: () => {
    const buildSnapshot = () => {
      const tier = h.callerTier ?? 'unknown';
      const ready = tier === 'admin' || tier === 'internal' || tier === 'external';
      return {
        tier: ready ? tier : 'unknown',
        status: ready ? 'ready' : 'error',
        authMode: ready ? 'local' : null,
        isPlatform: false,
        isKnown: ready,
        isConfirming: h.isConfirming,
        isMultiUserAuth: ready,
        isSingleUserOss: false,
        isAdmin: ready && tier === 'admin',
        isInternal: ready && (tier === 'admin' || tier === 'internal'),
        isExternal: ready && tier === 'external',
        canActAsOwner: ready && (tier === 'admin' || tier === 'internal'),
        canPublishPublic: ready && tier === 'admin',
        canInvite: h.canInvite,
        isTeamCredentialsShared: true,
        canManageCredentials: ready && tier === 'admin',
        isPm: ready && tier === 'admin',
        colleagueSharingEnabled: false,
      };
    };
    return {
      ...buildSnapshot(),
      getLatestIdentity: () => buildSnapshot(),
      isConfirmingNow: () => h.isConfirming,
      revalidate: h.revalidate,
    };
  },
}));

vi.mock('@/hooks/useLocale', () => ({
  useLocale: () => 'zh-CN',
}));

vi.mock('@/lib/clipboard', () => ({
  copyText: (text: string) => h.copyText(text),
}));

vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  fetchDaltonInvites: () => h.fetchInvites(),
  createDaltonInvite: () => h.createInvite(),
  revokeDaltonInvite: (id: string) => h.revokeInvite(id),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) =>
      opts && 'count' in opts && 'max' in opts
        ? `${key}:${opts.count}/${opts.max}`
        : key,
  }),
}));

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DaltonInvitesCard />
    </QueryClientProvider>,
  );
}

describe('DaltonInvitesCard', () => {
  beforeEach(() => {
    h.canInvite = true;
    h.callerTier = 'admin';
    h.isConfirming = false;
    h.revalidate.mockReset().mockResolvedValue(undefined);
    h.fetchInvites.mockReset();
    h.createInvite.mockReset();
    h.revokeInvite.mockReset();
    h.copyText.mockReset().mockResolvedValue(undefined);
  });

  it('does not render when canInvite is false', () => {
    h.canInvite = false;
    h.callerTier = 'external';
    renderCard();
    expect(screen.queryByTestId('dalton-invites-card')).not.toBeInTheDocument();
  });

  it('generates and copies an invite link and requires confirmation before revoking', async () => {
    h.fetchInvites.mockResolvedValue({
      invites: [
        {
          invite_id: 'inv-1234567890',
          status: 'pending',
          created_at: '2026-11-10T00:00:00Z',
          expires_at: '2026-11-17T00:00:00Z',
        },
      ],
      active_count: 1,
      max_active: 20,
    });
    h.createInvite.mockResolvedValueOnce({
      invite_id: 'inv-new',
      status: 'pending',
      created_at: '2026-11-16T00:00:00Z',
      expires_at: '2026-11-23T00:00:00Z',
      token: 'tok-1',
      url: 'https://dalton.test/signup?invite=tok-1',
    });
    h.revokeInvite.mockResolvedValueOnce({
      invite_id: 'inv-1234567890',
      status: 'revoked',
      created_at: '2026-11-10T00:00:00Z',
      expires_at: '2026-11-17T00:00:00Z',
    });

    renderCard();

    expect(await screen.findByText('auth.invites.activeQuota:1/20')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('dalton-invites-generate'));
    expect(await screen.findByTestId('dalton-invites-new-link')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('dalton-invites-copy'));
    await waitFor(() => {
      expect(h.copyText).toHaveBeenCalledWith('https://dalton.test/signup?invite=tok-1');
    });

    // Clicking Revoke opens ConfirmDialog instead of calling API directly
    fireEvent.click(screen.getByTestId('dalton-invite-revoke-inv-1234567890'));
    expect(h.revokeInvite).not.toHaveBeenCalled();

    const confirmBtns = await screen.findAllByRole('button', { name: 'auth.invites.revoke' });
    fireEvent.click(confirmBtns[confirmBtns.length - 1]);

    await waitFor(() => {
      expect(h.revokeInvite).toHaveBeenCalledWith('inv-1234567890');
    });
  });

  it('disables generate and revoke during isConfirming and blocks stale submissions via getLatestIdentity()', async () => {
    h.isConfirming = true;
    h.fetchInvites.mockResolvedValue({
      invites: [
        {
          invite_id: 'inv-confirm-1',
          status: 'pending',
          created_at: '2026-11-10T00:00:00Z',
          expires_at: '2026-11-17T00:00:00Z',
        },
      ],
      active_count: 1,
      max_active: 20,
    });

    const { unmount } = renderCard();
    expect(await screen.findByTestId('dalton-invite-revoke-inv-confirm-1')).toBeDisabled();
    expect(screen.getByTestId('dalton-invites-generate')).toBeDisabled();
    unmount();

    // Render ready, then flip confirming/canInvite synchronously before clicking generate.
    h.isConfirming = false;
    renderCard();
    const generateBtn = await screen.findByTestId('dalton-invites-generate');
    expect(generateBtn).toBeEnabled();

    h.isConfirming = true;
    fireEvent.click(generateBtn);
    expect(h.createInvite).not.toHaveBeenCalled();

    // Also block revoke confirmation when identity loses canInvite before confirming.
    h.isConfirming = false;
    const revokeBtn = await screen.findByTestId('dalton-invite-revoke-inv-confirm-1');
    fireEvent.click(revokeBtn);
    const confirmBtns = await screen.findAllByRole('button', { name: 'auth.invites.revoke' });
    h.canInvite = false;
    fireEvent.click(confirmBtns[confirmBtns.length - 1]);
    expect(h.revokeInvite).not.toHaveBeenCalled();
  });

  it('disables the confirm button inside an already-open revoke dialog when isConfirming turns true', async () => {
    h.fetchInvites.mockResolvedValue({
      invites: [
        {
          invite_id: 'inv-open-dialog',
          status: 'pending',
          created_at: '2026-11-10T00:00:00Z',
          expires_at: '2026-11-17T00:00:00Z',
        },
      ],
      active_count: 1,
      max_active: 20,
    });

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <DaltonInvitesCard />
      </QueryClientProvider>,
    );

    const revokeBtn = await screen.findByTestId('dalton-invite-revoke-inv-open-dialog');
    fireEvent.click(revokeBtn);
    const confirmBtns = await screen.findAllByRole('button', { name: 'auth.invites.revoke' });
    const dialogConfirmBtn = confirmBtns[confirmBtns.length - 1];
    expect(dialogConfirmBtn).toBeEnabled();

    h.isConfirming = true;
    rerender(
      <QueryClientProvider client={client}>
        <DaltonInvitesCard />
      </QueryClientProvider>,
    );

    expect(dialogConfirmBtn).toBeDisabled();
    fireEvent.click(dialogConfirmBtn);
    expect(h.revokeInvite).not.toHaveBeenCalled();
  });

  it('shows server detail only for 403 errors and falls back to saveFailed for non-403 errors with detail', async () => {
    h.fetchInvites.mockResolvedValue({ invites: [], active_count: 0, max_active: 20 });
    h.createInvite
      .mockRejectedValueOnce(
        new ApiError('Request failed with status code 422', 'ERR_BAD_REQUEST', {
          status: 422,
          statusText: 'Unprocessable Entity',
          headers: {},
          data: { detail: 'Internal validation trace' },
        }),
      )
      .mockRejectedValueOnce(
        new ApiError('Request failed with status code 403', 'ERR_BAD_REQUEST', {
          status: 403,
          statusText: 'Forbidden',
          headers: {},
          data: { detail: { code: 'external_member_forbidden', message: '外部成员无权生成邀请' } },
        }),
      );

    renderCard();
    const generateBtn = await screen.findByTestId('dalton-invites-generate');

    fireEvent.click(generateBtn);
    expect(await screen.findByText('dalton.common.saveFailed')).toBeInTheDocument();
    expect(screen.queryByText('Internal validation trace')).toBeNull();

    fireEvent.click(generateBtn);
    expect(await screen.findByText('外部成员无权生成邀请')).toBeInTheDocument();
    expect(h.revalidate).toHaveBeenCalledTimes(1);
  });
});
