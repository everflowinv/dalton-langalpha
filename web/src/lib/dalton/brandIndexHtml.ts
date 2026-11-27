/**
 * Build-time HTML branding + Caddy OpenGraph template placeholder.
 *
 * Non-share routes serve the static `index.html` verbatim: the `<!--@ ... @-->`
 * directives stay harmless HTML comments, leaving the static brand `og:site_name`
 * and `og:title` active.
 *
 * Public share routes (`/s/<token>`, `/a/<token>`) run Caddy `templates` with
 * `between <!--@ @-->`: the `if false` branch drops the static OG tags and
 * `httpInclude` splices the backend `/api/v1/public/og-meta` fragment so only a
 * single `og:title` appears in `<head>`. If the backend is unavailable, Caddy's
 * `handle_errors` serves static `index.html` with status 200 as the brand fallback.
 */

export const OG_META_ENDPOINT = '/api/v1/public/og-meta';
export const OG_TEMPLATE_OPEN = '<!--@';
export const OG_TEMPLATE_CLOSE = '@-->';

export interface BrandIndexHtmlPlugin {
  name: string;
  transformIndexHtml(html: string): string;
}

function escapeHtmlAttr(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

/**
 * Resolve the static HTML brand name from Vite build env vars.
 * Returns `''` when neither `VITE_BRAND_NAME` nor `VITE_DALTON_PROFILE=core` is set
 * (leaving upstream builds unmodified).
 */
export function resolveBuildBrandName(env: Record<string, string | undefined>): string {
  const explicit = (env.VITE_BRAND_NAME || '').trim();
  if (explicit) return explicit;
  const profile = (env.VITE_DALTON_PROFILE || '').trim().toLowerCase();
  return profile === 'core' ? 'Dalton' : '';
}

/**
 * Build the Caddy-template-replaceable OpenGraph meta block for `<head>`.
 */
export function buildOgPlaceholderBlock(brandName: string): string {
  const safeBrand = escapeHtmlAttr(brandName);
  const staticOg = [
    `<meta property="og:site_name" content="${safeBrand}" />`,
    `    <meta property="og:title" content="${safeBrand}" />`,
  ].join('\n');
  const includeExpr = `httpInclude (printf "${OG_META_ENDPOINT}?path=%s" (urlquery .OriginalReq.URL.Path))`;
  return (
    `${OG_TEMPLATE_OPEN} if false ${OG_TEMPLATE_CLOSE}` +
    `${staticOg}` +
    `${OG_TEMPLATE_OPEN} else ${OG_TEMPLATE_CLOSE}` +
    `${OG_TEMPLATE_OPEN} ${includeExpr} ${OG_TEMPLATE_CLOSE}` +
    `${OG_TEMPLATE_OPEN} end ${OG_TEMPLATE_CLOSE}`
  );
}

/**
 * Transform `index.html` with brand `<title>`, favicon, OG placeholder block,
 * and `application-name` meta tag.
 */
export function transformBrandIndexHtml(
  html: string,
  env: Record<string, string | undefined>,
): string {
  const name = resolveBuildBrandName(env);
  if (!name) return html;
  const everflow = (env.VITE_BRAND_MARK || '').trim() === 'everflow';
  const safeName = escapeHtmlAttr(name);
  let out = html.replace(/<title>[^<]*<\/title>/, `<title>${safeName}</title>`);
  if (everflow) {
    out = out.replace('href="/logo-favicon.svg"', 'href="/everflow/favicon.svg"');
  }
  const meta = [
    buildOgPlaceholderBlock(name),
    `<meta name="application-name" content="${safeName}" />`,
  ].join('\n    ');
  return out.replace('</head>', `    ${meta}\n  </head>`);
}

/**
 * Vite plugin factory used by `web/vite.config.js`.
 */
export function brandIndexHtmlPlugin(
  env: Record<string, string | undefined>,
): BrandIndexHtmlPlugin | null {
  if (!resolveBuildBrandName(env)) return null;
  return {
    name: 'dalton-brand-index-html',
    transformIndexHtml(html: string) {
      return transformBrandIndexHtml(html, env);
    },
  };
}
