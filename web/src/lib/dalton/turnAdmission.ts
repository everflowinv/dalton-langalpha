import { isDaltonBuild } from '@/config/brand';
import type { ChatMessage } from '@/types/chat';
import { finalizeAssistantMessage } from '@/pages/ChatAgent/session/stream/finalizeMessage';
import { updateMessage } from '@/pages/ChatAgent/hooks/utils/messageHelpers';

export interface ActiveTurnModel {
  model: string | null;
  pickSeq: number | null;
  admitted: boolean;
  /** True when 409 adopted an earlier run whose actual model is unknown (e.g.
   *  submission 1 did not name an explicit model and the server omitted
   *  `llm_model`), so the user's current pick must NOT be consumed. */
  modelUnknown?: boolean;
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
 * refusal frame, and not an unstarted retry/error frame lacking `run_id`).
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
      if (recorded && recorded.model !== null) {
        return { model: recorded.model, pickSeq: recorded.pickSeq, admitted: true };
      }
      // Adopted run's actual model is unknown (e.g. submission 1 relied on the
      // thread's saved model without naming llm_model and the server response
      // omitted llm_model): keep any newer pick unconsumed.
      return { model: null, pickSeq: null, admitted: true, modelUnknown: true };
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

export interface WorkflowStatusProbe {
  can_reconnect?: boolean;
  run_id?: string | null;
  status?: string;
  latest_turn_index?: number | null;
}

export interface RunLivenessProbe {
  runs?: Array<{ run_id: string; thread_id?: string; status?: string }>;
}

export type HitlDisconnectOutcome =
  | 'confirmed_reconnect'
  | 'unconfirmed_reconnect'
  | 'unadmitted';

export interface HitlResumeModelGuardDeps {
  requestKey: string;
  pick: SubmittedTurnPick;
  tracker: TurnModelTracker;
  setActiveTurnModel: (updater: (prev: ActiveTurnModel | null) => ActiveTurnModel | null) => void;
  getWorkflowStatus: (threadId: string) => Promise<WorkflowStatusProbe>;
  getRunLiveness?: (runId: string) => Promise<RunLivenessProbe>;
  threadId: string;
  turnIndexBefore?: number | null;
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

/**
 * Marks a refused or unstarted HITL resume placeholder bubble as `unadmitted`
 * so `projectTurns` / `newestKnownTurn()` does not assign it a backend turn
 * number and the next successful retry takes the right turn index.
 */
export function markHitlBubbleUnadmitted(
  setMessages: (updater: (prev: ChatMessage[]) => ChatMessage[]) => void,
  assistantMessageId: string,
  finalizeCompleted: boolean,
): void {
  setMessages((prev) =>
    updateMessage(prev, assistantMessageId, (msg) =>
      finalizeCompleted
        ? { ...finalizeAssistantMessage(msg, 'completed'), unadmitted: true, isHistory: false }
        : { ...msg, isStreaming: false, unadmitted: true, isHistory: false },
    ),
  );
}

export interface HitlResumeModelGuard {
  /** Returns true when `event` is an admission refusal frame that must be handled as refused. */
  observeEvent: (event: Record<string, unknown>, isSubagent: boolean) => boolean;
  onRunIdResolved: () => void;
  onDuplicateAdopted: (err: unknown) => void;
  /**
   * Verifies a pre-first-frame disconnect, unstarted `resend` notice, or
   * empty-body clean close without any admission signal:
   * - `'confirmed_reconnect'`: proven live or terminal via `Content-Location`
   *   `run_id` (confirmed via `/status` or `/dispatches/liveness`).
   * - `'unadmitted'`: proven never started (explicit `retry/resend` without
   *   `run_id`, or `/status` proves thread is still paused at `turnIndexBefore`
   *   with no matching run row).
   * - `'unconfirmed_reconnect'`: indeterminate — keep history reload via
   *   reconnect while keeping model selection unconfirmed (`admitted: false`).
   */
  verifyDisconnect: (
    contentLocation: string | null | undefined,
    isResendNotice: boolean,
    onConfirmedRun: (runId: string) => void,
  ) => Promise<HitlDisconnectOutcome>;
  /** True when a Dalton resume stream closed cleanly without ever receiving an admission signal. */
  isUnadmittedCleanEnd: () => boolean;
}

/**
 * Encapsulates Dalton's HITL resume admission tracking so `useChatMessages.ts`
 * stays thin while ensuring `activeTurnModel.admitted` only flips true on real
 * server admission signals (non-hold non-refusal frame, `onRunIdResolved`,
 * verified disconnect/terminal run match, or verified 409 duplicate adoption).
 */
export function createHitlResumeModelGuard({
  requestKey,
  pick,
  tracker,
  setActiveTurnModel,
  getWorkflowStatus,
  getRunLiveness,
  threadId,
  turnIndexBefore = null,
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

  const probeRunExists = async (
    expectedRunId: string,
  ): Promise<'found' | 'absent' | 'unknown'> => {
    if (!getRunLiveness) return 'unknown';
    try {
      const liveness = await getRunLiveness(expectedRunId);
      if (liveness?.runs?.some((r) => r.run_id === expectedRunId)) return 'found';
      return 'absent';
    } catch {
      return 'unknown';
    }
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
      if (!isDaltonBuild) return 'confirmed_reconnect';
      if (serverAdmitted) return 'confirmed_reconnect';
      if (isResendNotice) {
        unadmit();
        return 'unadmitted';
      }
      if (!threadId || threadId === '__default__') {
        unadmit();
        return 'unadmitted';
      }
      const expectedRunId = parseRunIdFromLocation(contentLocation);
      try {
        const status = await getWorkflowStatus(threadId);
        if (status?.can_reconnect) {
          if (expectedRunId && status.run_id === expectedRunId) {
            onConfirmedRun(expectedRunId);
            return 'confirmed_reconnect';
          }
          if (expectedRunId && (await probeRunExists(expectedRunId)) === 'found') {
            onConfirmedRun(expectedRunId);
            return 'confirmed_reconnect';
          }
          unadmit();
          return 'unconfirmed_reconnect';
        }
        // Not reconnectable: the run may have already finished (terminal) or never started.
        const runProbe = expectedRunId ? await probeRunExists(expectedRunId) : 'unknown';
        if (expectedRunId && runProbe === 'found') {
          onConfirmedRun(expectedRunId);
          return 'confirmed_reconnect';
        }
        const stillOnPriorInterrupt =
          status?.status === 'interrupted'
          && typeof status.latest_turn_index === 'number'
          && turnIndexBefore !== null
          && status.latest_turn_index <= turnIndexBefore
          && (!expectedRunId || runProbe === 'absent');
        if (stillOnPriorInterrupt) {
          unadmit();
          return 'unadmitted';
        }
        // Indeterminate: keep history reload via reconnect, keep model selection unconfirmed.
        unadmit();
        return 'unconfirmed_reconnect';
      } catch {
        // Offline / status unreachable: keep history reload via reconnect, keep admitted: false.
        unadmit();
        return 'unconfirmed_reconnect';
      }
    },
    isUnadmittedCleanEnd() {
      return isDaltonBuild && !serverAdmitted;
    },
  };
}
