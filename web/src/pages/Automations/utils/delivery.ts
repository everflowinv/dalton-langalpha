import type { TFunction } from 'i18next';
import { isDmAddress, messagingAppName, platformOf } from '@/pages/ChatAgent/components/messaging/messageDelivery';
import { apiErrorDetail, apiErrorStatus } from '@/api/errors';
// DALTON-HOOK(execution-delivery-status): import execution delivery summary types
import type {
  AutomationExecution,
  DeliveryAttempt,
  DeliveryDefault,
  DeliveryOptions,
  ExecutionDeliveryStatus,
  ExecutionDeliverySummary,
  ExecutionDeliveryTarget,
} from '@/types/automation';
import { mutationErrorMessage } from './errors';

const METHOD_KEY: Record<string, string> = {
  slack: 'automation.deliverToSlack',
  discord: 'automation.deliverToDiscord',
};

/** A delivery channel's name as the reader sees it. A DM address reads
 *  "<App> DM". The agent can pick a channel the form does not offer, which
 *  keeps its app's name, or a chat address capitalized. */
export function deliveryMethodName(method: string, t: TFunction): string {
  const key = METHOD_KEY[method];
  if (key) return t(key);
  if (method.includes(':') && isDmAddress(method)) {
    const app = platformOf(method) as string;
    const appKey = METHOD_KEY[app];
    return t('automation.deliverToDm', { app: appKey ? t(appKey) : messagingAppName(app) });
  }
  if (!method.includes(':')) return messagingAppName(method) ?? method;
  return method.charAt(0).toUpperCase() + method.slice(1);
}

/** Whether an entry names one chat (`slack:T1/C2`) rather than an app (`slack`). */
export function namesChat(entry: string): boolean {
  return entry.includes(':');
}

/** Chat names by address, from the chats the apps offer now and the names
 *  past deliveries reported. */
export type DeliveryNames = ReadonlyMap<string, string>;

/**
 * Every chat name known for this automation. The apps' current names win;
 * past runs, newest first, name a chat the apps no longer list. A bare app
 * entry is never named by a run: where it lands moves with the defaults.
 */
export function deliveryNames(
  options: DeliveryOptions | null | undefined,
  attempts: readonly DeliveryAttempt[] = [],
): Map<string, string> {
  const names = new Map<string, string>();
  for (const app of Object.values(options?.apps ?? {})) {
    for (const chat of app?.chats ?? []) if (chat.name) names.set(chat.address, chat.name);
  }
  for (const attempt of attempts) {
    if (!attempt.name) continue;
    for (const key of [attempt.address, attempt.method]) {
      if (key && namesChat(key) && !names.has(key)) names.set(key, attempt.name);
    }
  }
  return names;
}

/** The picked entries after a list's own choices changed to `picked`: an
 *  entry the list cannot show stays where it was, and a new pick goes last. */
export function mergeDeliverySelection(methods: string[], known: ReadonlySet<string>, picked: string[]): string[] {
  const next = new Set(picked);
  const kept = methods.filter((m) => !known.has(m) || next.has(m));
  const added = [...next].filter((k) => !methods.includes(k));
  return [...kept, ...added];
}

/** Where an app's bare entry lands now, or null where nothing says. */
export function deliveryDefaultOf(options: DeliveryOptions | null | undefined, app: string): DeliveryDefault | null {
  return options?.apps?.[app]?.default ?? null;
}

/** An entry as text: a bare app with where it lands now ("Slack (#demo)"),
 *  a chat by its name, else as `deliveryMethodName` spells it. */
export function deliveryEntryName(
  entry: string,
  t: TFunction,
  options?: DeliveryOptions | null,
  names?: DeliveryNames,
): string {
  if (!namesChat(entry)) {
    const app = deliveryMethodName(entry, t);
    const lands = deliveryDefaultOf(options, entry)?.name;
    return lands ? t('automation.deliverAppLandsIn', { app, name: lands }) : app;
  }
  return names?.get(entry) ?? deliveryMethodName(entry, t);
}

