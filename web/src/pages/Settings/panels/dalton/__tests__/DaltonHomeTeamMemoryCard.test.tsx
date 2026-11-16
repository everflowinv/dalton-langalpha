import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
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
}));

vi.mock('@/api/client', () => ({
  api: {
    get: (...args: []) => h.get(...args),
    put: (...args: [string, { home_team_memory_enabled: boolean }]) => h.put(...args),
  },
}));

vi.mock('@/config/daltonAuth', () => ({
  isDaltonAuth: true,
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
});
