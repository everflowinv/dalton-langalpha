export type BrandMarkKind = 'langalpha' | 'everflow';
export type DaltonProfile = 'core' | 'everflow';

/**
 * Resolved from the `VITE_DALTON_PROFILE` build-time env var (defaults to `'everflow'`).
 */
export const DALTON_PROFILE: DaltonProfile =
  (import.meta.env.VITE_DALTON_PROFILE as string | undefined)?.trim().toLowerCase() === 'core'
    ? 'core'
    : 'everflow';

export const isCoreProfileBuild: boolean = DALTON_PROFILE === 'core';

/**
 * Resolved from the `VITE_BRAND_NAME` build-time env var
 * (defaults to `'Dalton'` in core profile builds, `'LangAlpha'` otherwise).
 */
export const BRAND_NAME: string =
  (import.meta.env.VITE_BRAND_NAME as string | undefined)?.trim() ||
  (isCoreProfileBuild ? 'Dalton' : 'LangAlpha');

/**
 * Resolved from the `VITE_BRAND_MARK` build-time env var (defaults to `'langalpha'`).
 */
export const BRAND_MARK: BrandMarkKind =
  (import.meta.env.VITE_BRAND_MARK as string | undefined)?.trim() === 'everflow'
    ? 'everflow'
    : 'langalpha';

function resolveIsDaltonBuild(): boolean {
  const raw = (import.meta.env.VITE_DALTON_UI as string | undefined)?.trim().toLowerCase();
  if (raw === 'true' || raw === '1' || raw === 'yes' || raw === 'on') return true;
  if (raw === 'false' || raw === '0' || raw === 'no' || raw === 'off') return false;
  return BRAND_MARK === 'everflow' || isCoreProfileBuild;
}

/**
 * Whether this is a Dalton build. Controlled by `VITE_DALTON_UI` with backward-compatible
 * fallback to `BRAND_MARK === 'everflow'` or `VITE_DALTON_PROFILE === 'core'`.
 */
export const isDaltonBuild: boolean = resolveIsDaltonBuild();

/**
 * Resolved favicon path for the active brand mark and theme.
 */
export function faviconHref(theme: 'light' | 'dark'): string {
  if (BRAND_MARK === 'everflow') {
    return theme === 'light' ? '/everflow/favicon.svg' : '/everflow/favicon-dark.svg';
  }
  return theme === 'light' ? '/logo-favicon.svg' : '/logo-favicon-dark.svg';
}
