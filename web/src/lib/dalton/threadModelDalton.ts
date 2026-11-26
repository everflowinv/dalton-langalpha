import { isDaltonBuild } from '@/config/brand';
import type { Thread } from '@/types/api';

export interface ManualModelPick {
  model: string;
  seq: number;
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
}

export interface PendingSettledTracker {
  register: (model: string, onSettled?: (saved: boolean) => void) => void;
  revoke: () => void;
  take: (model: string) => ((saved: boolean) => void) | undefined;
  confirmIfMatching: (model: string | null) => void;
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
 * differs from the user's current selection).
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
    && !hasPending
    && !hasDeferredShared
    && snapshot.thread?.llm_model === currentPick.model,
  );

  const matchesCurrentPick = Boolean(
    currentPick
    && turnModel !== null
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
