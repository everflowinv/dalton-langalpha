import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { NAV_ITEMS, type NavItem } from '../../nav/navItems';
import type { useNavItems as useNavItemsFn } from '../../nav/useNavItems';
import BottomTabBar from '../BottomTabBar';

const openPalette = vi.fn<(...args: unknown[]) => void>();
const preloadPalette = vi.fn<() => void>();
let navItemsOverride: NavItem[] | null = null;

vi.mock('../../nav/useNavItems', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../nav/useNavItems')>();
  const mockUseNavItems = vi.fn<typeof useNavItemsFn>(() => navItemsOverride ?? actual.useNavItems());
  return {
    ...actual,
    useNavItems: mockUseNavItems,
  };
});

vi.mock('@/components/CommandPalette/CommandPaletteProvider', () => ({
  useCommandPalette: () => ({
    open: openPalette,
    preload: preloadPalette,
  }),
}));

vi.mock('@/hooks/useFeatures', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useFeatures')>();
  return {
    ...actual,
    useFeatures: () => ({
      data: [
        { key: 'org_library', enabled: true },
        { key: 'drive', enabled: false },
      ],
    }),
  };
});

function getPrimaryLabels(container: HTMLElement): string[] {
  const pill = container.querySelector('.bottom-tab-bar-pill') as HTMLElement;
  const buttons = Array.from(pill.querySelectorAll<HTMLButtonElement>('button.bottom-tab-item'));
  return buttons
    .filter((btn) => btn.getAttribute('data-testid') !== 'bottom-tab-more')
    .map((btn) => btn.getAttribute('aria-label') ?? '');
}

describe('BottomTabBar mobile overflow menu', () => {
  afterEach(() => {
    navItemsOverride = null;
    vi.unstubAllEnvs();
  });

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
    expect(screen.queryByRole('menuitem', { name: /^Drive$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Drive$/i })).not.toBeInTheDocument();

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

  it('places Market in the primary bar instead of Automations and moves Automations to More in standard mode', async () => {
    const user = userEvent.setup();
    const { container } = renderWithProviders(<BottomTabBar />, {
      route: '/automations',
    });

    // Standard mode (dashboard + market): [Dashboard, Chat Agent, Market View, Monitors]
    expect(getPrimaryLabels(container)).toEqual([
      'Dashboard',
      'Chat Agent',
      'Market View',
      'Monitors',
    ]);

    // Automations is now in More, so active route /automations highlights the More trigger
    const moreBtn = screen.getByTestId('bottom-tab-more');
    expect(moreBtn).toHaveClass('active');
    expect(moreBtn).toHaveAttribute('aria-current', 'page');

    await user.click(moreBtn);
    expect(await screen.findByRole('menuitem', { name: 'Automations' })).toBeInTheDocument();
  });

  it('uses [Chat, Market, Monitors, Settings] in Dalton mode when market capability is available', async () => {
    const user = userEvent.setup();
    navItemsOverride = NAV_ITEMS.filter(
      (item) => item.key !== '/dashboard' && item.key !== '/drive' && item.key !== '/library',
    );
    const { container } = renderWithProviders(<BottomTabBar />, {
      route: '/chat',
    });

    expect(getPrimaryLabels(container)).toEqual([
      'Chat Agent',
      'Market View',
      'Monitors',
      'Settings',
    ]);

    await user.click(screen.getByTestId('bottom-tab-more'));
    expect(await screen.findByRole('menuitem', { name: 'Automations' })).toBeInTheDocument();
  });

  it('falls back to Automations in the primary bar (keeping 4 primary tabs) when market capability is unavailable', () => {
    // Case 1: dashboard present, requires: 'market' unsatisfied
    navItemsOverride = NAV_ITEMS.filter(
      (item) => item.requires !== 'market' && item.requires !== 'drive' && item.requires !== 'org_library',
    );
    const { container: withDashboardContainer, unmount } = renderWithProviders(<BottomTabBar />, {
      route: '/chat',
    });
    expect(getPrimaryLabels(withDashboardContainer)).toEqual([
      'Dashboard',
      'Chat Agent',
      'Automations',
      'Monitors',
    ]);
    unmount();

    // Case 2: VITE_DALTON_MODE=1 (neither dashboard nor market available)
    navItemsOverride = null;
    vi.stubEnv('VITE_DALTON_MODE', '1');
    const { container: daltonModeContainer } = renderWithProviders(<BottomTabBar />, {
      route: '/chat',
    });
    expect(getPrimaryLabels(daltonModeContainer)).toEqual([
      'Chat Agent',
      'Automations',
      'Monitors',
      'Settings',
    ]);
  });
});
