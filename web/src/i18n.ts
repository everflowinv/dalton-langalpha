import i18n, { type BackendModule, type Resource, type ResourceKey } from 'i18next';
import { initReactI18next } from 'react-i18next';
import { BRAND_NAME } from './config/brand'; // DALTON-HOOK(brand-name)
import { detectLocale, isSupported, type Locale } from './lib/locale';

// Locale resolution (cookie → browser language → English) lives in ./lib/locale,
// shared with the cookie helpers that components use. The cross-tab `storage`
// listener was removed along with localStorage-based locale: locale now rides a
// shared cookie (readable server-side, unlike localStorage); other tabs adopt a
// change on their next navigation.
//
// The document's language follows the app's, so the browser shapes CJK text
// as Chinese and `:lang()` rules can key on it. Bound before init, which
// reports the first language through the same event.
i18n.on('languageChanged', (lng) => {
  if (typeof document !== 'undefined') document.documentElement.lang = lng;
});

// One chunk per locale. Bundled, the catalogs were about 100 kB gz of the entry,
// half of it in a language the visitor never sees. The active one still does
// not wait for the entry: scripts/locale-preload.ts has index.html start
// fetching it alongside.
const CATALOGS: Record<Locale, () => Promise<{ default: ResourceKey }>> = {
  'en-US': () => import('./locales/en-US.json'),
  'zh-CN': () => import('./locales/zh-CN.json'),
};

const catalogBackend: BackendModule = {
  type: 'backend',
  init() {},
  read(lng, _ns, callback) {
    // `false` tells i18next not to retry. The usual cause is a build asset the
    // server no longer has, and only a reload fixes that.
    if (!isSupported(lng)) return callback(new Error(`no catalog for ${lng}`), false);
    CATALOGS[lng]().then(
      (m) => callback(null, m.default),
      (err: unknown) => callback(err instanceof Error ? err : String(err), false),
    );
  },
};

/**
 * Resolves once the active locale's catalog is in, and rejects if it could not
 * be loaded. `resources` bypasses the loader entirely, which is how the unit
 * suite gets both catalogs synchronously.
 */
export async function initI18n(resources?: Resource): Promise<void> {
  const lng = detectLocale();
  await i18n
    .use(initReactI18next)
    .use(catalogBackend)
    .init({
      lng,
      resources,
      // No runtime fallback language: it would be a second catalog on every
      // zh-CN visit, and zh-CN resolves every en-US key, which
      // locales/__tests__/keys.test.ts pins.
      fallbackLng: false,
      // Only the exact code. The default also asks the loader for `zh` and
      // `en`, which have no catalogs.
      load: 'currentOnly',
      interpolation: { escapeValue: false, defaultVariables: { brand: BRAND_NAME } }, // DALTON-HOOK(brand-name)
    });
  if (!i18n.hasResourceBundle(lng, 'translation')) {
    throw new Error(`[i18n] the ${lng} catalog did not load`);
  }
}

export default i18n;
