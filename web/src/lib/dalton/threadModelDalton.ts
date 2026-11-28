import type { MutableRefObject } from 'react';
import { isDaltonBuild } from '@/config/brand';
import type { ThreadFieldSaveConditions } from '@/lib/dalton/threadFieldSaveDalton';
import type { Thread } from '@/types/api';

export interface ManualModelPick {
  model: string;
  seq: number;
  beforeRowModel?: string | null;
}

export interface DeferredSharedPick {
  threadId: string;
  picked: string;
  before: string | null;
  seq: number;
  expectedModel?: string | null;
  hasExpectedModel?: boolean;
  expectedEffort?: string | null;
  hasExpectedEffort?: boolean;
  onSettled?: (saved: boolean) => void;
}

export interface RepatchExpectedModel {
  known: boolean;
  model: string | null;
}

export interface TrackedTurnPick {
  threadId: string | null;
  model: string;
  seq: number;
  startThread: Thread | undefined;
  startUpdatedAt: number;
  rereadFailed?: boolean;
  failedThread?: Thread | undefined;
  failedUpdatedAt?: number;
}

export interface ActiveTurnModelInfo {
  model: string | null;
  pickSeq?: number | null;
  admitted?: boolean;
  modelUnknown?: boolean;
}

export interface PendingSettledTracker {
  register: (model: string, onSettled?: (saved: boolean) => void) => void;
  revoke: () => void;
  take: (model: string) => ((saved: boolean) => void) | undefined;
  confirmIfMatching: (model: string | null) => void;
}

/**
 * Settles a `deferredSharedPick`'s `onSettled` callback at most once (clearing
 * the callback reference before invoking so StrictMode or duplicate exits
 * never fire it twice).
 */
export function settleDeferredSharedPick(
  pick: DeferredSharedPick | null | undefined,
  saved: boolean,
): void {
  if (!isDaltonBuild || !pick?.onSettled) return;
  const cb = pick.onSettled;
  pick.onSettled = undefined;
  cb(saved);
}

/**
 * Manages the deferred `onSettled` callback for a pre-id pick made while a new
 * thread's first turn is creating the thread. Guarantees every early exit
 * (superseded pick, thread switch, empty/failed detail reread, reload during
 * reread) settles with `false` so optimistic effort clears always restore.
 */
export function createPendingSettledTracker(): PendingSettledTracker {
  let entry: { model: string; onSettled: (saved: boolean) => void } | null = null;

  const revoke = () => {
    const prev = entry;
    entry = null;
    if (isDaltonBuild) prev?.onSettled(false);
  };

  return {
    register(model, onSettled) {
      revoke();
      if (isDaltonBuild && onSettled) {
        entry = { model, onSettled };
      }
    },
    revoke,
    take(model) {
      if (!entry || entry.model !== model) return undefined;
      const settled = entry.onSettled;
      entry = null;
      return settled;
    },
    confirmIfMatching(model) {
      if (!entry) return;
      const prev = entry;
      entry = null;
      if (isDaltonBuild) {
        prev.onSettled(model !== null && prev.model === model);
      }
    },
  };
}

/**
 * Resolves whether an admitted turn consumed the current manual pick or an
 * already-saved personal thread model, and whether an unsent newer pick must be
 * protected across the turn (including when 409 adopts an older run whose model
 * differs from the user's current selection or is unknown).
 */
