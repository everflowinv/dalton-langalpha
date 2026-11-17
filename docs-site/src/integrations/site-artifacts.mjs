import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const FRONTMATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;

function parseSimpleFrontmatter(raw) {
  const match = raw.match(FRONTMATTER_RE);
  if (!match) return { meta: {}, body: raw };
  const meta = {};
  for (const line of match[1].split(/\r?\n/)) {
    const kv = line.match(/^([A-Za-z0-9_]+)\s*:\s*(.+)$/);
    if (!kv) continue;
    let val = kv[2].trim();
    if ((val.startsWith("'") && val.endsWith("'")) || (val.startsWith('"') && val.endsWith('"'))) {
      val = val.slice(1, -1);
    }
    meta[kv[1]] = val;
  }
  return { meta, body: raw.slice(match[0].length) };
}

function mdxToPlainMarkdown(title, description, body) {
  let text = body
    // Remove ESM imports and decorative UI tags used by MDX components
    .replace(/^import\s+[\s\S]*?from\s+['"][^'"]+['"];?\s*$/gm, '')
    .replace(/<(?:Icon|HeroImageSlot)\b[^>]*\/>\s*/g, '')
    // Convert LinkCard / Card tags to readable markdown bullets
    .replace(/<LinkCard\s+[^>]*title=["']([^"']+)["'][^>]*description=["']([^"']+)["'][^>]*href=["']([^"']+)["'][^>]*\/?>/g, '- [$1]($3)：$2')
    .replace(/<LinkCard\s+[^>]*title=["']([^"']+)["'][^>]*href=["']([^"']+)["'][^>]*\/?>/g, '- [$1]($2)')
    .replace(/<Card\s+[^>]*title=["']([^"']+)["'][^>]*>/g, '### $1\n')
    .replace(/<\/Card>/g, '')
    .replace(/<\/?CardGrid[^>]*>/g, '')
    .replace(/<\/?Steps[^>]*>/g, '')
    .replace(/<\/?Tabs[^>]*>/g, '')
    .replace(/<TabItem\s+[^>]*label=["']([^"']+)["'][^>]*>/g, '#### $1\n')
    .replace(/<\/TabItem>/g, '')
    // Clean Starlight ::: admonitions slightly while keeping readability
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  const header = [`# ${title}`];
  if (description) header.push('', `> ${description}`);
  return `${header.join('\n')}\n\n${text}\n`;
}

async function walkDocs(dir) {
  const entries = await fs.readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await walkDocs(full)));
    } else if (/\.(md|mdx)$/i.test(entry.name) && !entry.name.startsWith('_')) {
      files.push(full);
    }
  }
  return files.sort();
}

function escapeXml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

