import { useMemo } from 'react';
import { useFeatureEnabled } from '@/hooks/useFeatures';
import { NAV_ITEMS, type NavItem } from './navItems';

/**
 * The primary nav items this user can see, in NAV_ITEMS order.
 *
 * The library is offered where the organization library is on for the
 * account, read off the flags, which fail closed while they load.
 */
export function useNavItems(): NavItem[] {
  // DALTON-HOOK(hide-trading): do not call useOrdersVisible() and never show 'orders' nav item
  const daltonMode = Boolean(import.meta.env.VITE_DALTON_MODE); // DALTON-HOOK(dalton-nav)
  const libraryOn = useFeatureEnabled('org_library');
  return useMemo(
    () =>
      NAV_ITEMS.filter((item) => {
        if (item.requires === 'orders') return false; // DALTON-HOOK(hide-trading)
        if (item.requires === 'market' || item.requires === 'dashboard') return !daltonMode; // DALTON-HOOK(dalton-nav)
        if (item.requires === 'org_library') return libraryOn;
        return true;
      }),
    [daltonMode, libraryOn],
  );
}
