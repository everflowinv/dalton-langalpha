/**
 * Single-source build-time profile filter for the `/help` docs site (Standalone Core Batch 3).
 *
 * Resolves one consistent profile across `DALTON_PROFILE`, `APP_ENV`, `DOCS_PROFILE`,
 * and `VITE_DALTON_PROFILE`: when only one source is set, uses that value; when multiple
 * sources are set and disagree, throws immediately.
 *
 * - Everflow build (`isCore === false`): keeps all pages including `everflow/`, preserves
 *   Everflow-only conditional blocks, strips any `DALTON_DOC_ELSE` / `DALTON_DOC:core` blocks,
 *   and leaves frontmatter & site metadata byte-for-byte identical to existing Everflow builds.
 * - Core build (`isCore === true`): excludes `everflow/**` and `explore/deliver-to-feishu.*`,
 *   rewrites `get-started/what-is-everflow` -> `get-started/overview`, strips
 *   `DALTON_DOC:everflow` blocks (keeping optional `DALTON_DOC_ELSE` / `DALTON_DOC:core`
 *   blocks), and normalizes frontmatter brand/proprietary strings with YAML-safe quoting.
 */

export const CORE_OVERVIEW_SLUG = 'get-started/overview';
export const EVERFLOW_OVERVIEW_SLUG = 'get-started/what-is-everflow';

const FRONTMATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;
const VALID_DOC_TARGETS = new Set(['everflow', 'core']);

// Matches any closed JSX or HTML comment containing DALTON_DOC (case-insensitive),
// including hyphenated/malformed targets like `<!-- DALTON_DOC:core-typo -->`.
const ANY_DOC_TOKEN_RE =
  /\{\/\*\s*(?:\/?\s*dalton_doc[\s\S]*?)\*\/\}|<!--\s*(?:\/?\s*dalton_doc[\s\S]*?)-->/gi;

const STRAY_DOC_MARKER_RE = /dalton_doc/i;

const JSX_OPEN_RE = /^\{\/\*\s*DALTON_DOC:([a-z0-9_]+)\s*\*\/\}$/;
const JSX_ELSE_RE = /^\{\/\*\s*DALTON_DOC_ELSE\s*\*\/\}$/;
const JSX_CLOSE_RE = /^\{\/\*\s*\/DALTON_DOC\s*\*\/\}$/;

const HTML_OPEN_RE = /^<!--\s*DALTON_DOC:([a-z0-9_]+)\s*-->$/;
const HTML_ELSE_RE = /^<!--\s*DALTON_DOC_ELSE\s*-->$/;
const HTML_CLOSE_RE = /^<!--\s*\/DALTON_DOC\s*-->$/;

export function resolveDocsProfile(env = process.env) {
  const parseExplicit = (name, rawValue) => {
    const val = String(rawValue || '').trim().toLowerCase();
    if (val && val !== 'core' && val !== 'everflow') {
      throw new Error(
        `[dalton-docs-profile-filter] Invalid ${name}="${val}" (expected "core" or "everflow")`,
      );
    }
    return val;
  };

  const rawDaltonProfile = parseExplicit('DALTON_PROFILE', env.DALTON_PROFILE);
  const rawDocsProfile = parseExplicit('DOCS_PROFILE', env.DOCS_PROFILE);
  const rawViteProfile = parseExplicit('VITE_DALTON_PROFILE', env.VITE_DALTON_PROFILE);
  const rawAppEnv = String(env.APP_ENV || '').trim().toLowerCase();
  const appEnvProfile = rawAppEnv ? (rawAppEnv === 'core' ? 'core' : 'everflow') : '';

  const sources = [
    ['DALTON_PROFILE', rawDaltonProfile],
    ['APP_ENV', appEnvProfile],
    ['DOCS_PROFILE', rawDocsProfile],
    ['VITE_DALTON_PROFILE', rawViteProfile],
  ].filter(([, val]) => Boolean(val));

  if (sources.length > 1) {
    const [firstName, firstVal] = sources[0];
    for (const [name, val] of sources.slice(1)) {
      if (val !== firstVal) {
        throw new Error(
          `[dalton-docs-profile-filter] Conflicting profile env: ${firstName}="${firstVal}" vs ${name}="${val}"`,
        );
      }
    }
  }

  const profile = sources[0]?.[1] || 'everflow';

  const isCore = profile === 'core';
  const rawBrand = String(env.BRAND_NAME || env.VITE_BRAND_NAME || '').trim();
  const brandName = rawBrand || (isCore ? 'Dalton' : 'Everflow');
  return { profile, isCore, brandName };
}

export function normalizeDocRelPath(filePath) {
  const posix = String(filePath || '').replace(/\\/g, '/');
  const marker = 'src/content/docs/';
  const idx = posix.indexOf(marker);
  return idx >= 0 ? posix.slice(idx + marker.length) : posix.replace(/^\/+/, '');
}