/** The chat a run's delivery landed in, by the best name known. */
export function deliveryAttemptName(attempt: DeliveryAttempt, t: TFunction, names?: DeliveryNames): string {
  if (attempt.name) return attempt.name;
  for (const key of [attempt.address, attempt.method]) {
    const name = key && namesChat(key) ? names?.get(key) : undefined;
    if (name) return name;
  }
  return deliveryMethodName(attempt.method, t);
}

// DALTON-HOOK(execution-delivery-status): include retrying outcome for in-progress delivery attempts
export type DeliveryOutcome = 'sent' | 'fallback' | 'notice' | 'failed' | 'retrying';

/** How a delivery went: the agent sent it (or, for an older run, it was
 *  posted), its final answer was posted for it, a notice went, it is retrying, or it failed. */
export function deliveryOutcome(attempt: DeliveryAttempt): DeliveryOutcome {
  if (!attempt.success) return attempt.retrying === true ? 'retrying' : 'failed';
  if (attempt.via === 'fallback') return 'fallback';
  if (attempt.via === 'notice') return 'notice';
  return 'sent';
}

const OUTCOME_KEY: Record<DeliveryOutcome, string> = {
  sent: 'automation.deliveredVia',
  fallback: 'automation.deliveredFallbackVia',
  notice: 'automation.deliveredNoticeVia',
  failed: 'automation.deliveryFailedVia',
  retrying: 'automation.deliveryRetryingVia',
};

/** One delivery as a phrase: "sent to #demo", "#demo delivery failed". */
export function deliveryAttemptLabel(attempt: DeliveryAttempt, t: TFunction, names?: DeliveryNames): string {
  return t(OUTCOME_KEY[deliveryOutcome(attempt)], { method: deliveryAttemptName(attempt, t, names) });
}

// ── Refusals ───────────────────────────────────────────────

/** A delivery entry a save was refused over, and why. */
export interface DeliveryProblem {
  entry: string;
  message: string;
}

/** The refusal's list of problems, inside `detail` or beside it. */
function problemList(err: unknown): unknown {
  const detail = apiErrorDetail(err);
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const problems = (detail as { problems?: unknown }).problems;
    if (problems) return problems;
  }
  return (err as { response?: { data?: { problems?: unknown } } })?.response?.data?.problems;
}

/** The entries a refused automation save names, each with why; empty for a
 *  refusal that names none. */
export function deliveryProblems(err: unknown): DeliveryProblem[] {
  const raw = problemList(err);
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((p): DeliveryProblem[] => {
    const { entry, message } = (p ?? {}) as { entry?: unknown; message?: unknown };
    return typeof entry === 'string' && typeof message === 'string' && message ? [{ entry, message }] : [];
  });
}

/** Why a workspace default wasn't set, in a line. */
export function deliveryDefaultError(err: unknown, t: TFunction): string {
  const status = apiErrorStatus(err);
  if (status === 409) return t('automation.deliveryDefaultBusy');
  if (status === 503) return t('automation.deliveryDefaultUnavailable');
  const raw = problemList(err);
  const messages = Array.isArray(raw)
    ? raw.flatMap((p) => {
        const message = (p as { message?: unknown } | null)?.message;
        return typeof message === 'string' && message ? [message] : [];
      })
    : [];
  if (messages.length) return messages.join(' ');
  return mutationErrorMessage(err, t('automation.deliveryDefaultFailed'));
}

// DALTON-HOOK(execution-delivery-status): normalize and localize execution delivery outcomes & failure reasons
const DELIVERY_SETTLE_WINDOW_MS = 3 * 60_000;
const SYNCED_UNDELIVERED_RE = /已生成但未送达[（(]([^）)]+)[）)]/;
const SYNCED_EXCERPT_SUFFIX_RE =
  /(?:(?:\s*\n+\s*|\s+)投递状态：已生成但未送达(?:[（(][^）)]*[）)])?|^\s*投递状态：已生成但未送达(?:[（(][^）)]*[）)])?)\s*$/;
const CARD_ELEMENT_LIMIT_RE = /(?:element\s+card\s+limit|exceeds\s+\d+\s+element\s+card\s+limit)/i;
const CARD_BYTE_LIMIT_RE = /(?:byte\s+card\s+limit|exceeds\s+\d+\s+byte\s+card\s+limit|30720|card\s+too\s+large)/i;

