import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import { isDaltonBuild } from '@/config/brand';
import type { ThreadFieldSaveConditions } from '@/lib/dalton/threadFieldSaveDalton';

export type ClearLocalHandle = (() => void) & { confirm: () => void };

export type NullHandleBinder = (createdId: string) => void;

export interface DeferredTuningPick<V> {
  threadId: string;
  value: V;
  expected?: V;
  hasExpected?: boolean;
}

/**
 * True when an unresolved-permission deferred tuning pick is still eligible to
 * flush:
 * - In Dalton builds, if the thread detail had not loaded when the pick was
 *   staged (`!pick.hasExpected`), cancels automatic flush so we never issue an
 *   unconditional PATCH over a newer write from another tab.
 * - Also returns false if the cached thread row already moved away from the
 *   value observed when the deferred pick was staged.
 */
export function shouldFlushDeferredTuningPick<V>(
  pick: DeferredTuningPick<V>,
  rowValue: V | undefined,
): boolean {
  if (!isDaltonBuild) return true;
  if (!pick.hasExpected) return false;
  if (rowValue === undefined) return true;
  return rowValue === pick.expected;
}

/**
 * Builds the optional CAS guard field sent on a deferred tuning flush.
 */
export function buildDeferredTuningConditions<V>(
  field: 'reasoning_effort' | 'fast_mode',
  pick: DeferredTuningPick<V>,
): ThreadFieldSaveConditions | undefined {
  if (!isDaltonBuild || !pick.hasExpected) return undefined;
  if (field === 'reasoning_effort') {
    return { expected_reasoning_effort: (pick.expected ?? null) as string | null };
  }
  return { expected_fast_mode: (pick.expected ?? null) as boolean | null };
}

export type DaltonTuningHold<V> = {
  value: V;
  clearToken?: symbol;
  restoreHeld?: DaltonTuningHold<V>;
  restoreDeferred?: DeferredTuningPick<V> | null;
} | null;

export interface TuningClearSession<V> {
  shownThreadRef: MutableRefObject<string | null>;
  sessionEpochRef: MutableRefObject<number>;
  createdRef: MutableRefObject<string | null>;
  heldRef: MutableRefObject<DaltonTuningHold<V>>;
  deferredPickRef: MutableRefObject<DeferredTuningPick<V> | null>;
  boundNullHandlesRef: MutableRefObject<Set<NullHandleBinder>>;
  setHeld: Dispatch<SetStateAction<DaltonTuningHold<V>>>;
  setDeferredPick: Dispatch<SetStateAction<DeferredTuningPick<V> | null>>;
}

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
 * True when a `null -> liveThreadId` transition represents the thread created
 * by this view's first send (rather than navigating from a new chat directly
 * to an unrelated existing thread).
 */
export function isCreatedThreadTransition(
  prevThreadId: string | null,
  liveThreadId: string | null,
  createdThreadId: string | null | undefined,
): boolean {
  if (prevThreadId !== null || liveThreadId === null) return false;
  if (!isDaltonBuild || createdThreadId === undefined) return true;
  return createdThreadId === liveThreadId;
}

/**
 * The id of a thread this view's first send just created (moving off the
 * `__default__` placeholder), or null for any other thread-id change.
 */
export function createdThreadIdFromSend(prevThreadId: string, nextThreadId: string): string | null {
  if (!isDaltonBuild) return null;
  if (prevThreadId && prevThreadId !== '__default__') return null;
  return nextThreadId && nextThreadId !== '__default__' ? nextThreadId : null;
}

/**
 * Binds any pending null-origin `clearLocal` handles to the newly created
 * thread id so they follow ONLY that thread and ignore any subsequent switch
 * to another thread.
 */
export function bindNullHandlesToThread(
  handles: Set<NullHandleBinder>,
  createdId: string,
): void {
  if (handles.size === 0) return;
  for (const bind of handles) bind(createdId);
  handles.clear();
}

export type TuningThreadTransitionAction = 'keep' | 'reset';

/**
 * Resolves a `prevThreadId -> liveThreadId` change for tuning state:
 * - On real thread creation (`null -> createdThreadId`), binds pending
 *   null-origin `clearLocal` handles to the created id and returns `'keep'`.
 * - On switching away from a thread (or in Dalton builds navigating from `null`
 *   to an unrelated existing thread), increments `sessionEpochRef`, clears
 *   pending binders, and returns `'reset'` so the hook clears local state.
 * - Otherwise returns `'keep'`.
 */
export function resolveTuningThreadTransition(
  prevThreadId: string | null,
  liveThreadId: string | null,
  createdThreadId: string | null | undefined,
  sessionEpochRef: MutableRefObject<number>,
  boundNullHandlesRef: MutableRefObject<Set<NullHandleBinder>>,
): TuningThreadTransitionAction {
  if (isCreatedThreadTransition(prevThreadId, liveThreadId, createdThreadId)) {
    bindNullHandlesToThread(boundNullHandlesRef.current, liveThreadId!);
    return 'keep';
  }
  if (prevThreadId || (isDaltonBuild && liveThreadId !== null)) {
    sessionEpochRef.current += 1;
    boundNullHandlesRef.current.clear();
    return 'reset';
  }
  return 'keep';
}

/**
 * Creates a tokenized local-effort clear handle. When created before a thread
 * id exists (`originThreadId === null`), the handle binds to the single thread
 * id subsequently created by that first send and invalidates if the view
 * switches to any other thread.
 */
export function createClearLocalTuningHandle<V>(
  originThreadId: string | null,
  session: TuningClearSession<V>,
): ClearLocalHandle {
  const {
    shownThreadRef,
    sessionEpochRef,
    createdRef,
    heldRef,
    deferredPickRef,
    boundNullHandlesRef,
    setHeld,
    setDeferredPick,
  } = session;

  const originSessionEpoch = sessionEpochRef.current;
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

  let boundThreadId: string | null = originThreadId;
  if (originThreadId === null) {
    boundNullHandlesRef.current.add((createdId: string) => {
      if (boundThreadId === null && sessionEpochRef.current === originSessionEpoch) {
        boundThreadId = createdId;
      }
    });
  }

  const isSameThreadSession = () => {
    if (sessionEpochRef.current !== originSessionEpoch) return false;
    const shown = shownThreadRef.current;
    if (boundThreadId !== null) return shown === boundThreadId;
    return shown === null;
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