export function mapDocSlug(slug, { isCore }) {
  const clean = String(slug || '').replace(/^\/+|\/+$/g, '');
  if (isCore && clean === EVERFLOW_OVERVIEW_SLUG) {
    return CORE_OVERVIEW_SLUG;
  }
  return clean;
}

export function isDocIncluded(filePath, { isCore }) {
  if (!isCore) return true;
  const rel = normalizeDocRelPath(filePath);
  if (rel.startsWith('everflow/')) return false;
  if (rel === 'explore/deliver-to-feishu.mdx' || rel === 'explore/deliver-to-feishu.md') {
    return false;
  }
  return true;
}

export function isExcludedHelpUrl(url, { isCore }) {
  if (!isCore) return false;
  const clean = String(url || '').split('#')[0];
  return (
    clean.includes('/help/everflow/') ||
    clean.endsWith('/help/everflow') ||
    clean.includes('/help/explore/deliver-to-feishu') ||
    clean.includes(`/help/${EVERFLOW_OVERVIEW_SLUG}`)
  );
}

function lineNumberAt(text, offset) {
  let line = 1;
  for (let i = 0; i < offset; i += 1) {
    if (text.charCodeAt(i) === 10) line += 1;
  }
  return line;
}

function checkStrayDocMarker(segment, baseOffset, fullText, filePath) {
  const m = STRAY_DOC_MARKER_RE.exec(segment);
  if (m) {
    const line = lineNumberAt(fullText, baseOffset + m.index);
    throw new Error(
      `[dalton-docs-profile-filter] ${filePath}:${line}: unclosed or malformed DALTON_DOC directive`,
    );
  }
}

function parseDocToken(rawToken, { text, offset, filePath }) {
  const line = lineNumberAt(text, offset);
  const where = `${filePath || '<docs>'}:${line}`;
  const isJsx = rawToken.startsWith('{/*');
  const style = isJsx ? 'jsx' : 'html';

  const openMatch = rawToken.match(isJsx ? JSX_OPEN_RE : HTML_OPEN_RE);
  if (openMatch) {
    const target = openMatch[1];
    if (!VALID_DOC_TARGETS.has(target)) {
      throw new Error(
        `[dalton-docs-profile-filter] ${where}: unknown DALTON_DOC target "${target}" (expected "everflow" or "core")`,
      );
    }
    return { kind: 'open', style, target, line };
  }
  if ((isJsx ? JSX_ELSE_RE : HTML_ELSE_RE).test(rawToken)) {
    return { kind: 'else', style, line };
  }
  if ((isJsx ? JSX_CLOSE_RE : HTML_CLOSE_RE).test(rawToken)) {
    return { kind: 'close', style, line };
  }
  throw new Error(
    `[dalton-docs-profile-filter] ${where}: malformed DALTON_DOC directive ${JSON.stringify(rawToken)}`,
  );
}

/**
 * Strict stack-based conditional block assembly supporting arbitrary nesting.
 * Throws an Error naming `filePath:line` on any unclosed, unexpected, or
 * malformed `DALTON_DOC` directive (including unclosed comment delimiters).
 */
export function applyConditionalMarkers(text, { isCore, filePath = '<docs>' }) {
  if (!STRAY_DOC_MARKER_RE.test(text)) return text;

  const stack = [];
  const out = [];
  let cursor = 0;
  ANY_DOC_TOKEN_RE.lastIndex = 0;

  let match;
  while ((match = ANY_DOC_TOKEN_RE.exec(text)) !== null) {
    const start = match.index;
    const end = start + match[0].length;
    if (start > cursor) {
      const between = text.slice(cursor, start);
      checkStrayDocMarker(between, cursor, text, filePath);
      if (stack.every((frame) => frame.active)) {
        out.push(between);
      }
    }

    const token = parseDocToken(match[0], { text, offset: start, filePath });
    const where = `${filePath}:${token.line}`;

    if (token.kind === 'open') {
      const parentActive = stack.every((frame) => frame.active);
      const cond = token.target === 'core' ? isCore : !isCore;
      stack.push({
        style: token.style,
        line: token.line,
        parentActive,
        cond,
        inElse: false,
        active: parentActive && cond,
      });
    } else if (token.kind === 'else') {
      if (stack.length === 0) {
        throw new Error(
          `[dalton-docs-profile-filter] ${where}: unexpected DALTON_DOC_ELSE without matching DALTON_DOC open tag`,
        );
      }
      const top = stack[stack.length - 1];
      if (top.style !== token.style) {
        throw new Error(
          `[dalton-docs-profile-filter] ${where}: mismatched DALTON_DOC_ELSE delimiter style (opened at line ${top.line})`,
        );
      }
      if (top.inElse) {
        throw new Error(
          `[dalton-docs-profile-filter] ${where}: duplicate DALTON_DOC_ELSE for block opened at line ${top.line}`,
        );
      }
      top.inElse = true;
      top.active = top.parentActive && !top.cond;
    } else if (token.kind === 'close') {
      if (stack.length === 0) {
        throw new Error(
          `[dalton-docs-profile-filter] ${where}: unexpected /DALTON_DOC without matching DALTON_DOC open tag`,
        );
      }
      const top = stack[stack.length - 1];
      if (top.style !== token.style) {
        throw new Error(
          `[dalton-docs-profile-filter] ${where}: mismatched /DALTON_DOC delimiter style (opened at line ${top.line})`,
        );
      }
      stack.pop();
    }

    cursor = end;
  }

  if (cursor < text.length) {
    const tail = text.slice(cursor);
    checkStrayDocMarker(tail, cursor, text, filePath);
    if (stack.every((frame) => frame.active)) {
      out.push(tail);
    }
  }

  if (stack.length > 0) {
    const unclosed = stack[stack.length - 1];
    throw new Error(
      `[dalton-docs-profile-filter] ${filePath}:${unclosed.line}: unclosed DALTON_DOC block`,
    );
  }

  return out.join('');
}

