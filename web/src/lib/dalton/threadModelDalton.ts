import { useCallback, type MutableRefObject } from 'react';
import { isDaltonBuild } from '@/config/brand';
import { useAllModels } from '@/hooks/useAllModels';
import { useCapabilities } from '@/hooks/useCapabilities';
import { inspectSavedAntigravityModel, type InspectAntigravityOptions } from '@/lib/antigravitySeries';
import type { ThreadFieldSaveConditions } from '@/lib/dalton/threadFieldSaveDalton';
import type { ClearLocalHandle } from '@/lib/dalton/threadTuningDalton';
import type { Thread } from '@/types/api';

export interface ManualModelPick {
  model: string;
  seq: number;
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
 * Only when the ending turn explicitly carried a known model (`turnModel.model !== null`
 * and `!turnModel.modelUnknown`) can we confirm that the old turn wrote it;
 * otherwise returns `{ known: false, model: null }` so another tab's write is
 * never mistaken for the old turn's write.
 */
export function resolveRepatchExpectedModel(
  turnModel: ActiveTurnModelInfo | null,
): RepatchExpectedModel {
  if (!turnModel || turnModel.modelUnknown || turnModel.model === null) {
    return { known: false, model: null };
  }
  return { known: true, model: turnModel.model };
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
      refs.repatchExpectedRef.current = resolveRepatchExpectedModel(turnModel);
    }
  }
  return consumedPick;
}

/**
 * True when `model` is a built-in Antigravity variant whose server-side PATCH
 * will atomically clear `reasoning_effort = null`, so conditional background
 * saves must guard `expected_reasoning_effort` (including explicit `null`).
 */
export function isAntigravityVariantForClear(
  model: string | null | undefined,
  seriesOpts: InspectAntigravityOptions,
): boolean {
  if (!model) return false;
  if (inspectSavedAntigravityModel(model, seriesOpts)) return true;
  const hasLoadedSeries = Boolean(seriesOpts.seriesCatalog && seriesOpts.seriesCatalog.series.length > 0);
  if (hasLoadedSeries) return false;
  if (seriesOpts.customModels?.some((cm) => cm.name === model)) return false;
  return fallbackAntigravitySeriesId(model) !== null;
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
  if (!repatchExpected?.known) return false;
  return freshModel === repatchExpected.model;
}

export type FirstTurnRepatchAction = 'repatch' | 'keep_local' | 'follow_server';

/**
 * Decides what first-turn reread should do with a pre-id model pick made while
 * the new thread was being created (applies in both Dalton and Core builds):
 * - `turnAdmitted === true`: the first send ran `keep_named_model(firstSendModel)`,
 *   so repatch only while `freshModel === firstSendModel`; otherwise another tab
 *   changed it -> `'follow_server'`.
 * - `turnAdmitted === false`: the first send failed before admission (e.g. 503),
 *   so the row still holds `null` (or `firstSendModel` if the server admitted
 *   before disconnect). Repatch with `expected_llm_model: freshModel` when
 *   `freshModel === null || freshModel === firstSendModel`; otherwise another
 *   tab wrote a different model -> `'follow_server'`.
 * - `turnAdmitted === null` (indeterminate): repatch if `freshModel === firstSendModel`,
 *   keep the user's unsent pick locally without PATCH when `freshModel === null`
 *   (cannot distinguish pre-admission failure from another tab resetting to null),
 *   and follow server when `freshModel` is a different non-null model.
 */
export function resolveFirstTurnRepatchAction(
  freshModel: string | null,
  firstSendModel: string | null,
  turnAdmitted: boolean | null,
): FirstTurnRepatchAction {
  if (turnAdmitted === true) {
    return freshModel === firstSendModel ? 'repatch' : 'follow_server';
  }
  if (turnAdmitted === false) {
    return freshModel === null || freshModel === firstSendModel ? 'repatch' : 'follow_server';
  }
  if (freshModel === firstSendModel) return 'repatch';
  if (freshModel === null && firstSendModel !== null) return 'keep_local';
  return 'follow_server';
}

/**
 * True when a pre-id pick made during a new thread's first turn may still
 * background-repatch on first-turn reread.
 */
export function shouldRepatchFirstTurnPick(
  freshModel: string | null,
  firstSendModel: string | null,
): boolean {
  return resolveFirstTurnRepatchAction(freshModel, firstSendModel, true) === 'repatch';
}

