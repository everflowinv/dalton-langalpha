import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DaltonCosSharingCard, type UserCosSharingSettings } from '../DaltonCosSharingCard';

const h = vi.hoisted(() => ({
  get: vi.fn<() => Promise<{ data: UserCosSharingSettings }>>(),
  put: vi.fn<(url: string, body: { cos_rw_enabled: boolean }) => Promise<{ data: UserCosSharingSettings }>>(),
}));

vi.mock('@/api/client', () => ({
  api: {
    get: (...args: []) => h.get(...args),
    put: (...args: [string, { cos_rw_enabled: boolean }]) => h.put(...args),
  },
}));

vi.mock('@/config/daltonAuth', () => ({
  isDaltonAuth: true,
}));

vi.mock('@/hooks/useCapabilities', () => ({
  useCapabilities: () => ({
    hasCapability: () => true,
  }),
}));

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: () => {
    const snapshot = {
      tier: 'internal' as const,
      status: 'ready' as const,
      authMode: 'local' as const,
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
    };
    return {
      ...snapshot,
      getLatestIdentity: () => snapshot,
      isConfirmingNow: () => false,
      revalidate: vi.fn(async () => undefined),
    };
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

function renderWithQueryClient() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DaltonCosSharingCard />
    </QueryClientProvider>,
  );
}

describe('DaltonCosSharingCard', () => {
  beforeEach(() => {
    h.get.mockReset();
    h.put.mockReset();
  });

  it('renders user-level sharing switch and 3-item trust notice for internal users, and toggles via PUT', async () => {
    h.get.mockResolvedValueOnce({
      data: {
        user_id: 'u-1',
        cos_rw_enabled: false,
        can_edit: true,
        colleague_dispatch_enabled: true,
        caller_tier: 'internal',
      },
    });
    h.put.mockResolvedValueOnce({
      data: {
        user_id: 'u-1',
        cos_rw_enabled: true,
        can_edit: true,
        colleague_dispatch_enabled: true,
        caller_tier: 'internal',
      },
    });

    renderWithQueryClient();

    const toggle = await screen.findByRole('switch');
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText('dalton.cosSharing.notice1')).toBeInTheDocument();
    expect(screen.getByText('dalton.cosSharing.notice3')).toBeInTheDocument();

    fireEvent.click(toggle);

    await waitFor(() => {
      expect(h.put).toHaveBeenCalledWith('/api/v1/dalton/sharing/user-settings', {
        cos_rw_enabled: true,
      });
    });
    expect(await screen.findByRole('switch')).toHaveAttribute('aria-checked', 'true');
  });

  it('disables the switch when can_edit is false', async () => {
    h.get.mockResolvedValueOnce({
      data: {
        cos_rw_enabled: false,
        can_edit: false,
        colleague_dispatch_enabled: true,
        caller_tier: 'internal',
      },
    });

    renderWithQueryClient();

    const toggle = await screen.findByRole('switch');
    expect(toggle).toBeDisabled();
    fireEvent.click(toggle);
    expect(h.put).not.toHaveBeenCalled();
  });

  it('hides the card when colleague_dispatch_enabled is false or user is external', async () => {
    h.get.mockResolvedValueOnce({
      data: {
        cos_rw_enabled: false,
        can_edit: false,
        colleague_dispatch_enabled: false,
        caller_tier: 'internal',
      },
    });

    renderWithQueryClient();
    await waitFor(() =>
      expect(screen.queryByTestId('dalton-cos-sharing-card')).not.toBeInTheDocument(),
    );
  });
});
