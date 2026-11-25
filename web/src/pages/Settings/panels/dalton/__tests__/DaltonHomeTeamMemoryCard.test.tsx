import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import {
  DaltonHomeTeamMemoryCard,
  type UserTeamMemorySettings,
} from '../DaltonHomeTeamMemoryCard';

const h = vi.hoisted(() => ({
  get: vi.fn<() => Promise<{ data: UserTeamMemorySettings }>>(),
  put: vi.fn<
    (url: string, body: { home_team_memory_enabled: boolean }) => Promise<{
      data: UserTeamMemorySettings;
    }>
  >(),
  revalidate: vi.fn(async () => undefined),
}));

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      get: (...args: []) => h.get(...args),
      put: (...args: [string, { home_team_memory_enabled: boolean }]) => h.put(...args),
    },
  };
});

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
      revalidate: h.revalidate,
    };
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function renderWithQueryClient() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DaltonHomeTeamMemoryCard />
    </QueryClientProvider>,
  );
}

describe('DaltonHomeTeamMemoryCard', () => {
  beforeEach(() => {
    h.get.mockReset();
    h.put.mockReset();
    h.revalidate.mockReset().mockResolvedValue(undefined);
  });

  it('renders switch (default off) and toggles via PUT /api/v1/dalton/user-settings', async () => {
    h.get.mockResolvedValueOnce({
      data: {
        user_id: 'u-1',
        home_team_memory_enabled: false,
        can_edit: true,
        caller_tier: 'internal',
      },
    });
    h.put.mockResolvedValueOnce({
      data: {
        user_id: 'u-1',
        home_team_memory_enabled: true,
        can_edit: true,
        caller_tier: 'internal',
      },
    });

    renderWithQueryClient();

    const toggle = await screen.findByRole('switch');
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(h.put).toHaveBeenCalledWith('/api/v1/dalton/user-settings', {
        home_team_memory_enabled: true,
      });
    });
    expect(await screen.findByRole('switch')).toHaveAttribute('aria-checked', 'true');
  });

  it('shows error notice when toggling fails, blocks duplicate clicks while pending, and clears error on retry', async () => {
    h.get.mockResolvedValueOnce({
      data: {
        user_id: 'u-1',
        home_team_memory_enabled: false,
        can_edit: true,
        caller_tier: 'internal',
      },
    });

    let rejectFirst!: (err: Error) => void;
    h.put.mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectFirst = reject;
        }),
    );

    renderWithQueryClient();

    const toggle = await screen.findByRole('switch');
    fireEvent.click(toggle);
    fireEvent.click(toggle);
    await waitFor(() => expect(h.put).toHaveBeenCalledTimes(1));

    rejectFirst(new Error('Network fail'));
    expect(await screen.findByTestId('dalton-home-team-memory-error')).toBeInTheDocument();
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false');

    h.put.mockResolvedValueOnce({
      data: {
        home_team_memory_enabled: true,
        can_edit: true,
        caller_tier: 'internal',
      },
    });

    fireEvent.click(screen.getByRole('switch'));
    await waitFor(() => {
      expect(screen.queryByTestId('dalton-home-team-memory-error')).not.toBeInTheDocument();
    });
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true');
  });

  it('shows server detail only on 403 errors and falls back to saveFailed on non-403 errors with detail', async () => {
    h.get.mockResolvedValueOnce({
      data: {
        user_id: 'u-1',
        home_team_memory_enabled: false,
        can_edit: true,
        caller_tier: 'internal',
      },
    });
    h.put
      .mockRejectedValueOnce(
        new ApiError('Request failed with status code 422', 'ERR_BAD_REQUEST', {
          status: 422,
          statusText: 'Unprocessable Entity',
          headers: {},
          data: { detail: 'validation schema mismatch' },
        }),
      )
      .mockRejectedValueOnce(
        new ApiError('Request failed with status code 403', 'ERR_BAD_REQUEST', {
          status: 403,
          statusText: 'Forbidden',
          headers: {},
          data: { detail: { code: 'external_member_forbidden', message: '外部成员无法开启团队记忆' } },
        }),
      );

    renderWithQueryClient();
    const toggle = await screen.findByRole('switch');

    fireEvent.click(toggle);
    expect(await screen.findByTestId('dalton-home-team-memory-error')).toHaveTextContent(
      'dalton.common.saveFailed',
    );
    expect(screen.queryByText('validation schema mismatch')).toBeNull();

    fireEvent.click(toggle);
    await waitFor(() =>
      expect(screen.getByTestId('dalton-home-team-memory-error')).toHaveTextContent(
        '外部成员无法开启团队记忆',
      ),
    );
    expect(h.revalidate).toHaveBeenCalledTimes(1);
  });
});
