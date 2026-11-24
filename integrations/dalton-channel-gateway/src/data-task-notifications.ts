import { createHash } from "node:crypto";
import {
  type BackendClient,
  BackendHttpError,
  type I6AuthorizeAndClaimRequest,
  type I7DeliveriesProgressRequest,
  type I7DeliveriesProgressResponse,
  type WeixinPreflightAuthorizedData,
} from "./backend-client.js";
import {
  type ChannelAdapter,
  ChannelDeliveryError,
  type SendCardImageInput,
} from "./channels/adapter.js";
import {
  abortableRetrySleep,
  DELIVERY_CLAIM_503_INITIAL_DELAY_MS,
  DELIVERY_CLAIM_503_MAX_ATTEMPTS,
  DELIVERY_CLAIM_503_MAX_DELAY_MS,
  type WeixinPreflightBudget,
  WEIXIN_PREFLIGHT_RETRY_INITIAL_DELAY_MS,
  WEIXIN_PREFLIGHT_RETRY_MAX_DELAY_MS,
  WEIXIN_PREFLIGHT_RETRY_MAX_ELAPSED_MS,
} from "./delivery-service.js";
import {
  isStaleGatewayEpochError,
  StaleGatewayEpochError,
} from "./gateway-lease.js";
import type {
  DataTaskAuthorizePlanResponse,
  DataTaskItemDeliveryOutcome,
  DataTaskNotificationResponse,
  DataTaskPlanArtifactDescriptor,
  DataTaskPlanItem,
} from "./data-task-notification-types.js";
import {
  createSequentialQueue,
  type SequentialQueue,
} from "./shared/keyed-queue.js";
import {
  createSubsystemLogger,
  extractSafeLogErrorFields,
  sanitizeLogErrorCode,
} from "./shared/logger.js";
import type { ChannelPlatform } from "./types.js";

const MAX_ARTIFACT_BYTES = 20 * 1024 * 1024;
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ALLOWED_REQUEST_KEYS = new Set(["run_id", "target"]);
const SUPPORTED_PLATFORMS = new Set<string>(["feishu", "discord", "weixin"]);
const RECOGNIZED_DELIVERY_STATUSES = new Set<string>([
  "in_flight",
  "sent",
  "partial",
  "failed",
  "unknown",
]);
const SKIP_CODES = new Set([
  "dalton_disabled",
  "task_disabled",
  "task_notify_disabled",
  "toggle_off",
  "master_off",
  "override_off",
  "target_not_routed",
  "no_targets",
  "target_removed",
  "eve_tracking_mirror_pending",
  "task_deleted",
  "already_sent",
]);

class DataTaskBackendAckError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DataTaskBackendAckError";
  }
}

class DataTaskArtifactVerifyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DataTaskArtifactVerifyError";
  }
}

function isPlainRecord(val: unknown): val is Record<string, unknown> {
  return typeof val === "object" && val !== null && !Array.isArray(val);
}

function isSupportedChannelPlatform(val: unknown): val is ChannelPlatform {
  return typeof val === "string" && SUPPORTED_PLATFORMS.has(val);
}

function computeSha256Hex(buf: Buffer): string {
  return createHash("sha256").update(buf).digest("hex");
}

function safeBackendErrorMessage(err: unknown, fallback: string): string {
  if (
    err instanceof DataTaskBackendAckError ||
    err instanceof DataTaskArtifactVerifyError ||
    err instanceof BackendHttpError
  ) {
    return err.message;
  }
  return fallback;
}

function classifyAdapterError(err: unknown): {
  outcome: "failed" | "unknown";
  code: string;
  denyCode?: string;
  errorDetail?: string;
  message: string;
} {
  if (err instanceof ChannelDeliveryError) {
    return {
      outcome: err.outcome === "unknown" ? "unknown" : "failed",
      code: err.code,
      denyCode: err.denyCode,
      errorDetail: err.errorDetail,
      message: err.message,
    };
  }
  return {
    outcome: "unknown",
    code: "unavailable",
    message: err instanceof Error ? err.message : String(err),
  };
}

type ValidatedDataTaskClaim =
  | {
      kind: "claimed";
      platform: ChannelPlatform;
      targetAddress: string;
      recipientId: string;
      isCurrent: false;
      homeWorkspaceId: string | null;
      weixinAccountId: string | null;
      weixinContextToken: string | null;
      deliveryAttempt?: number;
    }
  | {
      kind: "already_completed";
      status: "sent" | "partial";
      platformMessageIds: string[];
    }
  | {
      kind: "in_flight_or_unknown";
      code: string;
      message: string;
    }
  | {
      kind: "denied";
      status: "failed" | "unknown";
      code: string;
      message: string;
    }
  | {
      kind: "invalid";
      reason: string;
    };

