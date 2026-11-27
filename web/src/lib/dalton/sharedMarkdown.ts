import { fileExtension } from '@/pages/ChatAgent/utils/filePaths';
import { buildServeUrl } from '@/pages/ChatAgent/components/viewers/html/wsfilesUrl';
import type { BodyReaders } from '@/pages/ChatAgent/components/filePanel/fileBody';
import { servedReaders } from '@/pages/SharedChat/api';
import { LIVE } from '@/pages/ChatAgent/utils/api/readFrom';

const MARKDOWN_EXTENSIONS = new Set(['md', 'markdown']);

/** Return true when *path* names a Markdown document (`.md` or `.markdown`). */
export function isMarkdownFile(path: string | null | undefined): boolean {
  if (!path) return false;
  return MARKDOWN_EXTENSIONS.has(fileExtension(path));
}

function parentDir(path: string): string {
  const clean = path.replace(/^\/+|\/+$/g, '');
  const idx = clean.lastIndexOf('/');
  return idx >= 0 ? clean.slice(0, idx) : '';
}

function resolveAgainstDir(baseDir: string, relPath: string): string | null {
  const stack = baseDir ? baseDir.split('/').filter(Boolean) : [];
  for (const seg of relPath.split('/')) {
    if (!seg || seg === '.') continue;
    if (seg === '..') {
      if (stack.length === 0) return null;
      stack.pop();
      continue;
    }
    stack.push(seg);
  }
  return stack.length > 0 ? stack.join('/') : null;
}

/** Split an href into its path portion and optional fragment (without `#`). */
export function splitSharedHref(href: string): { path: string; fragment: string | null } {
  const trimmed = (href || '').trim();
  const hashIdx = trimmed.indexOf('#');
  const withoutHash = hashIdx >= 0 ? trimmed.slice(0, hashIdx) : trimmed;
  const fragment = hashIdx >= 0 && hashIdx + 1 < trimmed.length ? trimmed.slice(hashIdx + 1) : null;
  const queryIdx = withoutHash.indexOf('?');
  const path = queryIdx >= 0 ? withoutHash.slice(0, queryIdx) : withoutHash;
  return { path, fragment };
}

export interface ResolveSharedPathOptions {
  /** Resolve relative paths against root (`""`) when the document's own directory is hidden (anonymous `/m/`). */
  rootRelative?: boolean;
  /** Decode percent-encoded path segments once (for raw Markdown link `href`s). */
  decode?: boolean;
}

/**
 * Resolve a relative file or image reference strictly against `docPath` within
 * the current shared version.
 *
 * Returns `null` for external URLs, cross-workspace references, malformed paths,
 * or parent traversals (`..`) that escape the shared root. Never falls back to
 * a same-named file at the workspace root.
 */
export function resolveSharedRelativePath(
  docPath: string,
  rawTarget: string,
  options?: ResolveSharedPathOptions,
): string | null {
  const { path: rawPath } = splitSharedHref(rawTarget);
  let target = rawPath.trim();
  if (!target) return null;
  if (options?.decode) {
    try {
      target = decodeURIComponent(target);
    } catch {
      // Keep original spelling when percent-encoding is malformed.
    }
  }
  if (
    /^[a-z][a-z0-9+.-]*:/i.test(target) ||
    target.startsWith('//') ||
    target.startsWith('__wsref__/') ||
    target.includes('\\') ||
    target.includes('\0')
  ) {
    return null;
  }

  if (target.startsWith('/')) {
    return resolveAgainstDir('', target);
  }

  const baseDir = options?.rootRelative ? '' : parentDir(docPath);
  return resolveAgainstDir(baseDir, target);
}

/**
 * Candidate workspace-relative paths to try when a shared Markdown document at
 * `docPath` embeds a local image `imagePath`.
 *
 * Strictly resolves relative to `docPath`'s directory with no fallback to the
 * workspace root. Traversals escaping above the shared root return `[]`.
 */
export function resolveSharedImageCandidates(
  docPath: string,
  imagePath: string,
  options?: ResolveSharedPathOptions,
): string[] {
  const resolved = resolveSharedRelativePath(docPath, imagePath, options);
  return resolved ? [resolved] : [];
}

/**
 * Fetch an embedded Markdown image under the shared document's `frameBase`,
 * resolving relative paths strictly against `docPath` within the current shared
 * version. Throws when the path is out of scope or inaccessible so
 * `WorkspaceImage` shows its fallback placeholder.
 */
export async function fetchSharedImageObjectUrl(
  frameBase: string,
  docPath: string,
  imagePath: string,
  fetchUrl: (prefix: string, path: string) => Promise<string>,
  options?: ResolveSharedPathOptions,
): Promise<string> {
  const candidates = resolveSharedImageCandidates(docPath, imagePath, options);
  if (candidates.length === 0) {
    throw new Error('Image path outside shared scope');
  }
  return fetchUrl(frameBase, candidates[0]);
}

/**
 * Verify whether `targetPath` is included and readable under the current shared
 * version's `frameBase` using a lightweight `HEAD` request (falling back to
 * `GET` only on HTTP 405).
 */
export async function probeSharedFileAccess(
  frameBase: string,
  targetPath: string,
): Promise<boolean> {
  const url = buildServeUrl(frameBase, targetPath);
  try {
    let res = await fetch(url, { method: 'HEAD' });
    if (res.status === 405) {
      res = await fetch(url, { method: 'GET' });
    }
    return Boolean(res.ok);
  } catch {
    return false;
  }
}

/**
 * Build `BodyReaders` for a shared viewer. When `entryUrl` is given (anonymous
 * `/m/` page whose real `entry_path` is withheld), reads for the entry file use
 * `entryUrl` directly while other paths under the same version read via
 * `frameBase`.
 */
export function createSharedEntryReaders(
  frameBase: string,
  entryPath: string,
  entryUrl?: string,
): BodyReaders {
  const base = servedReaders(frameBase);
  if (!entryUrl) return base;

  const readEntryText = async () => {
    const res = await fetch(entryUrl);
    if (!res.ok) {
      throw Object.assign(new Error(`Failed to load shared file (${res.status})`), {
        response: { status: res.status },
      });
    }
    const mime = res.headers?.get?.('content-type') || undefined;
    return { content: await res.text(), from: LIVE, ...(mime ? { mime } : {}) };
  };

  return {
    readFile: (wsId, reqPath) =>
      reqPath === entryPath ? readEntryText() : base.readFile(wsId, reqPath),
    readFileFull: (wsId, reqPath) =>
      reqPath === entryPath ? readEntryText() : base.readFileFull(wsId, reqPath),
    downloadFileAsArrayBuffer: base.downloadFileAsArrayBuffer,
  };
}
