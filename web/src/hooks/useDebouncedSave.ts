import { useCallback, useEffect, useRef, useState } from 'react';
// DALTON-HOOK(multi-file-upload): hold stale-build reload while a debounced, in-flight, or queued save is unfinished
import { holdReload } from '@/lib/reloadHolds';

type SaveStatus = 'idle' | 'saving' | 'saved' | 'error';

/**
 * Wraps a save function with debounce and status tracking.
 * - `trigger()`: schedule a debounced save (resets timer on rapid calls)
 * - `flush()`: save immediately, cancelling any pending debounce
 * - `status`: 'idle' | 'saving' | 'saved' | 'error'
 *
 * A save that resolves `false` wrote nothing, and the status goes back to idle
 * rather than claiming it saved.
 */
export function useDebouncedSave(
  saveFn: () => Promise<void | false>,
  delay = 800,
) {
  const [status, setStatus] = useState<SaveStatus>('idle');
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const savedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveFnRef = useRef(saveFn);
  saveFnRef.current = saveFn;
  const runningRef = useRef(false);
  const pendingRef = useRef(false);
  const mountedRef = useRef(true);
  const releaseReloadRef = useRef<(() => void) | null>(null);
  // DALTON-HOOK(multi-file-upload): hold stale-build reload while a debounced,
  // in-flight, or queued save is unfinished so a section switch never reloads
  // before pending edits finish saving.
  const syncReloadHold = useCallback(() => {
    const busy =
      timerRef.current !== null || runningRef.current || pendingRef.current;
    if (busy && !releaseReloadRef.current) {
      releaseReloadRef.current = holdReload();
    } else if (!busy && releaseReloadRef.current) {
      const release = releaseReloadRef.current;
      releaseReloadRef.current = null;
      release();
    }
  }, []);

  const execute = useCallback(async () => {
    // DALTON-HOOK(multi-file-upload): keep reload held while executing or queueing saves
    if (runningRef.current) {
      pendingRef.current = true;
      syncReloadHold();
      return;
    }
    runningRef.current = true;
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    syncReloadHold();
    if (savedTimerRef.current) {
      clearTimeout(savedTimerRef.current);
      savedTimerRef.current = null;
    }
    // The save itself must outlive unmount. `flush()` from a cleanup is the last
    // chance to persist a pending edit, and React clears `mountedRef` first —
    // this hook's own cleanup is registered before the caller's. Only the status
    // updates are gated on still being mounted.
    if (mountedRef.current) setStatus('saving');
    try {
      const saved = await saveFnRef.current();
      if (!mountedRef.current) return;
      if (saved === false) {
        setStatus('idle');
        return;
      }
      setStatus('saved');
      savedTimerRef.current = setTimeout(() => setStatus('idle'), 2000);
    } catch {
      if (!mountedRef.current) return;
      setStatus('error');
      savedTimerRef.current = setTimeout(() => setStatus('idle'), 3000);
    } finally {
      runningRef.current = false;
      // An edit made while this save ran still has to land, unmounted or not.
      if (pendingRef.current) {
        pendingRef.current = false;
        execute();
      } else {
        // DALTON-HOOK(multi-file-upload): release reload hold once queued saves finish
        syncReloadHold();
      }
    }
  }, [syncReloadHold]);

  const trigger = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(execute, delay);
    syncReloadHold();
  }, [delay, execute, syncReloadHold]);

  const flush = useCallback(() => {
    execute();
  }, [execute]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
      // DALTON-HOOK(multi-file-upload): release reload hold when unmounting with no running save
      syncReloadHold();
    };
  }, [syncReloadHold]);

  return { trigger, flush, status };
}