function validateDataTaskClaimResponse(
  rawClaim: unknown,
  expected: {
    platform: ChannelPlatform;
    canonicalTargetAddress: string;
    recipientId: string;
  },
): ValidatedDataTaskClaim {
  if (!isPlainRecord(rawClaim)) {
    return {
      kind: "invalid",
      reason: "I-6 authorize-and-claim response is not a JSON object.",
    };
  }
  const claimStatus = rawClaim.claim_status;
  if (typeof claimStatus !== "string") {
    return {
      kind: "invalid",
      reason: "I-6 authorize-and-claim response is missing a string claim_status.",
    };
  }

  if (claimStatus === "claimed") {
    if (rawClaim.platform !== expected.platform) {
      return {
        kind: "invalid",
        reason: "I-6 claimed platform does not match the authorized plan.",
      };
    }
    if (
      typeof rawClaim.recipient_id !== "string" ||
      rawClaim.recipient_id.trim().length === 0 ||
      rawClaim.recipient_id !== expected.recipientId
    ) {
      return {
        kind: "invalid",
        reason: "I-6 claimed recipient_id is missing or does not match the authorized plan.",
      };
    }
    if (
      typeof rawClaim.target_address !== "string" ||
      rawClaim.target_address.trim().length === 0 ||
      rawClaim.target_address !== expected.canonicalTargetAddress
    ) {
      return {
        kind: "invalid",
        reason: "I-6 claimed target_address is missing or does not match the authorized plan.",
      };
    }
    if (rawClaim.is_current !== false) {
      return {
        kind: "invalid",
        reason: "I-6 claimed response must confirm is_current=false.",
      };
    }
    if (
      !("home_workspace_id" in rawClaim) ||
      (rawClaim.home_workspace_id !== null &&
        (typeof rawClaim.home_workspace_id !== "string" ||
          rawClaim.home_workspace_id.trim().length === 0))
    ) {
      return {
        kind: "invalid",
        reason: "I-6 claimed response has missing or invalid home_workspace_id.",
      };
    }
    if (
      !("weixin_context_token" in rawClaim) ||
      (rawClaim.weixin_context_token !== null &&
        typeof rawClaim.weixin_context_token !== "string")
    ) {
      return {
        kind: "invalid",
        reason: "I-6 claimed response has missing or invalid weixin_context_token.",
      };
    }
    const deliveryAttempt =
      typeof rawClaim.delivery_attempt === "number" &&
      Number.isInteger(rawClaim.delivery_attempt) &&
      rawClaim.delivery_attempt > 0
        ? rawClaim.delivery_attempt
        : undefined;
    const weixinAccountId =
      typeof rawClaim.weixin_account_id === "string" &&
      rawClaim.weixin_account_id.trim().length > 0
        ? rawClaim.weixin_account_id.trim()
        : null;
    return {
      kind: "claimed",
      platform: expected.platform,
      targetAddress: rawClaim.target_address,
      recipientId: rawClaim.recipient_id,
      isCurrent: false,
      homeWorkspaceId: rawClaim.home_workspace_id,
      weixinAccountId,
      weixinContextToken: rawClaim.weixin_context_token,
      ...(deliveryAttempt !== undefined ? { deliveryAttempt } : {}),
    };
  }

  if (claimStatus === "already_completed") {
    if (rawClaim.status !== "sent" && rawClaim.status !== "partial") {
      return {
        kind: "invalid",
        reason: "I-6 already_completed response has missing or unrecognized status.",
      };
    }
    if (rawClaim.platform !== expected.platform) {
      return {
        kind: "invalid",
        reason: "I-6 already_completed platform does not match the authorized plan.",
      };
    }
    if (rawClaim.target_address !== expected.canonicalTargetAddress) {
      return {
        kind: "invalid",
        reason: "I-6 already_completed target_address does not match the authorized plan.",
      };
    }
    if (rawClaim.is_current !== undefined && rawClaim.is_current !== false) {
      return {
        kind: "invalid",
        reason: "I-6 already_completed response has invalid is_current.",
      };
    }
    if (
      rawClaim.first_response_json !== undefined &&
      rawClaim.first_response_json !== null &&
      !isPlainRecord(rawClaim.first_response_json)
    ) {
      return {
        kind: "invalid",
        reason: "I-6 already_completed response has invalid first_response_json.",
      };
    }
    const recordedMsgIds =
      isPlainRecord(rawClaim.first_response_json) &&
      Array.isArray(rawClaim.first_response_json.platform_message_ids) &&
      rawClaim.first_response_json.platform_message_ids.every(
        (id) => typeof id === "string" && id.length > 0,
      )
        ? (rawClaim.first_response_json.platform_message_ids as string[])
        : [];
    return {
      kind: "already_completed",
      status: rawClaim.status,
      platformMessageIds: recordedMsgIds,
    };
  }

  if (claimStatus === "in_flight_or_unknown") {
    if (rawClaim.status !== "unknown") {
      return {
        kind: "invalid",
        reason: "I-6 in_flight_or_unknown response must carry status=unknown.",
      };
    }
    if (rawClaim.platform !== expected.platform) {
      return {
        kind: "invalid",
        reason: "I-6 in_flight_or_unknown platform does not match the authorized plan.",
      };
    }
    if (rawClaim.target_address !== expected.canonicalTargetAddress) {
      return {
        kind: "invalid",
        reason: "I-6 in_flight_or_unknown target_address does not match the authorized plan.",
      };
    }
    if (rawClaim.is_current !== undefined && rawClaim.is_current !== false) {
      return {
        kind: "invalid",
        reason: "I-6 in_flight_or_unknown response has invalid is_current.",
      };
    }
    if (
      rawClaim.code !== undefined &&
      rawClaim.code !== null &&
      (typeof rawClaim.code !== "string" || rawClaim.code.trim().length === 0)
    ) {
      return {
        kind: "invalid",
        reason: "I-6 in_flight_or_unknown response has invalid code.",
      };
    }
    if (rawClaim.message !== undefined && typeof rawClaim.message !== "string") {
      return {
        kind: "invalid",
        reason: "I-6 in_flight_or_unknown response has invalid message.",
      };
    }
    return {
      kind: "in_flight_or_unknown",
      code: rawClaim.code ?? "in_flight",
      message:
        rawClaim.message ??
        "Item delivery state is uncertain or in flight; automatic resend is blocked.",
    };
  }

  if (claimStatus === "denied") {
    if (rawClaim.status !== "failed" && rawClaim.status !== "unknown") {
      return {
        kind: "invalid",
        reason: "I-6 denied response has missing or unrecognized status.",
      };
    }
    if (typeof rawClaim.code !== "string" || rawClaim.code.trim().length === 0) {
      return {
        kind: "invalid",
        reason: "I-6 denied response is missing a non-empty code.",
      };
    }
    if (rawClaim.message !== undefined && typeof rawClaim.message !== "string") {
      return {
        kind: "invalid",
        reason: "I-6 denied response has invalid message.",
      };
    }
    return {
      kind: "denied",
      status: rawClaim.status,
      code: rawClaim.code,
      message: rawClaim.message ?? "Item claim denied.",
    };
  }

  return {
    kind: "invalid",
    reason: "I-6 authorize-and-claim returned an unrecognized claim_status.",
  };
}

export interface DataTaskNotificationServiceOptions {
  backend: BackendClient;
  adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  mode?: "incoming" | "outgoing";
  queueTaskTimeoutMs?: number;
  retryDelayFn?: (ms: number, signal?: AbortSignal) => Promise<boolean>;
  preflightRetryMaxElapsedMs?: number;
  claim503MaxAttempts?: number;
  nowFn?: () => number;
}

export interface ExecutionStateTracker {
  claimedAnyItem: boolean;
  resolvedPlatform: ChannelPlatform | null;
  itemsTotal: number;
  itemsSent: number;
  outcomes: DataTaskItemDeliveryOutcome[];
}

export interface AuthorizedPlanDeliveryOptions {
  /** When present, the shared loop uses this plan and does not fetch a data-task plan. */
  rawPlan?: unknown;
  claimKind?: "data_task" | "automation_notify";
  liveCheckPurpose?: string;
}

export class DataTaskNotificationService {
  private readonly backend: BackendClient;
  private readonly adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  private readonly mode: "incoming" | "outgoing";
  private readonly queue: SequentialQueue;
  private readonly logger = createSubsystemLogger("data-task-notifications");
  private readonly retryDelayFn: (
    ms: number,
    signal?: AbortSignal,
  ) => Promise<boolean>;
  private readonly preflightRetryMaxElapsedMs: number;
  private readonly claim503MaxAttempts: number;
  private readonly nowFn: () => number;
  private retryAbortController = new AbortController();
  private startupBlocked = false;
  private leaseLost = false;

  constructor(options: DataTaskNotificationServiceOptions) {
    this.backend = options.backend;
    this.adapters = options.adapters;
    this.mode = options.mode ?? "incoming";
    this.retryDelayFn = options.retryDelayFn ?? abortableRetrySleep;
    this.preflightRetryMaxElapsedMs =
      options.preflightRetryMaxElapsedMs ??
      WEIXIN_PREFLIGHT_RETRY_MAX_ELAPSED_MS;
    this.claim503MaxAttempts =
      options.claim503MaxAttempts ?? DELIVERY_CLAIM_503_MAX_ATTEMPTS;
    this.nowFn = options.nowFn ?? (() => Date.now());
    // R5 #5 (Codex Medium #7): Preserve original 180s default queue task timeout unchanged for Feishu/Discord.
    this.queue = createSequentialQueue({
      taskTimeoutMs: options.queueTaskTimeoutMs ?? 180_000,
    });
  }

  setStartupBlocked(blocked: boolean): void {
    this.startupBlocked = blocked;
    if (blocked) {
      this.abortAllRetries();
    } else {
      this.leaseLost = false;
    }
  }

  /**
   * R5 #3 (Codex Medium #5): Synchronously aborts all in-delivery retries on lease loss
   * and marks `leaseLost = true` so aborted deliveries rethrow `StaleGatewayEpochError`
   * WITHOUT writing I-7 `failed` settlement.
   */
  abortOnLeaseLost(): void {
    this.leaseLost = true;
    this.startupBlocked = true;
    this.abortAllRetries();
  }

  abortAllRetries(): void {
    this.retryAbortController.abort();
    this.retryAbortController = new AbortController();
  }

  private createWeixinPreflightBudget(): WeixinPreflightBudget {
    return {
      deadlineMs: this.nowFn() + this.preflightRetryMaxElapsedMs,
      maxElapsedMs: this.preflightRetryMaxElapsedMs,
      virtualElapsedMs: 0,
    };
  }

