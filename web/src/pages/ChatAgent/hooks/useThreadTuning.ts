import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import type { ThreadTuning } from '@/components/ui/chat-input.types';
// DALTON-HOOK(antigravity-series): gate shared/unresolved tuning deferral to Dalton builds and resolve thread write access
import { isDaltonBuild } from '@/config/brand';
import { useThreadWriteAccess } from '@/lib/dalton/useThreadWriteAccess';
import type { Thread } from '@/types/api';
import { threadDetailQuery } from '../utils/threadQueries';
import { useThreadFieldSave } from './useThreadFieldSave';

type TuningField = 'reasoning_effort' | 'fast_mode';
type Override<F extends TuningField> = Exclude<Thread[F], undefined>;

/** A pick the row does not hold yet. Boxed, since null is a pick too: the
 *  thread going back to following Settings. */
// DALTON-HOOK(antigravity-series): attach optional rollback metadata for optimistic Antigravity clearLocal
type Hold<V> = {
  value: V;
  clearToken?: symbol;
  restoreHeld?: Hold<V>;
  restoreDeferred?: { threadId: string; value: V } | null;
} | null;

export type ClearLocalHandle = (() => void) & { confirm: () => void };

/** What a thread's first send stores as its own, beside the turn's level. */
export interface TuningToSend {
  reasoningEffortOverride?: string;
  fastModeOverride?: boolean;
}

interface UseThreadTuningOptions {
  /** The view's thread; `__default__` until the first send creates one. */
  threadId: string | null | undefined;
  /**
   * The navigation whose first send creates the thread, with the picks the
   * composer that sent it made, which the new row does not hold until then.
   * A new chat's view stays cached while unsent and the next such navigation
   * takes it up, so a new `key` replaces whatever the view held.
   */
  landing?: { key: string; reasoningEffort: string | null; fastMode: boolean | null } | null;
  // DALTON-HOOK(antigravity-series): workspace id to detect shared workspaces before issuing PATCH
  workspaceId?: string | null;
}

/**
 * A chat view's thread's own reasoning effort and fast mode: what its
 * composer shows over Settings, what the send creating the thread stores, and
 * the one place a pick on a live thread is saved.
 *
 * The thread row is the source, and null there follows Settings (the model's
 * profile, else the account default, else the model's own default), which
 * stay the only place a default is set: a pick here never writes them. A pick
 * on a live thread is saved on the row (`useThreadFieldSave`), which is the
 * only writer, since a send names the turn's level and the server never keeps
 * it. Until the row is read nothing is claimed (each field undefined): the
 * server runs the row's own when a send names none. A thread not created yet
 * holds its picks for the send that creates it, which stores them. A pick made
 * while that send is creating the thread, or one the new row does not hold
 * because the create failed and the legacy send minted the thread, is saved
 * on the row once it is read.
 */
export function useThreadTuning({ threadId, landing = null, workspaceId = null }: UseThreadTuningOptions) {
  const { t } = useTranslation();
  const liveThreadId = threadId && threadId !== '__default__' ? threadId : null;

  const { data: thread, isError: threadError } = useQuery({
    ...threadDetailQuery(liveThreadId ?? ''),
    enabled: !!liveThreadId,
  });

  // DALTON-HOOK(antigravity-series): resolve thread write access; deferred recovery flush only runs in Dalton builds so non-Dalton matches origin/dalton/main verbatim
  const writeAccess = useThreadWriteAccess({
    workspaceId,
    liveThreadId,
    thread,
    threadError,
  });
  const isUnresolved = isDaltonBuild && writeAccess.status === 'unresolved';
  const skipSave = writeAccess.status !== 'writable';

  const effort = useThreadOverride({
    liveThreadId,
    thread,
    field: 'reasoning_effort',
    landing: landing && { key: landing.key, value: landing.reasoningEffort },
    failedMessage: t('chat.pills.effortFailed'),
    skipSave,
    isUnresolved,
  });
  const fast = useThreadOverride({
    liveThreadId,
    thread,
    field: 'fast_mode',
    landing: landing && { key: landing.key, value: landing.fastMode },
    failedMessage: t('chat.pills.fastModeFailed'),
    skipSave,
    isUnresolved,
  });

  const tuning = useMemo<ThreadTuning>(
    () => ({ reasoningEffort: effort.value, fastMode: fast.value }),
    [effort.value, fast.value],
  );
  const toSend = useMemo<TuningToSend>(() => ({
    ...(effort.toSend !== undefined ? { reasoningEffortOverride: effort.toSend } : {}),
    ...(fast.toSend !== undefined ? { fastModeOverride: fast.toSend } : {}),
  }), [effort.toSend, fast.toSend]);

  // DALTON-HOOK(antigravity-series): expose clearLocalEffort & skipSave for shared/Antigravity atomic clear
  return {
    tuning,
    toSend,
    setReasoningEffort: effort.set,
    setFastMode: fast.set,
    clearLocalEffort: effort.clearLocal,
  };
}

