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

const _rawDaltonUi = (import.meta.env.VITE_DALTON_UI as string | undefined)?.trim().toLowerCase();

/**
 * Whether this is a Dalton UI build. Controlled explicitly by `VITE_DALTON_UI`
 * (`true`/`1` enables, `false`/`0` disables); when unset, preserves the legacy
 * `BRAND_MARK === 'everflow'` default so Everflow builds stay identical while
 * neutral-brand Standalone Core builds decouple Dalton UI from the brand mark.
 */
export const isDaltonBuild: boolean =
  _rawDaltonUi === 'true' || _rawDaltonUi === '1'
    ? true
    : _rawDaltonUi === 'false' || _rawDaltonUi === '0'
      ? false
      : BRAND_MARK === 'everflow';

/**
 * Resolved favicon path for the active brand mark and theme.
 */
export function faviconHref(theme: 'light' | 'dark'): string {
  if (BRAND_MARK === 'everflow') {
    return theme === 'light' ? '/everflow/favicon.svg' : '/everflow/favicon-dark.svg';
  }
  return theme === 'light' ? '/logo-favicon.svg' : '/logo-favicon-dark.svg';
}
