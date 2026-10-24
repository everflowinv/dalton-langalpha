import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useUser } from './useUser';
import { setLocaleCookie, switchLocale } from '../lib/locale';
// DALTON-HOOK(i18n): the account's users.locale outranks the browser cookie; no saved value → zh-CN
import { daltonUserLocale } from '../lib/dalton/userLocale';

/**
 * Apply the user's DB locale once /users/me is in. Dalton: the account value
 * wins over the `locale` cookie, so the language follows the user across
 * browsers/devices, and a user with no saved value gets zh-CN
 * (`daltonUserLocale`). Latched per user so a later /users/me refetch can't
 * clobber a value the user just picked. Writing the cookie lets the next cold
 * load on this browser paint in that language from the first frame.
 */
export function useSyncUserLocale() {
  const { user } = useUser();
  const { i18n } = useTranslation();
  // DALTON-HOOK(i18n): latch per user id (an account switch re-syncs), apply even when users.locale is NULL
  const synced = useRef<string | null>(null);
  const userId = user ? (user.user_id ?? '') : null;
  const storedLocale = user?.locale;

  useEffect(() => {
    if (!user || synced.current === userId) return;
    synced.current = userId; // latch even if no-op, so a later refetch can't trigger sync
    const target = daltonUserLocale(storedLocale);
    if (i18n.language !== target) void switchLocale(i18n, target);
    setLocaleCookie(target);
  }, [user, userId, storedLocale, i18n]);
}
