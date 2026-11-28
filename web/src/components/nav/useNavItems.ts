import { useMemo } from 'react';
import { isFeatureEnabled, useFeatures } from '@/hooks/useFeatures';
import { NAV_ITEMS, NEW_CHAT_ITEM, type NavItem } from './navItems';

/**
 * The primary nav items this user can see, in NAV_ITEMS order.
 *
 * An item that names a feature (the Drive, the organization library) is
 * offered where that feature is on, read off the flags, which fail closed
 * while they load.
 */
export function useNavItems(): NavItem[] {
  // DALTON-HOOK(hide-trading): do not call useOrdersVisible() and never show 'orders' nav item
  const daltonMode = Boolean(import.meta.env.VITE_DALTON_MODE); // DALTON-HOOK(dalton-nav)
  const { data: features } = useFeatures();
  return useMemo(
    () =>
      NAV_ITEMS.filter((item) => {
        if (!item.requires) return true;
        if (item.requires === 'orders') return false; // DALTON-HOOK(hide-trading)
        if (item.requires === 'market' || item.requires === 'dashboard') return !daltonMode; // DALTON-HOOK(dalton-nav)
        return isFeatureEnabled(features, item.requires);
      }),
    [daltonMode, features],
  );
}

/**
 * The places the desktop offers, in one order for the sidebar and the new-tab
 * menu: a new chat first, where the chat item would be, then the other pages.
 */
export function useDesktopNavItems(): NavItem[] {
  const items = useNavItems();
  return useMemo(() => [NEW_CHAT_ITEM, ...items.filter((item) => item.key !== NEW_CHAT_ITEM.key)], [items]);
}
