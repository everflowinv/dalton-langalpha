import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';
import { DaltonCosSharingCard, type UserCosSharingSettings } from '../DaltonCosSharingCard';

const h = vi.hoisted(() => ({
  get: vi.fn<() => Promise<{ data: UserCosSharingSettings }>>(),
  put: vi.fn<(url: string, body: { cos_rw_enabled: boolean }) => Promise<{ data: UserCosSharingSettings }>>(),
  revalidate: vi.fn(async () => undefined),
}));

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      get: (...args: []) => h.get(...args),
      put: (...args: [string, { cos_rw_enabled: boolean }]) => h.put(...args),
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
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

function renderWithQueryClient() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <DaltonCosSharingCard />
      </QueryClientProvider>,
    ),
  };
}

describe('DaltonCosSharingCard', () => {
  beforeEach(() => {
    h.get.mockReset();
    h.put.mockReset();
    h.revalidate.mockReset().mockResolvedValue(undefined);
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

  it('shows server detail only on 403 errors and falls back to saveFailed on non-403 errors with detail', async () => {
    h.get.mockResolvedValueOnce({
      data: {
        user_id: 'u-1',
        cos_rw_enabled: false,
        can_edit: true,
        colleague_dispatch_enabled: true,
        caller_tier: 'internal',
      },
    });
    h.put
      .mockRejectedValueOnce(
        new ApiError('Request failed with status code 500', 'ERR_BAD_RESPONSE', {
          status: 500,
          statusText: 'Internal Server Error',
          headers: {},
          data: { detail: 'raw backend traceback' },
        }),
      )
      .mockRejectedValueOnce(
        new ApiError('Request failed with status code 403', 'ERR_BAD_REQUEST', {
          status: 403,
          statusText: 'Forbidden',
          headers: {},
          data: { detail: { code: 'external_member_forbidden', message: '外部成员无法修改研究总监共享' } },
        }),
      );

    renderWithQueryClient();
    const toggle = await screen.findByRole('switch');

    fireEvent.click(toggle);
    expect(await screen.findByTestId('dalton-cos-sharing-error')).toHaveTextContent(
      'dalton.common.saveFailed',
    );
    expect(screen.queryByText('raw backend traceback')).toBeNull();

    fireEvent.click(toggle);
    await waitFor(() =>
      expect(screen.getByTestId('dalton-cos-sharing-error')).toHaveTextContent(
        '外部成员无法修改研究总监共享',
      ),
    );
    expect(h.revalidate).toHaveBeenCalledTimes(1);
  });

  it('hides card without load error when GET /sharing/user-settings returns 404 or 200 neutral payload', async () => {
    h.get.mockRejectedValueOnce(
      new ApiError('Request failed with status code 404', 'ERR_BAD_REQUEST', {
        status: 404,
        statusText: 'Not Found',
        headers: {},
        data: { detail: 'Capability disabled' },
      }),
    );

    const view1 = renderWithQueryClient();
    await waitFor(() => expect(h.get).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.queryByTestId('dalton-cos-sharing-card')).not.toBeInTheDocument(),
    );
    expect(screen.queryByText('dalton.common.loadFailed')).toBeNull();
    view1.unmount();

    h.get.mockResolvedValueOnce({
      data: {
        user_id: 'u-1',
        cos_rw_enabled: false,
        cos_access: 'hidden',
        can_edit: false,
        caller_tier: 'internal',
        colleague_dispatch_enabled: false,
      },
    });
    renderWithQueryClient();
    await waitFor(() => expect(h.get).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByTestId('dalton-cos-sharing-card')).not.toBeInTheDocument(),
    );
    expect(screen.queryByText('dalton.common.loadFailed')).toBeNull();
  });

  it('hides card without saveFailed and invalidates capabilities and userSharing when PUT /sharing/user-settings returns 404', async () => {
    const notFound = new ApiError('Request failed with status code 404', 'ERR_BAD_REQUEST', {
      status: 404,
      statusText: 'Not Found',
      headers: {},
      data: { detail: 'Capability disabled' },
    });
    h.get
      .mockResolvedValueOnce({
        data: {
          user_id: 'u-1',
          cos_rw_enabled: false,
          can_edit: true,
          colleague_dispatch_enabled: true,
          caller_tier: 'internal',
        },
      })
      .mockResolvedValueOnce({
        data: {
          user_id: 'u-1',
          cos_rw_enabled: false,
          can_edit: false,
          colleague_dispatch_enabled: false,
          caller_tier: 'internal',
        },
      });
    h.put.mockRejectedValueOnce(notFound);

    const { client } = renderWithQueryClient();
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');
    const toggle = await screen.findByRole('switch');

    fireEvent.click(toggle);
    await waitFor(() => expect(h.put).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.queryByTestId('dalton-cos-sharing-card')).not.toBeInTheDocument(),
    );
    expect(screen.queryByTestId('dalton-cos-sharing-error')).toBeNull();
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.dalton.capabilities() });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.dalton.userSharing() });
  });
});
