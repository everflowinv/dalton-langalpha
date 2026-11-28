import type { MutableRefObject } from 'react';

export interface ExplicitRetryEffortState {
  targetId: string | null;
  effort: string | null;
}

export interface SeriesEffortModelState {
  model: string;
  targetId: string | null;
  activeForRetry: boolean;
}

export interface RetryEffortRefs {
  explicitRetryEffortRef: MutableRefObject<ExplicitRetryEffortState | undefined>;
  seriesEffortModelRef: MutableRefObject<SeriesEffortModelState | undefined>;
}

/**
 * True when the composer already holds a draft (text, files, chart screenshot,
 * chips, widget snapshots, or external context), meaning an effort toggle
 * belongs to that draft rather than a retry of a failed reply.
 */
export function hasComposerDraftContent(draft: {
  message: string;
  attachedCount: number;
  hasChartImage: boolean;
  chipsCount: number;
  widgetSnapshotsCount: number;
  hasExternalContext: boolean;
}): boolean {
  return Boolean(
    draft.message.trim()
    || draft.attachedCount > 0
    || draft.hasChartImage
    || draft.chipsCount > 0
    || draft.widgetSnapshotsCount > 0
    || draft.hasExternalContext,
  );
}

/**
 * Updates retry-effort tracking when the user picks an effort level (either a
 * series variant on Antigravity models or a standard reasoning_effort override).
 */
export function recordEffortPickRetryIntent(
  refs: RetryEffortRefs,
  params: {
    isLoading: boolean;
    retryTargetId: string | null | undefined;
    hasDraftContent: boolean;
    seriesTargetModel?: string;
    effort: string | null;
    settingsEffort: string | null | undefined;
  },
): void {
  const eligibleForRetry = !params.isLoading && params.retryTargetId !== null && !params.hasDraftContent;
  if (params.seriesTargetModel !== undefined) {
    refs.seriesEffortModelRef.current = {
      model: params.seriesTargetModel,
      targetId: params.retryTargetId ?? null,
      activeForRetry: eligibleForRetry,
    };
    refs.explicitRetryEffortRef.current = undefined;
    return;
  }
  refs.explicitRetryEffortRef.current = eligibleForRetry
    ? { targetId: params.retryTargetId ?? null, effort: params.effort ?? params.settingsEffort ?? null }
    : undefined;
}

/**
 * Invalidates retry eligibility on draft edits or standalone slash actions
 * (`/compact`, `/offload`) without erasing `seriesEffortModelRef` over
 * `sentModel`, so a subsequent retry still suppresses an unsent series variant.
 */
export function invalidateRetryIntent(refs: RetryEffortRefs): void {
  refs.explicitRetryEffortRef.current = undefined;
  if (refs.seriesEffortModelRef.current) {
    refs.seriesEffortModelRef.current = {
      ...refs.seriesEffortModelRef.current,
      activeForRetry: false,
    };
  }
}
