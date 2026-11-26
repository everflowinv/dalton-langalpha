import { isDaltonBuild } from '@/config/brand';

export interface ActiveTurnModel {
  model: string | null;
  pickSeq: number | null;
  admitted: boolean;
}

export interface SubmittedTurnPick {
  model: string | null;
  pickSeq: number | null;
}

export interface TurnModelTracker {
  recordSubmission: (requestKey: string, pick: SubmittedTurnPick) => void;
  clearKey: (requestKey: string) => void;
  resolveAdoptedDuplicate: (
    requestKey: string,
    errorInfo: Record<string, unknown> | undefined,
  ) => ActiveTurnModel | null;
}

/**
 * Stream-level refusal codes emitted by START / admission before a run row is
 * committed. Receiving one of these frames means the server refused the turn
 * rather than admitting it.
 */
const STREAM_ADMISSION_REFUSAL_CODES: ReadonlySet<string> = new Set([
  'duplicate_request',
  'stale_fork',
  'stale_retry',
  'scope_changed',
  'running',
  'stopping',
  'compacting',
  'not_running',
  'request_cancelled',
  'not_retryable',
  'steer_taken',
  'workspace_moving',
  'comments_unavailable',
  'another_member',
  'turn_conflict',
  'attempt_conflict',
  'writer_capacity',
  'resend_required',
  'colleague_access_revoked',
  'catalog_route_changed',
]);

const STREAM_ADMISSION_REFUSAL_ERROR_TYPES: ReadonlySet<string> = new Set([
  'admission_conflict',
  'duplicate_request',
  'capacity_limit',
  'external_id_conflict',
]);

/**
 * True when an SSE frame is a pre-START admission refusal rather than a run
 * event. Used by transport.ts to withhold `onRunIdResolved` and by
 * useChatMessages.ts to keep refused HITL resumes unadmitted.
 */
export function isStreamAdmissionRefusalFrame(event: Record<string, unknown>): boolean {
  if (event.event !== 'error') return false;
  const errorType = typeof event.error_type === 'string' ? event.error_type : null;
  if (errorType && STREAM_ADMISSION_REFUSAL_ERROR_TYPES.has(errorType)) {
    return true;
  }
  if ((errorType === 'policy_denied' || errorType === 'account_suspended') && !event.run_id) {
    return true;
  }
  const code = typeof event.code === 'string' ? event.code : null;
  return code !== null && STREAM_ADMISSION_REFUSAL_CODES.has(code);
}

/**
 * True when a `retry` frame reports that the server never started a run
 * (`recovery: 'resend'` without a `run_id`).
 */
export function isUnstartedResendFrame(event: Record<string, unknown>): boolean {
  return event.event === 'retry' && event.recovery === 'resend' && typeof event.run_id !== 'string';
}

/**
 * True when an SSE frame proves the server actually admitted and started a run
 * for this request (not a hold, not a steer merged into another run, not a
 * refusal frame, and not an unstarted resend notice).
 */
export function isStreamRunAdmissionEvent(
  event: Record<string, unknown>,
  isSubagent = false,
): boolean {
  if (isSubagent) return false;
  if (event.event === 'turn_held' || event.event === 'steering_accepted') return false;
  if (isStreamAdmissionRefusalFrame(event)) return false;
  if (event.event === 'retry') {
    return typeof event.run_id === 'string' && event.run_id.length > 0;
  }
  if (event.event === 'error') {
    return Boolean(event.recovery) && typeof event.run_id === 'string' && event.run_id.length > 0;
  }
  return true;
}

const MAX_TRACKED_KEYS = 64;

function readDuplicateRunModel(errorInfo: Record<string, unknown> | undefined): string | null {
  if (!errorInfo) return null;
  const raw = errorInfo.llm_model ?? errorInfo.model;
  return typeof raw === 'string' && raw.length > 0 ? raw : null;
}

/**
 * Tracks the FIRST submitted `{ model, pickSeq }` for each `requestKey` so a
 * later retransmit under the same key (after the user switched models) never
 * attributes a 409 `duplicate_request` adoption to the newly read pick.
 */
export function createTurnModelTracker(): TurnModelTracker {
  const byKey = new Map<string, SubmittedTurnPick>();

  return {
    recordSubmission(requestKey, pick) {
      if (!isDaltonBuild || !requestKey || byKey.has(requestKey)) return;
      if (byKey.size >= MAX_TRACKED_KEYS) {
        const oldest = byKey.keys().next().value;
        if (oldest !== undefined) byKey.delete(oldest);
      }
      byKey.set(requestKey, { model: pick.model, pickSeq: pick.pickSeq });
    },
    clearKey(requestKey) {
      if (requestKey) byKey.delete(requestKey);
    },
    resolveAdoptedDuplicate(requestKey, errorInfo) {
      if (!isDaltonBuild) return null;
      const recorded = requestKey ? byKey.get(requestKey) : undefined;
      if (requestKey) byKey.delete(requestKey);
      const serverModel = readDuplicateRunModel(errorInfo);
      if (serverModel !== null) {
        if (recorded && recorded.model === serverModel) {
          return { model: recorded.model, pickSeq: recorded.pickSeq, admitted: true };
        }
        return { model: serverModel, pickSeq: null, admitted: true };
      }
      if (recorded) {
        return { model: recorded.model, pickSeq: recorded.pickSeq, admitted: true };
      }
      return null;
    },
  };
}