function extractWhatsNewItems(body) {
  const sections = body.split(/\n(?=##\s+)/);
  const items = [];
  for (const sec of sections) {
    const headingMatch = sec.match(/^##\s+(.+)/);
    if (!headingMatch) continue;
    const heading = headingMatch[1].trim();
    const dateMatch = heading.match(/(\d{4}-\d{2}-\d{2})/);
    const pubDate = dateMatch ? new Date(`${dateMatch[1]}T00:00:00+08:00`).toUTCString() : new Date().toUTCString();
    const content = sec.replace(/^##\s+.+\n?/, '').trim();
    const anchor = heading
      .toLowerCase()
      .replace(/[^\p{L}\p{N}\s-]/gu, '')
      .trim()
      .replace(/\s+/g, '-');
    items.push({ heading, pubDate, content, anchor });
  }
  return items;
}

function resolveSiteOrigin(explicitOrigin) {
  const raw = (
    explicitOrigin ||
    process.env.DOCS_SITE_ORIGIN ||
    process.env.DALTON_FRONTEND_URL ||
    process.env.PUBLIC_APP_URL ||
    process.env.FRONTEND_URL ||
    ''
  )
    .trim()
    .replace(/\/+$/, '');
  return /^https?:\/\//i.test(raw) ? raw : '';
}

async function collectFrontendHelpLinks(repoRoot, basePath) {
  const supportLinksPath = path.join(repoRoot, 'web', 'src', 'config', 'supportLinks.ts');
  const claudeCodePath = path.join(repoRoot, 'web', 'src', 'lib', 'claudeCode.ts');
  const accountMenuPath = path.join(repoRoot, 'web', 'src', 'components', 'Sidebar', 'AccountMenu.tsx');

  const [supportLinksSrc, claudeCodeSrc, accountMenuSrc] = await Promise.all([
    fs.readFile(supportLinksPath, 'utf-8'),
    fs.readFile(claudeCodePath, 'utf-8'),
    fs.readFile(accountMenuPath, 'utf-8'),
  ]);

  const docsUrlMatch = supportLinksSrc.match(/DOCS_URL\b[^=]*=\s*isDaltonBuild\s*\?\s*['"]([^'"]+)['"]/);
  if (!docsUrlMatch) {
    throw new Error(`Could not resolve Dalton DOCS_URL from ${supportLinksPath}`);
  }
  const docsUrl = docsUrlMatch[1];
  if (docsUrl !== basePath) {
    throw new Error(`Expected Dalton DOCS_URL in ${supportLinksPath} to equal "${basePath}", got "${docsUrl}"`);
  }

  const claudeDocsUrlMatch = claudeCodeSrc.match(/CLAUDE_CODE_DOCS_URL\s*=\s*`([^`]+)`/);
  if (!claudeDocsUrlMatch) {
    throw new Error(`Could not resolve CLAUDE_CODE_DOCS_URL from ${claudeCodePath}`);
  }
  const claudeDocsUrl = claudeDocsUrlMatch[1].replace(/\$\{DOCS_URL\}/g, docsUrl);

  const targets = new Set([`${docsUrl}/`, claudeDocsUrl]);

  for (const src of [supportLinksSrc, claudeCodeSrc, accountMenuSrc]) {
    for (const m of src.matchAll(/`([^`]*\$\{(?:DOCS_URL|CLAUDE_CODE_DOCS_URL)\}[^`]*)`/g)) {
      targets.add(
        m[1]
          .replace(/\$\{CLAUDE_CODE_DOCS_URL\}/g, claudeDocsUrl)
          .replace(/\$\{DOCS_URL\}/g, docsUrl),
      );
    }
    for (const m of src.matchAll(/['"`](\/help\/[^'"`\s)]*)['"`]/g)) {
      targets.add(m[1]);
    }
  }

  return [...targets].sort();
}

async function verifyFrontendHelpLinks(outDir, docsSiteRoot, basePath) {
  const repoRoot = path.resolve(docsSiteRoot, '..');
  const targets = await collectFrontendHelpLinks(repoRoot, basePath);
  const errors = [];

  for (const url of targets) {
    const [rawPath, anchor = null] = url.split('#');
    const rel = rawPath.replace(/^\/help\/?/, '').replace(/\/+$/, '');
    const candidates = rel === ''
      ? [path.join(outDir, 'index.html')]
      : [path.join(outDir, rel, 'index.html'), path.join(outDir, `${rel}.html`)];

    let htmlPath = null;
    for (const candidate of candidates) {
      try {
        const stat = await fs.stat(candidate);
        if (stat.isFile()) {
          htmlPath = candidate;
          break;
        }
      } catch {
        // try next candidate
      }
    }

    if (!htmlPath) {
      errors.push(`Missing built page for frontend link "${url}" (checked ${candidates.join(', ')})`);
      continue;
    }

    if (anchor) {
      const html = await fs.readFile(htmlPath, 'utf-8');
      const hasAnchor = html.includes(`id="${anchor}"`) || html.includes(`id='${anchor}'`);
      if (!hasAnchor) {
        errors.push(`Missing anchor "#${anchor}" in built page ${htmlPath} (referenced by "${url}")`);
      }
    }
  }

  if (errors.length > 0) {
    throw new Error(`Frontend /help link verification failed:\n- ${errors.join('\n- ')}`);
  }

  return targets.length;
}

/**
 * Generates per-page `.md` siblings, `/help/llms.txt`, and `/help/whats-new/rss.xml`
 * directly inside the Astro output directory using only Node built-ins,
 * and verifies that all `/help/...` URLs and anchors referenced by `web/` exist.
 */
export function everflowSiteArtifacts({ basePath = '/help', siteOrigin: explicitOrigin = '' } = {}) {
  return {
    name: 'everflow-site-artifacts',
    hooks: {
      'astro:build:done': async ({ dir, logger }) => {
        const outDir = fileURLToPath(dir);
        const docsSiteRoot = path.resolve(outDir, '..');
        const docsDir = path.join(docsSiteRoot, 'src', 'content', 'docs');
        const siteOrigin = resolveSiteOrigin(explicitOrigin);
        const rssBase = siteOrigin ? `${siteOrigin}${basePath}` : basePath;
        if (!siteOrigin) {
          logger.warn(
            'DOCS_SITE_ORIGIN / DALTON_FRONTEND_URL / PUBLIC_APP_URL is unset; whats-new/rss.xml links will be root-relative.',
          );
        }
        const files = await walkDocs(docsDir);
        const llmsEntries = [];
        let rssXml = null;

        for (const filePath of files) {
          const rel = path.relative(docsDir, filePath).replace(/\\/g, '/');
          const raw = await fs.readFile(filePath, 'utf-8');
          const { meta, body } = parseSimpleFrontmatter(raw);
          const title = meta.title || rel;
          const description = meta.description || '';
          const mdText = mdxToPlainMarkdown(title, description, body);

          const withoutExt = rel.replace(/\.(md|mdx)$/i, '');
          if (withoutExt === '404') continue;
          const isRootIndex = withoutExt === 'index';
          const routePath = isRootIndex
            ? `${basePath}/`
            : withoutExt.endsWith('/index')
              ? `${basePath}/${withoutExt.slice(0, -'/index'.length)}/`
              : `${basePath}/${withoutExt}/`;

          const mdRelOut = isRootIndex ? 'index.md' : `${withoutExt}.md`;
          const mdFullOut = path.join(outDir, mdRelOut);
          await fs.mkdir(path.dirname(mdFullOut), { recursive: true });
          await fs.writeFile(mdFullOut, mdText, 'utf-8');

          llmsEntries.push({
            title,
            description,
            url: routePath,
            mdUrl: `${basePath}/${mdRelOut}`,
          });

          if (withoutExt === 'whats-new') {
            const items = extractWhatsNewItems(body);
            const rssItems = items
              .map(
                (item) => [
                  '    <item>',
                  `      <title>${escapeXml(item.heading)}</title>`,
                  `      <link>${rssBase}/whats-new/#${escapeXml(item.anchor)}</link>`,
                  `      <guid isPermaLink="${siteOrigin ? 'true' : 'false'}">${rssBase}/whats-new/#${escapeXml(item.anchor)}</guid>`,
                  `      <pubDate>${escapeXml(item.pubDate)}</pubDate>`,
                  `      <description>${escapeXml(item.content.slice(0, 600))}</description>`,
                  '    </item>',
                ].join('\n'),
              )
              .join('\n');
            rssXml = [
              '<?xml version="1.0" encoding="UTF-8"?>',
              '<rss version="2.0">',
              '  <channel>',
              '    <title>Everflow 新功能</title>',
              `    <link>${rssBase}/whats-new/</link>`,
              '    <description>Everflow 投研平台与 Dalton 研究助手的用户可见更新说明。</description>',
              '    <language>zh-CN</language>',
              rssItems,
              '  </channel>',
              '</rss>',
              '',
            ].join('\n');
          }
        }

        const llmsLines = [
          '# Everflow 文档',
          '',
          '> 面向投研分析师的 Everflow 平台与 Dalton 研究助手使用文档。以下页面均提供对应的纯 Markdown（`.md`）版本。',
          '',
          ...llmsEntries.map((e) =>
            e.description
              ? `- [${e.title}](${e.mdUrl})（网页：${e.url}）：${e.description}`
              : `- [${e.title}](${e.mdUrl})（网页：${e.url}）`,
          ),
          '',
        ];
        await fs.writeFile(path.join(outDir, 'llms.txt'), llmsLines.join('\n'), 'utf-8');

        if (rssXml) {
          const rssDir = path.join(outDir, 'whats-new');
          await fs.mkdir(rssDir, { recursive: true });
          await fs.writeFile(path.join(rssDir, 'rss.xml'), rssXml, 'utf-8');
        }

        const verifiedCount = await verifyFrontendHelpLinks(outDir, docsSiteRoot, basePath);

        logger.info(
          `Generated ${llmsEntries.length} Markdown siblings, llms.txt${rssXml ? ', and whats-new/rss.xml' : ''}; verified ${verifiedCount} frontend /help links.`,
        );
      },
    },
  };
}
