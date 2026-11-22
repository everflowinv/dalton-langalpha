import type { AutomationExecution, ScriptExecutionRead } from '@/types/automation'; // DALTON-HOOK(execution-delivery-status)

export const IDLE_POLL_MS = 30_000;
/** A run takes seconds to minutes, and its row has to move from the loader
 *  to the result without a reload. */
export const LIVE_POLL_MS = 5_000;

/** How long after a run ends its delivery record can still change. The
 *  server asks the messaging service again to end a run while it gets no
 *  answer or rows still being posted: up to three asks 2 and 5 seconds
 *  apart, or five on an outbox job's backoff, each allowed 15 seconds, which
 *  comes to about two minutes at most. Past this the record stands as it
 *  reads. */
export const DELIVERY_SETTLE_MS = 3 * 60_000;

/** How often a list refetches. Only something starting or running polls
 *  fast (`isRunLive`, `isAutomationRunning`), or a run whose delivery may
 *  still change (`isDeliverySettling`): a wait can last half an hour, and the
 *  user feed already announces both its start and its end. */
export function pollMs(live: boolean): number {
  return live ? LIVE_POLL_MS : IDLE_POLL_MS;
}

/**
 * Whether a run that has ended may still get a later delivery record, which
 * nothing announces, so the list showing it has to poll for it. The record is
 * written after the run ends and may be replaced: for a while the run reads
 * finished with no record yet, or with rows that read failed ("Delivery
 * couldn't be confirmed.") until a later answer says where they landed. A
 * record whose every row landed is final, and so is any record once
 * `DELIVERY_SETTLE_MS` has passed. A run with no record waits for one only
 * when its automation has somewhere to deliver (`awaitsRecord`).
 */
export function isDeliverySettling(run: AutomationExecution, awaitsRecord: boolean, now = Date.now()): boolean {
  // DALTON-HOOK(execution-delivery-status): poll while delivery_summary is retrying within settle window; stop once terminal or expired
  if (run.delivery_summary) {
    if (run.delivery_summary.status !== 'retrying') return false;
    if (!run.completed_at) return false;
    return now - Date.parse(run.completed_at) < DELIVERY_SETTLE_MS;
  }
  if (!run.completed_at) return false;
  // NaN, from a timestamp that doesn't parse, settles nothing.
  if (!(now - Date.parse(run.completed_at) < DELIVERY_SETTLE_MS)) return false;
  const attempts = run.delivery_result;
  return attempts ? attempts.some((d) => !d.success && d.retrying !== false) : awaitsRecord;
}

// DALTON-HOOK(execution-delivery-status): keep slow polling when backend marks a failed/partial run as still reconcilable for a late receipt
export function isExecutionAwaitingReceipt(
  run: Pick<AutomationExecution, 'delivery_summary'> | null | undefined,
): boolean {
  const summary = run?.delivery_summary;
  if (!summary) return false;
  if (summary.status === 'delivered' || summary.status === 'retrying' || summary.status === 'skipped') {
    return false;
  }
  return summary.awaiting_receipt === true;
}

// DALTON-HOOK(execution-delivery-status): poll company script history fast while retrying, slow (30s) while unconfirmed awaiting receipt, and stop on terminal outcomes
const UNCONFIRMED_DELIVERY_CODES = new Set([
  'delivery_uncertain',
  'delivery_unconfirmed',
  'unknown',
  'in_flight',
  'partial',
  'gateway_restarted',
]);

export function isScriptDeliveryPending(
  run: Pick<ScriptExecutionRead, 'notify_status' | 'delivery_summary'> | null | undefined,
): boolean {
  if (!run) return false;
  if (run.delivery_summary) {
    return run.delivery_summary.status === 'retrying';
  }
  return run.notify_status === 'pending_dispatch';
}

// DALTON-HOOK(execution-delivery-status): prefer backend awaiting_receipt flag over legacy error-code heuristics
export function isScriptDeliveryAwaitingReceipt(
  run: Pick<ScriptExecutionRead, 'notify_status' | 'delivery_summary'> | null | undefined,
): boolean {
  if (!run || !run.delivery_summary) return false;
  if (
    run.delivery_summary.status === 'delivered' ||
    run.delivery_summary.status === 'retrying' ||
    run.delivery_summary.status === 'skipped'
  ) {
    return false;
  }
  if (typeof run.delivery_summary.awaiting_receipt === 'boolean') {
    return run.delivery_summary.awaiting_receipt;
  }
  if (run.notify_status !== 'pending_dispatch') return false;
  const topCode = (run.delivery_summary.failure_code ?? '').trim().toLowerCase();
  if (topCode && UNCONFIRMED_DELIVERY_CODES.has(topCode)) return true;
  return (run.delivery_summary.targets ?? []).some(
    (tgt) => tgt.status === 'failed' && UNCONFIRMED_DELIVERY_CODES.has((tgt.error_code ?? '').trim().toLowerCase()),
  );
}

export function scriptDeliveryPollMs(
  runs: readonly Pick<ScriptExecutionRead, 'notify_status' | 'delivery_summary'>[],
): number | false {
  if (runs.some((run) => isScriptDeliveryPending(run))) return LIVE_POLL_MS;
  if (runs.some((run) => isScriptDeliveryAwaitingReceipt(run))) return IDLE_POLL_MS;
  return false;
}
