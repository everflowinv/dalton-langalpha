import { fileExtension } from '@/pages/ChatAgent/utils/filePaths';

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

/**
 * Candidate workspace-relative paths to try when a shared Markdown document at
 * `docPath` embeds a local image `imagePath`.
 *
 * Traversal escaping above the workspace root returns an empty list so the
 * viewer never fetches outside the shared snapshot and instead shows a
 * placeholder.
 */
export function resolveSharedImageCandidates(docPath: string, imagePath: string): string[] {
  const trimmed = (imagePath || '').trim();
  if (!trimmed) return [];

  const dir = parentDir(docPath);

  if (trimmed.startsWith('/')) {
    const resolved = resolveAgainstDir('', trimmed);
    return resolved ? [resolved] : [];
  }

  const segments = trimmed.split('/');
  if (segments.includes('..') || trimmed.startsWith('./')) {
    const resolved = resolveAgainstDir(dir, trimmed);
    return resolved ? [resolved] : [];
  }

  const clean = resolveAgainstDir('', trimmed);
  if (!clean) return [];

  if (dir && !clean.startsWith(`${dir}/`)) {
    return [`${dir}/${clean}`, clean];
  }
  return [clean];
}

/**
 * Fetch an embedded Markdown image under the shared document's `frameBase`,
 * resolving relative paths against `docPath` while respecting snapshot access
 * boundaries. Throws when no candidate is accessible so `WorkspaceImage` shows
 * its fallback placeholder.
 */
export async function fetchSharedImageObjectUrl(
  frameBase: string,
  docPath: string,
  imagePath: string,
  fetchUrl: (prefix: string, path: string) => Promise<string>,
): Promise<string> {
  const candidates = resolveSharedImageCandidates(docPath, imagePath);
  if (candidates.length === 0) {
    throw new Error('Image path outside shared scope');
  }
  let lastError: unknown;
  for (const candidate of candidates) {
    try {
      return await fetchUrl(frameBase, candidate);
    } catch (err) {
      lastError = err;
    }
  }
  throw lastError ?? new Error('Shared image unavailable');
}
