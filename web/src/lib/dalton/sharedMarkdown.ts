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

function resolveAnonymousAgainstDir(baseDir: string, relPath: string): string | null {
  let upCount = 0;
  const stack: string[] = [];
  const apply = (segments: string[]) => {
    for (const seg of segments) {
      if (!seg || seg === '.') continue;
      if (seg === '..') {
        if (stack.length > 0) {
          stack.pop();
        } else {
          upCount += 1;
        }
        continue;
      }
      stack.push(seg);
    }
  };
  if (baseDir) apply(baseDir.split('/'));
  apply(relPath.split('/'));
  if (stack.length === 0) return null;
  return [...Array<string>(upCount).fill('..'), ...stack].join('/');
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
  /** Preserve leading `..` segments so the backend resolves them against the withheld entry directory (anonymous `/m/`). */
  anonymousEntry?: boolean;
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
    const resolved = resolveAgainstDir('', target);
    if (!resolved) return null;
    return options?.anonymousEntry ? `/${resolved}` : resolved;
  }

  const baseDir = options?.rootRelative ? '' : parentDir(docPath);
  if (options?.anonymousEntry) {
    return resolveAnonymousAgainstDir(baseDir, target);
  }
  return resolveAgainstDir(baseDir, target);
}

/** Build the fetch URL for a target path under `frameBase` (using `?rel=` when `anonymousEntry` hides the entry path). */
export function buildSharedTargetUrl(
  frameBase: string,
  targetPath: string,
  anonymousEntry = false,
): string {
  if (anonymousEntry) {
    return `${frameBase.replace(/\/+$/, '')}/?rel=${encodeURIComponent(targetPath)}`;
  }
  return buildServeUrl(frameBase, targetPath);
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
  if (options?.anonymousEntry) {
    const res = await fetch(buildSharedTargetUrl(frameBase, candidates[0], true));
    if (!res.ok) {
      throw Object.assign(new Error(`Failed to load shared image (${res.status})`), {
        response: { status: res.status },
      });
    }
    return URL.createObjectURL(await res.blob());
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
  options?: { anonymousEntry?: boolean },
): Promise<boolean> {
  const url = buildSharedTargetUrl(frameBase, targetPath, Boolean(options?.anonymousEntry));
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
 * `?rel=` on `frameBase` when `anonymousEntry` is true.
 */
export function createSharedEntryReaders(
  frameBase: string,
  entryPath: string,
  entryUrl?: string,
  anonymousEntry = false,
): BodyReaders {
  const base = servedReaders(frameBase);
  if (!entryUrl && !anonymousEntry) return base;

  const readTextFromUrl = async (url: string) => {
    const res = await fetch(url);
    if (!res.ok) {
      throw Object.assign(new Error(`Failed to load shared file (${res.status})`), {
        response: { status: res.status },
      });
    }
    const mime = res.headers?.get?.('content-type') || undefined;
    return { content: await res.text(), from: LIVE, ...(mime ? { mime } : {}) };
  };

  const readBufferFromUrl = async (url: string) => {
    const res = await fetch(url);
    if (!res.ok) {
      throw Object.assign(new Error(`Failed to load shared file (${res.status})`), {
        response: { status: res.status },
      });
    }
    return { buffer: await res.arrayBuffer(), from: LIVE };
  };

  const readText = (wsId: string, reqPath: string) => {
    if (entryUrl && reqPath === entryPath) return readTextFromUrl(entryUrl);
    if (anonymousEntry) return readTextFromUrl(buildSharedTargetUrl(frameBase, reqPath, true));
    return base.readFile(wsId, reqPath);
  };

  return {
    readFile: readText,
    readFileFull: readText,
    downloadFileAsArrayBuffer: (wsId, reqPath) => {
      if (entryUrl && reqPath === entryPath) return readBufferFromUrl(entryUrl);
      if (anonymousEntry) return readBufferFromUrl(buildSharedTargetUrl(frameBase, reqPath, true));
      return base.downloadFileAsArrayBuffer(wsId, reqPath);
    },
  };
}
