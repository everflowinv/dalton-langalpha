import { describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { CommandPaletteProvider } from '@/components/CommandPalette/CommandPaletteProvider';
import BottomTabBar from '@/components/BottomTabBar/BottomTabBar';

// Keep the lazy CommandPalette chunk suspended forever to simulate cold-open loading
vi.mock('@/components/CommandPalette/CommandPalette', () => {
  throw new Promise(() => {});
});

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => true,
  getIsMobileSnapshot: () => true,
}));

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ userId: null, logout: vi.fn() }),
}));

vi.mock('@/components/nav/useNavItems', () => ({
  useNavItems: () => [],
}));

describe('CommandPaletteProvider cold-open Escape focus restoration', () => {
  it('returns focus to BottomTabBar More trigger when Escape is pressed while CommandPalette is still lazy-loading', async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <CommandPaletteProvider>
        <BottomTabBar />
      </CommandPaletteProvider>,
      { route: '/chat' },
    );

    const moreBtn = screen.getByTestId('bottom-tab-more');
    moreBtn.focus();
    await user.keyboard('{Enter}');
    const searchItem = await screen.findByRole('menuitem', { name: /Commands & jump|命令与跳转/i });
    await user.click(searchItem);

    // Menu closed while CommandPalette chunk is still suspended (never mounted)
    await waitFor(() => {
      expect(screen.queryByTestId('bottom-tab-more-menu')).toBeNull();
    });
    expect(screen.queryByRole('combobox')).toBeNull();

    // Pressing Escape during cold-open must restore focus to the persistent More trigger button
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => {
      expect(moreBtn).toHaveFocus();
    });
  });
});