export function resolveTurnPickConsumption(params: {
  turnModel: ActiveTurnModelInfo | null;
  currentPick: ManualModelPick | null;
  liveThreadId: string | null;
  isSharedOrUnresolved: boolean;
  hasPending: boolean;
  hasDeferredShared: boolean;
  snapshot: { thread: Thread | undefined; updatedAt: number };
}): {
  matchesCurrentPick: boolean;
  consumedPick: TrackedTurnPick | null;
  preserveUnsentSeq: number | null;
} {
  const {
    turnModel,
    currentPick,
    liveThreadId,
    isSharedOrUnresolved,
    hasPending,
    hasDeferredShared,
    snapshot,
  } = params;

  const implicitSavedPersonalPick = Boolean(
    isDaltonBuild
    && !isSharedOrUnresolved
    && currentPick
    && turnModel !== null
    && turnModel.model === null
    && !turnModel.modelUnknown
    && !hasPending
    && !hasDeferredShared
    && snapshot.thread?.llm_model === currentPick.model,
  );

  const matchesCurrentPick = Boolean(
    currentPick
    && turnModel !== null
    && !turnModel.modelUnknown
    && (
      (turnModel.model === currentPick.model && (turnModel.pickSeq == null || turnModel.pickSeq === currentPick.seq))
      || implicitSavedPersonalPick
    ),
  );

  const preserveUnsentSeq = !matchesCurrentPick && currentPick ? currentPick.seq : null;

  if (!turnModel || turnModel.admitted === false) {
    return { matchesCurrentPick, consumedPick: null, preserveUnsentSeq };
  }

  if (currentPick && matchesCurrentPick) {
    return {
      matchesCurrentPick,
      preserveUnsentSeq,
      consumedPick: {
        threadId: liveThreadId,
        model: currentPick.model,
        seq: currentPick.seq,
        startThread: snapshot.thread,
        startUpdatedAt: snapshot.updatedAt,
      },
    };
  }

  if (turnModel.model !== null && turnModel.pickSeq != null) {
    return {
      matchesCurrentPick,
      preserveUnsentSeq,
      consumedPick: {
        threadId: liveThreadId,
        model: turnModel.model,
        seq: turnModel.pickSeq,
        startThread: snapshot.thread,
        startUpdatedAt: snapshot.updatedAt,
      },
    };
  }

  return { matchesCurrentPick, consumedPick: null, preserveUnsentSeq };
}

export interface TurnPickRecordingRefs {
  manualPickRef: MutableRefObject<ManualModelPick | null>;
  isSharedOrUnresolvedRef: MutableRefObject<boolean>;
  pendingRef: MutableRefObject<string | null>;
  deferredSharedPickRef: MutableRefObject<DeferredSharedPick | null>;
  preservedUnsentSeqRef: MutableRefObject<number | null>;
  repatchExpectedRef?: MutableRefObject<RepatchExpectedModel>;
}

/**
 * Computes the server `llm_model` value that the ending turn itself is expected
 * to have left on the thread row when an unsent newer pick is being protected.
 * A background repatch only fires if the reread row still holds this exact model.
 */
export function resolveRepatchExpectedModel(
  turnModel: ActiveTurnModelInfo | null,
  currentPick: ManualModelPick | null,
  snapshot: { thread: Thread | undefined; updatedAt: number },
): RepatchExpectedModel {
  if (!turnModel) return { known: false, model: null };
  if (turnModel.model !== null) return { known: true, model: turnModel.model };
  if (turnModel.modelUnknown) {
    if (currentPick && 'beforeRowModel' in currentPick) {
      return { known: true, model: currentPick.beforeRowModel ?? null };
    }
    return { known: false, model: null };
  }
  if (snapshot.thread !== undefined) {
    return { known: true, model: snapshot.thread.llm_model ?? null };
  }
  return { known: false, model: null };
}

/**
 * Thin-hook helper for `useThreadModel.ts`: computes consumed turn pick,
 * records unsent-pick protection in `preservedUnsentSeqRef`, and records the
 * expected old-turn model for conditional repatching.
 */
export function recordDaltonTurnPick(
  turnModel: ActiveTurnModelInfo | null,
  snapshot: { thread: Thread | undefined; updatedAt: number },
  liveThreadId: string | null,
  refs: TurnPickRecordingRefs,
): TrackedTurnPick | null {
  const { consumedPick, preserveUnsentSeq } = resolveTurnPickConsumption({
    turnModel,
    currentPick: refs.manualPickRef.current,
    liveThreadId,
    isSharedOrUnresolved: refs.isSharedOrUnresolvedRef.current,
    hasPending: refs.pendingRef.current !== null,
    hasDeferredShared: refs.deferredSharedPickRef.current !== null,
    snapshot,
  });
  if (preserveUnsentSeq !== null) {
    refs.preservedUnsentSeqRef.current = preserveUnsentSeq;
    if (refs.repatchExpectedRef) {
      refs.repatchExpectedRef.current = resolveRepatchExpectedModel(
        turnModel,
        refs.manualPickRef.current,
        snapshot,
      );
    }
  }
  return consumedPick;
}

/**
 * True only when the reread row still matches the model written by the old turn
 * that just finished. If another tab or `switch_thread_model` moved the thread
 * to a different model, returns false so the client abandons repatching.
 */
export function shouldRepatchPreservedPick(
  freshModel: string | null,
  repatchExpected: RepatchExpectedModel | null | undefined,
): boolean {
  if (!isDaltonBuild || !repatchExpected?.known) return false;
  return freshModel === repatchExpected.model;
}