/**
 * True when an unresolved-permission deferred model pick is still eligible to
 * flush:
 * - If the thread detail had not loaded when the pick was staged
 *   (`!pick.hasExpectedModel`, or `willClearEffort && !pick.hasExpectedEffort`),
 *   cancels automatic flush so we never issue an unconditional PATCH over a
 *   newer write from another tab.
 * - Also returns false if the cached thread row already moved away from the
 *   model (or reasoning_effort when the write clears effort) observed when the
 *   deferred pick was staged.
 */
export function shouldFlushDeferredModelPick(
  pick: DeferredSharedPick,
  thread: Thread | undefined,
  willClearEffort = false,
): boolean {
  if (!pick.hasExpectedModel) return false;
  if (willClearEffort && !pick.hasExpectedEffort) return false;
  if (thread === undefined) return true;
  if ((thread.llm_model ?? null) !== (pick.expectedModel ?? null)) return false;
  if (willClearEffort && (thread.reasoning_effort ?? null) !== (pick.expectedEffort ?? null)) {
    return false;
  }
  return true;
}

/**
 * Builds the optional CAS guard fields sent on a deferred model flush.
 * Whenever `willClearEffort` is true (including built-in Antigravity variants
 * where `expectedEffort` is `null`), explicitly attaches
 * `expected_reasoning_effort` (including `null`).
 */
export function buildDeferredModelConditions(
  pick: DeferredSharedPick,
  willClearEffort: boolean,
): ThreadFieldSaveConditions | undefined {
  const conditions: ThreadFieldSaveConditions = {};
  if (pick.hasExpectedModel) {
    conditions.expected_llm_model = pick.expectedModel ?? null;
  }
  if (willClearEffort && pick.hasExpectedEffort) {
    conditions.expected_reasoning_effort = pick.expectedEffort ?? null;
  }
  return Object.keys(conditions).length > 0 ? conditions : undefined;
}

const ANTIGRAVITY_EFFORT_LEVELS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] as const;

function fallbackAntigravitySeriesId(modelKey: string | null | undefined): string | null {
  if (!modelKey || !modelKey.endsWith('-oauth')) return null;
  const stem = modelKey.slice(0, -'-oauth'.length);
  if (!stem) return null;
  for (const effort of ANTIGRAVITY_EFFORT_LEVELS) {
    const suffix = `-${effort}`;
    if (stem.endsWith(suffix) && stem.length > suffix.length) {
      return `antigravity-oauth:${stem.slice(0, -suffix.length)}`;
    }
  }
  return null;
}

export interface QueuedTuningOptions {
  model?: string | null;
  modelPickSeq?: number | null;
  displayModel?: string | null;
  antigravitySeriesId?: string | null;
  reasoningEffort?: string | null;
  fastMode?: boolean | null;
}

function resolveQueuedModelAndSeries(
  opts: QueuedTuningOptions,
  antigravityEnabled: boolean,
): { effectiveModel: string | null; seriesId: string | null } {
  const effectiveModel = opts.displayModel ?? opts.model ?? null;
  if (!antigravityEnabled) {
    return { effectiveModel, seriesId: null };
  }
  if (opts.antigravitySeriesId !== undefined) {
    return { effectiveModel, seriesId: opts.antigravitySeriesId };
  }
  return { effectiveModel, seriesId: fallbackAntigravitySeriesId(effectiveModel) };
}

/**
 * Refreshes a queued message's tuning (and intra-series Antigravity variant)
 * from the live composer at flush time instead of sending the stale snapshot
 * captured when it was queued.
 *
 * - Disabled when `isDaltonBuild` (`VITE_DALTON_UI`) is false: returns
 *   `queuedOptions` unchanged.
 * - Compares displayed models (`displayModel ?? model`) and Antigravity series:
 *   - Same Antigravity series (e.g. `…-high-oauth` -> `…-low-oauth`): adopts
 *     the live composer's current variant (`model`, `modelPickSeq`, `displayModel`,
 *     `antigravitySeriesId`), always clears standard `reasoningEffort` to `null`,
 *     and adopts live `fastMode`.
 *   - Different Antigravity series or Antigravity <-> standard: preserves the
 *     queued message's model snapshot and always clears standard `reasoningEffort`
 *     to `null` when the queued message targets an Antigravity variant.
 *   - Different standard models: preserves the queued message's snapshot.
 *   - Same standard model: adopts live `reasoningEffort` and `fastMode`.
 * - When no composer reader is mounted (headless caller), clears the stale
 *   enqueue-time snapshot so the server reads the thread row's latest tuning.
 */
