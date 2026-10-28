export type BrandMarkKind = 'langalpha' | 'everflow';

/**
 * Resolved from the `VITE_BRAND_NAME` build-time env var (defaults to `'LangAlpha'`).
 */
export const BRAND_NAME: string =
  (import.meta.env.VITE_BRAND_NAME as string | undefined)?.trim() || 'LangAlpha';

/**
 * Resolved from the `VITE_BRAND_MARK` build-time env var (defaults to `'langalpha'`).
 */
export const BRAND_MARK: BrandMarkKind =
  (import.meta.env.VITE_BRAND_MARK as string | undefined)?.trim() === 'everflow'
    ? 'everflow'
    : 'langalpha';

/**
 * Whether this is a Dalton build. Gates the product decisions the roadmap
 * keeps on Dalton's side of the fork (the onboarding entry among them).
 */
export const isDaltonBuild: boolean = BRAND_MARK === 'everflow';

/**
 * Resolved favicon path for the active brand mark and theme.
 */
export function faviconHref(theme: 'light' | 'dark'): string {
  if (BRAND_MARK === 'everflow') {
    return theme === 'light' ? '/everflow/favicon.svg' : '/everflow/favicon-dark.svg';
  }
  return theme === 'light' ? '/logo-favicon.svg' : '/logo-favicon-dark.svg';
}