/**
 * True when an unresolved-permission deferred model pick is still eligible to
 * flush: returns false if the cached thread row already moved away from the
 * model observed when the deferred pick was staged.
 */
export function shouldFlushDeferredModelPick(
  pick: DeferredSharedPick,
  thread: Thread | undefined,
): boolean {
  if (!isDaltonBuild || !pick.hasExpectedModel || thread === undefined) return true;
  return (thread.llm_model ?? null) === (pick.expectedModel ?? null);
}

/**
 * Builds the optional CAS guard fields sent on a deferred model flush.
 */
export function buildDeferredModelConditions(
  pick: DeferredSharedPick,
  shouldClearEffort: boolean,
): ThreadFieldSaveConditions | undefined {
  if (!isDaltonBuild) return undefined;
  const conditions: ThreadFieldSaveConditions = {};
  if (pick.hasExpectedModel) {
    conditions.expected_llm_model = pick.expectedModel ?? null;
  }
  if (shouldClearEffort && pick.hasExpectedEffort) {
    conditions.expected_reasoning_effort = pick.expectedEffort ?? null;
  }
  return Object.keys(conditions).length > 0 ? conditions : undefined;
}

/**
 * Refreshes a queued message's reasoningEffort / fastMode from the live composer
 * at flush time instead of sending the stale snapshot captured when it was queued.
 *
 * - If the queued message explicitly targets a different model than the composer
 *   now shows (e.g. a queued Antigravity High turn after the user switched the
 *   composer to a standard model), keeps the queued message's own tuning.
 * - Otherwise, when live composer options are available, adopts their latest
 *   `reasoningEffort` and `fastMode`.
 * - When no composer reader is mounted (headless caller), clears the stale
 *   enqueue-time snapshot so the server reads the thread row's latest tuning.
 */
export function mergeLatestQueuedTuning<
  T extends {
    model?: string | null;
    reasoningEffort?: string | null;
    fastMode?: boolean | null;
  },
>(
  queuedOptions: T,
  latestComposerOptions:
    | {
        model?: string | null;
        reasoningEffort?: string | null;
        fastMode?: boolean | null;
      }
    | undefined,
): T {
  if (latestComposerOptions !== undefined) {
    if (
      queuedOptions.model
      && latestComposerOptions.model
      && queuedOptions.model !== latestComposerOptions.model
    ) {
      return queuedOptions;
    }
    return {
      ...queuedOptions,
      reasoningEffort: latestComposerOptions.reasoningEffort ?? null,
      ...(latestComposerOptions.fastMode !== undefined
        ? { fastMode: latestComposerOptions.fastMode }
        : {}),
    };
  }
  return {
    ...queuedOptions,
    reasoningEffort: undefined,
    fastMode: undefined,
  };
}

/**
 * Attaches `modelPickSeq` to composer options returned by `getModelOptions`
 * when the options carry the thread's active manual `sendModel` in Dalton
 * builds (used by `ChatView.tsx` and `MarketChatPanel.tsx` for HITL resumes).
 */
export function attachComposerModelPickSeq<
  T extends { model?: string | null; modelPickSeq?: number | null },
>(
  options: T | undefined,
  threadModel: { sendModel: string | null; sendModelSeq: number | null },
): T | undefined {
  if (!options || !isDaltonBuild) return options;
  if (options.modelPickSeq != null) return options;
  if (options.model && options.model === threadModel.sendModel && threadModel.sendModelSeq != null) {
    return { ...options, modelPickSeq: threadModel.sendModelSeq };
  }
  return options;
}

/**
 * Widget prompt payload helper for MarketChatPanel: in Dalton builds forwards
 * ONLY `model` and `modelPickSeq` (never uncommitted composer effort, fast
 * mode, or subagent settings); in non-Dalton builds returns `{}`.
 */
export function buildWidgetPromptModelOptions(
  composerModel: string | null | undefined,
  threadModel: { sendModel: string | null; sendModelSeq: number | null },
): { model?: string | null; modelPickSeq?: number } {
  if (!isDaltonBuild) return {};
  const model = composerModel ?? threadModel.sendModel;
  const modelPickSeq = model && model === threadModel.sendModel && threadModel.sendModelSeq != null
    ? threadModel.sendModelSeq
    : undefined;
  return {
    ...(model !== undefined ? { model } : {}),
    ...(modelPickSeq !== undefined ? { modelPickSeq } : {}),
  };
}