function useThreadOverride<F extends TuningField>({
  liveThreadId,
  thread,
  field,
  landing,
  failedMessage,
  skipSave = false,
  isUnresolved = false,
}: {
  liveThreadId: string | null;
  thread: Thread | undefined;
  field: F;
  landing: { key: string; value: Override<F> } | null;
  failedMessage: string;
  // DALTON-HOOK(antigravity-series): allow shared/unresolved workspace turns to skip or defer PATCH in useThreadOverride
  skipSave?: boolean;
  isUnresolved?: boolean;
}) {
  // Undefined while there is no row: none yet, or one not read (loading, failed).
  const rowValue: Override<F> | undefined = liveThreadId && thread
    ? (thread[field] ?? null) as Override<F>
    : undefined;

  const landingHold = (value: Override<F>): Hold<Override<F>> => (value === null ? null : { value });
  const [held, setHeld] = useState<Hold<Override<F>>>(landing ? landingHold(landing.value) : null);
  // DALTON-HOOK(antigravity-series): retain a tuning pick made while workspace write permission is unresolved so it flushes via PATCH once confirmed personal
  const [deferredPick, setDeferredPick] = useState<{ threadId: string; value: Override<F> } | null>(null);
  const heldRef = useRef<Hold<Override<F>>>(held);
  const deferredPickRef = useRef<{ threadId: string; value: Override<F> } | null>(deferredPick);
  // A pick belongs to the thread it was made on, and one instance can move
  // between threads. A thread being created by its first send is not a move.
  const [heldThreadId, setHeldThreadId] = useState(liveThreadId);
  const [landingKey, setLandingKey] = useState(landing?.key ?? null);
  let nextHeld = held;
  let nextDeferredPick = deferredPick;
  if (heldThreadId !== liveThreadId) {
    setHeldThreadId(liveThreadId);
    if (heldThreadId) {
      nextHeld = null;
      nextDeferredPick = null;
      setHeld(null);
      setDeferredPick(null);
    }
  }
  if (landing && landing.key !== landingKey) {
    setLandingKey(landing.key);
    nextHeld = landingHold(landing.value);
    nextDeferredPick = null;
    setHeld(nextHeld);
    setDeferredPick(null);
  }
  // DALTON-HOOK(antigravity-series): in Dalton builds never drop an unsent deferredPick (or held while deferred / awaiting optimistic Antigravity clear settlement) just because it matches possibly-stale cached rowValue
  const keepHeldForDalton = isDaltonBuild && (nextDeferredPick !== null || Boolean(nextHeld?.clearToken));
  if (nextHeld && !keepHeldForDalton && rowValue !== undefined && rowValue === nextHeld.value) {
    nextHeld = null;
    setHeld(null);
  }
  if (!isDaltonBuild && nextDeferredPick && rowValue !== undefined && rowValue === nextDeferredPick.value) {
    nextDeferredPick = null;
    setDeferredPick(null);
  }
  heldRef.current = nextHeld;
  deferredPickRef.current = nextDeferredPick;

  const save = useThreadFieldSave({
    threadId: liveThreadId,
    field,
    failedMessage,
    // The row moving back carries the composer with it, so the hold goes and
    // the row shows through, unless the pick has moved on since. With no row
    // cached the composer falls back to Settings until the settle reads it.
    onFailed: ({ value }) => {
      if (heldRef.current && heldRef.current.value === value) heldRef.current = null;
      setHeld((current) => (current && current.value === value ? null : current));
    },
  });

  // A pick made while the first send is creating the thread had no row to be
  // saved on, and a thread the legacy send minted (its create failed) stored
  // none, since a send's level is the turn's alone. Either way the new row,
  // once read, does not hold the pick, which is then saved there. A pick the
  // create stored agrees with the row and is let go above, so it is never
  // written twice.
  const shownBeforeRef = useRef(liveThreadId);
  const createdRef = useRef<string | null>(null);
  useEffect(() => {
    if (shownBeforeRef.current === null && liveThreadId) createdRef.current = liveThreadId;
    shownBeforeRef.current = liveThreadId;
    if (!liveThreadId || skipSave || createdRef.current !== liveThreadId || rowValue === undefined) return;
    createdRef.current = null;
    if (held === null || deferredPick !== null) return;
    void save({ threadId: liveThreadId, value: held.value, before: null });
  }, [deferredPick, held, liveThreadId, rowValue, save, skipSave]);

  // DALTON-HOOK(antigravity-series): flush a deferred tuning pick via PATCH once write permission resolves to confirmed personal, or drop it on shared threads
  useEffect(() => {
    if (!deferredPick || isUnresolved) return;
    if (deferredPick.threadId !== liveThreadId || !liveThreadId) {
      deferredPickRef.current = null;
      setDeferredPick(null);
      return;
    }
    if (skipSave) {
      deferredPickRef.current = null;
      setDeferredPick(null);
      return;
    }
    const { threadId: targetThreadId, value: targetValue } = deferredPick;
    deferredPickRef.current = null;
    setDeferredPick(null);
    createdRef.current = null;
    // DALTON-HOOK(antigravity-series): always persist the user's last explicit deferred pick even when it matches cached rowValue (which may be stale)
    void save({ threadId: targetThreadId, value: targetValue, before: null });
  }, [deferredPick, isUnresolved, liveThreadId, save, skipSave]);

  /** Resolves false only when this pick's own save failed. */
  const set = useCallback((next: Override<F>): Promise<boolean> => {
    const nextHold: Hold<Override<F>> = { value: next };
    heldRef.current = nextHold;
    setHeld(nextHold);
    if (!liveThreadId) {
      deferredPickRef.current = null;
      setDeferredPick(null);
      return Promise.resolve(true);
    }
    // DALTON-HOOK(antigravity-series): defer PATCH while workspace write permission is unresolved; skip on confirmed shared workspaces
    if (isUnresolved) {
      const nextDeferred = { threadId: liveThreadId, value: next };
      deferredPickRef.current = nextDeferred;
      setDeferredPick(nextDeferred);
      return Promise.resolve(true);
    }
    deferredPickRef.current = null;
    setDeferredPick(null);
    if (skipSave) return Promise.resolve(true);
    // `before` goes unused: a failure drops the hold rather than restoring one.
    return save({ threadId: liveThreadId, value: next, before: null });
  }, [isUnresolved, liveThreadId, save, skipSave]);

  const shownThreadRef = useRef(liveThreadId);
  shownThreadRef.current = liveThreadId;

  // DALTON-HOOK(antigravity-series): clear local reasoning_effort state without triggering a second PATCH, returning a restore handle with .confirm() so a later failure rolls back to the pre-save value and never an earlier one
  const clearLocal = useCallback((): ClearLocalHandle => {
    const token = Symbol('clearLocal');
    const targetThreadId = liveThreadId;
    const prevDeferred = deferredPickRef.current;
    if (prevDeferred !== null) {
      deferredPickRef.current = null;
      setDeferredPick(null);
    }
    const current = heldRef.current;
    const restoreHeld = current?.clearToken ? (current.restoreHeld ?? null) : current;
    const restoreDeferred = current?.clearToken ? (current.restoreDeferred ?? null) : prevDeferred;
    const cleared: Hold<Override<F>> = {
      value: null as Override<F>,
      clearToken: token,
      restoreHeld,
      restoreDeferred,
    };
    heldRef.current = cleared;
    setHeld(cleared);

    const restore = () => {
      if (shownThreadRef.current !== targetThreadId) return;
      const active = heldRef.current;
      if (active?.clearToken !== token) return;
      const nextRestored = active.restoreHeld ?? null;
      const deferredToRestore = active.restoreDeferred ?? null;
      heldRef.current = nextRestored;
      setHeld(nextRestored);
      if (isDaltonBuild && deferredToRestore && deferredToRestore.threadId === targetThreadId) {
        deferredPickRef.current = deferredToRestore;
        setDeferredPick(deferredToRestore);
      }
    };

    const confirm = () => {
      if (shownThreadRef.current !== targetThreadId) return;
      const active = heldRef.current;
      if (!active?.clearToken) return;
      if (active.clearToken === token) {
        const confirmed: Hold<Override<F>> = { value: null as Override<F> };
        heldRef.current = confirmed;
        setHeld(confirmed);
        return;
      }
      // A newer clearLocal is in flight behind this confirmed clear: advance its
      // rollback baseline to null (the value now confirmed on the server).
      const advanced: Hold<Override<F>> = {
        ...active,
        restoreHeld: { value: null as Override<F> },
        restoreDeferred: null,
      };
      heldRef.current = advanced;
      setHeld(advanced);
    };

    return Object.assign(restore, { confirm });
  }, [liveThreadId]);

  // DALTON-HOOK(antigravity-series): derive displayed value and first-send overrides from adjusted nextHeld
  const value: Override<F> | undefined = nextHeld ? nextHeld.value : liveThreadId ? rowValue : null as Override<F>;
  // A new thread's create names a pick, and otherwise nothing, so the thread
  // follows Settings.
  const toSend = !liveThreadId && nextHeld && nextHeld.value !== null
    ? nextHeld.value as Exclude<Override<F>, null>
    : undefined;

  return { value, toSend, set, clearLocal };
}
