import { useMemo } from 'react';
import { NAV_ITEMS, SETTINGS_ITEM, type NavItem } from '@/components/nav/navItems';
import { useCapabilities } from '@/hooks/useCapabilities';

const PRIMARY_WITH_DASHBOARD = new Set(['/dashboard', '/chat', '/market', '/monitors']);
const PRIMARY_DALTON_MODE = new Set(['/chat', '/market', '/monitors', '/settings']);
const MARKET_NAV_ITEM = NAV_ITEMS.find((item) => item.key === '/market');

export interface MobileBottomNavPartition {
  primaryItems: NavItem[];
  overflowItems: NavItem[];
}

/**
 * Partition mobile bottom-bar items into 4 primary tabs + "More" overflow.
 *
 * - Reads market capability from `useCapabilities()` rather than relying on the
 *   build-time `VITE_DALTON_MODE` filter in `useNavItems()`.
 * - Standard mode (dashboard present) + market enabled: `[Dashboard, Chat, Market, Monitors]`
 * - Dalton mode (dashboard absent) + market enabled: `[Chat, Market, Monitors, Settings]`
 * - Market capability unavailable: falls back to `/automations` in place of `/market`
 *   while preserving 4 primary tabs in both modes.
 */
export function partitionMobileBottomNav(
  navItems: readonly NavItem[],
  options: { marketEnabled: boolean },
): MobileBottomNavPartition {
  const { marketEnabled } = options;
  const hasMarketInInput = navItems.some((item) => item.key === '/market');
  const menuItems: NavItem[] = [];
  let seenMarket = false;

  for (const item of navItems) {
    if (item.key === '/market') {
      if (!marketEnabled) continue;
      seenMarket = true;
    }
    menuItems.push(item);
    if (item.key === '/chat' && marketEnabled && !seenMarket && !hasMarketInInput && MARKET_NAV_ITEM) {
      menuItems.push(MARKET_NAV_ITEM);
      seenMarket = true;
    }
  }
  menuItems.push(SETTINGS_ITEM);

  const hasDashboard = menuItems.some((item) => item.key === '/dashboard');
  const hasMarket = marketEnabled && menuItems.some((item) => item.key === '/market');
  const basePrimaryKeys = hasDashboard ? PRIMARY_WITH_DASHBOARD : PRIMARY_DALTON_MODE;
  const primaryKeys = hasMarket ? basePrimaryKeys : new Set([...basePrimaryKeys, '/automations']);

  const primaryItems: NavItem[] = [];
  const overflowItems: NavItem[] = [];
  for (const item of menuItems) {
    if (primaryKeys.has(item.key) && primaryItems.length < 4) {
      primaryItems.push(item);
    } else {
      overflowItems.push(item);
    }
  }

  return { primaryItems, overflowItems };
}

export function useMobileBottomNavItems(navItems: readonly NavItem[]): MobileBottomNavPartition {
  const { marketEnabled } = useCapabilities();
  const isMarketEnabled = marketEnabled !== false;
  return useMemo(
    () => partitionMobileBottomNav(navItems, { marketEnabled: isMarketEnabled }),
    [navItems, isMarketEnabled],
  );
}
