import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createTestQueryClient, renderWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import type { CapabilitiesApiResponse } from '@/hooks/useCapabilities';
import BottomTabBar from '../BottomTabBar';

const openPalette = vi.fn<(...args: unknown[]) => void>();
const preloadPalette = vi.fn<() => void>();

vi.mock('@/components/CommandPalette/CommandPaletteProvider', () => ({
  useCommandPalette: () => ({
    open: openPalette,
    preload: preloadPalette,
  }),
}));

function makeClient(options: { marketEnabled?: boolean; profile?: 'core' | 'everflow' } = {}) {
  const { marketEnabled = true, profile = 'everflow' } = options;
  const client = createTestQueryClient();
  client.setQueryData(queryKeys.features.list(), [
    { key: 'org_library', enabled: true },
    { key: 'drive', enabled: false },
  ]);
  const capsPayload: CapabilitiesApiResponse = {
    profile,
    capabilities: profile === 'everflow' ? ['company_wiki', 'data_jobs'] : ['company_wiki'],
    market_enabled: marketEnabled,
  };
  client.setQueryData(queryKeys.dalton.capabilities(), capsPayload);
  return client;
}

function getPrimaryLabels(container: HTMLElement): string[] {
  const pill = container.querySelector('.bottom-tab-bar-pill') as HTMLElement;
  const buttons = Array.from(pill.querySelectorAll<HTMLButtonElement>('button.bottom-tab-item'));
  return buttons
    .filter((btn) => btn.getAttribute('data-testid') !== 'bottom-tab-more')
    .map((btn) => btn.getAttribute('aria-label') ?? '');
}

describe('BottomTabBar mobile overflow menu', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    openPalette.mockClear();
    preloadPalette.mockClear();
  });

  it('caps direct bottom-bar buttons to <= 5, uses Radix DropdownMenu with keyboard navigation, and opens Search', async () => {
    const user = userEvent.setup();
    const { container } = renderWithProviders(<BottomTabBar />, {
      route: '/chat',
      queryClient: makeClient({ marketEnabled: true }),
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
      queryClient: makeClient({ marketEnabled: true }),
    });

    const moreBtn = screen.getByTestId('bottom-tab-more');
    expect(moreBtn).toHaveClass('active');
    expect(moreBtn).toHaveAttribute('aria-current', 'page');
  });

  it('uses real useNavItems + useCapabilities chain for standard mode: [Dashboard, Chat, Market, Monitors] and Automations in More', async () => {
    const user = userEvent.setup();
    const { container } = renderWithProviders(<BottomTabBar />, {
      route: '/automations',
      queryClient: makeClient({ marketEnabled: true }),
    });

    // Standard mode (dashboard + market capability): [Dashboard, Chat Agent, Market View, Monitors]
    expect(getPrimaryLabels(container)).toEqual([
      'Dashboard',
      'Chat Agent',
      'Market View',
      'Monitors',
    ]);

    // Automations is in More, so active route /automations highlights the More trigger
    const moreBtn = screen.getByTestId('bottom-tab-more');
    expect(moreBtn).toHaveClass('active');
    expect(moreBtn).toHaveAttribute('aria-current', 'page');

    await user.click(moreBtn);
    expect(await screen.findByRole('menuitem', { name: 'Automations' })).toBeInTheDocument();
  });

  it('uses real useNavItems + useCapabilities chain for Dalton mode (VITE_DALTON_MODE=1): [Chat, Market, Monitors, Settings] when market capability is enabled', async () => {
    const user = userEvent.setup();
    vi.stubEnv('VITE_DALTON_MODE', '1');
    const { container } = renderWithProviders(<BottomTabBar />, {
      route: '/chat',
      queryClient: makeClient({ marketEnabled: true }),
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

  it('falls back to Automations in both modes (keeping 4 primary tabs) when useCapabilities reports market capability unavailable', () => {
    // Case 1: Standard mode (dashboard present) + market_enabled: false
    const { container: withDashboardContainer, unmount } = renderWithProviders(<BottomTabBar />, {
      route: '/chat',
      queryClient: makeClient({ marketEnabled: false }),
    });
    expect(getPrimaryLabels(withDashboardContainer)).toEqual([
      'Dashboard',
      'Chat Agent',
      'Automations',
      'Monitors',
    ]);
    expect(screen.queryByRole('button', { name: 'Market View' })).not.toBeInTheDocument();
    unmount();

    // Case 2: Dalton mode (VITE_DALTON_MODE=1, no dashboard) + market_enabled: false
    vi.stubEnv('VITE_DALTON_MODE', '1');
    const { container: daltonModeContainer } = renderWithProviders(<BottomTabBar />, {
      route: '/chat',
      queryClient: makeClient({ marketEnabled: false }),
    });
    expect(getPrimaryLabels(daltonModeContainer)).toEqual([
      'Chat Agent',
      'Automations',
      'Monitors',
      'Settings',
    ]);
    expect(screen.queryByRole('button', { name: 'Market View' })).not.toBeInTheDocument();
  });
});
