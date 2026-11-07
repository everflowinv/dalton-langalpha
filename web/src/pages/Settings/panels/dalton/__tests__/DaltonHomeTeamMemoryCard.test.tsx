import { beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { createTestQueryClient, renderWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import {
  DaltonHomeTeamMemoryCard,
  type UserTeamMemorySettings,
} from '../DaltonHomeTeamMemoryCard';

const mockApiGet = vi.hoisted(() => vi.fn());
const mockApiPut = vi.hoisted(() => vi.fn());

vi.mock('@/api/client', () => ({
  api: {
    get: (...args: unknown[]) => mockApiGet(...args),
    put: (...args: unknown[]) => mockApiPut(...args),
  },
}));

describe('DaltonHomeTeamMemoryCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders user-level Home Team Memory switch (default off) and toggles via PUT /api/v1/dalton/user-settings', async () => {
    const { default: i18n } = await import('@/i18n');
    await i18n.changeLanguage('zh-CN');
    onTestFinished(async () => {
      await i18n.changeLanguage('en-US');
    });

    const initial: UserTeamMemorySettings = {
      user_id: 'u-richard',
      home_team_memory_enabled: false,
      can_edit: true,
      caller_tier: 'internal',
      updated_at: null,
    };
    const updated: UserTeamMemorySettings = {
      ...initial,
      home_team_memory_enabled: true,
      updated_at: '2026-11-05T12:00:00Z',
    };

    mockApiGet.mockResolvedValueOnce({ data: initial });
    mockApiPut.mockResolvedValueOnce({ data: updated });

    const queryClient = createTestQueryClient();
    renderWithProviders(<DaltonHomeTeamMemoryCard />, { queryClient });

    const toggle = await screen.findByTestId('home-team-memory-switch');
    expect(mockApiGet).toHaveBeenCalledWith('/api/v1/dalton/user-settings');
    await waitFor(() => {
      expect(toggle).not.toBeDisabled();
    });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText('Home 团队记忆')).toBeInTheDocument();
    expect(
      screen.getByText(
        '允许研究总监（Home）查阅团队过去的对话记录（OpenClaw 对话卡片）',
      ),
    ).toBeInTheDocument();

    fireEvent.click(toggle);
    await waitFor(() => {
      expect(mockApiPut).toHaveBeenCalledWith('/api/v1/dalton/user-settings', {
        home_team_memory_enabled: true,
      });
    });
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(queryClient.getQueryData(queryKeys.dalton.userSettings())).toEqual(
      updated,
    );
  });

  it('hides the card when caller_tier is external', async () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dalton.userSettings(), {
      user_id: 'u-ext',
      home_team_memory_enabled: false,
      can_edit: false,
      caller_tier: 'external',
      updated_at: null,
    });

    renderWithProviders(<DaltonHomeTeamMemoryCard />, { queryClient });

    expect(screen.queryByTestId('dalton-home-team-memory-card')).toBeNull();
  });

  it('shows error message when toggling fails, blocks duplicate clicks while pending, and clears error on retry', async () => {
    const { default: i18n } = await import('@/i18n');
    await i18n.changeLanguage('zh-CN');
    onTestFinished(async () => {
      await i18n.changeLanguage('en-US');
    });

    const initial: UserTeamMemorySettings = {
      user_id: 'u-richard',
      home_team_memory_enabled: false,
      can_edit: true,
      caller_tier: 'internal',
      updated_at: null,
    };
    const updated: UserTeamMemorySettings = {
      ...initial,
      home_team_memory_enabled: true,
      updated_at: '2026-11-05T12:05:00Z',
    };

    let rejectPut: (err: unknown) => void = () => {};
    mockApiGet.mockResolvedValueOnce({ data: initial });
    mockApiPut
      .mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            rejectPut = reject;
          }),
      )
      .mockResolvedValueOnce({ data: updated });

    const queryClient = createTestQueryClient();
    renderWithProviders(<DaltonHomeTeamMemoryCard />, { queryClient });

    const toggle = await screen.findByTestId('home-team-memory-switch');
    await waitFor(() => {
      expect(toggle).not.toBeDisabled();
    });

    // Rapid double click only fires one PUT request and disables the switch while pending
    fireEvent.click(toggle);
    fireEvent.click(toggle);
    await waitFor(() => {
      expect(mockApiPut).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(toggle).toBeDisabled();
    });
    expect(mockApiPut).toHaveBeenCalledTimes(1);

    rejectPut(new Error('500 Internal Server Error'));

    await waitFor(() => {
      expect(screen.getByTestId('home-team-memory-error')).toHaveTextContent(
        '切换失败，请稍后重试',
      );
    });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(toggle).not.toBeDisabled();

    // Retry succeeds and clears the error message
    fireEvent.click(toggle);
    await waitFor(() => {
      expect(toggle).toHaveAttribute('aria-checked', 'true');
    });
    expect(screen.queryByTestId('home-team-memory-error')).toBeNull();
    expect(mockApiPut).toHaveBeenCalledTimes(2);
  });
});
