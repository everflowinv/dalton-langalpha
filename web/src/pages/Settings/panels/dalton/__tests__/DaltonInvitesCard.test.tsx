import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { DaltonInvitesCard } from '../DaltonInvitesCard';

const h = vi.hoisted(() => ({
  isPm: true,
  canInvite: true,
  callerTier: null as string | null,
  fetchDaltonInvites: vi.fn(),
  createDaltonInvite: vi.fn(),
  revokeDaltonInvite: vi.fn(),
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  useTeamCredentials: () => ({
    isShared: true,
    canDisconnect: h.isPm,
    canManage: h.isPm,
    isPm: h.isPm,
    canInvite: h.canInvite,
    callerTier: h.callerTier,
  }),
}));

vi.mock('../../../utils/daltonApi', () => ({
  fetchDaltonInvites: (...args: unknown[]) => h.fetchDaltonInvites(...args),
  createDaltonInvite: (...args: unknown[]) => h.createDaltonInvite(...args),
  revokeDaltonInvite: (...args: unknown[]) => h.revokeDaltonInvite(...args),
}));

function renderWithQueryClient(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe('DaltonInvitesCard', () => {
  beforeEach(() => {
    h.isPm = true;
    h.canInvite = true;
    h.callerTier = null;
    h.fetchDaltonInvites.mockReset();
    h.createDaltonInvite.mockReset();
    h.revokeDaltonInvite.mockReset();
  });

  it('does not render when canInvite is false even if isPm is true', () => {
    h.isPm = true;
    h.canInvite = false;
    renderWithQueryClient(<DaltonInvitesCard />);
    expect(screen.queryByTestId('dalton-invites-card')).not.toBeInTheDocument();
  });

  it('renders for internal employee (isPm=false, callerTier="internal") with active quota N / 20 and hides for external member', async () => {
    h.isPm = false;
    h.canInvite = true;
    h.callerTier = 'internal';
    h.fetchDaltonInvites.mockResolvedValue({
      invites: [
        {
          invite_id: 'inv-own-1',
          status: 'pending',
          created_at: '2026-04-20T10:00:00Z',
          expires_at: '2026-04-27T10:00:00Z',
          used_by_username: null,
        },
        {
          invite_id: 'inv-own-2',
          status: 'pending',
          created_at: '2026-04-20T11:00:00Z',
          expires_at: '2026-04-27T11:00:00Z',
          used_by_username: null,
        },
        {
          invite_id: 'inv-own-3',
          status: 'used',
          created_at: '2026-04-19T10:00:00Z',
          expires_at: '2026-04-26T10:00:00Z',
          used_by_username: 'bob_ext',
        },
      ],
      active_count: 2,
      max_active: 20,
    });

    const { unmount } = renderWithQueryClient(<DaltonInvitesCard />);
    expect(await screen.findByTestId('dalton-invite-row-inv-own-1')).toBeInTheDocument();
    expect(screen.getByTestId('dalton-invite-row-inv-own-2')).toBeInTheDocument();
    const quota = screen.getByTestId('dalton-invites-quota');
    expect(quota).toHaveTextContent(/2\s*\/\s*20/);
    unmount();

    h.canInvite = false;
    h.callerTier = 'external';
    renderWithQueryClient(<DaltonInvitesCard />);
    expect(screen.queryByTestId('dalton-invites-card')).not.toBeInTheDocument();
  });

  it('renders invite list, generates one-time fragment link with copy button, and revokes pending invites for explicit PM', async () => {
    h.fetchDaltonInvites.mockResolvedValue({
      invites: [
        {
          invite_id: 'inv-pending-1',
          status: 'pending',
          created_at: '2026-04-20T10:00:00Z',
          expires_at: '2026-04-27T10:00:00Z',
          used_by_username: null,
        },
        {
          invite_id: 'inv-used-2',
          status: 'used',
          created_at: '2026-04-19T10:00:00Z',
          expires_at: '2026-04-26T10:00:00Z',
          used_by_username: 'alice_01',
        },
      ],
    });
    h.createDaltonInvite.mockResolvedValue({
      invite_id: 'inv-new-3',
      status: 'pending',
      token: 'tok-plaintext-once',
      url: 'https://dalton.example.com/invite#tok-plaintext-once',
      created_at: '2026-04-20T12:00:00Z',
      expires_at: '2026-04-27T12:00:00Z',
    });
    h.revokeDaltonInvite.mockResolvedValue({
      invite_id: 'inv-pending-1',
      status: 'revoked',
      created_at: '2026-04-20T10:00:00Z',
      expires_at: '2026-04-27T10:00:00Z',
    });

    const writeTextMock = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: writeTextMock },
      configurable: true,
    });

    renderWithQueryClient(<DaltonInvitesCard />);

    expect(await screen.findByTestId('dalton-invites-table')).toBeInTheDocument();
    expect(screen.getByText('alice_01')).toBeInTheDocument();
    expect(screen.getByTestId('dalton-invite-revoke-inv-pending-1')).toBeInTheDocument();
    expect(screen.queryByTestId('dalton-invite-revoke-inv-used-2')).not.toBeInTheDocument();

    // Generate new invite link and copy
    fireEvent.click(screen.getByTestId('dalton-invite-generate-btn'));
    const generatedUrl = await screen.findByTestId('dalton-invite-generated-url');
    expect(generatedUrl).toHaveTextContent(
      'https://dalton.example.com/invite#tok-plaintext-once',
    );

    fireEvent.click(screen.getByTestId('dalton-invite-copy-btn'));
    await waitFor(() => {
      expect(writeTextMock).toHaveBeenCalledWith(
        'https://dalton.example.com/invite#tok-plaintext-once',
      );
    });

    // Revoke pending invite
    fireEvent.click(screen.getByTestId('dalton-invite-revoke-inv-pending-1'));
    await waitFor(() => {
      expect(h.revokeDaltonInvite).toHaveBeenCalledWith('inv-pending-1');
    });
  });
});
