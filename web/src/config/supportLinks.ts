/**
 * Where a build sends a user who needs more than the app gives them: the plan
 * page of the account portal, the first support address, and the privacy
 * policy. All are deployment configuration, so each is null when the build
 * sets none, and the plan page only exists in platform mode.
 */
import { isDaltonBuild } from './brand'; // DALTON-HOOK(help)
import { isPlatformMode } from './hostMode';

/**
 * The account portal's base, from `VITE_PLATFORM_URL`: a path on this origin or
 * an absolute URL, with no trailing slash. Null outside platform mode, where the
 * portal does not exist even if the variable is set.
 */
export const PLATFORM_URL: string | null = isPlatformMode
  ? import.meta.env.VITE_PLATFORM_URL || '/account'
  : null;

/** A page of the account portal, by its path (`plans`, `integrations`); null
 *  where there is no portal. */
export function consoleUrl(path: string): string | null {
  return PLATFORM_URL && `${PLATFORM_URL}/${path}`;
}

export const PLAN_URL: string | null = consoleUrl('plans');

export const INTEGRATIONS_URL: string | null = consoleUrl('integrations');

/** The hosted onboarding questions; null where this app asks them. */
export const ONBOARDING_USE_URL: string | null = consoleUrl('onboarding/use');

export const SUPPORT_EMAIL: string | null =
  (import.meta.env.VITE_CONTACT_EMAILS ?? '')
    .split(',')
    .map((email) => email.trim())
    .find(Boolean) ?? null;

// DALTON-HOOK(help): Dalton builds omit the desktop download and point docs to self-hosted /help
/** Where to get the desktop app. Null in Dalton builds where no desktop client is offered. */
export const DESKTOP_DOWNLOAD_URL: string | null = isDaltonBuild ? null : 'https://langalpha.ai/download';

/** The documentation site: getting started, self-hosting, the CLI and the concepts behind the app. */
export const DOCS_URL: string = isDaltonBuild ? '/help' : 'https://docs.langalpha.ai';

/** The portal's support page, where the contact channels live behind sign-in. Null unless the build sets
 *  `VITE_SUPPORT_CONTACT_ENABLED=true` in platform mode, so the Help menu offers it only where a channel is open. */
export const SUPPORT_URL: string | null =
  import.meta.env.VITE_SUPPORT_CONTACT_ENABLED === 'true' ? consoleUrl('support/contact') : null;

export const PRIVACY_URL: string | null = import.meta.env.VITE_PRIVACY_URL || null;
