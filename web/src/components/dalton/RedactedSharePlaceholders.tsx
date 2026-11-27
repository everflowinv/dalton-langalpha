/* eslint-disable react-refresh/only-export-components */
/**
 * Dalton public-share redaction UI placeholders, controlled-media fetchers,
 * HAST resource-style sanitizer, and share panel boundary notice.
 *
 * Imported only via lazy route chunks (SharedChatView, ChatView, ShareThreadDialog)
 * to stay off the initial critical-path bundle budget.
 */

import React, { createContext, useContext, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { Image as ImageIcon, LayoutGrid } from 'lucide-react';
import type { Element as HastElement, Root, RootContent } from 'hast';
import { getAuthHeaders } from '@/lib/authToken';
import {
  beginScopedImageRead,
  buildSharedImageScope,
  commitScopedImageUrl,
  finishScopedImageRead,
  getScopedImageUrl,
  shouldCommitScopedImageRead,
  subscribeSharedImageCache,
} from '@/lib/dalton/sharedImageCache';
import type { ShareRedactionSummary } from '@/types/api';

// ---------------------------------------------------------------------------
// Shared redaction context (propagates SharedChatView allow_files state down
// through MessageList/MessageBubble to Markdown, WorkspaceImage, InlineWidget)
// ---------------------------------------------------------------------------

export interface SharedRedactionContextValue {
  /** True only inside a public/organization shared conversation view. */
  active: boolean;
  /** Effective `permissions.allow_files` (defaults to true in owner views). */
  allowFiles: boolean;
  /** Whether rich diagrams (`mermaid`) may render (false in text-only shares). */
  allowDiagramRender: boolean;
  /** Active share token when in a shared view. */
  shareToken: string | null;
  /** Image cache scope key for the active shared view. */
  imageScope: string | null;
  /** Invoked when a controlled media or file request returns HTTP 403. */
  onForbidden?: () => void;
}

const DEFAULT_SHARED_REDACTION: SharedRedactionContextValue = Object.freeze({
  active: false,
  allowFiles: true,
  allowDiagramRender: true,
  shareToken: null,
  imageScope: null,
});

const SharedRedactionContext = createContext<SharedRedactionContextValue>(DEFAULT_SHARED_REDACTION);

export function SharedRedactionProvider({
  allowFiles,
  allowDiagramRender = allowFiles,
  shareToken,
  imageScope,
  onForbidden,
  children,
}: {
  allowFiles: boolean;
  allowDiagramRender?: boolean;
  shareToken: string;
  imageScope: string;
  onForbidden?: () => void;
  children: React.ReactNode;
}): React.ReactElement {
  const value = useMemo<SharedRedactionContextValue>(
    () => ({
      active: true,
      allowFiles,
      allowDiagramRender,
      shareToken,
      imageScope,
      onForbidden,
    }),
    [allowFiles, allowDiagramRender, shareToken, imageScope, onForbidden],
  );
  return <SharedRedactionContext.Provider value={value}>{children}</SharedRedactionContext.Provider>;
}

export function useSharedRedaction(): SharedRedactionContextValue {
  return useContext(SharedRedactionContext);
}

// ---------------------------------------------------------------------------
// Static placeholder components (zero network requests, zero <iframe>)
// ---------------------------------------------------------------------------

export function RedactedWidgetCard(): React.ReactElement {
  const { t } = useTranslation();
  return (
    <div
      role="note"
      className="my-2 flex items-start gap-3 rounded-xl border px-4 py-3"
      style={{
        borderColor: 'var(--color-border-muted)',
        backgroundColor: 'var(--color-bg-card)',
      }}
    >
      <div
        className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg"
        style={{
          backgroundColor: 'var(--color-bg-input)',
          color: 'var(--color-text-tertiary)',
        }}
      >
        <LayoutGrid className="h-4 w-4" aria-hidden="true" />
      </div>
      <div className="min-w-0 space-y-0.5">
        <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {t('share.redactedWidgetTitle')}
        </p>
        <p className="text-xs leading-relaxed" style={{ color: 'var(--color-text-secondary)' }}>
          {t('share.redactedWidgetDescription')}
        </p>
      </div>
    </div>
  );
}

export function RedactedImage({ alt }: { node?: unknown; alt?: string }): React.ReactElement {
  const { t } = useTranslation();
  const label = alt && alt.trim() ? alt.trim() : t('share.redactedImageLabel');
  return (
    <span
      className="my-1.5 inline-flex items-center gap-1.5 rounded-lg border px-3 py-2 text-xs align-middle"
      style={{
        borderColor: 'var(--color-border-muted)',
        backgroundColor: 'var(--color-bg-input)',
        color: 'var(--color-text-secondary)',
      }}
    >
      <ImageIcon className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
      <span>{label}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Controlled media URL resolution, origin validation & credentialed blob fetch
// ---------------------------------------------------------------------------

const OWNER_MEDIA_PATH_RE = /^\/api\/v1\/threads\/[^/]+\/media\/[^/?#]+/;
const PUBLIC_MEDIA_PATH_RE = /^\/api\/v1\/public\/shared\/([^/]+)\/media\/[^/?#]+/;

export class ControlledMediaError extends Error {
  status: number;
  response: { status: number };

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ControlledMediaError';
    this.status = status;
    this.response = { status };
  }
}

export function classifyControlledMediaUrl(
  src: string | null | undefined,
): { kind: 'owner' } | { kind: 'public'; shareToken: string } | null {
  if (!src) return null;
  let pathname: string;
  if (src.startsWith('/')) {
    const qIdx = src.search(/[?#]/);
    pathname = qIdx === -1 ? src : src.slice(0, qIdx);
  } else if (/^https?:\/\//i.test(src)) {
    try {
      pathname = new URL(src).pathname;
    } catch {
      return null;
    }
  } else {
    return null;
  }

  if (OWNER_MEDIA_PATH_RE.test(pathname)) {
    return { kind: 'owner' };
  }
  const pubMatch = PUBLIC_MEDIA_PATH_RE.exec(pathname);
  if (pubMatch) {
    let shareToken = pubMatch[1];
    try {
      shareToken = decodeURIComponent(shareToken);
    } catch {
      // Keep raw segment if malformed percent-encoding.
    }
    return { kind: 'public', shareToken };
  }
  return null;
}

/**
 * Resolve root-relative `/api/v1/...` media URLs against `VITE_API_BASE_URL`;
 * return absolute URLs unchanged.
 */
export function resolveControlledMediaUrl(src: string): string {
  if (/^https?:\/\//i.test(src)) {
    return src;
  }
  if (src.startsWith('/api/v1/')) {
    const apiBase = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '');
    return `${apiBase}${src}`;
  }
  return src;
}

function currentPageOrigin(): string | null {
  if (typeof window === 'undefined' || !window.location?.origin || window.location.origin === 'null') {
    return null;
  }
  return window.location.origin;
}

/**
 * Verify that `url` targets either the current page's exact origin or the
 * configured `VITE_API_BASE_URL` origin. Fails closed on parse errors or
 * opaque (`null`) origins.
 */
export function isTrustedApiOrigin(url: string): boolean {
  const pageOrigin = currentPageOrigin();
  const baseForParse = pageOrigin ?? 'http://localhost';
  let targetOrigin: string;
  try {
    targetOrigin = new URL(url, baseForParse).origin;
  } catch {
    return false;
  }
  if (!targetOrigin || targetOrigin === 'null') return false;
  if (pageOrigin && targetOrigin === pageOrigin) return true;

  const apiBase = (import.meta.env.VITE_API_BASE_URL ?? '').trim();
  if (apiBase) {
    try {
      const apiOrigin = new URL(apiBase, baseForParse).origin;
      if (apiOrigin && apiOrigin !== 'null' && targetOrigin === apiOrigin) {
        return true;
      }
    } catch {
      return false;
    }
  }
  return false;
}

function makeAbortError(message = 'Aborted'): Error {
  const err = new Error(message);
  err.name = 'AbortError';
  return err;
}

function mergeSignals(a: AbortSignal, b?: AbortSignal): { signal: AbortSignal; cleanup: () => void } {
  if (!b) return { signal: a, cleanup: () => {} };
  const merged = new AbortController();
  const onAbort = () => {
    if (!merged.signal.aborted) merged.abort();
  };
  if (a.aborted || b.aborted) {
    merged.abort();
    return { signal: merged.signal, cleanup: () => {} };
  }
  a.addEventListener('abort', onAbort, { once: true });
  b.addEventListener('abort', onAbort, { once: true });
  return {
    signal: merged.signal,
    cleanup: () => {
      a.removeEventListener('abort', onAbort);
      b.removeEventListener('abort', onAbort);
    },
  };
}

export interface FetchControlledMediaOptions {
  kind: 'owner' | 'public';
  shareToken?: string;
  allowFiles?: boolean;
  signal?: AbortSignal;
}

/**
 * Fetch bytes from a controlled media route and return an object URL:
 * - `owner`: allowed only for current page origin or `originOf(VITE_API_BASE_URL)`;
 *   uses `{ mode: 'cors', cache: 'no-store', credentials: 'include', headers: await getAuthHeaders() }`.
 * - `public`: uses `{ mode: 'cors', cache: 'no-store', credentials: 'omit' }` and
 *   commits into `sharedImageCache` under the share scope (guarded by epoch).
 * - HTTP 403 throws `ControlledMediaError(403, ...)` so callers can trigger downgrade.
 */
export async function fetchControlledMediaBlob(
  src: string,
  options: FetchControlledMediaOptions,
): Promise<string> {
  const resolvedUrl = resolveControlledMediaUrl(src);

  if (options.kind === 'owner') {
    if (!isTrustedApiOrigin(resolvedUrl)) {
      throw new Error('Refused to send owner credentials to untrusted media origin');
    }
    const headers = await getAuthHeaders();
    const res = await fetch(resolvedUrl, {
      mode: 'cors',
      cache: 'no-store',
      credentials: 'include',
      headers,
      ...(options.signal ? { signal: options.signal } : {}),
    });
    if (!res.ok) {
      throw new ControlledMediaError(
        res.status,
        res.status === 403 ? 'Controlled media access forbidden' : `Failed to load controlled media (${res.status})`,
      );
    }
    const blob = await res.blob();
    return URL.createObjectURL(blob);
  }

  // Public controlled media
  const parsed = classifyControlledMediaUrl(src);
  const token = options.shareToken ?? (parsed?.kind === 'public' ? parsed.shareToken : '');
  const scope = buildSharedImageScope(token, options.allowFiles ?? true);
  const cached = getScopedImageUrl(scope, resolvedUrl);
  if (cached) {
    return cached;
  }

  const { epoch, signal: scopedSignal } = beginScopedImageRead(scope);
  const { signal: effectiveSignal, cleanup } = mergeSignals(scopedSignal, options.signal);
  try {
    const res = await fetch(resolvedUrl, {
      mode: 'cors',
      cache: 'no-store',
      credentials: 'omit',
      signal: effectiveSignal,
    });
    if (!res.ok) {
      throw new ControlledMediaError(
        res.status,
        res.status === 403 ? 'Shared media access forbidden' : `Failed to load shared media (${res.status})`,
      );
    }
    const blob = await res.blob();
    if (!shouldCommitScopedImageRead(scope, epoch)) {
      throw makeAbortError('Shared image scope revoked before commit');
    }
    const objectUrl = URL.createObjectURL(blob);
    if (!commitScopedImageUrl(scope, resolvedUrl, epoch, objectUrl)) {
      throw makeAbortError('Shared image scope revoked during commit');
    }
    return objectUrl;
  } finally {
    cleanup();
    finishScopedImageRead(scope, scopedSignal);
  }
}

export interface ControlledMediaHookResult {
  isControlled: boolean;
  redacted: boolean;
  objectUrl: string | null;
  loading: boolean;
  error: boolean;
}

/**
 * React hook for `WorkspaceImage` and `AttachmentCard`: resolves controlled
 * media URLs (`/api/v1/threads/{id}/media/...` or `/api/v1/public/shared/{token}/media/...`)
 * into blob object URLs using the appropriate credential mode.
 */
export function useControlledMediaSrc(src: string | null | undefined): ControlledMediaHookResult {
  const {
    active: shareActive,
    allowFiles: shareAllowFiles,
    shareToken: shareContextToken,
    onForbidden: onShareForbidden,
  } = useSharedRedaction();
  const classified = useMemo(() => classifyControlledMediaUrl(src), [src]);
  const isControlled = classified !== null;
  const isPublicRedacted = Boolean(
    classified?.kind === 'public' && shareActive && !shareAllowFiles,
  );
  const resolvedUrl = useMemo(() => (src && isControlled ? resolveControlledMediaUrl(src) : ''), [src, isControlled]);
  const publicScope = useMemo(() => {
    if (classified?.kind !== 'public') return '';
    const token = shareContextToken ?? classified.shareToken;
    return buildSharedImageScope(token, shareAllowFiles);
  }, [classified, shareContextToken, shareAllowFiles]);

  const cachedPublicUrl = useSyncExternalStore(
    subscribeSharedImageCache,
    () => (publicScope && resolvedUrl ? getScopedImageUrl(publicScope, resolvedUrl) : null),
    () => null,
  );

  const [ownerState, setOwnerState] = useState<{ src: string; url: string | null; error: boolean }>({
    src: '',
    url: null,
    error: false,
  });
  const [publicFailedUrl, setPublicFailedUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!src || !classified || isPublicRedacted) return;
    let cancelled = false;
    const controller = new AbortController();

    if (classified.kind === 'public') {
      if (getScopedImageUrl(publicScope, resolvedUrl)) return;
      void fetchControlledMediaBlob(src, {
        kind: 'public',
        shareToken: shareContextToken ?? classified.shareToken,
        allowFiles: shareAllowFiles,
        signal: controller.signal,
      }).catch((err: unknown) => {
        if (cancelled) return;
        const status = (err as { status?: number; response?: { status?: number } })?.status
          ?? (err as { response?: { status?: number } })?.response?.status;
        if (status === 403) {
          onShareForbidden?.();
        }
        if ((err as { name?: string })?.name !== 'AbortError') {
          setPublicFailedUrl(resolvedUrl);
        }
      });
      return () => {
        cancelled = true;
        controller.abort();
      };
    }

    // Owner controlled media
    let createdUrl: string | null = null;
    void fetchControlledMediaBlob(src, {
      kind: 'owner',
      signal: controller.signal,
    }).then(
      (url) => {
        if (cancelled) {
          if (typeof URL !== 'undefined' && typeof URL.revokeObjectURL === 'function') {
            URL.revokeObjectURL(url);
          }
          return;
        }
        createdUrl = url;
        setOwnerState({ src, url, error: false });
      },
      (err: unknown) => {
        if (cancelled) return;
        if ((err as { name?: string })?.name !== 'AbortError') {
          setOwnerState({ src, url: null, error: true });
        }
      },
    );

    return () => {
      cancelled = true;
      controller.abort();
      if (createdUrl && typeof URL !== 'undefined' && typeof URL.revokeObjectURL === 'function') {
        URL.revokeObjectURL(createdUrl);
      }
    };
  }, [
    src,
    classified,
    isPublicRedacted,
    publicScope,
    resolvedUrl,
    shareContextToken,
    shareAllowFiles,
    onShareForbidden,
  ]);

  if (!isControlled) {
    return { isControlled: false, redacted: false, objectUrl: null, loading: false, error: false };
  }
  if (isPublicRedacted) {
    return { isControlled: true, redacted: true, objectUrl: null, loading: false, error: false };
  }
  if (classified.kind === 'public') {
    const failed = !cachedPublicUrl && publicFailedUrl === resolvedUrl;
    return {
      isControlled: true,
      redacted: false,
      objectUrl: cachedPublicUrl,
      loading: !cachedPublicUrl && !failed,
      error: failed,
    };
  }
  const sameOwner = ownerState.src === src;
  const objectUrl = sameOwner ? ownerState.url : null;
  const error = sameOwner ? ownerState.error : false;
  return {
    isControlled: true,
    redacted: false,
    objectUrl,
    loading: !objectUrl && !error,
    error,
  };
}

// ---------------------------------------------------------------------------
// Rehype plugin: strip resource/style/free-text attributes & media elements
// ---------------------------------------------------------------------------

const STRIPPED_TAG_NAMES = new Set([
  'img',
  'picture',
  'source',
  'video',
  'audio',
  'iframe',
  'object',
  'embed',
  'image',
  'feimage',
]);

function isStrippedProperty(key: string): boolean {
  const lower = key.toLowerCase();
  if (
    lower === 'style' ||
    lower === 'srcset' ||
    lower === 'imagesrcset' ||
    lower === 'poster' ||
    lower === 'background' ||
    lower === 'title'
  ) {
    return true;
  }
  if (lower.startsWith('aria-') || /^aria[A-Z]/.test(key)) return true;
  if (lower.startsWith('data-') || /^data[A-Z]/.test(key)) return true;
  return false;
}

function scrubNodeChildren(parent: Root | HastElement): void {
  if (!Array.isArray(parent.children)) return;
  const next: RootContent[] = [];
  for (const child of parent.children) {
    if (child.type === 'element') {
      const tag = child.tagName.toLowerCase();
      if (STRIPPED_TAG_NAMES.has(tag)) {
        continue;
      }
      if (child.properties) {
        for (const key of Object.keys(child.properties)) {
          if (isStrippedProperty(key)) {
            delete child.properties[key];
          }
        }
      }
      scrubNodeChildren(child);
    }
    next.push(child);
  }
  parent.children = next as typeof parent.children;
}

/**
 * Rehype plugin for `allow_files=false` shared views: strips resource-bearing
 * elements (`img`, `picture`, `source`, `video`, `audio`, `iframe`, `object`,
 * `embed`, `image`, `feImage`) while preserving `redacted-image`, and removes
 * `style`, `srcset`, `poster`, `background`, `title`, `aria-*`, and `data-*`
 * properties from every remaining element.
 */
export function rehypeStripResourceStyles(): (tree: Root) => void {
  return (tree: Root) => {
    scrubNodeChildren(tree);
  };
}

// ---------------------------------------------------------------------------
// ShareBoundaryNotice: publishing boundary & DB-only hidden counts in panel
// ---------------------------------------------------------------------------

export interface ShareBoundaryNoticeProps {
  allowFiles: boolean;
  summary?: ShareRedactionSummary | null;
}

export function ShareBoundaryNotice({ allowFiles, summary }: ShareBoundaryNoticeProps): React.ReactElement {
  const { t } = useTranslation();

  let detailLine: string;
  if (allowFiles) {
    detailLine = t('share.boundaryWithFiles');
  } else {
    const parts: string[] = [];
    if (summary) {
      if (summary.widgets > 0) {
        parts.push(t('share.boundaryCountWidgets', { count: summary.widgets }));
      }
      if (summary.tool_calls > 0) {
        parts.push(t('share.boundaryCountToolCalls', { count: summary.tool_calls }));
      }
      if (summary.images > 0) {
        parts.push(t('share.boundaryCountImages', { count: summary.images }));
      }
      if (summary.attachments > 0) {
        parts.push(t('share.boundaryCountAttachments', { count: summary.attachments }));
      }
    }
    if (parts.length === 0) {
      detailLine = t('share.boundaryAllHidden');
    } else {
      let joined: string;
      if (parts.length === 1) {
        joined = parts[0];
      } else if (parts.length === 2) {
        joined = t('share.boundaryListLastTwo', { first: parts[0], second: parts[1] });
      } else {
        const sep = t('share.boundaryListSeparator');
        joined = t('share.boundaryListLastMany', {
          head: parts.slice(0, -1).join(sep),
          last: parts[parts.length - 1],
        });
      }
      detailLine = t('share.boundarySummaryPrefix', { items: joined });
    }
  }

  return (
    <div
      className="rounded-md border px-2.5 py-2 space-y-1 text-xs leading-relaxed"
      style={{
        borderColor: 'var(--color-border-muted)',
        backgroundColor: 'var(--color-bg-input)',
        color: 'var(--color-text-secondary)',
      }}
    >
      <p className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
        {t('share.boundaryLine')}
      </p>
      <p style={{ color: 'var(--color-text-tertiary)' }}>{detailLine}</p>
    </div>
  );
}