export function rewriteCoreFrontmatterValue(value, brandName = 'Dalton') {
  if (typeof value !== 'string') return value;
  const brandRepl = () => brandName;
  const workbenchRepl = () => `${brandName} 研究工作台`;
  return value
    .replace(/Everflow（Dalton）/g, brandRepl)
    .replace(/Everflow 投研平台和研究助手 Dalton/g, workbenchRepl)
    .replace(/Everflow 与 Dalton/g, brandRepl)
    .replace(/Everflow 投研研究平台与 Dalton 研究助手/g, workbenchRepl)
    .replace(/、公司 Wiki 与销售纪要/g, '')
    .replace(/公司 Wiki 与销售纪要、/g, '')
    .replace(/、团队对话归档/g, '')
    .replace(/和团队对话归档/g, '')
    .replace(/，以及飞书写作边界/g, '')
    .replace(
      /接通内部研报、专家纪要、SEC 财报和实时行情，直接交出研究结论、监控面板和定时报告。/g,
      '接通 SEC 财报、业绩电话会与公开市场数据，直接交出研究结论、监控面板和定时报告。',
    )
    .replace(/\bEverflow\b/gi, brandRepl);
}

function unquoteYamlScalar(raw) {
  const trimmed = raw.trim();
  if (trimmed.length >= 2 && trimmed.startsWith('"') && trimmed.endsWith('"')) {
    try {
      return JSON.parse(trimmed);
    } catch {
      return trimmed.slice(1, -1);
    }
  }
  if (trimmed.length >= 2 && trimmed.startsWith("'") && trimmed.endsWith("'")) {
    return trimmed.slice(1, -1).replace(/''/g, "'");
  }
  return trimmed;
}

export function transformDocFrontmatter(frontmatterYaml, { isCore, brandName }) {
  if (!isCore) return frontmatterYaml;
  return frontmatterYaml
    .split(/\r?\n/)
    .map((line) => {
      if (/^\s*link\s*:\s*/.test(line)) {
        return line.replace(
          `/help/${EVERFLOW_OVERVIEW_SLUG}/`,
          `/help/${CORE_OVERVIEW_SLUG}/`,
        );
      }
      const kv = line.match(/^(\s*[A-Za-z0-9_-]+\s*:\s*)(.+)$/);
      if (kv) {
        const prefix = kv[1];
        const rawVal = kv[2].trim();
        // Leave YAML block-scalar headers (`|`, `>`, `|-`, `>-`) intact.
        if (/^[|>][+-]?$/.test(rawVal)) return line;
        const unquoted = unquoteYamlScalar(rawVal);
        const rewritten = rewriteCoreFrontmatterValue(unquoted, brandName);
        if (rewritten === unquoted) return line;
        return `${prefix}${JSON.stringify(rewritten)}`;
      }
      return rewriteCoreFrontmatterValue(line, brandName);
    })
    .join('\n');
}

export function transformDocBody(body, { isCore, filePath = '<docs>' }) {
  return applyConditionalMarkers(body, { isCore, filePath });
}

export function transformDocSource(raw, { isCore, brandName, filePath = '<docs>' }) {
  const match = raw.match(FRONTMATTER_RE);
  if (!match) {
    return transformDocBody(raw, { isCore, filePath });
  }
  const fm = transformDocFrontmatter(match[1], { isCore, brandName });
  const body = transformDocBody(raw.slice(match[0].length), { isCore, filePath });
  return `---\n${fm}\n---\n${body}`;
}

export function daltonDocsProfileVitePlugin({ isCore, brandName }) {
  return {
    name: 'dalton-docs-profile-filter',
    enforce: 'pre',
    transform(code, id) {
      const cleanId = id.split('?')[0].replace(/\\/g, '/');
      if (!cleanId.includes('/src/content/docs/')) return null;
      if (!/\.(md|mdx)$/i.test(cleanId)) return null;
      const transformed = transformDocSource(code, {
        isCore,
        brandName,
        filePath: normalizeDocRelPath(cleanId),
      });
      if (transformed === code) return null;
      return { code: transformed, map: null };
    },
  };
}
