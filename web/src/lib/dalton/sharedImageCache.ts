/**
 * Share-scoped image object-URL cache with monotonic epochs and in-flight
 * abort/revocation so permission downgrades (`allow_files: true -> false`)
 * immediately cancel slow fetches, release object URLs, and prevent late
 * responses from backfilling the cache.
 *
 * Pure TypeScript with zero third-party dependencies.
 */

const SHARE_SCOPE_PREFIX = 'share:';

const scopeEpochs = new Map<string, number>();
const scopeControllers = new Map<string, Map<AbortSignal, AbortController>>();
const scopeEntries = new Map<string, Map<string, string>>();
const cacheListeners = new Set<() => void>();
const revokeListeners = new Set<(shareToken: string) => void>();

function safeRevokeObjectUrl(url: string): void {
  if (typeof URL !== 'undefined' && typeof URL.revokeObjectURL === 'function') {
    URL.revokeObjectURL(url);
  }
}

function emitCacheChange(): void {
  for (const listener of cacheListeners) {
    listener();
  }
}

/** Build the cache scope key for a shared thread under a specific `allow_files` state. */
export function buildSharedImageScope(shareToken: string, allowFiles: boolean): string {
  return `${SHARE_SCOPE_PREFIX}${shareToken}:${allowFiles ? 'files' : 'text'}`;
}

/**
 * Extract the shared scope prefix (`share:<token>:files` or `share:<token>:text`)
 * from a composite cache key (`<scope>:<path>`), or return null for non-shared keys.
 */
export function parseSharedImageScopeFromKey(cacheKey: string): string | null {
  if (!cacheKey.startsWith(SHARE_SCOPE_PREFIX)) return null;
  const filesMarker = ':files:';
  const filesIdx = cacheKey.indexOf(filesMarker, SHARE_SCOPE_PREFIX.length);
  if (filesIdx !== -1) {
    return cacheKey.slice(0, filesIdx + ':files'.length);
  }
  const textMarker = ':text:';
  const textIdx = cacheKey.indexOf(textMarker, SHARE_SCOPE_PREFIX.length);
  if (textIdx !== -1) {
    return cacheKey.slice(0, textIdx + ':text'.length);
  }
  return null;
}

/** Read the current monotonic epoch for a scope. */
export function getSharedImageScopeEpoch(scope: string): number {
  return scopeEpochs.get(scope) ?? 0;
}

/**
 * Begin an image read in `scope`: snapshots the scope's current monotonic epoch
 * and registers an `AbortController` so `revokeSharedImageScope` can abort it.
 */
export function beginScopedImageRead(scope: string): { epoch: number; signal: AbortSignal } {
  const epoch = scopeEpochs.get(scope) ?? 0;
  if (!scopeEpochs.has(scope)) {
    scopeEpochs.set(scope, epoch);
  }
  const controller = new AbortController();
  let controllers = scopeControllers.get(scope);
  if (!controllers) {
    controllers = new Map<AbortSignal, AbortController>();
    scopeControllers.set(scope, controllers);
  }
  controllers.set(controller.signal, controller);
  return { epoch, signal: controller.signal };
}

/** Unregister a settled in-flight controller once its read finishes. */
export function finishScopedImageRead(scope: string, signal: AbortSignal): void {
  const controllers = scopeControllers.get(scope);
  if (!controllers) return;
  controllers.delete(signal);
  if (controllers.size === 0) {
    scopeControllers.delete(scope);
  }
}

/**
 * Return true only while `scope` is still at `epoch` (i.e. has not been
 * revoked since the read began).
 */
export function shouldCommitScopedImageRead(scope: string, epoch: number): boolean {
  if (!scope) return false;
  return (scopeEpochs.get(scope) ?? 0) === epoch;
}

/** Read a cached object URL for `(scope, key)`. */
export function getScopedImageUrl(scope: string, key: string): string | null {
  return scopeEntries.get(scope)?.get(key) ?? null;
}

/**
 * Commit an object URL into `(scope, key)` iff the scope's epoch still matches
 * `epoch`. If the scope was revoked while the fetch was in flight, immediately
 * revoke the object URL and return false (preventing late backfill).
 */
export function commitScopedImageUrl(
  scope: string,
  key: string,
  epoch: number,
  objectUrl: string,
): boolean {
  if (!shouldCommitScopedImageRead(scope, epoch)) {
    safeRevokeObjectUrl(objectUrl);
    return false;
  }
  let entries = scopeEntries.get(scope);
  if (!entries) {
    entries = new Map<string, string>();
    scopeEntries.set(scope, entries);
  }
  const previous = entries.get(key);
  if (previous === objectUrl) {
    return true;
  }
  entries.set(key, objectUrl);
  emitCacheChange();
  if (previous) {
    safeRevokeObjectUrl(previous);
  }
  return true;
}

/**
 * Revoke every image scope owned by `shareToken`:
 * 1. Monotonically advance epochs for all of its scopes;
 * 2. Abort all registered in-flight `AbortController`s;
 * 3. Call `URL.revokeObjectURL` on every issued object URL and clear entries;
 * 4. Notify subscribers so UI and delegated caches drop stale references.
 */
export function revokeSharedImageScope(shareToken: string): void {
  const tokenPrefix = `${SHARE_SCOPE_PREFIX}${shareToken}:`;
  const scopes = new Set<string>([
    buildSharedImageScope(shareToken, true),
    buildSharedImageScope(shareToken, false),
  ]);
  for (const key of scopeEpochs.keys()) {
    if (key.startsWith(tokenPrefix)) scopes.add(key);
  }
  for (const key of scopeControllers.keys()) {
    if (key.startsWith(tokenPrefix)) scopes.add(key);
  }
  for (const key of scopeEntries.keys()) {
    if (key.startsWith(tokenPrefix)) scopes.add(key);
  }

  let changed = false;
  for (const scope of scopes) {
    const nextEpoch = (scopeEpochs.get(scope) ?? 0) + 1;
    scopeEpochs.set(scope, nextEpoch);

    const controllers = scopeControllers.get(scope);
    if (controllers) {
      scopeControllers.delete(scope);
      for (const controller of controllers.values()) {
        if (!controller.signal.aborted) {
          controller.abort();
        }
      }
    }

    const entries = scopeEntries.get(scope);
    if (entries) {
      scopeEntries.delete(scope);
      for (const url of entries.values()) {
        safeRevokeObjectUrl(url);
      }
      if (entries.size > 0) changed = true;
    }
  }

  if (changed) {
    emitCacheChange();
  }
  for (const listener of revokeListeners) {
    listener(shareToken);
  }
}

/** Subscribe to scoped object-URL cache updates (for `useSyncExternalStore`). */
export function subscribeSharedImageCache(listener: () => void): () => void {
  cacheListeners.add(listener);
  return () => {
    cacheListeners.delete(listener);
  };
}

/** Subscribe to share-token revocations (used by WorkspaceImage to evict matching entries). */
export function onSharedImageScopeRevoked(listener: (shareToken: string) => void): () => void {
  revokeListeners.add(listener);
  return () => {
    revokeListeners.delete(listener);
  };
}
