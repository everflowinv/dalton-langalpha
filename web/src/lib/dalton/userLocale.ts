import { isSupported, type Locale } from '../locale';

/**
 * Dalton's language for a signed-in user who has never picked one
 * (`users.locale` is NULL). The server applies the same default to the agent's
 * view of the profile (`src/dalton/user_locale.py`); keep the two in step.
 */
export const DALTON_DEFAULT_USER_LOCALE: Locale = 'zh-CN';

/**
 * The language a signed-in user's UI should be in: their saved `users.locale`
 * when it names a catalog we ship, else the Dalton default. The account value
 * outranks the browser's `locale` cookie, so the choice follows the user
 * across browsers and devices.
 */
export function daltonUserLocale(stored: string | null | undefined): Locale {
  return isSupported(stored) ? stored : DALTON_DEFAULT_USER_LOCALE;
}
