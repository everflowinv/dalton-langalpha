/**
 * Share-permission revocation, 403 classification, file-read epoch control,
 * and React Query file-body cache cleanup for shared conversations.
 *
 * Pure TypeScript with zero third-party dependencies.
 */

export type ShareForbiddenSource = 'list' | 'read' | 'serve' | 'resolve' | 'media' | 'download';
export type ShareForbiddenClass = 'files' | 'download';

/**
 * Classify an HTTP 403 from a shared thread surface.
 *
 * Only file-browsing / preview / controlled-media routes (`list`, `read`,
 * `serve`, `resolve`, `media`) prove `allow_files` was revoked. A 403 from
 * `/files/download` cannot distinguish `allow_download=false` from
 * `allow_files=false`, so it triggers metadata revalidation + toast without
 * immediately collapsing the view into text-only mode.
 */
export function classifyShareForbidden(source: ShareForbiddenSource): ShareForbiddenClass {
  return source === 'download' ? 'download' : 'files';
}

/**
 * Monotonic revocation handle for a shared view's file reads.
 *
 * Every adapter read receives `epoch.signal` and verifies `epoch.isCurrent()`
 * before committing or saving its result. Calling `revoke()` aborts in-flight
 * fetches and invalidates the epoch so late responses are discarded with an
 * `AbortError`.
 */
export interface ShareFileEpoch {
  readonly signal: AbortSignal;
  isCurrent: () => boolean;
  revoke: () => void;
}

export function createAbortError(): DOMException {
  return new DOMException('Aborted', 'AbortError');
}

export function createShareFileEpoch(): ShareFileEpoch {
  const controller = new AbortController();
  let revoked = false;
  return {
    get signal() {
      return controller.signal;
    },
    isCurrent: () => !revoked && !controller.signal.aborted,
    revoke: () => {
      if (revoked) return;
      revoked = true;
      if (!controller.signal.aborted) {
        controller.abort();
      }
    },
  };
}

export interface RunGuardedShareFileReadOptions<T> {
  epoch: ShareFileEpoch;
  source: ShareForbiddenSource;
  read: (signal: AbortSignal) => Promise<T>;
  onFilesForbidden: () => void;
  onDownloadForbidden: () => void;
  onDiscard?: (value: T) => void;
}

/**
 * Run a shared-file read bound to `epoch`, discarding late results with
 * `AbortError` if the epoch changed while in flight and routing HTTP 403
 * responses by `classifyShareForbidden(source)`.
 */
export async function runGuardedShareFileRead<T>({
  epoch,
  source,
  read,
  onFilesForbidden,
  onDownloadForbidden,
  onDiscard,
}: RunGuardedShareFileReadOptions<T>): Promise<T> {
  if (!epoch.isCurrent()) {
    throw createAbortError();
  }
  try {
    const value = await read(epoch.signal);
    if (!epoch.isCurrent()) {
      onDiscard?.(value);
      throw createAbortError();
    }
    return value;
  } catch (err: unknown) {
    if (!epoch.isCurrent() || (err as { name?: string })?.name === 'AbortError') {
      throw (err as { name?: string })?.name === 'AbortError' ? err : createAbortError();
    }
    const status =
      (err as { response?: { status?: unknown }; status?: unknown })?.response?.status ??
      (err as { status?: unknown })?.status;
    if (status === 403) {
      if (classifyShareForbidden(source) === 'download') {
        onDownloadForbidden();
      } else {
        onFilesForbidden();
      }
    }
    throw err;
  }
}

export interface ShareBodyQueryFilter {
  predicate: (query: { queryKey: readonly unknown[] }) => boolean;
}

/** Minimal structural slice of TanStack QueryClient needed for cache eviction. */
export interface ShareBodyQueryClient {
  cancelQueries: (filters: ShareBodyQueryFilter) => Promise<void> | void;
  removeQueries: (filters: ShareBodyQueryFilter) => void;
}

/**
 * Match React Query file-body cache keys created by `FilePanel` for shared
 * adapter mounts (`['workspaceFiles', 'adapter:<mountId>', 'body', ...]`),
 * leaving real workspace file caches untouched.
 */
export function isSharedFileBodyQueryKey(queryKey: readonly unknown[]): boolean {
  return (
    queryKey.length >= 2 &&
    queryKey[0] === 'workspaceFiles' &&
    typeof queryKey[1] === 'string' &&
    queryKey[1].startsWith('adapter:')
  );
}

/**
 * Cancel in-flight shared file-body queries and remove all cached shared
 * adapter file bodies from React Query.
 */
export function clearSharedFileBodies(
  queryClient: ShareBodyQueryClient | null | undefined,
): Promise<void> {
  if (!queryClient) return Promise.resolve();
  const filter: ShareBodyQueryFilter = {
    predicate: (query) => isSharedFileBodyQueryKey(query.queryKey),
  };
  const cancelPromise = Promise.resolve(queryClient.cancelQueries(filter)).catch(() => undefined);
  queryClient.removeQueries(filter);
  return cancelPromise.then(() => {
    queryClient.removeQueries(filter);
  });
}