const CARD_LIMIT_UPGRADEABLE_CODES = new Set([
  '',
  'not_allowed',
  'card_limit_exceeded',
  'card_element_limit_exceeded',
]);

const DELIVERY_REASON_KEY: Record<string, string> = {
  card_limit_exceeded: 'automation.deliveryReasonCardLimit',
  card_element_limit_exceeded: 'automation.deliveryReasonCardElementLimit',
  attempts_exhausted: 'automation.deliveryReasonAttemptsExhausted',
  target_failed: 'automation.deliveryReasonTargetFailed',
  delivery_failed: 'automation.deliveryReasonTargetFailed',
  delivery_uncertain: 'automation.deliveryReasonUnconfirmed',
  delivery_unconfirmed: 'automation.deliveryReasonUnconfirmed',
  unknown: 'automation.deliveryReasonUnconfirmed',
  in_flight: 'automation.deliveryReasonUnconfirmed',
  partial: 'automation.deliveryReasonUnconfirmed',
  gateway_restarted: 'automation.deliveryReasonUnconfirmed',
  feishu_cards_not_supported: 'automation.deliveryReasonUnsupportedMedia',
  images_not_supported: 'automation.deliveryReasonUnsupportedMedia',
  image_placeholder_not_supported: 'automation.deliveryReasonUnsupportedMedia',
  missing_card: 'automation.deliveryReasonEmptyContent',
  missing_text: 'automation.deliveryReasonEmptyContent',
  unsupported_platform: 'automation.deliveryReasonUnsupportedPlatform',
  service_token_unconfigured: 'automation.deliveryReasonGatewayUnconfigured',
  gateway_url_unconfigured: 'automation.deliveryReasonGatewayUnconfigured',
  connecterror: 'automation.deliveryReasonGatewayUnavailable',
  connecttimeout: 'automation.deliveryReasonGatewayUnavailable',
  http_500: 'automation.deliveryReasonGatewayUnavailable',
  http_502: 'automation.deliveryReasonGatewayUnavailable',
  http_503: 'automation.deliveryReasonGatewayUnavailable',
  http_504: 'automation.deliveryReasonGatewayUnavailable',
  not_allowed: 'automation.deliveryReasonNotAllowed',
};

export function stripSyncedDeliveryExcerptSuffix(excerpt: string | null | undefined): string | null {
  if (typeof excerpt !== 'string') return null;
  const cleaned = excerpt.replace(SYNCED_EXCERPT_SUFFIX_RE, '').trim();
  return cleaned || null;
}

export function detectDeliveryErrorCode(
  code: string | null | undefined,
  message?: string | null,
): string | null {
  let rawCode = (code ?? '').trim();
  const rawMsg = (message ?? '').trim();
  if (!rawCode && rawMsg) {
    const matched = SYNCED_UNDELIVERED_RE.exec(rawMsg);
    if (matched?.[1]) rawCode = matched[1].trim();
  }
  const lower = rawCode.toLowerCase();
  if (CARD_LIMIT_UPGRADEABLE_CODES.has(lower)) {
    if (CARD_ELEMENT_LIMIT_RE.test(rawMsg)) return 'card_element_limit_exceeded';
    if (lower === 'card_limit_exceeded' || CARD_BYTE_LIMIT_RE.test(rawMsg)) return 'card_limit_exceeded';
  }
  if (lower && lower in DELIVERY_REASON_KEY) return lower;
  return lower || null;
}

export function formatDeliveryFailureReason(
  code: string | null | undefined,
  fallback: string | null | undefined,
  t: TFunction,
): string | null {
  const detected = detectDeliveryErrorCode(code, fallback);
  if (detected && DELIVERY_REASON_KEY[detected]) {
    return t(DELIVERY_REASON_KEY[detected]);
  }
  const raw = (fallback ?? '').trim();
  if (raw) {
    const matched = SYNCED_UNDELIVERED_RE.exec(raw);
    if (matched?.[1]) {
      const inner = matched[1].trim();
      const innerKey = DELIVERY_REASON_KEY[inner.toLowerCase()];
      return innerKey ? t(innerKey) : inner;
    }
    return raw;
  }
  return detected;
}

