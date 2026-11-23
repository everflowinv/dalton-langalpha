import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DaltonInvitesCard } from '../DaltonInvitesCard';
import type {
  DaltonInviteCreateResponse,
  DaltonInviteItem,
  DaltonInviteListResponse,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  canInvite: true,
  callerTier: 'admin' as string | null,
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
});