export interface ShareDowngradeRecord {
  token: string;
  stamp: number | undefined;
}

/**
 * Check whether an active 403 downgrade still suppresses `allow_files` for
 * `shareToken`. A strictly newer metadata `refreshStamp` that affirms
 * `allowFiles === true` clears the downgrade so the view can recover.
 */
export function isShareDowngradeActive(
  record: ShareDowngradeRecord | null,
  shareToken: string,
  refreshStamp: number | undefined,
  allowFiles: boolean,
): boolean {
  if (!record || record.token !== shareToken) return false;
  if (
    allowFiles &&
    typeof refreshStamp === 'number' &&
    refreshStamp > 0 &&
    (record.stamp === undefined || refreshStamp > record.stamp)
  ) {
    return false;
  }
  return true;
}

/**
 * Decide whether a 404 (`gone`) or 401 `members_only` error must unmount the
 * currently displayed share view. Applies to both public file links and shared
 * conversation threads so revoked shares or removed members never keep stale
 * replays, images, or file panels mounted.
 */
export function shouldUnmountShareView(
  data: { kind: string; access?: string } | undefined,
  refused: boolean,
): boolean {
  if (!refused || !data) return false;
  return data.kind === 'thread' || (data.kind === 'file' && data.access === 'public');
}

export interface ShareRevalidationSnapshot {
  isFetching: boolean;
  isSuccess: boolean;
  isError: boolean;
  dataUpdatedAt: number;
}

export interface ShareRevalidationGateState {
  key: string;
  mountedAt: number;
  initialUpdatedAt: number;
  sawFetch: boolean;
  revalidated: boolean;
}

export function createShareRevalidationGate(
  key: string,
  snapshot: ShareRevalidationSnapshot,
  now: number = Date.now(),
): ShareRevalidationGateState {
  return {
    key,
    mountedAt: now,
    initialUpdatedAt: snapshot.dataUpdatedAt,
    sawFetch: snapshot.isFetching,
    revalidated: false,
  };
}

function hasFreshRevalidationSuccess(
  gate: ShareRevalidationGateState,
  snapshot: ShareRevalidationSnapshot,
  sawFetch: boolean,
): boolean {
  if (!snapshot.isSuccess || snapshot.isFetching || snapshot.dataUpdatedAt <= 0) {
    return false;
  }
  if (gate.initialUpdatedAt === 0) {
    return snapshot.dataUpdatedAt >= gate.mountedAt;
  }
  if (snapshot.dataUpdatedAt > gate.mountedAt || snapshot.dataUpdatedAt > gate.initialUpdatedAt) {
    return true;
  }
  return sawFetch && snapshot.dataUpdatedAt >= gate.mountedAt;
}

export function isShareRevalidated(
  gate: ShareRevalidationGateState,
  key: string,
  snapshot: ShareRevalidationSnapshot,
): boolean {
  if (gate.key !== key) return false;
  if (gate.revalidated) return true;
  return hasFreshRevalidationSuccess(gate, snapshot, gate.sawFetch);
}

export function advanceShareRevalidationGate(
  prev: ShareRevalidationGateState,
  key: string,
  snapshot: ShareRevalidationSnapshot,
  now: number = Date.now(),
): ShareRevalidationGateState {
  if (prev.key !== key) {
    return createShareRevalidationGate(key, snapshot, now);
  }
  const sawFetch = prev.sawFetch || snapshot.isFetching;
  const revalidated = prev.revalidated || hasFreshRevalidationSuccess(prev, snapshot, sawFetch);
  if (sawFetch === prev.sawFetch && revalidated === prev.revalidated) {
    return prev;
  }
  return { ...prev, sawFetch, revalidated };
}

/**
 * Gate rendering of a cached `kind === 'thread'` share whose cached permissions
 * have `allow_files === true` until this mount completes a successful
 * revalidation:
 * - in-flight -> `'loading'`
 * - failed (non-404/401) -> `'failed'` (never paint stale full-files view)
 * - revalidated once -> `'allow'` (subsequent refetch failures keep last good view)
 */
export function getStaleThreadFilesGate(
  data: { kind: string; permissions?: Record<string, unknown> },
  snapshot: Pick<ShareRevalidationSnapshot, 'isFetching' | 'isError'>,
  hasRevalidated: boolean,
): 'allow' | 'loading' | 'failed' {
  if (data.kind !== 'thread' || data.permissions?.allow_files !== true) return 'allow';
  if (hasRevalidated) return 'allow';
  if (snapshot.isFetching) return 'loading';
  if (snapshot.isError) return 'failed';
  return 'loading';
}
