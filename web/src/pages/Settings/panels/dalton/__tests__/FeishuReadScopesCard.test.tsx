import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { FeishuReadScopesCard } from '../FeishuReadScopesCard';
import type {
  DaltonFeishuReadScopesResponse,
  DaltonFeishuSpace,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  callerTier: 'admin' as string | null,
  fetchScopes: vi.fn<() => Promise<DaltonFeishuReadScopesResponse>>(),
  fetchSpaces: vi.fn<() => Promise<{ spaces: DaltonFeishuSpace[] }>>(),
  updateScopes: vi.fn<
    (scopes: Array<{ space_id: string; name: string }>) => Promise<DaltonFeishuReadScopesResponse>
  >(),
}));

vi.mock('@/config/daltonAuth', () => ({
  isDaltonAuth: true,
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  useTeamCredentials: () => ({
    callerTier: h.callerTier,
    callerTierPending: false,
    callerTierError: false,
  }),
}));

vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  fetchDaltonFeishuReadScopes: () => h.fetchScopes(),
  fetchDaltonFeishuSpaces: () => h.fetchSpaces(),
  updateDaltonFeishuReadScopes: (scopes: Array<{ space_id: string; name: string }>) =>
    h.updateScopes(scopes),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <FeishuReadScopesCard />
    </QueryClientProvider>,
  );
}

describe('FeishuReadScopesCard', () => {
  beforeEach(() => {
    h.callerTier = 'admin';
    h.fetchScopes.mockReset();
    h.fetchSpaces.mockReset();
    h.updateScopes.mockReset();
  });

  it('does not render when user is not admin', () => {
    h.callerTier = 'internal';
    renderCard();
    expect(screen.queryByTestId('dalton-feishu-scopes-card')).not.toBeInTheDocument();
  });

  it('preserves persisted scope names, toggles via Switch, and saves updated selection', async () => {
    h.fetchScopes.mockResolvedValueOnce({
      scopes: [
        { space_id: 'sp_saved', name: 'Saved Space Only', added_by: 'u1', added_at: null },
      ],
    });
    h.fetchSpaces.mockResolvedValueOnce({
      spaces: [{ space_id: 'sp_new', name: 'New Wiki Space' }],
    });
    h.updateScopes.mockResolvedValueOnce({ scopes: [] });

    renderCard();

    const savedSwitch = await screen.findByRole('switch', { name: 'Saved Space Only' });
    const newSwitch = screen.getByRole('switch', { name: 'New Wiki Space' });
    expect(savedSwitch).toHaveAttribute('aria-checked', 'true');
    expect(newSwitch).toHaveAttribute('aria-checked', 'false');

    fireEvent.click(newSwitch);
    fireEvent.click(savedSwitch);

    fireEvent.click(screen.getByTestId('dalton-feishu-scopes-save'));
    await waitFor(() => {
      expect(h.updateScopes).toHaveBeenCalledWith([
        { space_id: 'sp_new', name: 'New Wiki Space' },
      ]);
    });
  });

  it('shows empty state when spaces list is empty and refetches on Refresh click', async () => {
    h.fetchScopes.mockResolvedValue({ scopes: [] });
    h.fetchSpaces.mockResolvedValue({ spaces: [] });

    renderCard();

    expect(await screen.findByText('auth.feishuScopes.empty')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('dalton-feishu-scopes-refresh'));
    await waitFor(() => expect(h.fetchSpaces).toHaveBeenCalledTimes(2));
  });

  it('shows API error when spaces request fails', async () => {
    h.fetchScopes.mockResolvedValueOnce({ scopes: [] });
    h.fetchSpaces.mockRejectedValueOnce(new Error('桥接层不可达'));

    renderCard();

    expect(await screen.findByRole('alert')).toHaveTextContent('桥接层不可达');
  });
});
