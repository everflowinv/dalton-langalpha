import React, { Suspense, lazy, useEffect, useSyncExternalStore } from 'react';

const LazyGlobalSearchModal = lazy(() => import('./GlobalSearchModal'));

let globalSearchOpenState = false;
const globalSearchListeners = new Set<() => void>();

function notifyGlobalSearchListeners() {
  globalSearchListeners.forEach((listener) => listener());
}

export function openGlobalSearch(): void {
  if (globalSearchOpenState) return;
  globalSearchOpenState = true;
  notifyGlobalSearchListeners();
}

export function closeGlobalSearch(): void {
  if (!globalSearchOpenState) return;
  globalSearchOpenState = false;
  notifyGlobalSearchListeners();
}

export function toggleGlobalSearch(): void {
  globalSearchOpenState = !globalSearchOpenState;
  notifyGlobalSearchListeners();
}

function subscribeGlobalSearch(listener: () => void): () => void {
  globalSearchListeners.add(listener);
  return () => {
    globalSearchListeners.delete(listener);
  };
}

function getGlobalSearchSnapshot(): boolean {
  return globalSearchOpenState;
}

/**
 * Single application-level host for the lazy-loaded Global Search modal and
 * `⌘K` / `Ctrl+K` listener. Mounted once in `AuthenticatedShell` (`App.tsx`)
 * so desktop, mobile, and desktop-to-mobile viewport transitions share the
 * same persistent host while `AppSidebar` and `BottomTabBar` only call
 * `openGlobalSearch()`.
 */
export function GlobalSearchHost(): React.ReactElement | null {
  const isOpen = useSyncExternalStore(subscribeGlobalSearch, getGlobalSearchSnapshot);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        toggleGlobalSearch();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  if (!isOpen) return null;

  return (
    <Suspense fallback={null}>
      <LazyGlobalSearchModal open={isOpen} onClose={closeGlobalSearch} />
    </Suspense>
  );
}
