import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import BottomTabBar from '../BottomTabBar';

const openPalette = vi.fn();
const preloadPalette = vi.fn();

vi.mock('@/components/CommandPalette/CommandPaletteProvider', () => ({
  useCommandPalette: () => ({
    open: openPalette,
    preload: preloadPalette,
  }),
}));

vi.mock('@/hooks/useFeatures', () => ({
  useFeatureEnabled: () => true,
}));

describe('BottomTabBar mobile overflow menu', () => {
  it('caps direct bottom-bar buttons to <= 5, uses Radix DropdownMenu with keyboard navigation, and opens Search', async () => {
    const user = userEvent.setup();
    const { container } = renderWithProviders(<BottomTabBar />, {
      route: '/chat',
    });

    const pill = container.querySelector('.bottom-tab-bar-pill') as HTMLElement;
    expect(pill).not.toBeNull();
    const directButtons = pill.querySelectorAll('button.bottom-tab-item');
    expect(directButtons.length).toBeLessThanOrEqual(5);

    const moreBtn = screen.getByTestId('bottom-tab-more');
    expect(moreBtn).toBeInTheDocument();
    expect(moreBtn).toHaveAttribute('aria-haspopup', 'menu');
    expect(moreBtn).not.toHaveClass('active');
    expect(moreBtn).not.toHaveAttribute('aria-current');

    await user.click(moreBtn);
    const moreMenu = await screen.findByTestId('bottom-tab-more-menu');
    expect(moreMenu).toBeInTheDocument();

    // Overflow items (e.g. Plugins) and global search live inside the Radix DropdownMenu
    const items = screen.getAllByRole('menuitem');
    expect(items.length).toBeGreaterThanOrEqual(2);

    // Arrow-key roving focus & Escape close work natively via Radix DropdownMenu
    await user.keyboard('{ArrowDown}');
    const focusedAfterDown = document.activeElement;
    expect(items).toContain(focusedAfterDown);
    await user.keyboard('{ArrowDown}');
    expect(items).toContain(document.activeElement);
    expect(document.activeElement).not.toBe(focusedAfterDown);

    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByTestId('bottom-tab-more-menu')).not.toBeInTheDocument());

    // Re-open and activate Search item
    await user.click(moreBtn);
    const searchItem = await screen.findByRole('menuitem', { name: /Commands & jump|命令与跳转/i });
    await user.click(searchItem);
    expect(openPalette).toHaveBeenCalledTimes(1);
  });

  it('highlights the More tab and sets aria-current="page" when the active route belongs to an overflow item', () => {
    renderWithProviders(<BottomTabBar />, {
      route: '/plugins',
    });

    const moreBtn = screen.getByTestId('bottom-tab-more');
    expect(moreBtn).toHaveClass('active');
    expect(moreBtn).toHaveAttribute('aria-current', 'page');
  });
});
