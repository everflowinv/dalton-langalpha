import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { FeishuReadScopesCard } from '../FeishuReadScopesCard';

const h = vi.hoisted(() => ({
  canInvite: true,
  fetchDaltonFeishuReadScopes: vi.fn(),
  updateDaltonFeishuReadScopes: vi.fn(),
  fetchDaltonFeishuSpaces: vi.fn(),
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  useTeamCredentials: () => ({
    isShared: true,
    canDisconnect: h.canInvite,
    isPm: h.canInvite,
    canInvite: h.canInvite,
  }),
}));

vi.mock('../../../utils/daltonApi', () => ({
  fetchDaltonFeishuReadScopes: (...args: unknown[]) => h.fetchDaltonFeishuReadScopes(...args),
  updateDaltonFeishuReadScopes: (...args: unknown[]) => h.updateDaltonFeishuReadScopes(...args),
  fetchDaltonFeishuSpaces: (...args: unknown[]) => h.fetchDaltonFeishuSpaces(...args),
}));

function renderWithQueryClient(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe('FeishuReadScopesCard', () => {
  beforeEach(() => {
    h.canInvite = true;
    h.fetchDaltonFeishuReadScopes.mockReset();
    h.updateDaltonFeishuReadScopes.mockReset();
    h.fetchDaltonFeishuSpaces.mockReset();
  });

  it('does not render when the user is not an admin (canInvite false)', () => {
    h.canInvite = false;
    renderWithQueryClient(<FeishuReadScopesCard />);
    expect(screen.queryByTestId('dalton-feishu-scopes-card')).not.toBeInTheDocument();
    expect(h.fetchDaltonFeishuReadScopes).not.toHaveBeenCalled();
  });

  it('shows whitelisted spaces checked and saves the new selection', async () => {
    h.fetchDaltonFeishuReadScopes.mockResolvedValue({
      scopes: [
        { space_id: 'sp1', name: 'Research', added_by: 'pm', added_at: '2026-01-01T00:00:00Z' },
      ],
    });
    h.fetchDaltonFeishuSpaces.mockResolvedValue({
      spaces: [
        { space_id: 'sp1', name: 'Research' },
        { space_id: 'sp2', name: 'Playbook' },
      ],
    });
    h.updateDaltonFeishuReadScopes.mockResolvedValue({ scopes: [] });

    renderWithQueryClient(<FeishuReadScopesCard />);

    const sp1 = await screen.findByTestId('dalton-feishu-scope-sp1');
    const sp2 = screen.getByTestId('dalton-feishu-scope-sp2');
    expect((sp1 as HTMLInputElement).checked).toBe(true);
    expect((sp2 as HTMLInputElement).checked).toBe(false);

    // Save is disabled until the selection changes.
    const saveBtn = screen.getByTestId('dalton-feishu-scopes-save-btn');
    expect(saveBtn).toBeDisabled();

    fireEvent.click(sp2);
    await waitFor(() => {
      expect((screen.getByTestId('dalton-feishu-scope-sp2') as HTMLInputElement).checked).toBe(true);
    });
    expect(saveBtn).toBeEnabled();

    fireEvent.click(saveBtn);
    await waitFor(() => {
      // Names travel with the selection so saving does not wipe them.
      expect(h.updateDaltonFeishuReadScopes).toHaveBeenCalledWith([
        { space_id: 'sp1', name: 'Research' },
        { space_id: 'sp2', name: 'Playbook' },
      ]);
    });
  });

  it('unchecking a whitelisted space removes it on save', async () => {
    h.fetchDaltonFeishuReadScopes.mockResolvedValue({
      scopes: [
        { space_id: 'sp1', name: 'Research', added_by: 'pm', added_at: '2026-01-01T00:00:00Z' },
      ],
    });
    h.fetchDaltonFeishuSpaces.mockResolvedValue({
      spaces: [{ space_id: 'sp1', name: 'Research' }],
    });
    h.updateDaltonFeishuReadScopes.mockResolvedValue({ scopes: [] });

    renderWithQueryClient(<FeishuReadScopesCard />);
    const sp1 = await screen.findByTestId('dalton-feishu-scope-sp1');
    fireEvent.click(sp1);
    fireEvent.click(screen.getByTestId('dalton-feishu-scopes-save-btn'));

    await waitFor(() => {
      expect(h.updateDaltonFeishuReadScopes).toHaveBeenCalledWith([]);
    });
  });

  it('keeps a name that only the persisted whitelist knows', async () => {
    h.fetchDaltonFeishuReadScopes.mockResolvedValue({
      scopes: [
        { space_id: 'sp1', name: 'Research', added_by: 'pm', added_at: '2026-01-01T00:00:00Z' },
      ],
    });
    // The bot can no longer see sp1, but its persisted name must survive a save.
    h.fetchDaltonFeishuSpaces.mockResolvedValue({
      spaces: [{ space_id: 'sp2', name: 'Playbook' }],
    });
    h.updateDaltonFeishuReadScopes.mockResolvedValue({ scopes: [] });

    renderWithQueryClient(<FeishuReadScopesCard />);
    await screen.findByTestId('dalton-feishu-scope-sp1');
    fireEvent.click(screen.getByTestId('dalton-feishu-scope-sp2'));
    fireEvent.click(screen.getByTestId('dalton-feishu-scopes-save-btn'));

    await waitFor(() => {
      expect(h.updateDaltonFeishuReadScopes).toHaveBeenCalledWith([
        { space_id: 'sp1', name: 'Research' },
        { space_id: 'sp2', name: 'Playbook' },
      ]);
    });
  });
});
