import { beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { createTestQueryClient, renderWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import { DaltonCosSharingCard, type UserCosSharingSettings } from '../DaltonCosSharingCard';

const mockApiGet = vi.hoisted(() => vi.fn());
const mockApiPut = vi.hoisted(() => vi.fn());

vi.mock('@/api/client', () => ({
  api: {
    get: (...args: unknown[]) => mockApiGet(...args),
    put: (...args: unknown[]) => mockApiPut(...args),
  },
}));

describe('DaltonCosSharingCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders user-level Chief of Staff switch and 5-item trust notice for internal users, and toggles via PUT', async () => {
    const { default: i18n } = await import('@/i18n');
    await i18n.changeLanguage('zh-CN');
    onTestFinished(async () => {
      await i18n.changeLanguage('en-US');
    });

    const initial: UserCosSharingSettings = {
      user_id: 'u-chao',
      cos_rw_enabled: true,
      cos_access: 'rw',
      can_edit: true,
      caller_tier: 'internal',
      colleague_dispatch_enabled: true,
      updated_at: '2026-04-20T12:00:00Z',
    };
    const updated: UserCosSharingSettings = {
      ...initial,
      cos_rw_enabled: false,
      cos_access: 'hidden',
      updated_at: '2026-04-20T12:05:00Z',
    };

    mockApiGet.mockResolvedValueOnce({ data: initial });
    mockApiPut.mockResolvedValueOnce({ data: updated });

    const queryClient = createTestQueryClient();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    renderWithProviders(<DaltonCosSharingCard />, { queryClient });

    const toggle = await screen.findByTestId('cos-user-sharing-switch');
    expect(mockApiGet).toHaveBeenCalledWith('/api/v1/dalton/sharing/user-settings');
    await waitFor(() => {
      expect(toggle).not.toBeDisabled();
    });
    expect(toggle).toHaveAttribute('aria-checked', 'true');

    expect(screen.getByText('其他人的研究总监')).toBeInTheDocument();
    expect(screen.getByText('允许内部同事的研究总监访问我的工作区并派任务')).toBeInTheDocument();
    expect(screen.getByTestId('cos-user-scope-hint')).toHaveTextContent(
      '作用于除 Home 外的全部工作区',
    );
    const trustNotice = screen.getByTestId('cos-user-trust-notice');
    expect(trustNotice).toHaveTextContent('整机读写');
    expect(trustNotice).toHaveTextContent('连接与外部影响');
    expect(trustNotice).toHaveTextContent('资源与配额');
    expect(trustNotice).toHaveTextContent('对话可见与保留');
    expect(trustNotice).toHaveTextContent('撤销边界');

    fireEvent.click(toggle);
    await waitFor(() => {
      expect(mockApiPut).toHaveBeenCalledWith('/api/v1/dalton/sharing/user-settings', {
        cos_rw_enabled: false,
      });
    });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(queryClient.getQueryData(queryKeys.dalton.userSharing())).toEqual(updated);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.workspaces.team() });
  });

  it('hides the card when caller_tier is external', async () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dalton.userSharing(), {
      user_id: 'u-ext',
      cos_rw_enabled: false,
      cos_access: 'hidden',
      can_edit: false,
      caller_tier: 'external',
      updated_at: null,
    });

    renderWithProviders(<DaltonCosSharingCard />, { queryClient });

    expect(screen.queryByTestId('dalton-cos-sharing-card')).toBeNull();
  });

  it('hides the card when colleague_dispatch_enabled is false', async () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dalton.userSharing(), {
      user_id: 'u-chao',
      cos_rw_enabled: true,
      cos_access: 'rw',
      can_edit: true,
      caller_tier: 'internal',
      colleague_dispatch_enabled: false,
      updated_at: null,
    });

    renderWithProviders(<DaltonCosSharingCard />, { queryClient });

    expect(screen.queryByTestId('dalton-cos-sharing-card')).toBeNull();
  });

  it('disables the switch when can_edit is false', async () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dalton.userSharing(), {
      user_id: 'u-ro',
      cos_rw_enabled: true,
      cos_access: 'rw',
      can_edit: false,
      caller_tier: 'internal',
      colleague_dispatch_enabled: true,
      updated_at: null,
    });

    renderWithProviders(<DaltonCosSharingCard />, { queryClient });

    const toggle = await screen.findByTestId('cos-user-sharing-switch');
    expect(toggle).toBeDisabled();
  });
});
