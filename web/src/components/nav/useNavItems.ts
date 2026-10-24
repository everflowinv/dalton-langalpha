import { useMemo } from 'react';
import { NAV_ITEMS, type NavItem } from './navItems';

/**
 * The primary nav items this user can see, in NAV_ITEMS order.
 */
export function useNavItems(): NavItem[] {
  // DALTON-HOOK(hide-trading): do not call useOrdersVisible() and never show 'orders' nav item
  const daltonMode = Boolean(import.meta.env.VITE_DALTON_MODE); // DALTON-HOOK(dalton-nav)
  return useMemo(
    () =>
      NAV_ITEMS.filter((item) => {
        if (item.requires === 'orders') return false; // DALTON-HOOK(hide-trading)
        if (item.requires === 'market' || item.requires === 'dashboard') return !daltonMode; // DALTON-HOOK(dalton-nav)
        return true;
      }),
    [daltonMode],
  );
}