  private async claimDeliveryWith503Retry(
    req: I6AuthorizeAndClaimRequest,
  ): Promise<unknown> {
    // R5 #5 (Codex Medium #7): In-delivery I-6 503 retry applies ONLY to Weixin;
    // Feishu and Discord keep original fail-immediately behavior on 503.
    const isWeixinTarget = req.platform === "weixin";
    const maxAttempts = isWeixinTarget ? this.claim503MaxAttempts : 1;
    const signal = this.retryAbortController.signal;
    let delayMs = DELIVERY_CLAIM_503_INITIAL_DELAY_MS;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      if (this.leaseLost) {
        throw new StaleGatewayEpochError(
          "Gateway lease lost before data-task I-6 authorize-and-claim",
        );
      }
      if (this.startupBlocked || signal.aborted) {
        throw new BackendHttpError(
          "I-6 authorize-and-claim aborted due to lease loss or shutdown",
          503,
          null,
        );
      }
      try {
        return await this.backend.authorizeAndClaimDelivery(
          req,
          isWeixinTarget ? { signal } : undefined,
        );
      } catch (err) {
        if (isStaleGatewayEpochError(err) || this.leaseLost) {
          throw isStaleGatewayEpochError(err)
            ? err
            : new StaleGatewayEpochError(
                "Gateway lease lost during data-task I-6 authorize-and-claim",
                { cause: err },
              );
        }
        const isRetryable503 =
          isWeixinTarget &&
          err instanceof BackendHttpError &&
          err.status === 503;
        if (!isRetryable503 || attempt >= maxAttempts) {
          throw err;
        }
        this.logger.warn(
          "Data-task I-6 authorize-and-claim returned 503; retrying in delivery",
          {
            delivery_key: req.delivery_key,
            platform: req.platform ?? null,
            attempt,
            delay_ms: delayMs,
            ...extractSafeLogErrorFields(err),
          },
        );
        const slept = await this.retryDelayFn(delayMs, signal);
        if (this.leaseLost) {
          throw new StaleGatewayEpochError(
            "Gateway lease lost while retrying data-task I-6 authorize-and-claim",
          );
        }
        if (!slept || this.startupBlocked || signal.aborted) {
          throw err;
        }
        delayMs = Math.min(delayMs * 2, DELIVERY_CLAIM_503_MAX_DELAY_MS);
      }
    }
    throw new BackendHttpError("I-6 authorize-and-claim failed (503)", 503, null);
  }

  private async runWeixinPreflight(
    deliveryKey: string,
    deliveryAttempt: number,
    chunkIndex: number,
    budget: WeixinPreflightBudget,
    signal?: AbortSignal,
  ): Promise<WeixinPreflightAuthorizedData> {
    const combinedSignal = signal
      ? AbortSignal.any([signal, this.retryAbortController.signal])
      : this.retryAbortController.signal;
    let delayMs = WEIXIN_PREFLIGHT_RETRY_INITIAL_DELAY_MS;
    let attempt = 0;
    const getRemainingBudgetMs = (): number =>
      Math.min(
        budget.deadlineMs - this.nowFn(),
        budget.maxElapsedMs - budget.virtualElapsedMs,
      );
    const throwIfAbortedOrLeaseLost = (): void => {
      if (this.leaseLost) {
        throw new StaleGatewayEpochError(
          "Gateway lease lost during data-task Weixin preflight",
        );
      }
      if (this.startupBlocked || combinedSignal.aborted) {
        throw new ChannelDeliveryError("Weixin preflight aborted", {
          outcome: "failed",
          code: "unavailable",
          retryable: false,
        });
      }
    };

    const buildDeadlineExceededError = (
      fallbackMessage: string,
    ): ChannelDeliveryError =>
      new ChannelDeliveryError(
        budget.lastRetryableMessage ?? fallbackMessage,
        {
          outcome: "failed",
          code: "unavailable",
          retryable: false,
          denyCode:
            budget.lastRetryableDenyCode ?? "preflight_deadline_exceeded",
          errorDetail: "preflight_deadline_exceeded",
        },
      );

    for (;;) {
      attempt += 1;
      throwIfAbortedOrLeaseLost();

      const preCallRemainingMs = getRemainingBudgetMs();
      if (preCallRemainingMs <= 0) {
        throw buildDeadlineExceededError(
          "Weixin send preflight exceeded shared delivery deadline.",
        );
      }

      const attemptSignal = AbortSignal.any([
        combinedSignal,
        AbortSignal.timeout(Math.max(1, Math.ceil(preCallRemainingMs))),
      ]);

      let pf: Awaited<ReturnType<BackendClient["preflightWeixinSend"]>>;
      try {
        pf = await this.backend.preflightWeixinSend(
          deliveryKey,
          {
            delivery_attempt: deliveryAttempt,
            chunk_index: chunkIndex,
          },
          { signal: attemptSignal },
        );
      } catch (err) {
        if (isStaleGatewayEpochError(err) || this.leaseLost) {
          throw isStaleGatewayEpochError(err)
            ? err
            : new StaleGatewayEpochError(
                "Gateway lease lost during data-task Weixin preflight",
                { cause: err },
              );
        }
        throwIfAbortedOrLeaseLost();
        if (attemptSignal.aborted || getRemainingBudgetMs() <= 0) {
          throw buildDeadlineExceededError(
            "Weixin send preflight exceeded shared delivery deadline.",
          );
        }
        const isRetryable503 =
          err instanceof BackendHttpError && err.status === 503;
        if (!isRetryable503) {
          throw err;
        }
        budget.lastRetryableDenyCode = "roster_lookup_error";
        budget.lastRetryableMessage =
          "Weixin preflight remained temporarily unavailable (503).";
        const remainingMs = getRemainingBudgetMs();
        if (remainingMs <= 0) {
          throw buildDeadlineExceededError(
            "Weixin preflight remained temporarily unavailable (503).",
          );
        }
        const waitMs = Math.min(delayMs, remainingMs);
        this.logger.warn(
          "Data-task Weixin preflight returned 503; retrying in delivery",
          {
            delivery_key: deliveryKey,
            chunk_index: chunkIndex,
            attempt,
            delay_ms: waitMs,
            ...extractSafeLogErrorFields(err),
          },
        );
        const slept = await this.retryDelayFn(waitMs, combinedSignal);
        budget.virtualElapsedMs += waitMs;
        if (!slept) {
          throwIfAbortedOrLeaseLost();
          throw new ChannelDeliveryError("Weixin preflight aborted", {
            outcome: "failed",
            code: "unavailable",
            retryable: false,
          });
        }
        throwIfAbortedOrLeaseLost();
        delayMs = Math.min(delayMs * 2, WEIXIN_PREFLIGHT_RETRY_MAX_DELAY_MS);
        continue;
      }

      throwIfAbortedOrLeaseLost();
      if (getRemainingBudgetMs() <= 0) {
        throw buildDeadlineExceededError(
          "Weixin preflight response arrived after shared delivery deadline.",
        );
      }

      if (pf.authorized) {
        return pf;
      }
      if (pf.deny_code === "source_generation_unknown") {
        throw new ChannelDeliveryError(
          pf.message ?? "Weixin delivery source generation is unknown.",
          {
            outcome: "unknown",
            code: "unavailable",
            retryable: false,
            denyCode: pf.deny_code,
          },
        );
      }
      if (pf.retryable === true || pf.decision === "defer") {
        budget.lastRetryableDenyCode = pf.deny_code;
        budget.lastRetryableMessage =
          pf.message ??
          `Weixin send preflight remained unavailable (${pf.deny_code}).`;
        const remainingMs = getRemainingBudgetMs();
        if (remainingMs <= 0) {
          throw buildDeadlineExceededError(budget.lastRetryableMessage);
        }
        const waitMs = Math.min(delayMs, remainingMs);
        this.logger.warn(
          "Data-task Weixin preflight returned retryable denial; retrying in delivery",
          {
            delivery_key: deliveryKey,
            chunk_index: chunkIndex,
            attempt,
            delay_ms: waitMs,
            error_code: sanitizeLogErrorCode(pf.deny_code),
          },
        );
        const slept = await this.retryDelayFn(waitMs, combinedSignal);
        budget.virtualElapsedMs += waitMs;
        if (!slept) {
          throwIfAbortedOrLeaseLost();
          throw new ChannelDeliveryError("Weixin preflight aborted", {
            outcome: "failed",
            code: "unavailable",
            retryable: false,
          });
        }
        throwIfAbortedOrLeaseLost();
        delayMs = Math.min(delayMs * 2, WEIXIN_PREFLIGHT_RETRY_MAX_DELAY_MS);
        continue;
      }
      throw new ChannelDeliveryError(
        pf.message ?? `Weixin send denied (${pf.deny_code}).`,
        {
          outcome: "failed",
          code: "not_allowed",
          retryable: false,
          denyCode: pf.deny_code,
        },
      );
    }
  }

  private isAdapterOutboundReady(adapter: ChannelAdapter | undefined): boolean {
    if (!adapter || !adapter.isConfigured()) {
      return false;
    }
    if (typeof adapter.isReadyForOutbound === "function") {
      return adapter.isReadyForOutbound();
    }
    return this.mode === "outgoing" ? true : adapter.isConnected();
  }

  private async recordValidatedDeliveryProgress(
    req: I7DeliveriesProgressRequest,
    platform?: ChannelPlatform,
  ): Promise<I7DeliveriesProgressResponse> {
    if (this.leaseLost) {
      throw new StaleGatewayEpochError(
        "Gateway lease lost before data-task I-7 settlement",
      );
    }
    const expectedStatus = req.status ?? "in_flight";
    if (!RECOGNIZED_DELIVERY_STATUSES.has(expectedStatus)) {
      throw new DataTaskBackendAckError(
        "Submitted I-7 delivery status is not recognized.",
      );
    }
    let rawAck: unknown;
    try {
      rawAck = await this.backend.updateDeliveryProgress(req, {
        platform: platform ?? null,
      });
    } catch (err) {
      if (isStaleGatewayEpochError(err)) {
        throw err;
      }
      throw new DataTaskBackendAckError(
        safeBackendErrorMessage(
          err,
          "I-7 deliveries/progress request failed without a confirmed acknowledgement.",
        ),
      );
    }
    if (!isPlainRecord(rawAck)) {
      throw new DataTaskBackendAckError(
        "I-7 deliveries/progress acknowledgement is not a JSON object.",
      );
    }
    if (rawAck.ok !== true) {
      throw new DataTaskBackendAckError(
        "I-7 deliveries/progress acknowledgement did not confirm ok=true.",
      );
    }
    if (
      typeof rawAck.delivery_key !== "string" ||
      rawAck.delivery_key !== req.delivery_key
    ) {
      throw new DataTaskBackendAckError(
        "I-7 deliveries/progress acknowledgement delivery_key mismatch.",
      );
    }
    if (
      typeof rawAck.status !== "string" ||
      !RECOGNIZED_DELIVERY_STATUSES.has(rawAck.status) ||
      rawAck.status !== expectedStatus
    ) {
      throw new DataTaskBackendAckError(
        "I-7 deliveries/progress acknowledgement status is unrecognized or inconsistent with submitted transition.",
      );
    }
    if (
      typeof rawAck.sent_to_current_count !== "number" ||
      !Number.isInteger(rawAck.sent_to_current_count) ||
      rawAck.sent_to_current_count < 0
    ) {
      throw new DataTaskBackendAckError(
        "I-7 deliveries/progress acknowledgement has an invalid sent_to_current_count.",
      );
    }
    return {
      ok: true,
      delivery_key: rawAck.delivery_key,
      status: rawAck.status,
      sent_to_current_count: rawAck.sent_to_current_count,
    };
  }

  private async fetchAndVerifyArtifact(
    runId: string,
    desc: DataTaskPlanArtifactDescriptor,
  ): Promise<{
    content: Buffer;
    mimeType: string;
    filename: string;
  }> {
    if (desc.size_bytes <= 0 || desc.size_bytes > MAX_ARTIFACT_BYTES) {
      throw new DataTaskArtifactVerifyError(
        `Artifact ${desc.artifact_id} declared size (${desc.size_bytes}) is out of bounds`,
      );
    }
    if (typeof this.backend.downloadDataTaskRunArtifact !== "function") {
      throw new DataTaskArtifactVerifyError(
        "BackendClient does not implement downloadDataTaskRunArtifact",
      );
    }
    let downloaded;
    try {
      downloaded = await this.backend.downloadDataTaskRunArtifact(
        runId,
        desc.artifact_id,
        MAX_ARTIFACT_BYTES,
      );
    } catch (err) {
      throw new DataTaskArtifactVerifyError(
        safeBackendErrorMessage(
          err,
          `Artifact ${desc.artifact_id} download failed.`,
        ),
      );
    }
    if (downloaded.content.byteLength !== desc.size_bytes) {
      throw new DataTaskArtifactVerifyError(
        `Artifact ${desc.artifact_id} byte length mismatch: expected ${desc.size_bytes}, got ${downloaded.content.byteLength}`,
      );
    }
    const actualSha = computeSha256Hex(downloaded.content);
    const expectedSha = desc.sha256.trim().toLowerCase();
    if (actualSha !== expectedSha) {
      throw new DataTaskArtifactVerifyError(
        `Artifact ${desc.artifact_id} SHA-256 digest mismatch`,
      );
    }
    if (
      downloaded.sha256Header &&
      downloaded.sha256Header.trim().toLowerCase() !== expectedSha
    ) {
      throw new DataTaskArtifactVerifyError(
        `Artifact ${desc.artifact_id} header digest mismatch`,
      );
    }
    if (downloaded.mimeType !== desc.mime_type.toLowerCase()) {
      throw new DataTaskArtifactVerifyError(
        `Artifact ${desc.artifact_id} MIME mismatch: expected ${desc.mime_type}, got ${downloaded.mimeType}`,
      );
    }
    return {
      content: downloaded.content,
      mimeType: desc.mime_type,
      filename: desc.filename,
    };
  }

  async handleDataTaskNotification(
    rawBody: unknown,
  ): Promise<{ status: number; data: DataTaskNotificationResponse }> {
    if (!isPlainRecord(rawBody)) {
      return {
        status: 400,
        data: {
          ok: false,
          status: "failed",
          code: "invalid_request",
          message: "Request body must be a JSON object.",
          run_id: "",
          target: "",
          platform: null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const extraKeys = Object.keys(rawBody).filter(
      (k) => !ALLOWED_REQUEST_KEYS.has(k),
    );
    if (extraKeys.length > 0) {
      return {
        status: 400,
        data: {
          ok: false,
          status: "failed",
          code: "invalid_request",
          message: `Forbidden caller fields in data-task notification request: ${extraKeys.join(", ")}`,
          run_id: typeof rawBody.run_id === "string" ? rawBody.run_id : "",
          target: typeof rawBody.target === "string" ? rawBody.target : "",
          platform: null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const runId = typeof rawBody.run_id === "string" ? rawBody.run_id.trim() : "";
    const target = typeof rawBody.target === "string" ? rawBody.target.trim() : "";

    if (
      !runId ||
      !UUID_RE.test(runId) ||
      !target ||
      target.includes("://") ||
      target.includes("/") ||
      target.includes("\\") ||
      /\s/.test(target)
    ) {
      return {
        status: 400,
        data: {
          ok: false,
          status: "failed",
          code: "invalid_request",
          message: "run_id must be a valid UUID and target must be a canonical channel address.",
          run_id: runId,
          target,
          platform: null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const normalizedRunId = runId.toLowerCase();
    const queueKey = `${normalizedRunId}::${target}`;
    const tracker: ExecutionStateTracker = {
      claimedAnyItem: false,
      resolvedPlatform: null,
      itemsTotal: 0,
      itemsSent: 0,
      outcomes: [],
    };
    let capturedResult:
      | { status: number; data: DataTaskNotificationResponse }
      | undefined;

    try {
      const queueOutcome = await this.queue.enqueue(queueKey, async () => {
        capturedResult = await this.executeAuthorizedPlan(
          normalizedRunId,
          target,
          tracker,
        );
      });

      if (queueOutcome === "completed" && capturedResult !== undefined) {
        return capturedResult;
      }

      const isUncertain = tracker.claimedAnyItem || tracker.itemsSent > 0;
      return {
        status: 200,
        data: {
          ok: false,
          status: isUncertain ? "unknown" : "failed",
          code: isUncertain ? "delivery_uncertain" : "unavailable",
          message: isUncertain
            ? "Data-task notification timed out after claiming delivery; state is uncertain."
            : "Data-task notification timed out before claiming any item.",
          run_id: normalizedRunId,
          target,
          platform: tracker.resolvedPlatform,
          items_total: tracker.itemsTotal,
          items_sent: tracker.itemsSent,
          items: [...tracker.outcomes],
        },
      };
    } catch (err) {
      if (isStaleGatewayEpochError(err)) {
        throw err;
      }
      const isUncertain = tracker.claimedAnyItem || tracker.itemsSent > 0;
      const msg = safeBackendErrorMessage(
        err,
        isUncertain
          ? "Backend communication failed after claiming delivery; state is uncertain."
          : "Backend communication failed before claiming delivery.",
      );
      return {
        status: 200,
        data: {
          ok: false,
          status: isUncertain ? "unknown" : "failed",
          code: isUncertain ? "delivery_uncertain" : "unavailable",
          message: msg,
          run_id: normalizedRunId,
          target,
          platform: tracker.resolvedPlatform,
          items_total: tracker.itemsTotal,
          items_sent: tracker.itemsSent,
          items: [...tracker.outcomes],
        },
      };
    }
  }

  async deliverAuthorizedPlan(
    runId: string,
    target: string,
    tracker: ExecutionStateTracker,
    options?: AuthorizedPlanDeliveryOptions,
  ): Promise<{ status: number; data: DataTaskNotificationResponse }> {
    return this.executeAuthorizedPlan(runId, target, tracker, options);
  }

  private async executeAuthorizedPlan(
    runId: string,
    target: string,
    tracker: ExecutionStateTracker,
    options?: AuthorizedPlanDeliveryOptions,
  ): Promise<{ status: number; data: DataTaskNotificationResponse }> {
    if (this.startupBlocked) {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "unavailable",
          message: "Gateway startup recovery is still in progress.",
          run_id: runId,
          target,
          platform: null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }
    const claimKind = options?.claimKind ?? "data_task";
    const liveCheckPurpose =
      options?.liveCheckPurpose ?? "data_task_notification";
    const suppliedPlan = Object.prototype.hasOwnProperty.call(
      options ?? {},
      "rawPlan",
    );
    let rawPlan: unknown;
    if (suppliedPlan) {
      rawPlan = options?.rawPlan;
    } else {
      if (typeof this.backend.authorizeDataTaskNotificationPlan !== "function") {
        throw new DataTaskBackendAckError(
          "BackendClient does not implement authorizeDataTaskNotificationPlan",
        );
      }
      rawPlan = await this.backend.authorizeDataTaskNotificationPlan({
        run_id: runId,
        target,
      });
    }

    if (!isPlainRecord(rawPlan) || typeof rawPlan.authorized !== "boolean") {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "unavailable",
          message: "Malformed authorization plan response from backend.",
          run_id: runId,
          target,
          platform: null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const plan = rawPlan as unknown as DataTaskAuthorizePlanResponse;
    if (!plan.authorized) {
      const denyCode =
        typeof plan.code === "string" && plan.code.trim().length > 0
          ? plan.code
          : "not_allowed";
      const mappedStatus =
        plan.status === "skipped" || SKIP_CODES.has(denyCode)
          ? "skipped"
          : "failed";
      return {
        status: 200,
        data: {
          ok: false,
          status: mappedStatus,
          code: denyCode,
          message:
            typeof plan.message === "string"
              ? plan.message
              : "Data-task notification is not authorized.",
          run_id: runId,
          target,
          platform: isSupportedChannelPlatform(plan.platform)
            ? plan.platform
            : null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    if (
      !isSupportedChannelPlatform(plan.platform) ||
      typeof plan.canonical_target_address !== "string" ||
      plan.canonical_target_address.trim().length === 0 ||
      typeof plan.recipient_id !== "string" ||
      plan.recipient_id.trim().length === 0 ||
      (plan.target_kind !== "dm" && plan.target_kind !== "group") ||
      !Array.isArray(plan.items)
    ) {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "unavailable",
          message: "Authorized delivery plan from backend is missing required fields.",
          run_id: runId,
          target,
          platform: isSupportedChannelPlatform(plan.platform)
            ? plan.platform
            : null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const platform = plan.platform;
    const canonicalPlanTargetAddress = plan.canonical_target_address;
    const expectedRecipientId = plan.recipient_id;
    const planItems: DataTaskPlanItem[] = plan.items;
    tracker.resolvedPlatform = platform;
    tracker.itemsTotal = planItems.length;
    if (planItems.length === 0) {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "empty_payload",
          message: "Authorized delivery plan contains zero items.",
          run_id: runId,
          target,
          platform,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const adapter = this.adapters.get(platform);
    if (!this.isAdapterOutboundReady(adapter)) {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "unavailable",
          message: `The ${platform} channel adapter is unavailable; pending notification work is preserved.`,
          run_id: runId,
          target,
          platform,
          items_total: planItems.length,
          items_sent: 0,
          items: [],
        },
      };
    }
    const readyAdapter = adapter!;

    const needsSendCard = planItems.some((it) => it.kind === "feishu_card");
    if (needsSendCard && typeof readyAdapter.sendCard !== "function") {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "unsupported_item_kind",
          message: `Adapter ${platform} does not support structured Card 2.0 delivery.`,
          run_id: runId,
          target,
          platform,
          items_total: planItems.length,
          items_sent: 0,
          items: [],
        },
      };
    }

    const needsPlainFeishu =
      platform === "feishu" &&
      planItems.some((it) => it.kind === "text" && it.plain === true);
    if (
      needsPlainFeishu &&
      (typeof readyAdapter.sendTextMessage !== "function" ||
        typeof readyAdapter.splitTextMessageChunks !== "function")
    ) {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "unsupported_item_kind",
          message: `Adapter ${platform} does not support plain text delivery.`,
          run_id: runId,
          target,
          platform,
          items_total: planItems.length,
          items_sent: 0,
          items: [],
        },
      };
    }

    if (
      plan.target_kind === "group" &&
      typeof readyAdapter.checkTargetLive !== "function"
    ) {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "unavailable",
          message: `Adapter ${platform} cannot verify live group target ${canonicalPlanTargetAddress} before sending.`,
          run_id: runId,
          target,
          platform,
          items_total: planItems.length,
          items_sent: 0,
          items: [],
        },
      };
    }

    const itemOutcomes: DataTaskItemDeliveryOutcome[] = tracker.outcomes;
    let itemsSent = 0;
    let groupLiveVerified = false;
    // R5 #2 (Codex High #4): ONE shared deadline across all Weixin preflights in this delivery.
    const weixinPreflightBudget =
      platform === "weixin" ? this.createWeixinPreflightBudget() : null;

    for (const item of planItems) {
      if (
        plan.target_kind === "group" &&
        !groupLiveVerified &&
        item.existing_status !== "sent"
      ) {
        const preClaimLiveCheck = await readyAdapter.checkTargetLive!({
          userId: plan.owner_user_id ?? "",
          address: canonicalPlanTargetAddress,
          purpose: liveCheckPurpose,
        });
        if (!preClaimLiveCheck.ok) {
          const failCode = preClaimLiveCheck.code ?? "not_allowed";
          const denyMsg =
            preClaimLiveCheck.message ??
            `Group target ${canonicalPlanTargetAddress} is not accessible.`;
          return {
            status: 200,
            data: {
              ok: false,
              status: "failed",
              code: failCode,
              message: denyMsg,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }
        groupLiveVerified = true;
      }

      const usePlainFeishu =
        item.kind === "text" && item.plain === true && platform === "feishu";
      const textChunks =
        item.kind === "feishu_card"
          ? []
          : item.text
            ? usePlainFeishu
              ? readyAdapter.splitTextMessageChunks!(item.text)
              : readyAdapter.splitTextChunks(item.text)
            : [];
      const totalChunks = item.kind === "feishu_card" ? 1 : textChunks.length;
      const hasNonEmptyText =
        item.kind === "feishu_card" ? true : textChunks.length > 0;

      const hadPriorClaim = tracker.claimedAnyItem;
      tracker.claimedAnyItem = true;
      let rawClaim: unknown;
      try {
        rawClaim = await this.claimDeliveryWith503Retry({
          delivery_key: item.delivery_key,
          kind: claimKind,
          run_id: plan.run_id ?? runId,
          target: plan.requested_target ?? target,
          platform,
          has_non_empty_text: hasNonEmptyText,
          total_chunks: totalChunks,
        });
      } catch (claimErr) {
        if (isStaleGatewayEpochError(claimErr)) {
          throw claimErr;
        }
        const cMsg = safeBackendErrorMessage(
          claimErr,
          "I-6 authorize-and-claim request failed without a confirmed response.",
        );
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "unknown",
          duplicate: false,
          platform_message_ids: [],
          code: "delivery_uncertain",
          message: cMsg,
        });
        return {
          status: 200,
          data: {
            ok: false,
            status: "unknown",
            code: "delivery_uncertain",
            message: cMsg,
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      const validatedClaim = validateDataTaskClaimResponse(rawClaim, {
        platform,
        canonicalTargetAddress: canonicalPlanTargetAddress,
        recipientId: expectedRecipientId,
      });

      if (validatedClaim.kind === "invalid") {
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "unknown",
          duplicate: false,
          platform_message_ids: [],
          code: "delivery_uncertain",
          message: validatedClaim.reason,
        });
        return {
          status: 200,
          data: {
            ok: false,
            status: "unknown",
            code: "delivery_uncertain",
            message: validatedClaim.reason,
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      if (validatedClaim.kind === "already_completed") {
        if (validatedClaim.status === "sent") {
          tracker.claimedAnyItem = hadPriorClaim;
          itemsSent += 1;
          tracker.itemsSent = itemsSent;
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "sent",
            duplicate: true,
            platform_message_ids: validatedClaim.platformMessageIds,
            code: null,
          });
          continue;
        }
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "partial",
          duplicate: true,
          platform_message_ids: validatedClaim.platformMessageIds,
          code: "partial",
          message: "Earlier attempt partially delivered this item; automatic resend is blocked.",
        });
        return {
          status: 200,
          data: {
            ok: false,
            status: "unknown",
            code: "partial",
            message: "Earlier attempt partially delivered an item; automatic resend is blocked.",
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      if (validatedClaim.kind === "in_flight_or_unknown") {
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "unknown",
          duplicate: false,
          platform_message_ids: [],
          code: validatedClaim.code,
          message: validatedClaim.message,
        });
        return {
          status: 200,
          data: {
            ok: false,
            status: "unknown",
            code: validatedClaim.code,
            message: validatedClaim.message,
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      if (validatedClaim.kind === "denied") {
        tracker.claimedAnyItem = hadPriorClaim;
        const denyCode = validatedClaim.code;
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: validatedClaim.status,
          duplicate: false,
          platform_message_ids: [],
          code: denyCode,
          message: validatedClaim.message,
        });
        const overallStatus =
          validatedClaim.status === "unknown"
            ? "unknown"
            : itemsSent === 0 && SKIP_CODES.has(denyCode)
              ? "skipped"
              : "failed";
        return {
          status: 200,
          data: {
            ok: false,
            status: overallStatus,
            code: denyCode,
            message: validatedClaim.message,
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      // Strictly validated `claim_status === "claimed"`: never substitute plan fields.
      const targetAddress = validatedClaim.targetAddress;
      const recipientId = validatedClaim.recipientId;
      const recipientPlatformUserId =
        plan.target_kind === "dm" ? recipientId : undefined;

      // Safety fallback if `existing_status` was stale and `claim` transitioned to `claimed`.
      if (plan.target_kind === "group" && !groupLiveVerified) {
        const liveCheck = await readyAdapter.checkTargetLive!({
          userId: plan.owner_user_id ?? "",
          address: targetAddress,
          purpose: liveCheckPurpose,
        });
        if (!liveCheck.ok) {
          const failCode = liveCheck.code ?? "not_allowed";
          const denyMsg =
            liveCheck.message ?? `Group target ${targetAddress} is no longer accessible.`;
          try {
            await this.recordValidatedDeliveryProgress(
              {
                delivery_key: item.delivery_key,
                sent_chunks: 0,
                total_chunks: totalChunks,
                status: "failed",
                error_code: failCode,
                error_message: denyMsg,
              },
              platform,
            );
          } catch (progressErr) {
            const pMsg = safeBackendErrorMessage(
              progressErr,
              "Failed to record group target denial progress.",
            );
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [],
              code: "delivery_uncertain",
              message: pMsg,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: pMsg,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "failed",
            duplicate: false,
            platform_message_ids: [],
            code: failCode,
            message: denyMsg,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: "failed",
              code: failCode,
              message: denyMsg,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }
        groupLiveVerified = true;
      }

      if (item.kind === "feishu_card") {
        const cardImages: SendCardImageInput[] = [];
        try {
          for (const imgDesc of item.images) {
            if (!imgDesc.placeholder) {
              throw new DataTaskArtifactVerifyError(
                `Card image artifact ${imgDesc.artifact_id} is missing placeholder`,
              );
            }
            const verified = await this.fetchAndVerifyArtifact(runId, imgDesc);
            cardImages.push({
              placeholder: imgDesc.placeholder,
              content: verified.content,
              mimeType: verified.mimeType,
              filename: verified.filename,
            });
          }
        } catch (artErr) {
          const artMsg = safeBackendErrorMessage(
            artErr,
            "Card image artifact validation failed.",
          );
          try {
            await this.recordValidatedDeliveryProgress(
              {
                delivery_key: item.delivery_key,
                sent_chunks: 0,
                total_chunks: 1,
                status: "failed",
                error_code: "artifact_invalid",
                error_message: artMsg,
              },
              platform,
            );
          } catch (progressErr) {
            const pMsg = safeBackendErrorMessage(
              progressErr,
              "Failed to record artifact validation failure progress.",
            );
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [],
              code: "delivery_uncertain",
              message: pMsg,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: pMsg,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "failed",
            duplicate: false,
            platform_message_ids: [],
            code: "artifact_invalid",
            message: artMsg,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: "failed",
              code: "artifact_invalid",
              message: artMsg,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }

        try {
          const sendRes = await readyAdapter.sendCard!({
            targetAddress,
            recipientPlatformUserId,
            card: item.card,
            images: cardImages,
          });
          const filesStatus = item.images.map((img) => ({
            path: img.filename,
            status: "sent" as const,
            reason: null,
          }));
          try {
            await this.recordValidatedDeliveryProgress(
              {
                delivery_key: item.delivery_key,
                sent_chunks: 1,
                total_chunks: 1,
                platform_message_ids: [sendRes.messageId],
                files_status: filesStatus,
                status: "sent",
                first_response_json: {
                  status: "sent",
                  platform_message_ids: [sendRes.messageId],
                },
              },
              platform,
            );
          } catch (progressErr) {
            const pMsg = safeBackendErrorMessage(
              progressErr,
              "I-7 progress acknowledgement failed after card delivery; state is uncertain.",
            );
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [sendRes.messageId],
              code: "delivery_uncertain",
              message: pMsg,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: pMsg,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }

          itemsSent += 1;
          tracker.itemsSent = itemsSent;
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "sent",
            duplicate: false,
            platform_message_ids: [sendRes.messageId],
            code: null,
          });
          continue;
        } catch (sendErr) {
          const classified = classifyAdapterError(sendErr);
          const itemStatus =
            classified.outcome === "unknown" ? "unknown" : "failed";
          try {
            await this.recordValidatedDeliveryProgress(
              {
                delivery_key: item.delivery_key,
                sent_chunks: 0,
                total_chunks: 1,
                status: itemStatus,
                error_code: classified.code,
                error_message: classified.message,
              },
              platform,
            );
          } catch {
            // Row remains 'in_flight' in dalton_channel_deliveries (safe uncertain state)
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [],
              code: "delivery_uncertain",
              message: classified.message,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: classified.message,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: itemStatus,
            duplicate: false,
            platform_message_ids: [],
            code: classified.code,
            message: classified.message,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: itemStatus,
              code: classified.code,
              message: classified.message,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }
      }

      // `text_and_file` (e.g. T26) or `text`
      let verifiedAttachment: {
        content: Buffer;
        mimeType: string;
        filename: string;
      } | null = null;
      if (item.kind === "text_and_file") {
        try {
          verifiedAttachment = await this.fetchAndVerifyArtifact(
            runId,
            item.artifact,
          );
        } catch (artErr) {
          const artMsg = safeBackendErrorMessage(
            artErr,
            "Attachment artifact validation failed.",
          );
          try {
            await this.recordValidatedDeliveryProgress(
              {
                delivery_key: item.delivery_key,
                sent_chunks: 0,
                total_chunks: totalChunks,
                status: "failed",
                error_code: "artifact_invalid",
                error_message: artMsg,
              },
              platform,
            );
          } catch (progressErr) {
            if (isStaleGatewayEpochError(progressErr)) {
              throw progressErr;
            }
            const pMsg = safeBackendErrorMessage(
              progressErr,
              "Failed to record attachment validation failure progress.",
            );
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [],
              code: "delivery_uncertain",
              message: pMsg,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: pMsg,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "failed",
            duplicate: false,
            platform_message_ids: [],
            code: "artifact_invalid",
            message: artMsg,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: "failed",
              code: "artifact_invalid",
              message: artMsg,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }
      }

      const messageIds: string[] = [];
      let sentChunks = 0;
      const filesStatus: Array<{
        path: string;
        status: "sent" | "linked" | "failed" | "unknown";
        reason: string | null;
      }> = [];
      const sendTextWithFile = readyAdapter.sendTextWithFile?.bind(readyAdapter);
      // One platform message for a caption plus its file. Longer text still
      // chunks, and the file rides on the last chunk instead of a second post.
      // Adapters without sendTextWithFile keep text-then-file.
      const attachFileToLastTextChunk =
        item.kind === "text_and_file" &&
        verifiedAttachment !== null &&
        typeof sendTextWithFile === "function" &&
        textChunks.length > 0;
      let fileAttachedToText = false;

      const runWithWeixinSignal = async <T>(
        fn: (ctx: {
          signal?: AbortSignal;
          attachAccount: (id: string | null | undefined) => void;
        }) => Promise<T>,
      ): Promise<T> => {
        if (
          platform === "weixin" &&
          typeof readyAdapter.runWithAccountOperationSignal === "function"
        ) {
          return readyAdapter.runWithAccountOperationSignal(
            validatedClaim.weixinAccountId,
            fn,
          );
        }
        return fn({ signal: undefined, attachAccount: () => undefined });
      };

      for (let i = 0; i < textChunks.length; i += 1) {
        let chunkRes: { messageId: string };
        const attachFile =
          attachFileToLastTextChunk && i === textChunks.length - 1;
        try {
          if (attachFile && verifiedAttachment && sendTextWithFile) {
            const combined = await sendTextWithFile({
              targetAddress,
              recipientPlatformUserId,
              path: verifiedAttachment.filename,
              filename: verifiedAttachment.filename,
              content: verifiedAttachment.content,
              mimeType: verifiedAttachment.mimeType,
              text: textChunks[i]!,
              chunkIndex: i,
              totalChunks: textChunks.length,
            });
            if (combined.status !== "sent") {
              throw new ChannelDeliveryError(
                combined.reason ??
                  "Chart file must be uploaded directly; text-only link substitution is not permitted.",
                { outcome: "failed", code: "not_allowed" },
              );
            }
            chunkRes = { messageId: combined.messageId };
            fileAttachedToText = true;
            filesStatus.push({
              path: verifiedAttachment.filename,
              status: "sent",
              reason: null,
            });
          } else if (usePlainFeishu) {
            chunkRes = await readyAdapter.sendTextMessage!({
              targetAddress,
              recipientPlatformUserId,
              text: textChunks[i]!,
            });
          } else {
            const effectiveWeixinAttempt = validatedClaim.deliveryAttempt ?? 1;
            chunkRes = await runWithWeixinSignal(
              async ({ signal, attachAccount }) => {
                let weixinPreflight: WeixinPreflightAuthorizedData | null = null;
                if (platform === "weixin") {
                  weixinPreflight = await this.runWeixinPreflight(
                    item.delivery_key,
                    effectiveWeixinAttempt,
                    i,
                    weixinPreflightBudget!,
                    signal,
                  );
                  attachAccount(weixinPreflight.account_id);
                  if (this.leaseLost) {
                    throw new StaleGatewayEpochError(
                      "Gateway lease lost before data-task Weixin text send",
                    );
                  }
                  if (signal?.aborted) {
                    throw new ChannelDeliveryError("Weixin text send aborted", {
                      outcome: "failed",
                      code: "unavailable",
                    });
                  }
                }
                return readyAdapter.sendTextChunk({
                  targetAddress,
                  recipientPlatformUserId:
                    weixinPreflight?.recipient_id ?? recipientPlatformUserId,
                  text: textChunks[i]!,
                  chunkIndex: i,
                  totalChunks: textChunks.length,
                  deliveryKey: item.delivery_key,
                  deliveryAttempt: effectiveWeixinAttempt,
                  weixinPreflight,
                  weixinAccountId:
                    weixinPreflight?.account_id ??
                    validatedClaim.weixinAccountId,
                  weixinContextToken:
                    weixinPreflight?.weixin_context_token ??
                    validatedClaim.weixinContextToken,
                  signal,
                });
              },
            );
          }
        } catch (chunkErr) {
          if (isStaleGatewayEpochError(chunkErr) || this.leaseLost) {
            throw isStaleGatewayEpochError(chunkErr)
              ? chunkErr
              : new StaleGatewayEpochError(
                  "Gateway lease lost during data-task chunk delivery",
                  { cause: chunkErr },
                );
          }
          const classified = classifyAdapterError(chunkErr);
          const anySent = sentChunks > 0 || filesStatus.length > 0;
          // `unknown` overrides `partial` ONLY on Weixin; Feishu & Discord keep original `partial`.
          const itemStatus: "partial" | "unknown" | "failed" =
            classified.outcome === "unknown"
              ? platform === "weixin" || !anySent
                ? "unknown"
                : "partial"
              : anySent
                ? "partial"
                : "failed";
          const recordedErrorCode = classified.denyCode ?? classified.code;
          const overallStatus: DataTaskNotificationResponse["status"] =
            itemStatus === "failed" ? "failed" : "unknown";
          try {
            await this.recordValidatedDeliveryProgress(
              {
                delivery_key: item.delivery_key,
                sent_chunks: sentChunks,
                total_chunks: totalChunks,
                platform_message_ids: [...messageIds],
                ...(filesStatus.length > 0 ? { files_status: [...filesStatus] } : {}),
                status: itemStatus,
                error_code: recordedErrorCode,
                ...(platform === "weixin" && classified.errorDetail
                  ? { error_detail: classified.errorDetail }
                  : {}),
                error_message: classified.message,
              },
              platform,
            );
          } catch (progressErr) {
            if (isStaleGatewayEpochError(progressErr)) {
              throw progressErr;
            }
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [...messageIds],
              code: "delivery_uncertain",
              message: classified.message,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: classified.message,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: itemStatus,
            duplicate: false,
            platform_message_ids: [...messageIds],
            code: itemStatus === "partial" ? "partial" : recordedErrorCode,
            message: classified.message,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: overallStatus,
              code: itemStatus === "partial" ? "partial" : recordedErrorCode,
              message: classified.message,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }

        sentChunks += 1;
        messageIds.push(chunkRes.messageId);
        try {
          await this.recordValidatedDeliveryProgress(
            {
              delivery_key: item.delivery_key,
              sent_chunks: sentChunks,
              total_chunks: totalChunks,
              platform_message_ids: [...messageIds],
            },
            platform,
          );
        } catch (progressErr) {
          if (isStaleGatewayEpochError(progressErr)) {
            throw progressErr;
          }
          const pMsg = safeBackendErrorMessage(
            progressErr,
            "I-7 chunk progress acknowledgement failed after platform send; state is uncertain.",
          );
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "unknown",
            duplicate: false,
            platform_message_ids: [...messageIds],
            code: "delivery_uncertain",
            message: pMsg,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: "unknown",
              code: "delivery_uncertain",
              message: pMsg,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }
      }

      if (item.kind === "text_and_file" && verifiedAttachment && !fileAttachedToText) {
        try {
          const effectiveWeixinAttempt = validatedClaim.deliveryAttempt ?? 1;
          const fileChunkIndex = textChunks.length;
          const fileRes = await runWithWeixinSignal(
            async ({ signal, attachAccount }) => {
              let weixinPreflight: WeixinPreflightAuthorizedData | null = null;
              if (platform === "weixin") {
                weixinPreflight = await this.runWeixinPreflight(
                  item.delivery_key,
                  effectiveWeixinAttempt,
                  fileChunkIndex,
                  weixinPreflightBudget!,
                  signal,
                );
                attachAccount(weixinPreflight.account_id);
                if (this.leaseLost) {
                  throw new StaleGatewayEpochError(
                    "Gateway lease lost before data-task Weixin file send",
                  );
                }
                if (signal?.aborted) {
                  throw new ChannelDeliveryError("Weixin file send aborted", {
                    outcome: "failed",
                    code: "unavailable",
                  });
                }
              }
              return readyAdapter.sendFile({
                targetAddress,
                recipientPlatformUserId:
                  weixinPreflight?.recipient_id ?? recipientPlatformUserId,
                path: verifiedAttachment.filename,
                filename: verifiedAttachment.filename,
                content: verifiedAttachment.content,
                mimeType: verifiedAttachment.mimeType,
                deliveryKey: item.delivery_key,
                deliveryAttempt: effectiveWeixinAttempt,
                weixinPreflight,
                weixinAccountId:
                  weixinPreflight?.account_id ??
                  validatedClaim.weixinAccountId,
                weixinContextToken:
                  weixinPreflight?.weixin_context_token ??
                  validatedClaim.weixinContextToken,
                signal,
                refreshWeixinPreflight:
                  platform === "weixin"
                    ? () =>
                        this.runWeixinPreflight(
                          item.delivery_key,
                          effectiveWeixinAttempt,
                          fileChunkIndex,
                          weixinPreflightBudget!,
                          signal,
                        )
                    : undefined,
              });
            },
          );
          if (fileRes.status !== "sent") {
            const substMsg =
              fileRes.reason ??
              "Chart file must be uploaded directly; text-only link substitution is not permitted.";
            const itemStatus: "partial" | "failed" =
              sentChunks > 0 ? "partial" : "failed";
            filesStatus.push({
              path: verifiedAttachment.filename,
              status: "failed",
              reason: substMsg,
            });
            try {
              await this.recordValidatedDeliveryProgress(
                {
                  delivery_key: item.delivery_key,
                  sent_chunks: sentChunks,
                  total_chunks: totalChunks,
                  platform_message_ids: [...messageIds],
                  files_status: filesStatus,
                  status: itemStatus,
                  error_code: "file_not_sent",
                  error_message: substMsg,
                },
                platform,
              );
            } catch (progressErr) {
              if (isStaleGatewayEpochError(progressErr)) {
                throw progressErr;
              }
              const pMsg = safeBackendErrorMessage(
                progressErr,
                "Failed to record file delivery failure progress; state is uncertain.",
              );
              itemOutcomes.push({
                item_index: item.item_index,
                delivery_key: item.delivery_key,
                status: "unknown",
                duplicate: false,
                platform_message_ids: [...messageIds],
                code: "delivery_uncertain",
                message: pMsg,
              });
              return {
                status: 200,
                data: {
                  ok: false,
                  status: "unknown",
                  code: "delivery_uncertain",
                  message: pMsg,
                  run_id: runId,
                  target,
                  platform,
                  items_total: planItems.length,
                  items_sent: itemsSent,
                  items: itemOutcomes,
                },
              };
            }
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: itemStatus,
              duplicate: false,
              platform_message_ids: [...messageIds],
              code: itemStatus === "partial" ? "partial" : "file_not_sent",
              message: substMsg,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: itemStatus === "failed" ? "failed" : "unknown",
                code: itemStatus === "partial" ? "partial" : "file_not_sent",
                message: substMsg,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          messageIds.push(fileRes.messageId);
          filesStatus.push({
            path: verifiedAttachment.filename,
            status: "sent",
            reason: null,
          });
        } catch (fileErr) {
          if (isStaleGatewayEpochError(fileErr) || this.leaseLost) {
            throw isStaleGatewayEpochError(fileErr)
              ? fileErr
              : new StaleGatewayEpochError(
                  "Gateway lease lost during data-task file delivery",
                  { cause: fileErr },
                );
          }
          const classified = classifyAdapterError(fileErr);
          const anySent = sentChunks > 0 || filesStatus.length > 0;
          // `unknown` overrides `partial` ONLY on Weixin; Feishu & Discord keep original `partial`.
          const itemStatus: "partial" | "unknown" | "failed" =
            classified.outcome === "unknown"
              ? platform === "weixin" || !anySent
                ? "unknown"
                : "partial"
              : anySent
                ? "partial"
                : "failed";
          const recordedErrorCode = classified.denyCode ?? classified.code;
          filesStatus.push({
            path: verifiedAttachment.filename,
            status: classified.outcome === "unknown" ? "unknown" : "failed",
            reason: classified.message,
          });
          const overallStatus: DataTaskNotificationResponse["status"] =
            itemStatus === "failed" ? "failed" : "unknown";
          try {
            await this.recordValidatedDeliveryProgress(
              {
                delivery_key: item.delivery_key,
                sent_chunks: sentChunks,
                total_chunks: totalChunks,
                platform_message_ids: [...messageIds],
                files_status: filesStatus,
                status: itemStatus,
                error_code: recordedErrorCode,
                ...(platform === "weixin" && classified.errorDetail
                  ? { error_detail: classified.errorDetail }
                  : {}),
                error_message: classified.message,
              },
              platform,
            );
          } catch (progressErr) {
            if (isStaleGatewayEpochError(progressErr)) {
              throw progressErr;
            }
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [...messageIds],
              code: "delivery_uncertain",
              message: classified.message,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: classified.message,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: itemStatus,
            duplicate: false,
            platform_message_ids: [...messageIds],
            code: itemStatus === "partial" ? "partial" : recordedErrorCode,
            message: classified.message,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: overallStatus,
              code: itemStatus === "partial" ? "partial" : recordedErrorCode,
              message: classified.message,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }
      }

      try {
        await this.recordValidatedDeliveryProgress(
          {
            delivery_key: item.delivery_key,
            sent_chunks: sentChunks,
            total_chunks: totalChunks,
            platform_message_ids: [...messageIds],
            files_status: filesStatus,
            status: "sent",
            first_response_json: {
              status: "sent",
              platform_message_ids: [...messageIds],
            },
          },
          platform,
        );
      } catch (progressErr) {
        if (isStaleGatewayEpochError(progressErr)) {
          throw progressErr;
        }
        const pMsg = safeBackendErrorMessage(
          progressErr,
          "I-7 progress acknowledgement failed after platform delivery; state is uncertain.",
        );
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "unknown",
          duplicate: false,
          platform_message_ids: [...messageIds],
          code: "delivery_uncertain",
          message: pMsg,
        });
        return {
          status: 200,
          data: {
            ok: false,
            status: "unknown",
            code: "delivery_uncertain",
            message: pMsg,
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      itemsSent += 1;
      tracker.itemsSent = itemsSent;
      itemOutcomes.push({
        item_index: item.item_index,
        delivery_key: item.delivery_key,
        status: "sent",
        duplicate: false,
        platform_message_ids: [...messageIds],
        code: null,
      });
    }

    return {
      status: 200,
      data: {
        ok: true,
        status: "sent",
        code: null,
        message: `Delivered ${itemsSent} item(s).`,
        run_id: runId,
        target,
        platform,
        items_total: planItems.length,
        items_sent: itemsSent,
        items: itemOutcomes,
      },
    };
  }
}