export function mergeLatestQueuedTuning<T extends QueuedTuningOptions>(
  queuedOptions: T,
  latestComposerOptions: QueuedTuningOptions | undefined,
  { antigravityEnabled = true }: { antigravityEnabled?: boolean } = {},
): T {
  if (!isDaltonBuild) return queuedOptions;
  const hasExplicitModel = typeof queuedOptions.model === 'string' && queuedOptions.model.length > 0;
  const queued = resolveQueuedModelAndSeries(queuedOptions, antigravityEnabled);

  if (latestComposerOptions !== undefined) {
    const latest = resolveQueuedModelAndSeries(latestComposerOptions, antigravityEnabled);

    // 1) Queued message follows the thread's model (`model` is null/omitted):
    //    its final target model is whatever the thread/composer now holds, so
    //    adopt the live target model's tuning and NEVER carry an old model's
    //    `reasoningEffort` onto a new model (especially an Antigravity variant).
    if (!hasExplicitModel) {
      if (queued.seriesId !== null && latest.seriesId !== null && latest.seriesId === queued.seriesId) {
        const nextModel = latestComposerOptions.model !== undefined
          ? latestComposerOptions.model
          : queuedOptions.model;
        const nextPickSeq = latestComposerOptions.modelPickSeq !== undefined
          ? latestComposerOptions.modelPickSeq
          : (nextModel === queuedOptions.model ? queuedOptions.modelPickSeq : undefined);
        return {
          ...queuedOptions,
          model: nextModel,
          modelPickSeq: nextPickSeq,
          ...(latestComposerOptions.displayModel !== undefined
            ? { displayModel: latestComposerOptions.displayModel }
            : {}),
          ...(latestComposerOptions.antigravitySeriesId !== undefined
            ? { antigravitySeriesId: latestComposerOptions.antigravitySeriesId }
            : {}),
          reasoningEffort: null,
          ...(latestComposerOptions.fastMode !== undefined
            ? { fastMode: latestComposerOptions.fastMode }
            : {}),
        };
      }
      return {
        ...queuedOptions,
        ...(latestComposerOptions.displayModel !== undefined
          ? { displayModel: latestComposerOptions.displayModel }
          : {}),
        ...(latestComposerOptions.antigravitySeriesId !== undefined
          ? { antigravitySeriesId: latestComposerOptions.antigravitySeriesId }
          : {}),
        reasoningEffort: latest.seriesId !== null ? null : (latestComposerOptions.reasoningEffort ?? null),
        ...(latestComposerOptions.fastMode !== undefined
          ? { fastMode: latestComposerOptions.fastMode }
          : {}),
      };
    }

    // 2) Queued message carries an explicit `model`:
    if (queued.seriesId !== null) {
      if (latest.seriesId !== null && latest.seriesId === queued.seriesId) {
        const nextModel = latestComposerOptions.model
          ?? (latestComposerOptions.displayModel && latest.seriesId === queued.seriesId
            ? latestComposerOptions.displayModel
            : queuedOptions.model);
        const nextPickSeq = latestComposerOptions.modelPickSeq !== undefined
          ? latestComposerOptions.modelPickSeq
          : (nextModel === queuedOptions.model ? queuedOptions.modelPickSeq : undefined);
        return {
          ...queuedOptions,
          model: nextModel,
          modelPickSeq: nextPickSeq,
          ...(latestComposerOptions.displayModel !== undefined
            ? { displayModel: latestComposerOptions.displayModel }
            : {}),
          ...(latestComposerOptions.antigravitySeriesId !== undefined
            ? { antigravitySeriesId: latestComposerOptions.antigravitySeriesId }
            : {}),
          reasoningEffort: null,
          ...(latestComposerOptions.fastMode !== undefined
            ? { fastMode: latestComposerOptions.fastMode }
            : {}),
        };
      }
      return {
        ...queuedOptions,
        reasoningEffort: null,
      };
    }

    if (latest.seriesId !== null) {
      return queuedOptions;
    }

    if (
      queued.effectiveModel
      && latest.effectiveModel
      && queued.effectiveModel !== latest.effectiveModel
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

  if (hasExplicitModel) {
    return {
      ...queuedOptions,
      ...(queued.seriesId !== null ? { reasoningEffort: null } : {}),
    };
  }

  return {
    ...queuedOptions,
    reasoningEffort: undefined,
    fastMode: undefined,
  };
}

/**
 * Strips standard `reasoningEffort` when assembling a send payload targeting a
 * built-in Antigravity variant (whose effort is encoded in the model key).
 */
export function sanitizeDaltonSendEffort(
  options: {
    model?: string | null;
    displayModel?: string | null;
    antigravitySeriesId?: string | null;
    reasoningEffort?: string | null;
  },
  { antigravityEnabled = true }: { antigravityEnabled?: boolean } = {},
): string | null {
  const effort = options.reasoningEffort || null;
  if (!isDaltonBuild || !antigravityEnabled || effort === null) return effort;
  const effectiveModel = options.model ?? options.displayModel ?? null;
  const seriesId = options.antigravitySeriesId !== undefined
    ? options.antigravitySeriesId
    : fallbackAntigravitySeriesId(effectiveModel);
  if (seriesId !== null) return null;
  return effort;
}

/**
 * Dalton hook for `useChatMessages`: reads `native_oauth_sync` via
 * `useCapabilities()` and exposes queued-send tuning merger + send effort
 * sanitizer so upstream `useChatMessages.ts` only carries thin hook calls.
 */
export function useDaltonQueuedSendTuning() {
  const { hasCapability } = useCapabilities({ enabled: isDaltonBuild });
  const antigravityEnabled = hasCapability('native_oauth_sync');

  const resolveQueuedOptions = useCallback(
    <T extends QueuedTuningOptions>(
      queuedOptions: T,
      latestComposerOptions: QueuedTuningOptions | undefined,
    ): T => mergeLatestQueuedTuning(queuedOptions, latestComposerOptions, { antigravityEnabled }),
    [antigravityEnabled],
  );

  const sanitizeSendEffort = useCallback(
    (options: {
      model?: string | null;
      displayModel?: string | null;
      antigravitySeriesId?: string | null;
      reasoningEffort?: string | null;
    }): string | null => sanitizeDaltonSendEffort(options, { antigravityEnabled }),
    [antigravityEnabled],
  );

  return { resolveQueuedOptions, sanitizeSendEffort };
}

/**
 * Attaches displayed-model & Antigravity series metadata to composer options
 * in Dalton builds so queued-send flushes can compare displayed models & series
 * even when `sendModel` is `null` (following the thread row).
 */
export function withDaltonComposerMeta<T extends object>(
  options: T,
  meta: { displayModel: string | null; antigravitySeriesId: string | null },
): T & { displayModel?: string | null; antigravitySeriesId?: string | null } {
  if (!isDaltonBuild) return options;
  return {
    ...options,
    displayModel: meta.displayModel,
    antigravitySeriesId: meta.antigravitySeriesId,
  };
}

/**
 * Dalton hook wrapping `pickModel` + `clearLocalEffort` for surfaces like
 * `MarketView` mobile FAB so switching to an Antigravity variant atomically
 * clears local standard reasoning effort and rolls back on failure.
 */
export function useDaltonModelPickerWithEffortClear(
  pickModel: (model: string, options?: { onSettled?: (saved: boolean) => void }) => Promise<boolean>,
  clearLocalEffort: () => ClearLocalHandle,
): (nextModel: string) => Promise<boolean> {
  const { seriesCatalog, customModels, metadata: modelMetadata, rawApiResponse } = useAllModels();
  return useCallback((nextModel: string) => {
    const rawMeta = rawApiResponse?.model_metadata ?? modelMetadata;
    const isAntigravity = Boolean(
      inspectSavedAntigravityModel(nextModel, { seriesCatalog, rawMetadata: rawMeta, customModels }),
    );
    if (!isDaltonBuild) {
      if (isAntigravity) clearLocalEffort();
      return pickModel(nextModel);
    }
    const effortClear = isAntigravity ? clearLocalEffort() : null;
    return pickModel(nextModel, effortClear ? {
      onSettled: (saved) => {
        if (saved) effortClear.confirm();
        else effortClear();
      },
    } : undefined).catch((err: unknown) => {
      effortClear?.();
      throw err;
    });
  }, [clearLocalEffort, customModels, modelMetadata, pickModel, rawApiResponse, seriesCatalog]);
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