/** Match a backend delivery_summary target back to a delivery_result attempt for chat name / via enrichment. */
export function matchAttemptForTarget(
  target: ExecutionDeliveryTarget,
  attempts: readonly DeliveryAttempt[],
): DeliveryAttempt | null {
  const tAddr = (target.target ?? '').trim();
  const tChan = (target.channel ?? '').trim().toLowerCase();
  if (tAddr) {
    const byAddress = attempts.find((a) => (a.address ?? '').trim() === tAddr || a.method.trim() === tAddr);
    if (byAddress) return byAddress;
  }
  if (tChan) {
    const byChannel = attempts.filter(
      (a) => a.method.split(':', 1)[0]?.toLowerCase() === tChan,
    );
    if (byChannel.length === 1) return byChannel[0];
  }
  return null;
}

/** Display name for a delivery_summary target in history rows and error lines. */
export function deliveryTargetName(
  target: ExecutionDeliveryTarget,
  t: TFunction,
  names?: DeliveryNames,
  matchedAttempt?: DeliveryAttempt | null,
): string {
  if (target.name) return target.name;
  if (matchedAttempt?.name) return matchedAttempt.name;
  const addr = (target.target ?? '').trim();
  if (addr && namesChat(addr)) {
    const known = names?.get(addr);
    if (known) return known;
  }
  if (matchedAttempt) return deliveryAttemptName(matchedAttempt, t, names);
  const fallbackKey = addr || (target.channel ?? '').trim();
  return fallbackKey ? deliveryMethodName(fallbackKey, t) : t('automation.delivery');
}

export function resolveExecutionDelivery(
  run: Pick<AutomationExecution, 'delivery_summary' | 'delivery_result' | 'completed_at'>,
  now = Date.now(),
): ExecutionDeliverySummary | null {
  if (run.delivery_summary) return run.delivery_summary;
  const attempts = run.delivery_result;
  if (!attempts || attempts.length === 0) return null;

  const withinSettle =
    !run.completed_at ||
    (() => {
      const elapsed = now - Date.parse(run.completed_at);
      return elapsed >= 0 && elapsed < DELIVERY_SETTLE_WINDOW_MS;
    })();

  const targets: ExecutionDeliveryTarget[] = [];
  let sentCount = 0;
  let failedCount = 0;
  let retryingCount = 0;
  let firstCode: string | null = null;
  const failureReasons: string[] = [];

  for (const attempt of attempts) {
    if (attempt.success) {
      sentCount += 1;
      targets.push({
        target: attempt.address ?? attempt.method,
        channel: attempt.method.split(':', 1)[0]?.toLowerCase() || null,
        name: attempt.name ?? null,
        status: 'sent',
      });
      continue;
    }
    const err = (attempt.error ?? '').trim();
    if (attempt.retrying === true && withinSettle) {
      retryingCount += 1;
      targets.push({
        target: attempt.address ?? attempt.method,
        channel: attempt.method.split(':', 1)[0]?.toLowerCase() || null,
        name: attempt.name ?? null,
        status: 'retrying',
      });
      continue;
    }
    failedCount += 1;
    const detected = detectDeliveryErrorCode(null, err);
    if (!firstCode && detected) firstCode = detected;
    if (err && !failureReasons.includes(err)) failureReasons.push(err);
    targets.push({
      target: attempt.address ?? attempt.method,
      channel: attempt.method.split(':', 1)[0]?.toLowerCase() || null,
      name: attempt.name ?? null,
      status: 'failed',
      error_code: detected,
      error_message: err || null,
    });
  }

  let status: ExecutionDeliveryStatus;
  if (retryingCount > 0) status = 'retrying';
  else if (failedCount === 0) status = 'delivered';
  else if (sentCount > 0) status = 'partial_failed';
  else status = 'failed';

  return {
    status,
    total_targets: targets.length,
    sent_count: sentCount,
    failed_count: failedCount,
    retrying_count: retryingCount,
    failure_code: status === 'failed' || status === 'partial_failed' ? firstCode : null,
    failure_reason:
      (status === 'failed' || status === 'partial_failed') && failureReasons.length > 0
        ? failureReasons[0]
        : null,
    failure_reasons: status === 'failed' || status === 'partial_failed' ? failureReasons : [],
    targets,
  };
}
