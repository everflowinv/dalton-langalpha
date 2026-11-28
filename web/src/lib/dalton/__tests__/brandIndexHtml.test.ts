// @vitest-environment node
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  OG_META_ENDPOINT,
  OG_TEMPLATE_CLOSE,
  OG_TEMPLATE_OPEN,
  brandIndexHtmlPlugin,
  transformBrandIndexHtml,
} from '../brandIndexHtml';

const REAL_INDEX_HTML = readFileSync(resolve(__dirname, '../../../../index.html'), 'utf8');

function stripHtmlComments(html: string): string {
  return html.replace(/<!--[\s\S]*?-->/g, '');
}

/**
 * Minimal faithful simulator of Caddy's `templates { between <!--@ @--> }`
 * for the `if false ... else ... httpInclude ... end` block emitted by brandIndexHtml.
 */
function simulateCaddyShareRender(html: string, backendFragment: string): string {
  const pattern = new RegExp(
    `${OG_TEMPLATE_OPEN}\\s*if false\\s*${OG_TEMPLATE_CLOSE}` +
      `[\\s\\S]*?` +
      `${OG_TEMPLATE_OPEN}\\s*else\\s*${OG_TEMPLATE_CLOSE}` +
      `${OG_TEMPLATE_OPEN}\\s*httpInclude \\(printf "${OG_META_ENDPOINT}\\?path=%s" \\(urlquery \\.OriginalReq\\.URL\\.Path\\)\\)\\s*${OG_TEMPLATE_CLOSE}` +
      `${OG_TEMPLATE_OPEN}\\s*end\\s*${OG_TEMPLATE_CLOSE}`,
  );
  expect(html).toMatch(pattern);
  return html.replace(pattern, backendFragment);
}

describe('brandIndexHtml', () => {
  it('source index.html contains no accidental template delimiters', () => {
    expect(REAL_INDEX_HTML).not.toContain(OG_TEMPLATE_OPEN);
    expect(REAL_INDEX_HTML).not.toContain(OG_TEMPLATE_CLOSE);
  });

  it('wraps OG tags in a Caddy template placeholder that falls back to brand on static routes', () => {
    const plugin = brandIndexHtmlPlugin({
      VITE_BRAND_NAME: 'Everflow',
      VITE_BRAND_MARK: 'everflow',
    });
    expect(plugin).not.toBeNull();
    expect(plugin?.name).toBe('dalton-brand-index-html');

    const built = plugin!.transformIndexHtml(REAL_INDEX_HTML);
    expect(built).toContain('<title>Everflow</title>');
    expect(built).toContain('href="/everflow/favicon.svg"');
    expect(built).toContain('<meta name="application-name" content="Everflow" />');

    // Non-share route (served statically without Caddy templates): HTML comments
    // are ignored and exactly one static brand og:title / og:site_name remains.
    const staticParsed = stripHtmlComments(built);
    const ogTitles = staticParsed.match(/<meta\s+property="og:title"[^>]*>/g) ?? [];
    const ogSites = staticParsed.match(/<meta\s+property="og:site_name"[^>]*>/g) ?? [];
    expect(ogTitles).toEqual(['<meta property="og:title" content="Everflow" />']);
    expect(ogSites).toEqual(['<meta property="og:site_name" content="Everflow" />']);
    expect(staticParsed).not.toContain('httpInclude');
  });

  it('produces exactly one og:title on share routes after Caddy template rendering', () => {
    const built = transformBrandIndexHtml(REAL_INDEX_HTML, {
      VITE_BRAND_NAME: 'Everflow',
      VITE_BRAND_MARK: 'everflow',
    });
    const fragment = [
      '<meta property="og:site_name" content="Everflow" />',
      '<meta property="og:title" content="英伟达 Q3 财报深度分析" />',
      '<meta property="og:description" content="来自 Everflow 的分享" />',
    ].join('\n');

    const rendered = simulateCaddyShareRender(built, fragment);
    const ogTitles = rendered.match(/<meta\s+property="og:title"[^>]*>/g) ?? [];
    const ogSites = rendered.match(/<meta\s+property="og:site_name"[^>]*>/g) ?? [];
    const ogDescs = rendered.match(/<meta\s+property="og:description"[^>]*>/g) ?? [];

    expect(ogTitles).toEqual(['<meta property="og:title" content="英伟达 Q3 财报深度分析" />']);
    expect(ogSites).toEqual(['<meta property="og:site_name" content="Everflow" />']);
    expect(ogDescs).toEqual(['<meta property="og:description" content="来自 Everflow 的分享" />']);
    expect(rendered).not.toContain(OG_TEMPLATE_OPEN);
    expect(rendered).not.toContain(OG_TEMPLATE_CLOSE);
  });

  it('defaults to Dalton on core profile builds without hardcoding Everflow', () => {
    const built = transformBrandIndexHtml(REAL_INDEX_HTML, {
      VITE_DALTON_PROFILE: 'core',
    });
    const staticParsed = stripHtmlComments(built);
    expect(built).toContain('<title>Dalton</title>');
    expect(built).toContain('href="/logo-favicon.svg"');
    expect(staticParsed).toContain('<meta property="og:site_name" content="Dalton" />');
    expect(staticParsed).toContain('<meta property="og:title" content="Dalton" />');
    const headSection = built.slice(0, built.indexOf('</head>'));
    expect(headSection).not.toContain('Everflow');
  });

  it('returns null and leaves HTML untouched for upstream builds with no brand env', () => {
    expect(brandIndexHtmlPlugin({})).toBeNull();
    expect(transformBrandIndexHtml(REAL_INDEX_HTML, {})).toBe(REAL_INDEX_HTML);
  });

  it('HTML-escapes special characters in brand name', () => {
    const built = transformBrandIndexHtml(REAL_INDEX_HTML, {
      VITE_BRAND_NAME: 'A & "B" <C>',
    });
    expect(built).toContain('<title>A &amp; &quot;B&quot; &lt;C&gt;</title>');
    expect(built).toContain('content="A &amp; &quot;B&quot; &lt;C&gt;"');
  });
});
