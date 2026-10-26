/**
 * Where a build sends a user who needs more than the app gives them: the plan
 * page of the account portal, the first support address, and the privacy
 * policy. All are deployment configuration, so each is null when the build
 * sets none, and the plan page only exists in platform mode.
 */
import { isPlatformMode } from './hostMode';

export const PLAN_URL: string | null = isPlatformMode
  ? `${import.meta.env.VITE_PLATFORM_URL || '/account'}/plans`
  : null;

export const SUPPORT_EMAIL: string | null =
  (import.meta.env.VITE_CONTACT_EMAILS ?? '')
    .split(',')
    .map((email) => email.trim())
    .find(Boolean) ?? null;

export const PRIVACY_URL: string | null = import.meta.env.VITE_PRIVACY_URL || null;