export function applyAdoptedDuplicateTurnModel(
  tracker: TurnModelTracker,
  requestKey: string,
  err: unknown,
  setActiveTurnModel: (updater: (prev: ActiveTurnModel | null) => ActiveTurnModel | null) => void,
): void {
  if (!isDaltonBuild) return;
  const errorInfo = (err as { errorInfo?: Record<string, unknown> } | null | undefined)?.errorInfo;
  const adopted = tracker.resolveAdoptedDuplicate(requestKey, errorInfo);
  if (adopted) {
    setActiveTurnModel(() => adopted);
  } else {
    setActiveTurnModel((prev) => (prev && prev.admitted ? { ...prev, admitted: false } : prev));
  }
}

export interface HitlResumeModelGuardDeps {
  requestKey: string;
  pick: SubmittedTurnPick;
  tracker: TurnModelTracker;
  setActiveTurnModel: (updater: (prev: ActiveTurnModel | null) => ActiveTurnModel | null) => void;
  getWorkflowStatus: (threadId: string) => Promise<{ can_reconnect?: boolean; run_id?: string | null }>;
  threadId: string;
}

function parseRunIdFromLocation(contentLocation: string | null | undefined): string | null {
  if (!contentLocation) return null;
  const qIdx = contentLocation.indexOf('?');
  if (qIdx === -1) return null;
  try {
    const runId = new URLSearchParams(contentLocation.slice(qIdx + 1)).get('run_id');
    return runId && runId.length > 0 ? runId : null;
  } catch {
    return null;
  }
}

export interface HitlResumeModelGuard {
  /** Returns true when `event` is an admission refusal frame that must be handled as refused. */
  observeEvent: (event: Record<string, unknown>, isSubagent: boolean) => boolean;
  onRunIdResolved: () => void;
  onDuplicateAdopted: (err: unknown) => void;
  /**
   * Verifies a stream disconnect that happened before any admission signal.
   * Returns `'unadmitted'` when status proves this request never started on the
   * server, or `'reconnect'` to hand off to reconnect (with `activeTurnModel`
   * confirmed only when the server's active run matches this request's run id).
   */
  verifyDisconnect: (
    contentLocation: string | null | undefined,
    isResendNotice: boolean,
    onConfirmedRun: (runId: string) => void,
  ) => Promise<'reconnect' | 'unadmitted'>;
  /** True when a Dalton resume stream closed cleanly without ever receiving an admission signal. */
  isUnadmittedCleanEnd: () => boolean;
}

/**
 * Encapsulates Dalton's HITL resume admission tracking so `useChatMessages.ts`
 * stays thin while ensuring `activeTurnModel.admitted` only flips true on real
 * server admission signals (non-hold non-refusal frame, `onRunIdResolved`,
 * verified disconnect run match, or verified 409 duplicate adoption).
 */
export function createHitlResumeModelGuard({
  requestKey,
  pick,
  tracker,
  setActiveTurnModel,
  getWorkflowStatus,
  threadId,
}: HitlResumeModelGuardDeps): HitlResumeModelGuard {
  let serverAdmitted = false;
  let refused = false;

  if (isDaltonBuild) {
    tracker.recordSubmission(requestKey, pick);
    setActiveTurnModel(() => ({ model: pick.model, pickSeq: pick.pickSeq, admitted: false }));
  }

  const confirm = () => {
    if (!isDaltonBuild || refused) return;
    setActiveTurnModel((prev) => (prev && !prev.admitted ? { ...prev, admitted: true } : prev));
  };

  const unadmit = () => {
    if (!isDaltonBuild) return;
    setActiveTurnModel((prev) => (prev && prev.admitted ? { ...prev, admitted: false } : prev));
  };

  return {
    observeEvent(event, isSubagent) {
      if (isStreamAdmissionRefusalFrame(event)) {
        refused = true;
        serverAdmitted = false;
        unadmit();
        return true;
      }
      if (isUnstartedResendFrame(event)) {
        serverAdmitted = false;
        unadmit();
        return false;
      }
      if (!refused && isStreamRunAdmissionEvent(event, isSubagent)) {
        serverAdmitted = true;
        tracker.clearKey(requestKey);
        confirm();
      }
      return false;
    },
    onRunIdResolved() {
      if (refused) return;
      serverAdmitted = true;
      tracker.clearKey(requestKey);
      confirm();
    },
    onDuplicateAdopted(err) {
      applyAdoptedDuplicateTurnModel(tracker, requestKey, err, setActiveTurnModel);
    },
    async verifyDisconnect(contentLocation, isResendNotice, onConfirmedRun) {
      if (!isDaltonBuild) return 'reconnect';
      if (serverAdmitted) return 'reconnect';
      if (isResendNotice) {
        unadmit();
        return 'reconnect';
      }
      if (!threadId || threadId === '__default__') return 'unadmitted';
      const expectedRunId = parseRunIdFromLocation(contentLocation);
      try {
        const status = await getWorkflowStatus(threadId);
        if (!status?.can_reconnect) {
          unadmit();
          return 'unadmitted';
        }
        if (expectedRunId) {
          if (status.run_id === expectedRunId) {
            onConfirmedRun(expectedRunId);
            return 'reconnect';
          }
          if (status.run_id && status.run_id !== expectedRunId) {
            unadmit();
            return 'unadmitted';
          }
        }
        // Reconnect without falsely confirming the pick when no run_id matched.
        unadmit();
        return 'reconnect';
      } catch {
        // Offline / status unreachable: let reconnect retry, keeping admitted: false.
        unadmit();
        return 'reconnect';
      }
    },
    isUnadmittedCleanEnd() {
      return isDaltonBuild && !serverAdmitted;
    },
  };
}
