import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import { isDaltonBuild } from '@/config/brand';

export type ClearLocalHandle = (() => void) & { confirm: () => void };

export type DaltonTuningHold<V> = {
  value: V;
  clearToken?: symbol;
  restoreHeld?: DaltonTuningHold<V>;
  restoreDeferred?: { threadId: string; value: V } | null;
} | null;

/**
 * Removes a failed optimistic effort value either from `held` directly or from
 * an active `clearLocal` handle's `restoreHeld` baseline so a subsequent
 * Antigravity save failure never writes the failed value back.
 */
export function scrubFailedTuningHold<V>(
  current: DaltonTuningHold<V>,
  failedValue: V,
): DaltonTuningHold<V> {
  if (!current) return current;
  if (!current.clearToken) {
    return current.value === failedValue ? null : current;
  }
  if (current.restoreHeld && current.restoreHeld.value === failedValue) {
    return { ...current, restoreHeld: null };
  }
  return current;
}

/**
 * Prevents a temporary optimistic `clearLocal` (set while an Antigravity model
 * switch is pending/deferred during thread creation) from being persisted as a
 * standalone `PATCH { reasoning_effort: null }` when the new thread's detail
 * row first loads.
 */
export function shouldDeferCreatedRowTuningSave<V>(held: DaltonTuningHold<V>): boolean {
  return Boolean(isDaltonBuild && held?.clearToken);
}

/**
 * Creates a tokenized local-effort clear handle that follows the same thread
 * creation (`null -> realThreadId`) via `sessionEpochRef`, while still ignoring
 * stale callbacks after switching away from an existing thread.
 */
export function createClearLocalTuningHandle<V>(params: {
  originThreadId: string | null;
  originSessionEpoch: number;
  shownThreadRef: MutableRefObject<string | null>;
  sessionEpochRef: MutableRefObject<number>;
  createdRef: MutableRefObject<string | null>;
  heldRef: MutableRefObject<DaltonTuningHold<V>>;
  deferredPickRef: MutableRefObject<{ threadId: string; value: V } | null>;
  setHeld: Dispatch<SetStateAction<DaltonTuningHold<V>>>;
  setDeferredPick: Dispatch<SetStateAction<{ threadId: string; value: V } | null>>;
}): ClearLocalHandle {
  const {
    originThreadId,
    originSessionEpoch,
    shownThreadRef,
    sessionEpochRef,
    createdRef,
    heldRef,
    deferredPickRef,
    setHeld,
    setDeferredPick,
  } = params;

  const token = Symbol('clearLocal');
  const prevDeferred = deferredPickRef.current;
  if (prevDeferred !== null) {
    deferredPickRef.current = null;
    setDeferredPick(null);
  }
  const current = heldRef.current;
  const restoreHeld = current?.clearToken ? (current.restoreHeld ?? null) : current;
  const restoreDeferred = current?.clearToken ? (current.restoreDeferred ?? null) : prevDeferred;
  const cleared: DaltonTuningHold<V> = {
    value: null as V,
    clearToken: token,
    restoreHeld,
    restoreDeferred,
  };
  heldRef.current = cleared;
  setHeld(cleared);

  const isSameThreadSession = () => {
    if (sessionEpochRef.current !== originSessionEpoch) return false;
    if (originThreadId !== null && shownThreadRef.current !== originThreadId) return false;
    return true;
  };

  const restore = () => {
    if (!isSameThreadSession()) return;
    const active = heldRef.current;
    if (active?.clearToken !== token) return;
    const nextRestored = active.restoreHeld ?? null;
    const deferredToRestore = active.restoreDeferred ?? null;
    heldRef.current = nextRestored;
    setHeld(nextRestored);
    if (isDaltonBuild && deferredToRestore && deferredToRestore.threadId === shownThreadRef.current) {
      deferredPickRef.current = deferredToRestore;
      setDeferredPick(deferredToRestore);
    }
  };

  const confirm = () => {
    if (!isSameThreadSession()) return;
    const active = heldRef.current;
    if (!active?.clearToken) return;
    // The Antigravity model switch settled and owns clearing effort on the server.
    if (createdRef.current === shownThreadRef.current) {
      createdRef.current = null;
    }
    if (active.clearToken === token) {
      const confirmed: DaltonTuningHold<V> = { value: null as V };
      heldRef.current = confirmed;
      setHeld(confirmed);
      return;
    }
    // A newer clearLocal is in flight behind this confirmed clear: advance its
    // rollback baseline to null (the value now confirmed on the server).
    const advanced: DaltonTuningHold<V> = {
      ...active,
      restoreHeld: { value: null as V },
      restoreDeferred: null,
    };
    heldRef.current = advanced;
    setHeld(advanced);
  };

  return Object.assign(restore, { confirm });
}
