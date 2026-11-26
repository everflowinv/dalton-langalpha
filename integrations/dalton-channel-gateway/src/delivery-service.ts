import { createHash } from "node:crypto";
import { basename } from "node:path";
import {
  type BackendClient,
  BackendHttpError,
  type BotTopicRegistrationPayload,
  type DeliveryActivityOrigin,
  type I6AuthorizeAndClaimRequest,
  type I6AuthorizeAndClaimResponse,
  type VerifyDmRecipientResponse,
  type WeixinPreflightAuthorizedData,
} from "./backend-client.js";
import { ChannelDeliveryError, type ChannelAdapter } from "./channels/adapter.js";
import {
  isStaleGatewayEpochError,
  StaleGatewayEpochError,
} from "./gateway-lease.js";
import {
  createSubsystemLogger,
  extractSafeLogErrorFields,
  sanitizeLogErrorCode,
} from "./shared/logger.js";
import {
  buildWorkspaceFileDeepLinkLine,
  rewriteSandboxLinks,
} from "./shared/sandbox-links.js";
import {
  type AgentSendFileOutcome,
  type AgentSendFileRef,
  type AgentSendRequest,
  type AgentSendResponse,
  type ChannelPlatform,
  isSupportedPlatform,
  MAX_ATTACHMENT_BYTES,
  MAX_ATTACHMENT_COUNT,
  MAX_SEND_TEXT_CHARS,
  MAX_TOTAL_ATTACHMENT_BYTES,
} from "./types.js";

/**
 * R4/R5 Design: in-delivery bounded backoff retry for retryable preflight (`roster_lookup_error`)
 * and Weixin I-6 503. Shared deadline across an entire delivery MUST stay strictly below backend
 * caller HTTP read timeouts (`_SEND_TIMEOUT = 90s` in `src/tools/messaging/tools.py:57` and
 * `max(45s, notify_http_timeout_s * 2.5)` in `src/dalton/notify/channel_transport.py:176`).
 */
export const WEIXIN_PREFLIGHT_RETRY_INITIAL_DELAY_MS = 1_000;
export const WEIXIN_PREFLIGHT_RETRY_MAX_DELAY_MS = 16_000;
export const WEIXIN_PREFLIGHT_RETRY_MAX_ELAPSED_MS = 35_000;
export const DELIVERY_CLAIM_503_INITIAL_DELAY_MS = 1_000;
export const DELIVERY_CLAIM_503_MAX_DELAY_MS = 8_000;
export const DELIVERY_CLAIM_503_MAX_ATTEMPTS = 5;

export const WEIXIN_ROTATION_DENY_CODES: ReadonlySet<string> = new Set([
  "weixin_bot_generation_superseded",
  "weixin_bot_generation_mismatch",
  "weixin_account_rebound",
  "weixin_session_expired",
  "ilink_user_conflict_auto_unbound",
  "conflict_auto_unbound",
]);

/**
 * R6 #3 (Grok High): Returns true when an error originates from a per-request HTTP timeout
 * (e.g. `fetchWithTimeout` 15s single-call timeout on `preflightWeixinSend`), so preflight
 * retries within the shared delivery deadline instead of escaping as `outcome: "unknown"`.
 */
export function isPreflightTimeoutError(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  const e = err as { name?: unknown; code?: unknown; cause?: unknown };
  const name = typeof e.name === "string" ? e.name : "";
  const code = typeof e.code === "string" ? e.code : "";
  if (
    name === "TimeoutError" ||
    name === "AbortError" ||
    code === "ETIMEDOUT" ||
    code === "UND_ERR_CONNECT_TIMEOUT" ||
    code === "UND_ERR_HEADERS_TIMEOUT" ||
    code === "UND_ERR_BODY_TIMEOUT" ||
    code === "ABORT_ERR"
  ) {
    return true;
  }
  if (e.cause && e.cause !== err) {
    return isPreflightTimeoutError(e.cause);
  }
  return false;
}

export interface WeixinPreflightBudget {
  readonly deadlineMs: number;
  readonly maxElapsedMs: number;
  virtualElapsedMs: number;
  lastRetryableDenyCode?: string;
  lastRetryableMessage?: string;
}

export async function abortableRetrySleep(
  ms: number,
  signal?: AbortSignal,
): Promise<boolean> {
  if (signal?.aborted) {
    return false;
  }
  if (ms <= 0) {
    return true;
  }
  return await new Promise<boolean>((resolve) => {
    const onAbort = () => {
      clearTimeout(timer);
      resolve(false);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve(true);
    }, ms);
    (timer as { unref?: () => void }).unref?.();
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export interface LinkOnlyFileRef {
  path: string;
  workspace_id?: string | null;
  filename?: string | null;
  reason?: string | null;
}

export interface ExecuteDeliveryOptions {
  deliveryKey: string;
  kind: "agent_send" | "fallback" | "interrupt" | "automation" | "notice";
  userId?: string | null;
  threadId?: string | null;
  workspaceId?: string | null;
  runId?: string | null;
  executionId?: string | null;
  automationExecutionId?: string | null;
  platform?: string | null;
  target?: string | null;
  inboundMessageId?: string | null;
  text: string;
  files?: AgentSendFileRef[];
  /** Files delivered as web login deep links (tracked per `(workspace_id, path)` in `files_status` per confirmed chunk). */
  linkOnlyFiles?: LinkOnlyFileRef[];
  /** `"deliverable"` downloads via `/internal/deliverables/download` and applies DM deliverable dedupe/link fallback. */
  downloadMode?: "workspace" | "deliverable";
  /** When true (DM auto-attach deliverables), version mismatch / 404 / oversize falls back to a web login deep link. */
  linkFallbackOnDeliverableMismatch?: boolean;
  /** When true, emit web login deep links only (never download/upload binaries) for un-delivered DM deliverables. */
  forceLinkOnly?: boolean;
  replyToMessageId?: string | null;
  newThread?: boolean;
  threadScopeKey?: string | null;
  sourceScopeKey?: string | null;
  sourceGeneration?: number | null;
  activityOrigin?: DeliveryActivityOrigin | null;
}

export interface FirstCurrentDeliveryInfo {
  platform: ChannelPlatform;
  runId?: string | null;
  inboundMessageId?: string | null;
  kind: "agent_send" | "fallback" | "interrupt";
}

export class DeliveryService {
  private readonly backend: BackendClient;
  private readonly adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  private readonly webBaseUrl: string;
  private readonly logger = createSubsystemLogger("delivery-service");
  private readonly retryDelayFn: (
    ms: number,
    signal?: AbortSignal,
  ) => Promise<boolean>;
  private readonly preflightRetryMaxElapsedMs: number;
  private readonly claim503MaxAttempts: number;
  private readonly nowFn: () => number;
  private readonly onWeixinAccountRotated?: (info: {
    accountId?: string | null;
    reason: string;
  }) => void;
  private readonly firstCurrentDeliveryListeners = new Set<
    (info: FirstCurrentDeliveryInfo) => void
  >();
  private readonly inFlightCurrentDeliveriesByRun = new Map<
    string,
    Set<Promise<AgentSendResponse>>
  >();
  private retryAbortController = new AbortController();
  private startupBlocked = false;
  private leaseLost = false;

  constructor(options: {
    backend: BackendClient;
    adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
    webBaseUrl?: string;
    onFirstCurrentDelivery?: (info: FirstCurrentDeliveryInfo) => void;
    onWeixinAccountRotated?: (info: {
      accountId?: string | null;
      reason: string;
    }) => void;
    retryDelayFn?: (ms: number, signal?: AbortSignal) => Promise<boolean>;
    preflightRetryMaxElapsedMs?: number;
    claim503MaxAttempts?: number;
    nowFn?: () => number;
  }) {
    this.backend = options.backend;
    this.adapters = options.adapters;
    this.webBaseUrl = (options.webBaseUrl ?? "").trim().replace(/\/+$/, "");
    this.onWeixinAccountRotated = options.onWeixinAccountRotated;
    this.retryDelayFn = options.retryDelayFn ?? abortableRetrySleep;
    this.preflightRetryMaxElapsedMs =
      options.preflightRetryMaxElapsedMs ??
      WEIXIN_PREFLIGHT_RETRY_MAX_ELAPSED_MS;
    this.claim503MaxAttempts =
      options.claim503MaxAttempts ?? DELIVERY_CLAIM_503_MAX_ATTEMPTS;
    this.nowFn = options.nowFn ?? (() => Date.now());
    if (options.onFirstCurrentDelivery) {
      this.firstCurrentDeliveryListeners.add(options.onFirstCurrentDelivery);
    }
  }

  /**
   * Blocks new I-6 delivery claims while startup `mode=startup` recovery is still running (D1 / §3.5).
   * When blocking, also aborts any in-delivery retry waits immediately.
   */
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

  /**
   * R6 #6 (Codex Medium #5): Allows callers such as `Orchestrator.settleTurnFallbackAndPartialNotice`
   * to share the exact same lease-loss guard before I-6 claim and I-7 settlement.
   */
  assertNotLeaseLost(operation: string): void {
    if (this.leaseLost) {
      throw new StaleGatewayEpochError(`Gateway lease lost before ${operation}`);
    }
  }

  getRetryAbortSignal(): AbortSignal {
    return this.retryAbortController.signal;
  }

  private createWeixinPreflightBudget(): WeixinPreflightBudget {
    return {
      deadlineMs: this.nowFn() + this.preflightRetryMaxElapsedMs,
      maxElapsedMs: this.preflightRetryMaxElapsedMs,
      virtualElapsedMs: 0,
    };
  }

  addFirstCurrentDeliveryListener(
    listener: (info: FirstCurrentDeliveryInfo) => void,
  ): void {
    this.firstCurrentDeliveryListeners.add(listener);
  }

  hasInFlightCurrentDelivery(runId: string | null | undefined): boolean {
    const trimmed = runId?.trim();
    if (!trimmed) {
      return false;
    }
    const set = this.inFlightCurrentDeliveriesByRun.get(trimmed);
    return Boolean(set && set.size > 0);
  }

  /**
   * Awaits all currently in-flight current-conversation content deliveries
   * (`agent_send` / `fallback`) tracked for `runId` and returns their final outcomes.
   */
  async waitForInFlightCurrentDeliveries(
    runId: string | null | undefined,
  ): Promise<AgentSendResponse[]> {
    const trimmed = runId?.trim();
    if (!trimmed) {
      return [];
    }
    const outcomes: AgentSendResponse[] = [];
    const seen = new Set<Promise<AgentSendResponse>>();
    for (;;) {
      const set = this.inFlightCurrentDeliveriesByRun.get(trimmed);
      if (!set || set.size === 0) {
        break;
      }
      const pending = [...set].filter((p) => !seen.has(p));
      if (pending.length === 0) {
        break;
      }
      for (const p of pending) {
        seen.add(p);
      }
      const settled = await Promise.allSettled(pending);
      for (const item of settled) {
        if (item.status === "fulfilled") {
          outcomes.push(item.value);
        } else {
          outcomes.push({
            status: "unknown",
            code: "unavailable",
            address: null,
            current: true,
            duplicate: false,
            message: "In-flight delivery rejected unexpectedly.",
            files: [],
          });
        }
      }
    }
    return outcomes;
  }

  private notifyFirstCurrentDelivery(info: FirstCurrentDeliveryInfo): void {
    for (const listener of this.firstCurrentDeliveryListeners) {
      try {
        listener(info);
      } catch {
        // Best-effort status feedback hook must never disrupt delivery.
      }
    }
  }

  /**
   * Compute `delivery_key` for `POST /agent/send` (§1.2.1):
   * - If `tool_call_id` is non-empty: `${run_id}:${tool_call_id}`
   * - Otherwise fallback hash: `${run_id}:notcid:${sha256(canonical_json({target, text, files, reply, new_thread}))}`
   */
  static makeAgentSendDeliveryKey(req: AgentSendRequest): string {
    const tcid = (req.tool_call_id ?? "").trim();
    if (tcid.length > 0) {
      return `${req.run_id}:${tcid}`;
    }
    const canonical = JSON.stringify({
      target: req.target ?? null,
      text: req.text ?? "",
      files: (req.files ?? []).map((f) => ({
        path: f.path,
        workspace_id: f.workspace_id,
      })),
      reply: Boolean(req.reply),
      new_thread: Boolean(req.new_thread),
    });
    const digest = createHash("sha256").update(canonical, "utf8").digest("hex");
    return `${req.run_id}:notcid:${digest}`;
  }

  /**
   * Execute an outbound delivery through `I-6` (`authorize-and-claim`) and `I-7` (`deliveries/progress`).
   */
  executeDelivery(options: ExecuteDeliveryOptions): Promise<AgentSendResponse> {
    const trackRunId =
      (options.kind === "agent_send" || options.kind === "fallback") &&
      typeof options.runId === "string" &&
      options.runId.trim().length > 0
        ? options.runId.trim()
        : null;

    if (!trackRunId) {
      return this.executeDeliveryCore(options);
    }

    let detached = false;
    let trackedPromise!: Promise<AgentSendResponse>;
    const untrack = (): void => {
      if (detached) return;
      detached = true;
      const set = this.inFlightCurrentDeliveriesByRun.get(trackRunId);
      if (set) {
        set.delete(trackedPromise);
        if (set.size === 0) {
          this.inFlightCurrentDeliveriesByRun.delete(trackRunId);
        }
      }
    };

    trackedPromise = this.executeDeliveryCore(options, untrack).finally(untrack);
    let set = this.inFlightCurrentDeliveriesByRun.get(trackRunId);
    if (!set) {
      set = new Set<Promise<AgentSendResponse>>();
      this.inFlightCurrentDeliveriesByRun.set(trackRunId, set);
    }
    set.add(trackedPromise);
    return trackedPromise;
  }

  private async executeDeliveryCore(
    options: ExecuteDeliveryOptions,
    untrackIfNotCurrent?: () => void,
  ): Promise<AgentSendResponse> {
    if (this.startupBlocked) {
      untrackIfNotCurrent?.();
      return {
        status: "failed",
        code: "unavailable",
        address: options.target ?? null,
        current: options.target === undefined || options.target === null,
        duplicate: false,
        message: "Gateway startup recovery is still in progress.",
        files: [],
      };
    }

    const rawText = options.text ?? "";
    const trimmedText = rawText.trim();
    const hasNonEmptyText = trimmedText.length > 0;
    const files = options.files ?? [];

    if (!hasNonEmptyText && files.length === 0) {
      return {
        status: "failed",
        code: null,
        address: options.target ?? null,
        current: options.target === undefined || options.target === null,
        duplicate: false,
        message: "Both text and files are empty; nothing to send.",
        files: [],
      };
    }

    if (rawText.length > MAX_SEND_TEXT_CHARS) {
      return {
        status: "failed",
        code: null,
        address: options.target ?? null,
        current: options.target === undefined || options.target === null,
        duplicate: false,
        message: `Message text exceeds the ${MAX_SEND_TEXT_CHARS}-character limit.`,
        files: [],
      };
    }

    if (files.length > MAX_ATTACHMENT_COUNT) {
      return {
        status: "failed",
        code: null,
        address: options.target ?? null,
        current: options.target === undefined || options.target === null,
        duplicate: false,
        message: `Too many files attached (max ${MAX_ATTACHMENT_COUNT}).`,
        files: files.map((f) => ({
          path: f.path,
          status: "failed",
          reason: `Exceeds maximum of ${MAX_ATTACHMENT_COUNT} files per send`,
        })),
      };
    }

    const outboundText = hasNonEmptyText
      ? rewriteSandboxLinks(
          rawText,
          this.webBaseUrl,
          options.threadId,
          options.workspaceId ?? null,
        )
      : rawText;
    const hasOutboundText = outboundText.trim().length > 0;
    const linkOnlyFiles = options.linkOnlyFiles ?? [];

    if (!hasOutboundText && files.length === 0 && linkOnlyFiles.length === 0) {
      return {
        status: "failed",
        code: null,
        address: options.target ?? null,
        current: options.target === undefined || options.target === null,
        duplicate: false,
        message: "Both text and files are empty; nothing to send.",
        files: [],
      };
    }

    // Preliminary chunk count (refined once platform is known from I-6)
    const preliminaryPlat = this.inferPlatform(options.platform, options.target);
    const preliminaryAdapter = preliminaryPlat ? this.adapters.get(preliminaryPlat) : undefined;
    const preliminaryChunks = hasOutboundText
      ? preliminaryAdapter
        ? preliminaryAdapter.splitTextChunks(outboundText)
        : [outboundText]
      : [];

    // Deliverable deliveries (`deliverables:*` / `deliverables-links:*`) only send attachment
    // deep-link fallbacks in their text field — never treat them as conversation body text for
    // body dedupe (`has_non_empty_text`).
    const isDeliverableDelivery =
      options.downloadMode === "deliverable" ||
      options.deliveryKey.startsWith("deliverables:") ||
      options.deliveryKey.startsWith("deliverables-links:");

    let claim: I6AuthorizeAndClaimResponse;
    try {
      claim = await this.claimDeliveryWith503Retry({
        delivery_key: options.deliveryKey,
        kind: options.kind,
        user_id: options.userId ?? null,
        thread_id: options.threadId ?? null,
        run_id: options.runId ?? null,
        execution_id: options.executionId ?? null,
        automation_execution_id: options.automationExecutionId ?? null,
        platform: preliminaryPlat ?? options.platform ?? null,
        target: options.target ?? null,
        inbound_message_id: options.inboundMessageId ?? null,
        has_non_empty_text: isDeliverableDelivery ? false : hasOutboundText,
        total_chunks: Math.max(preliminaryChunks.length, hasOutboundText ? 1 : 0),
        source_scope_key: options.sourceScopeKey ?? options.threadScopeKey ?? null,
        source_generation: options.sourceGeneration ?? null,
        activity_origin: options.activityOrigin ?? null,
      });
    } catch (err) {
      if (isStaleGatewayEpochError(err)) {
        throw err;
      }
      if (err instanceof BackendHttpError && err.status === 503) {
        untrackIfNotCurrent?.();
        return {
          status: "failed",
          code: "unavailable",
          address: options.target ?? null,
          current: options.target === undefined || options.target === null,
          duplicate: false,
          message: "Delivery authorization is temporarily unavailable (503).",
          files: [],
        };
      }
      throw err;
    }

    if (claim.claim_status === "already_completed") {
      if (!claim.is_current) {
        untrackIfNotCurrent?.();
      }
      const prev = (claim.first_response_json ?? {}) as Partial<AgentSendResponse>;
      return {
        status: (prev.status ?? claim.status ?? "sent") as AgentSendResponse["status"],
        code: (prev.code ?? null) as AgentSendResponse["code"],
        address: (prev.address ?? claim.target_address ?? null) as string | null,
        current: Boolean(prev.current ?? claim.is_current),
        duplicate: true,
        message:
          prev.message ??
          "An earlier attempt of this same call already delivered it; nothing was sent twice.",
        files: Array.isArray(prev.files) ? prev.files : [],
      };
    }

    if (claim.claim_status === "in_flight_or_unknown") {
      if (!claim.is_current) {
        untrackIfNotCurrent?.();
      }
      if (
        options.downloadMode === "deliverable" &&
        options.deliveryKey.startsWith("deliverables:") &&
        claim.existing_status === "unknown" &&
        options.runId &&
        !options.forceLinkOnly
      ) {
        // Keep the original `deliverables:${runId}` row as `unknown` (never re-uploading binary files)
        // and deliver web login deep links via an independent, deduplicable key based on `prior_run_files`.
        return this.executeDelivery({
          ...options,
          deliveryKey: `deliverables-links:${options.runId}`,
          text:
            linkOnlyFiles.length > 0
              ? options.text
              : (claim.sent_chunks ?? 0) > 0
                ? ""
                : options.text,
          forceLinkOnly: true,
        });
      }
      return {
        status: "unknown",
        code: "in_flight",
        address: claim.target_address ?? options.target ?? null,
        current: Boolean(claim.is_current),
        duplicate: false,
        message:
          claim.message ??
          "An earlier attempt of this same call is still delivering it, and it will arrive or fail on its own. Do not send it again, and do not tell the user it was sent.",
        files: [],
      };
    }

    if (claim.claim_status === "denied") {
      if (!claim.is_current) {
        untrackIfNotCurrent?.();
      }
      if (claim.code && WEIXIN_ROTATION_DENY_CODES.has(claim.code)) {
        this.onWeixinAccountRotated?.({
          accountId: claim.weixin_account_id ?? null,
          reason: claim.code,
        });
      }
      const mappedCode =
        claim.code === "unknown_delivery_blocks_fallback"
          ? null
          : ((claim.code ?? "not_allowed") as AgentSendResponse["code"]);
      // R4 #7 (Codex #12): preserve `status: "unknown"` when backend denies with unknown state (e.g. `source_generation_unknown`).
      const deniedStatus: AgentSendResponse["status"] =
        claim.status === "unknown" ? "unknown" : "failed";
      return {
        status: deniedStatus,
        code: mappedCode,
        address: claim.target_address ?? options.target ?? null,
        current: Boolean(claim.is_current),
        duplicate: false,
        message: claim.message ?? "Delivery was refused.",
        files: [],
      };
    }

    // `claim_status === "claimed"`
    if (!claim.is_current) {
      untrackIfNotCurrent?.();
    }
    const deliveryAttempt =
      typeof claim.delivery_attempt === "number" ? claim.delivery_attempt : undefined;
    // Note (`PROTOCOL.md` §9 I-6): `claim.recipient_id` is the platform recipient ID
    // (`platform_user_id` for DM or `chat_id` for group), whereas `options.userId` is the Dalton user UUID!
    const platformStr = (claim.platform ?? preliminaryPlat ?? "").toLowerCase();
    const recordProgress = (
      body: Parameters<BackendClient["updateDeliveryProgress"]>[0],
    ) => {
      if (this.leaseLost) {
        throw new StaleGatewayEpochError(
          "Gateway lease lost before I-7 settlement",
        );
      }
      return this.backend.updateDeliveryProgress(
        deliveryAttempt === undefined
          ? body
          : { ...body, delivery_attempt: deliveryAttempt },
        { platform: platformStr || null },
      );
    };
    const targetAddress = claim.target_address ?? options.target ?? null;
    const isCurrent = Boolean(claim.is_current);
    const effectiveThreadScopeKey =
      claim.thread_scope_key && claim.thread_scope_key.trim().length > 0
        ? claim.thread_scope_key.trim()
        : options.target && !isCurrent
          ? null
          : (options.threadScopeKey ?? null);
    const daltonUserId = (options.userId ?? "").trim();

    if (!isSupportedPlatform(platformStr) || !targetAddress) {
      const failResp: AgentSendResponse = {
        status: "failed",
        code: "unavailable",
        address: targetAddress,
        current: isCurrent,
        duplicate: false,
        message: `Platform ${platformStr || "unknown"} is not supported or target address is missing.`,
        files: [],
      };
      await recordProgress({
        delivery_key: options.deliveryKey,
        status: "failed",
        error_code: "unavailable",
        error_message: failResp.message,
        first_response_json: failResp as unknown as Record<string, unknown>,
      });
      return failResp;
    }

    const adapter = this.adapters.get(platformStr);
    if (!adapter || !adapter.isConfigured() || !adapter.isConnected()) {
      const failResp: AgentSendResponse = {
        status: "failed",
        code: "unavailable",
        address: targetAddress,
        current: isCurrent,
        duplicate: false,
        message: `The ${platformStr} channel adapter is not connected.`,
        files: [],
      };
      await recordProgress({
        delivery_key: options.deliveryKey,
        status: "failed",
        error_code: "unavailable",
        error_message: failResp.message,
        first_response_json: failResp as unknown as Record<string, unknown>,
      });
      return failResp;
    }

    const isDmTarget = targetAddress.endsWith(":@me");
    const currentSessionWsId =
      (options.workspaceId ?? "").trim() ||
      (claim.workspace_id ?? "").trim() ||
      (claim.home_workspace_id ?? "").trim() ||
      null;
    const fileOutcomes: AgentSendFileOutcome[] = [];
    let filesToSend: AgentSendFileRef[] = [...files];
    const pendingLinkEntries: Array<{
      line: string;
      outcome: AgentSendFileOutcome;
    }> = [];

    const normalizeDeliverablePath = (rawPath: string): string => {
      const segments: string[] = [];
      for (const seg of rawPath.trim().replace(/\\/g, "/").split("/")) {
        if (!seg || seg === ".") continue;
        segments.push(seg);
      }
      return segments.join("/");
    };

    const priorByWsAndPath = new Map<string, string>();
    const legacyPriorByPath = new Map<string, string>();
    if (isDmTarget && options.downloadMode === "deliverable") {
      const statusPriority = (status: string): number => {
        if (status === "sent" || status === "linked") return 3;
        if (status === "unknown" || status === "pending") return 2;
        return 1;
      };
      const recordPrior = (
        map: Map<string, string>,
        key: string,
        status: string,
      ): void => {
        const prev = map.get(key);
        if (!prev || statusPriority(status) >= statusPriority(prev)) {
          map.set(key, status);
        }
      };
      for (const prior of claim.prior_run_files ?? []) {
        const normPath = normalizeDeliverablePath(prior.path);
        const wsKey = (prior.workspace_id ?? "").trim();
        const status = (prior.status ?? "").trim().toLowerCase();
        if (!normPath || !status) continue;
        if (wsKey) {
          recordPrior(priorByWsAndPath, `${wsKey}::${normPath}`, status);
        } else {
          recordPrior(legacyPriorByPath, normPath, status);
        }
      }
    }
    const lookupPriorDeliverableStatus = (
      rawPath: string,
      wsId: string,
    ): string | undefined => {
      const normPath = normalizeDeliverablePath(rawPath);
      if (!normPath) return undefined;
      const canUseLegacyMatch =
        !wsId || !currentSessionWsId || wsId === currentSessionWsId;
      return (
        (wsId ? priorByWsAndPath.get(`${wsId}::${normPath}`) : undefined) ??
        (canUseLegacyMatch ? legacyPriorByPath.get(normPath) : undefined)
      );
    };

    // Track overflow / HTML / oversized link-only deliverables per `(workspace_id, path)` so
    // multi-chunk link messages persist confirmed links per chunk and restart recovery only
    // sends remaining unconfirmed links.
    for (const linkRef of linkOnlyFiles) {
      const wsId =
        (linkRef.workspace_id ?? "").trim() || currentSessionWsId || "";
      if (isDmTarget && options.downloadMode === "deliverable") {
        const priorStatus = lookupPriorDeliverableStatus(linkRef.path, wsId);
        if (priorStatus === "sent" || priorStatus === "linked") {
          continue;
        }
      }
      pendingLinkEntries.push({
        line: buildWorkspaceFileDeepLinkLine(
          linkRef.filename || basename(linkRef.path) || linkRef.path,
          linkRef.path,
          this.webBaseUrl,
          options.threadId,
          wsId || null,
          currentSessionWsId,
        ),
        outcome: {
          path: linkRef.path,
          workspace_id: wsId || null,
          status: "linked",
          reason: linkRef.reason ?? "deliverable_overflow_link",
        },
      });
    }

    // Lead Decision #7: Group targets (including explicit `send_message(files=...)`) NEVER upload binary files;
    // after I-6 authorizes the delivery, convert every file into a sign-in-protected web deep link.
    if (!isDmTarget && filesToSend.length > 0) {
      for (const fileRef of filesToSend) {
        const wsId =
          fileRef.workspace_id || currentSessionWsId || null;
        pendingLinkEntries.push({
          line: buildWorkspaceFileDeepLinkLine(
            basename(fileRef.path) || fileRef.path,
            fileRef.path,
            this.webBaseUrl,
            options.threadId,
            wsId,
            currentSessionWsId,
          ),
          outcome: {
            path: fileRef.path,
            workspace_id: wsId,
            status: "linked",
            reason: "group_target_deep_link",
          },
        });
      }
      filesToSend = [];
    }

    // Convert `.html` / `.htm` files into sign-in-protected web deep links after I-6 authorizes
    // the actual target (covers explicit DM `send_message(files=...)` as well as auto-attach),
    // ensuring zero file downloads and zero platform uploads for HTML files.
    if (filesToSend.length > 0) {
      const nonHtmlFiles: AgentSendFileRef[] = [];
      for (const fileRef of filesToSend) {
        const lowerPath = fileRef.path.trim().toLowerCase();
        if (lowerPath.endsWith(".html") || lowerPath.endsWith(".htm")) {
          const wsId = fileRef.workspace_id || currentSessionWsId || null;
          pendingLinkEntries.push({
            line: buildWorkspaceFileDeepLinkLine(
              basename(fileRef.path) || fileRef.path,
              fileRef.path,
              this.webBaseUrl,
              options.threadId,
              wsId,
              currentSessionWsId,
            ),
            outcome: {
              path: fileRef.path,
              workspace_id: wsId,
              status: "linked",
              reason: "html_file_deep_link",
            },
          });
          continue;
        }
        nonHtmlFiles.push(fileRef);
      }
      filesToSend = nonHtmlFiles;
    }

    // Lead Decisions #6 & #7 (DM auto-attach deliverables):
    // 1. Deduplicate against `claim.prior_run_files` across the run's `is_current=TRUE` deliveries
    //    by `(workspace_id, normalized_path)`. Only fall back to legacy path-only matching when a
    //    prior record lacks `workspace_id` and the file belongs to the current session workspace.
    //    - `sent` / `linked` -> skip (already delivered)
    //    - `pending` / `unknown` -> never re-send binary; emit web login deep link only
    // 2. If `options.forceLinkOnly` (recovery after an `unknown` deliverables attempt) or DM deliverable
    //    lacks a pinned original DM recipient (`claim.dm_recipient_platform_user_id`), emit web login deep
    //    links only (never upload binaries).
    if (isDmTarget && options.downloadMode === "deliverable" && filesToSend.length > 0) {
      const pinnedDmRecipient = (claim.dm_recipient_platform_user_id ?? "").trim();
      const nextFilesToSend: AgentSendFileRef[] = [];
      for (const fileRef of filesToSend) {
        const wsId =
          (fileRef.workspace_id ?? "").trim() || currentSessionWsId || "";
        const priorStatus = lookupPriorDeliverableStatus(fileRef.path, wsId);
        if (priorStatus === "sent" || priorStatus === "linked") {
          continue;
        }
        if (priorStatus === "pending" || priorStatus === "unknown") {
          pendingLinkEntries.push({
            line: buildWorkspaceFileDeepLinkLine(
              basename(fileRef.path) || fileRef.path,
              fileRef.path,
              this.webBaseUrl,
              options.threadId,
              wsId || null,
              currentSessionWsId,
            ),
            outcome: {
              path: fileRef.path,
              workspace_id: wsId || null,
              status: "linked",
              reason: "prior_pending_or_unknown_link_only",
            },
          });
          continue;
        }
        if (options.forceLinkOnly) {
          pendingLinkEntries.push({
            line: buildWorkspaceFileDeepLinkLine(
              basename(fileRef.path) || fileRef.path,
              fileRef.path,
              this.webBaseUrl,
              options.threadId,
              wsId || null,
              currentSessionWsId,
            ),
            outcome: {
              path: fileRef.path,
              workspace_id: wsId || null,
              status: "linked",
              reason: "prior_unknown_delivery_link_only",
            },
          });
          continue;
        }
        if (!pinnedDmRecipient) {
          pendingLinkEntries.push({
            line: buildWorkspaceFileDeepLinkLine(
              basename(fileRef.path) || fileRef.path,
              fileRef.path,
              this.webBaseUrl,
              options.threadId,
              wsId || null,
              currentSessionWsId,
            ),
            outcome: {
              path: fileRef.path,
              workspace_id: wsId || null,
              status: "linked",
              reason: "missing_dm_recipient_link_only",
            },
          });
          continue;
        }
        nextFilesToSend.push(fileRef);
      }
      filesToSend = nextFilesToSend;
    }

    const hasEffectiveOutboundText =
      hasOutboundText || pendingLinkEntries.length > 0;

    // If all deliverable files were already satisfied by prior deliveries and there is no text to send,
    // settle cleanly as `sent` without dispatching anything duplicate.
    if (!hasEffectiveOutboundText && filesToSend.length === 0) {
      const noopResp: AgentSendResponse = {
        status: "sent",
        code: null,
        address: targetAddress,
        current: isCurrent,
        duplicate: false,
        message: `No additional content or files needed for ${targetAddress}.`,
        files: fileOutcomes.map(({ path, status, reason }) => ({
          path,
          status,
          reason,
        })),
      };
      await recordProgress({
        delivery_key: options.deliveryKey,
        sent_chunks: 0,
        total_chunks: 0,
        platform_message_ids: [],
        files_status: fileOutcomes,
        first_response_json: noopResp as unknown as Record<string, unknown>,
        status: "sent",
        error_code: null,
        error_message: null,
      });
      return noopResp;
    }

    const packLinkEntriesIntoChunks = (
      entries: Array<{ line: string; outcome: AgentSendFileOutcome }>,
    ): { packedChunks: string[]; outcomesByChunk: AgentSendFileOutcome[][] } => {
      const packedChunks: string[] = [];
      const outcomesByChunk: AgentSendFileOutcome[][] = [];
      let curLines: string[] = [];
      let curOutcomes: AgentSendFileOutcome[] = [];
      for (const entry of entries) {
        const candidateLines = [...curLines, entry.line];
        const candidateText = candidateLines.join("\n");
        const splitCandidate = adapter.splitTextChunks(candidateText);
        if (splitCandidate.length === 1) {
          curLines = candidateLines;
          curOutcomes.push(entry.outcome);
          continue;
        }
        if (curLines.length > 0) {
          packedChunks.push(curLines.join("\n"));
          outcomesByChunk.push(curOutcomes);
          curLines = [];
          curOutcomes = [];
        }
        const singleSplit = adapter.splitTextChunks(entry.line);
        if (singleSplit.length <= 1) {
          curLines = [entry.line];
          curOutcomes = [entry.outcome];
        } else {
          for (let s = 0; s < singleSplit.length; s += 1) {
            packedChunks.push(singleSplit[s]!);
            outcomesByChunk.push(
              s === singleSplit.length - 1 ? [entry.outcome] : [],
            );
          }
        }
      }
      if (curLines.length > 0) {
        packedChunks.push(curLines.join("\n"));
        outcomesByChunk.push(curOutcomes);
      }
      return { packedChunks, outcomesByChunk };
    };

    let chunks: string[] = [];
    let chunkLinkOutcomes: AgentSendFileOutcome[][] = [];
    if (pendingLinkEntries.length === 0) {
      chunks = hasOutboundText ? adapter.splitTextChunks(outboundText) : [];
      chunkLinkOutcomes = chunks.map(() => []);
    } else if (hasOutboundText) {
      const combined = `${outboundText}\n\n${pendingLinkEntries.map((e) => e.line).join("\n")}`;
      const combinedSplit = adapter.splitTextChunks(combined);
      if (combinedSplit.length === 1) {
        chunks = combinedSplit;
        chunkLinkOutcomes = [pendingLinkEntries.map((e) => e.outcome)];
      } else {
        const textChunks = adapter.splitTextChunks(outboundText);
        const { packedChunks, outcomesByChunk } =
          packLinkEntriesIntoChunks(pendingLinkEntries);
        chunks = [...textChunks, ...packedChunks];
        chunkLinkOutcomes = [
          ...textChunks.map(() => [] as AgentSendFileOutcome[]),
          ...outcomesByChunk,
        ];
      }
    } else {
      const { packedChunks, outcomesByChunk } =
        packLinkEntriesIntoChunks(pendingLinkEntries);
      chunks = packedChunks;
      chunkLinkOutcomes = outcomesByChunk;
    }
    let totalChunks = chunks.length;
    if (totalChunks !== preliminaryChunks.length) {
      await recordProgress({
        delivery_key: options.deliveryKey,
        total_chunks: totalChunks,
      });
    }

    // `claim.recipient_id` already carries the platform recipient ID from `I-6`.
    // For DM deliveries, prefer `claim.dm_recipient_platform_user_id` when present (Lead Decision #6).
    // Only fall back to `I-4` lookup (with Dalton `user_id`) if `claim.recipient_id` was omitted on a DM.
    let recipientPlatformUserId: string | null =
      (isDmTarget &&
        claim.dm_recipient_platform_user_id &&
        claim.dm_recipient_platform_user_id.trim().length > 0
        ? claim.dm_recipient_platform_user_id.trim()
        : null) ??
      (claim.recipient_id && claim.recipient_id.trim().length > 0
        ? claim.recipient_id.trim()
        : null);
    if (!recipientPlatformUserId && isDmTarget && daltonUserId) {
      try {
        const state = await this.backend.getChannelState({
          user_id: daltonUserId,
          platform: platformStr,
        });
        recipientPlatformUserId =
          state.identities?.[platformStr]?.platform_user_id ?? null;
      } catch {
        // Proceed with targetAddress if lookup fails
      }
    }

    // Lead Decisions #1 & #3: Bot-owned group topic creation & routing.
    // Authorized platform topic ID (`claim.external_topic_id`) covers BOTH `:topic:`
    // and `:thread:` sessions (existing topic `@bot` or other member `@bot` in bot topic).
    const isTopicScope = Boolean(
      effectiveThreadScopeKey && effectiveThreadScopeKey.includes(":topic:"),
    );
    let currentExternalTopicId = (claim.external_topic_id ?? "").trim() || null;
    const isGroupTopicTarget =
      !isDmTarget &&
      isCurrent &&
      (isTopicScope || Boolean(currentExternalTopicId)) &&
      (platformStr === "feishu" || platformStr === "discord");
    const verifiedRootMessageId =
      (claim.root_message_id ?? "").trim() ||
      (options.replyToMessageId ?? "").trim() ||
      null;
    const currentInboundAnchor =
      (options.replyToMessageId ?? "").trim() ||
      (options.inboundMessageId ?? "").trim() ||
      (claim.inbound_message_id ?? "").trim() ||
      null;
    const isReplayOrBackgroundOrigin =
      options.activityOrigin === "replay" ||
      options.activityOrigin === "notification";
    let firstReplyMessageId = (claim.first_reply_message_id ?? "").trim() || null;
    const topicCanonicalScopeKey =
      (claim.topic_canonical_scope_key ?? effectiveThreadScopeKey ?? "").trim() ||
      null;
    const topicInitiatorPlatformUserId =
      (claim.topic_initiator_platform_user_id ?? "").trim() || null;
    const buildTopicRegistration = (
      extTopicId: string | null,
      firstReplyId?: string | null,
    ): BotTopicRegistrationPayload | null => {
      const resolvedFirstReply = firstReplyId ?? firstReplyMessageId ?? null;
      if (
        !isGroupTopicTarget ||
        (!extTopicId && !resolvedFirstReply) ||
        !topicCanonicalScopeKey ||
        !topicInitiatorPlatformUserId
      ) {
        return null;
      }
      return {
        platform: platformStr,
        chat_address: targetAddress,
        canonical_scope_key: topicCanonicalScopeKey,
        external_topic_id: extTopicId,
        root_message_id: verifiedRootMessageId,
        first_reply_message_id: resolvedFirstReply,
        initiator_platform_user_id: topicInitiatorPlatformUserId,
        initiator_dalton_user_id: daltonUserId || null,
      };
    };
    const recordAdapterTopicCache = (extTopicId: string): void => {
      if (
        !isGroupTopicTarget ||
        !extTopicId ||
        !topicCanonicalScopeKey ||
        !topicInitiatorPlatformUserId
      ) {
        return;
      }
      adapter.recordBotTopic?.({
        chatAddress: targetAddress,
        externalTopicId: extTopicId,
        canonicalScopeKey: topicCanonicalScopeKey,
        initiatorPlatformUserId: topicInitiatorPlatformUserId,
        rootMessageId: verifiedRootMessageId,
      });
    };

    const platformMessageIds: string[] = [];
    let sentChunks = 0;
    let feishuTopicCreationFailed = false;
    let feishuTopicConfirmedInThread = Boolean(currentExternalTopicId);
    let feishuLastInThreadMessageId: string | null = firstReplyMessageId;
    let terminalNoticeCompleted = false;
    let firstCurrentDeliveryNotified = false;
    const isFeedbackTrackedKind =
      options.kind === "agent_send" ||
      options.kind === "fallback" ||
      options.kind === "interrupt";
    const maybeNotifyFirstCurrentDelivery = (): void => {
      if (firstCurrentDeliveryNotified || !isCurrent || !isFeedbackTrackedKind) {
        return;
      }
      firstCurrentDeliveryNotified = true;
      this.notifyFirstCurrentDelivery({
        platform: platformStr,
        runId: options.runId ?? null,
        inboundMessageId: options.inboundMessageId ?? null,
        kind: options.kind as "agent_send" | "fallback" | "interrupt",
      });
    };
    let finalAddress = targetAddress;
    const confirmedThreadAddress = (claim.confirmed_thread_address ?? "").trim();
    const reuseConfirmedThread =
      platformStr === "discord" && confirmedThreadAddress.length > 0;
    let chunkFailure: {
      outcome: "failed" | "unknown";
      forceUnknown?: boolean;
      code: AgentSendResponse["code"];
      denyCode?: string;
      errorDetail?: string;
      retryable?: boolean;
      message: string;
    } | null = null;
    const effectiveWeixinAttempt =
      typeof deliveryAttempt === "number" && deliveryAttempt > 0
        ? deliveryAttempt
        : 1;
    // R5 #2 (Codex High #4): ONE shared deadline across all preflights in this delivery
    // (all text chunks, all files, and post-upload refresh preflights).
    const weixinPreflightBudget =
      platformStr === "weixin" ? this.createWeixinPreflightBudget() : null;

    const runWithWeixinSignal = async <T>(
      initialAccountId: string | null | undefined,
      fn: (ctx: {
        signal?: AbortSignal;
        attachAccount: (id: string | null | undefined) => void;
      }) => Promise<T>,
    ): Promise<T> => {
      if (
        platformStr === "weixin" &&
        typeof adapter.runWithAccountOperationSignal === "function"
      ) {
        return adapter.runWithAccountOperationSignal(initialAccountId, fn);
      }
      return fn({ signal: undefined, attachAccount: () => undefined });
    };

    for (let i = 0; i < chunks.length; i++) {
      const chunkText = chunks[i]!;
      try {
        const res = await runWithWeixinSignal(
          claim.weixin_account_id ?? null,
          async ({ signal, attachAccount }) => {
            let weixinPreflight: WeixinPreflightAuthorizedData | null = null;
            let weixinContextToken: string | null =
              claim.weixin_context_token ?? null;
            let effectiveRecipientId = recipientPlatformUserId;
            if (platformStr === "weixin") {
              weixinPreflight = await this.runWeixinPreflight(
                options.deliveryKey,
                effectiveWeixinAttempt,
                i,
                weixinPreflightBudget!,
                signal,
                claim.weixin_account_id ?? null,
              );
              attachAccount(weixinPreflight.account_id);
              if (this.leaseLost) {
                throw new StaleGatewayEpochError(
                  "Gateway lease lost before Weixin text send",
                );
              }
              if (signal?.aborted) {
                throw new ChannelDeliveryError("Weixin text send aborted", {
                  outcome: "failed",
                  code: "unavailable",
                });
              }
              weixinContextToken = weixinPreflight.weixin_context_token;
              effectiveRecipientId = weixinPreflight.recipient_id;
            }

            const shouldCreateGroupTopic =
              isGroupTopicTarget &&
              isTopicScope &&
              !currentExternalTopicId &&
              !feishuTopicCreationFailed;
            const creatingDiscordThread =
              i === 0 &&
              platformStr === "discord" &&
              (options.newThread === true || shouldCreateGroupTopic) &&
              !reuseConfirmedThread &&
              !currentExternalTopicId;
            if (i === 0 && reuseConfirmedThread) {
              await this.authorizeDiscordBodyDispatch(
                options.deliveryKey,
                deliveryAttempt,
              );
            }
            const feishuReplyInThread =
              platformStr === "feishu" &&
              isGroupTopicTarget &&
              (Boolean(currentExternalTopicId) || shouldCreateGroupTopic);
            // Main-group direct fallback is ONLY permitted before any chunk has entered a
            // Feishu topic (no authorized `currentExternalTopicId` and no chunk confirmed
            // replied in thread yet). Once inside a topic, never leak chunks to the main group.
            const feishuAllowDirectFallback =
              platformStr === "feishu" && feishuReplyInThread
                ? !currentExternalTopicId && !feishuTopicConfirmedInThread
                : true;
            let chunkReplyToMessageId: string | null | undefined;
            let feishuFallbackReplyIds: string[] | undefined;
            if (feishuReplyInThread) {
              if (!currentExternalTopicId) {
                // First-turn topic creation: anchor chunk 0 on verified root message;
                // subsequent chunks anchor on already-confirmed in-thread reply messages
                // (never falling back to main group once chunk 0 entered the topic).
                if (i === 0) {
                  chunkReplyToMessageId = verifiedRootMessageId;
                } else {
                  const createAnchors: string[] = [];
                  for (const rawAnchor of [
                    feishuLastInThreadMessageId,
                    firstReplyMessageId,
                    verifiedRootMessageId,
                  ]) {
                    const cleanedAnchor = rawAnchor?.trim();
                    if (cleanedAnchor && !createAnchors.includes(cleanedAnchor)) {
                      createAnchors.push(cleanedAnchor);
                    }
                  }
                  chunkReplyToMessageId = createAnchors[0] ?? null;
                  if (createAnchors.length > 1) {
                    feishuFallbackReplyIds = createAnchors.slice(1);
                  }
                }
              } else {
                // Authorized topic (`:topic:` or `:thread:`): prefer current inbound message on live
                // turns; use registered message anchors for replay and background report-backs.
                const orderedAnchors =
                  !isReplayOrBackgroundOrigin && currentInboundAnchor
                    ? [currentInboundAnchor, firstReplyMessageId, verifiedRootMessageId]
                    : [firstReplyMessageId, verifiedRootMessageId, currentInboundAnchor];
                const dedupedAnchors: string[] = [];
                for (const rawAnchor of orderedAnchors) {
                  const cleanedAnchor = rawAnchor?.trim();
                  if (cleanedAnchor && !dedupedAnchors.includes(cleanedAnchor)) {
                    dedupedAnchors.push(cleanedAnchor);
                  }
                }
                chunkReplyToMessageId = dedupedAnchors[0] ?? null;
                if (dedupedAnchors.length > 1) {
                  feishuFallbackReplyIds = dedupedAnchors.slice(1);
                }
              }
            } else {
              chunkReplyToMessageId =
                i === 0
                  ? (shouldCreateGroupTopic
                      ? verifiedRootMessageId
                      : options.replyToMessageId)
                  : null;
            }
            return adapter.sendTextChunk({
              targetAddress: finalAddress,
              recipientPlatformUserId: effectiveRecipientId,
              text: chunkText,
              chunkIndex: i,
              totalChunks,
              replyToMessageId: chunkReplyToMessageId,
              fallbackReplyToMessageIds: feishuFallbackReplyIds,
              replyInThread: feishuReplyInThread,
              allowDirectFallback: feishuAllowDirectFallback,
              externalTopicId:
                platformStr === "discord" && isGroupTopicTarget
                  ? currentExternalTopicId
                  : null,
              webBaseUrl: this.webBaseUrl,
              threadId: options.threadId ?? null,
              newThread:
                i === 0
                  ? (Boolean(options.newThread) || shouldCreateGroupTopic) &&
                    !reuseConfirmedThread &&
                    !currentExternalTopicId
                  : false,
              threadScopeKey: effectiveThreadScopeKey,
              deliveryKey: options.deliveryKey,
              deliveryAttempt: effectiveWeixinAttempt,
              weixinPreflight,
              weixinAccountId:
                weixinPreflight?.account_id ?? claim.weixin_account_id ?? null,
              weixinContextToken,
              inboundMessageId: options.inboundMessageId ?? null,
              signal,
              confirmedThreadAddress:
                i === 0 && reuseConfirmedThread ? confirmedThreadAddress : null,
              onConfirmedThread: creatingDiscordThread
                ? async (confirmed) => {
                    const threadMatch = /:thread:([^:]+)$/.exec(
                      confirmed.address,
                    );
                    const confirmedTopicId = threadMatch?.[1]?.trim() || null;
                    const topicReg = confirmedTopicId
                      ? buildTopicRegistration(confirmedTopicId, null)
                      : null;
                    await this.storeConfirmedDiscordThread(
                      options.deliveryKey,
                      confirmed.address,
                      deliveryAttempt,
                      topicReg,
                    );
                    if (confirmedTopicId) {
                      currentExternalTopicId = confirmedTopicId;
                      recordAdapterTopicCache(confirmedTopicId);
                    }
                    finalAddress = confirmed.address;
                    await this.authorizeDiscordBodyDispatch(
                      options.deliveryKey,
                      deliveryAttempt,
                    );
                  }
                : undefined,
            });
          },
        );
        sentChunks += 1;
        platformMessageIds.push(res.messageId);
        if (i === 0 && !firstReplyMessageId) {
          firstReplyMessageId = res.messageId;
        }
        if (platformStr === "feishu" && res.repliedInThread === true) {
          feishuTopicConfirmedInThread = true;
          feishuLastInThreadMessageId = res.messageId;
        }
        if (res.resolvedAddress) {
          finalAddress = res.resolvedAddress;
        }
        let feishuTopicReg: BotTopicRegistrationPayload | null = null;
        let stopForUnconfirmedFeishuTopic = false;
        if (
          platformStr === "feishu" &&
          isGroupTopicTarget &&
          !currentExternalTopicId &&
          res.externalTopicId
        ) {
          currentExternalTopicId = res.externalTopicId;
          feishuTopicConfirmedInThread = true;
          feishuLastInThreadMessageId = res.messageId;
          feishuTopicReg = buildTopicRegistration(
            res.externalTopicId,
            firstReplyMessageId ?? res.messageId,
          );
          recordAdapterTopicCache(res.externalTopicId);
        } else if (
          platformStr === "feishu" &&
          isGroupTopicTarget &&
          isTopicScope &&
          !currentExternalTopicId &&
          !res.externalTopicId &&
          i === 0
        ) {
          if (res.usedDirectFallback === true) {
            // Platform explicitly rejected topic creation and fell back to main group.
            feishuTopicCreationFailed = true;
          } else if (res.repliedInThread === true) {
            // Chunk 0 succeeded inside the topic but topic ID is not yet available;
            // keep subsequent chunks inside the topic using the confirmed reply anchor
            // and persist `first_reply_message_id` on the pending topic row so single-chunk
            // replies can still confirm the topic on the next inbound message.
            feishuTopicConfirmedInThread = true;
            feishuLastInThreadMessageId = res.messageId;
            if (res.messageId) {
              feishuTopicReg = buildTopicRegistration(null, res.messageId);
            }
          } else {
            // Cannot confirm whether chunk 0 landed in the topic or main group;
            // stop sending and mark `unknown` instead of risking a main-group leak.
            stopForUnconfirmedFeishuTopic = true;
          }
        } else if (
          platformStr === "feishu" &&
          isGroupTopicTarget &&
          currentExternalTopicId &&
          !claim.first_reply_message_id &&
          i === 0 &&
          res.messageId
        ) {
          feishuTopicReg = buildTopicRegistration(
            currentExternalTopicId,
            res.messageId,
          );
        }
        if (res.terminalFallbackNotice === true) {
          terminalNoticeCompleted = true;
          totalChunks = sentChunks;
        }
        const chunkConfirmedLinks = chunkLinkOutcomes[i] ?? [];
        if (chunkConfirmedLinks.length > 0) {
          fileOutcomes.push(...chunkConfirmedLinks);
        }
        maybeNotifyFirstCurrentDelivery();
        await recordProgress({
          delivery_key: options.deliveryKey,
          sent_chunks: sentChunks,
          total_chunks: totalChunks,
          platform_message_ids: [...platformMessageIds],
          ...(chunkConfirmedLinks.length > 0
            ? { files_status: [...fileOutcomes] }
            : {}),
          ...(feishuTopicReg ? { bot_topic_registration: feishuTopicReg } : {}),
        });
        if (terminalNoticeCompleted) {
          break;
        }
        if (stopForUnconfirmedFeishuTopic) {
          chunkFailure = {
            outcome: "unknown",
            forceUnknown: true,
            code: "unavailable",
            message:
              "Feishu topic location could not be confirmed; stopping delivery to avoid leaking into main group.",
          };
          break;
        }
      } catch (err) {
        if (isStaleGatewayEpochError(err) || this.leaseLost) {
          // D1 / §3.5 / R5 #3: lease loss MUST stop locally without writing I-7 settlement.
          throw isStaleGatewayEpochError(err)
            ? err
            : new StaleGatewayEpochError(
                "Gateway lease lost during chunk delivery",
                { cause: err },
              );
        }
        const feishuUnconfirmedTopicStop =
          platformStr === "feishu" &&
          feishuTopicConfirmedInThread &&
          !currentExternalTopicId;
        if (err instanceof ChannelDeliveryError) {
          chunkFailure = {
            outcome:
              feishuUnconfirmedTopicStop || err.outcome === "unknown"
                ? "unknown"
                : "failed",
            ...(feishuUnconfirmedTopicStop ? { forceUnknown: true } : {}),
            code: err.code,
            denyCode: err.denyCode,
            errorDetail: err.errorDetail,
            retryable: err.retryable,
            message: err.message,
          };
        } else {
          chunkFailure = {
            outcome: "unknown",
            ...(feishuUnconfirmedTopicStop ? { forceUnknown: true } : {}),
            code: "unavailable",
            message: err instanceof Error ? err.message : String(err),
          };
        }
        for (let j = i; j < chunkLinkOutcomes.length; j += 1) {
          const unsentStatus: AgentSendFileOutcome["status"] =
            j === i && chunkFailure.outcome === "unknown" ? "unknown" : "failed";
          for (const unsent of chunkLinkOutcomes[j] ?? []) {
            fileOutcomes.push({
              ...unsent,
              status: unsentStatus,
              reason: chunkFailure.message,
            });
          }
        }
        break;
      }
    }

    // Process files if text chunks did not fail before any progress
    let totalDownloadedBytes = 0;
    let anyFileSucceeded = fileOutcomes.some((f) => f.status === "linked" || f.status === "sent");
    let anyFileFailed = false;
    let fileUnknownError: string | null = null;
    let fileFailureCode: AgentSendResponse["code"] = null;
    let fileFailureDenyCode: string | undefined;
    let fileFailureErrorDetail: string | undefined;

    const verifyDmRecipientNow = async (): Promise<void> => {
      if (
        !isDmTarget ||
        (platformStr !== "feishu" && platformStr !== "discord") ||
        typeof this.backend.verifyDmDeliveryRecipient !== "function"
      ) {
        return;
      }
      let verified: VerifyDmRecipientResponse;
      try {
        verified = await this.backend.verifyDmDeliveryRecipient(
          options.deliveryKey,
          deliveryAttempt,
        );
      } catch (err) {
        if (isStaleGatewayEpochError(err) || this.leaseLost) {
          throw isStaleGatewayEpochError(err)
            ? err
            : new StaleGatewayEpochError(
                "Gateway lease lost during DM recipient verification",
                { cause: err },
              );
        }
        if (err instanceof BackendHttpError) {
          const bodyObj =
            err.body && typeof err.body === "object"
              ? (err.body as Record<string, unknown>)
              : null;
          const detailObj =
            bodyObj?.detail && typeof bodyObj.detail === "object"
              ? (bodyObj.detail as Record<string, unknown>)
              : null;
          const errCode =
            typeof bodyObj?.code === "string"
              ? bodyObj.code
              : typeof detailObj?.code === "string"
                ? detailObj.code
                : undefined;
          const errMsg =
            typeof bodyObj?.message === "string"
              ? bodyObj.message
              : typeof detailObj?.message === "string"
                ? detailObj.message
                : err.message;
          if (err.status === 503) {
            throw new ChannelDeliveryError(
              errMsg || "DM recipient verification temporarily unavailable.",
              {
                outcome: "failed",
                code: "unavailable",
                denyCode: errCode ?? "roster_lookup_error",
                retryable: true,
              },
            );
          }
          if (err.status === 403 || err.status === 404 || err.status === 409) {
            throw new ChannelDeliveryError(
              errMsg || "DM recipient binding changed or was revoked.",
              {
                outcome: "failed",
                code: "not_allowed",
                denyCode: errCode ?? "dm_recipient_revoked",
              },
            );
          }
        }
        throw err;
      }
      if (verified.authorized !== true) {
        throw new ChannelDeliveryError(
          verified.message ?? "DM recipient binding changed or was revoked.",
          {
            outcome: "failed",
            code: "not_allowed",
            denyCode:
              verified.deny_code ?? verified.code ?? "dm_recipient_revoked",
          },
        );
      }
    };

    const sendDeliverableLinkFallback = async (
      fileRef: AgentSendFileRef,
      wsId: string,
      reason: string,
      fallbackChunkIdx: number,
    ): Promise<boolean> => {
      const linkLine = buildWorkspaceFileDeepLinkLine(
        basename(fileRef.path) || fileRef.path,
        fileRef.path,
        this.webBaseUrl,
        options.threadId,
        wsId || null,
        currentSessionWsId,
      );
      try {
        await verifyDmRecipientNow();
        const linkRes = await runWithWeixinSignal(
          claim.weixin_account_id ?? null,
          async ({ signal, attachAccount }) => {
            let weixinPreflight: WeixinPreflightAuthorizedData | null = null;
            let weixinContextToken: string | null =
              claim.weixin_context_token ?? null;
            let effectiveRecipientId = recipientPlatformUserId;
            if (platformStr === "weixin") {
              weixinPreflight = await this.runWeixinPreflight(
                options.deliveryKey,
                effectiveWeixinAttempt,
                fallbackChunkIdx,
                weixinPreflightBudget!,
                signal,
                claim.weixin_account_id ?? null,
              );
              attachAccount(weixinPreflight.account_id);
              weixinContextToken = weixinPreflight.weixin_context_token;
              effectiveRecipientId = weixinPreflight.recipient_id;
            }
            return adapter.sendTextChunk({
              targetAddress: finalAddress,
              recipientPlatformUserId: effectiveRecipientId,
              text: linkLine,
              chunkIndex: fallbackChunkIdx,
              totalChunks: Math.max(totalChunks, 1),
              threadScopeKey: effectiveThreadScopeKey,
              deliveryKey: options.deliveryKey,
              deliveryAttempt: effectiveWeixinAttempt,
              weixinPreflight,
              weixinAccountId:
                weixinPreflight?.account_id ?? claim.weixin_account_id ?? null,
              weixinContextToken,
              inboundMessageId: options.inboundMessageId ?? null,
              signal,
            });
          },
        );
        platformMessageIds.push(linkRes.messageId);
        anyFileSucceeded = true;
        fileOutcomes.push({
          path: fileRef.path,
          workspace_id: wsId || null,
          status: "linked",
          reason,
        });
        await recordProgress({
          delivery_key: options.deliveryKey,
          sent_chunks: sentChunks,
          total_chunks: totalChunks,
          platform_message_ids: [...platformMessageIds],
          files_status: [...fileOutcomes],
        });
        return true;
      } catch (err) {
        if (isStaleGatewayEpochError(err) || this.leaseLost) {
          throw isStaleGatewayEpochError(err)
            ? err
            : new StaleGatewayEpochError(
                "Gateway lease lost during deliverable link fallback",
                { cause: err },
              );
        }
        return false;
      }
    };

    if (!chunkFailure && !terminalNoticeCompleted) {
      for (let fileIdx = 0; fileIdx < filesToSend.length; fileIdx += 1) {
        const fileRef = filesToSend[fileIdx]!;
        const wsId =
          fileRef.workspace_id || currentSessionWsId || "";
        if (!wsId) {
          anyFileFailed = true;
          fileOutcomes.push({
            path: fileRef.path,
            workspace_id: null,
            status: "failed",
            reason: "Missing workspace_id for file attachment",
          });
          continue;
        }
        let downloaded: { content: Buffer; mimeType: string };
        try {
          if (
            options.downloadMode === "deliverable" &&
            typeof this.backend.downloadVerifiedDeliverable === "function"
          ) {
            const verifiedDl = await this.backend.downloadVerifiedDeliverable({
              deliveryKey: options.deliveryKey,
              deliveryAttempt,
              workspaceId: wsId,
              path: fileRef.path,
            });
            downloaded = {
              content: verifiedDl.content,
              mimeType: verifiedDl.mimeType,
            };
          } else {
            // Always pass Dalton user UUID (`daltonUserId`) as `X-User-Id` (`PROTOCOL.md` §8)
            downloaded = await this.backend.downloadWorkspaceFile(
              daltonUserId,
              wsId,
              fileRef.path,
            );
          }
        } catch (err) {
          if (isStaleGatewayEpochError(err) || this.leaseLost) {
            throw isStaleGatewayEpochError(err)
              ? err
              : new StaleGatewayEpochError(
                  "Gateway lease lost during deliverable download",
                  { cause: err },
                );
          }
          const dlBody =
            err instanceof BackendHttpError && err.body && typeof err.body === "object"
              ? (err.body as Record<string, unknown>)
              : null;
          const dlDetail =
            dlBody?.detail && typeof dlBody.detail === "object"
              ? (dlBody.detail as Record<string, unknown>)
              : null;
          const dlErrCode =
            typeof dlBody?.code === "string"
              ? dlBody.code
              : typeof dlDetail?.code === "string"
                ? dlDetail.code
                : undefined;
          const isVersionOrMissing =
            err instanceof BackendHttpError &&
            (err.status === 409 || err.status === 404);
          const isSandboxUnavailable =
            err instanceof BackendHttpError &&
            err.status === 503 &&
            dlErrCode === "deliverable_sandbox_unavailable";
          const isSizeExceeded =
            err instanceof Error && err.name === "SafeFetchError";
          if (
            options.linkFallbackOnDeliverableMismatch &&
            (isVersionOrMissing || isSandboxUnavailable || isSizeExceeded)
          ) {
            const linked = await sendDeliverableLinkFallback(
              fileRef,
              wsId,
              isSizeExceeded
                ? "deliverable_size_exceeded_link_fallback"
                : isSandboxUnavailable
                  ? "deliverable_sandbox_unavailable_link_fallback"
                  : "deliverable_version_mismatch_link_fallback",
              chunks.length + fileIdx,
            );
            if (linked) {
              continue;
            }
          }
          anyFileFailed = true;
          if (!fileFailureCode && err instanceof BackendHttpError && err.status === 403) {
            fileFailureCode = "not_allowed";
          }
          fileOutcomes.push({
            path: fileRef.path,
            workspace_id: wsId,
            status: "failed",
            reason: err instanceof Error ? err.message : "Failed to download file from workspace",
          });
          continue;
        }

        const byteLen = downloaded.content.byteLength;
        if (byteLen > MAX_ATTACHMENT_BYTES) {
          if (options.linkFallbackOnDeliverableMismatch) {
            const linked = await sendDeliverableLinkFallback(
              fileRef,
              wsId,
              "deliverable_size_exceeded_link_fallback",
              chunks.length + fileIdx,
            );
            if (linked) continue;
          }
          anyFileFailed = true;
          fileOutcomes.push({
            path: fileRef.path,
            workspace_id: wsId,
            status: "failed",
            reason: "File exceeds 20 MB limit",
          });
          continue;
        }
        if (totalDownloadedBytes + byteLen > MAX_TOTAL_ATTACHMENT_BYTES) {
          if (options.linkFallbackOnDeliverableMismatch) {
            const linked = await sendDeliverableLinkFallback(
              fileRef,
              wsId,
              "deliverable_total_size_exceeded_link_fallback",
              chunks.length + fileIdx,
            );
            if (linked) continue;
          }
          anyFileFailed = true;
          fileOutcomes.push({
            path: fileRef.path,
            workspace_id: wsId,
            status: "failed",
            reason: "Total attachments exceed 50 MB limit",
          });
          continue;
        }
        totalDownloadedBytes += byteLen;

        try {
          // Lead Decision #6: Re-verify pinned DM recipient before uploading/sending file.
          await verifyDmRecipientNow();

          // Lead Decision #7 / Codex r6 #6: Persist `pending` entry BEFORE platform send
          // so a crash after platform receipt leaves `pending` (link-only on recovery, never duplicate upload).
          if (isDmTarget) {
            await recordProgress({
              delivery_key: options.deliveryKey,
              sent_chunks: sentChunks,
              total_chunks: totalChunks,
              platform_message_ids: [...platformMessageIds],
              files_status: [
                ...fileOutcomes,
                {
                  path: fileRef.path,
                  workspace_id: wsId,
                  status: "pending",
                  reason: null,
                },
              ],
            });
          }

          const fileChunkIndex = chunks.length + fileIdx;
          const sendRes = await runWithWeixinSignal(
            claim.weixin_account_id ?? null,
            async ({ signal, attachAccount }) => {
              let weixinPreflight: WeixinPreflightAuthorizedData | null = null;
              let weixinContextToken: string | null =
                claim.weixin_context_token ?? null;
              let effectiveRecipientId = recipientPlatformUserId;
              if (platformStr === "weixin") {
                weixinPreflight = await this.runWeixinPreflight(
                  options.deliveryKey,
                  effectiveWeixinAttempt,
                  fileChunkIndex,
                  weixinPreflightBudget!,
                  signal,
                  claim.weixin_account_id ?? null,
                );
                attachAccount(weixinPreflight.account_id);
                if (this.leaseLost) {
                  throw new StaleGatewayEpochError(
                    "Gateway lease lost before Weixin file send",
                  );
                }
                if (signal?.aborted) {
                  throw new ChannelDeliveryError("Weixin file send aborted", {
                    outcome: "failed",
                    code: "unavailable",
                  });
                }
                weixinContextToken = weixinPreflight.weixin_context_token;
                effectiveRecipientId = weixinPreflight.recipient_id;
              }

              return adapter.sendFile({
                targetAddress: finalAddress,
                recipientPlatformUserId: effectiveRecipientId,
                path: fileRef.path,
                filename: basename(fileRef.path) || "attachment",
                content: downloaded.content,
                mimeType: downloaded.mimeType,
                threadScopeKey: effectiveThreadScopeKey,
                deliveryKey: options.deliveryKey,
                deliveryAttempt: effectiveWeixinAttempt,
                verifyRecipientBeforeSend:
                  isDmTarget &&
                  (platformStr === "feishu" || platformStr === "discord")
                    ? () => verifyDmRecipientNow()
                    : undefined,
                weixinPreflight,
                weixinAccountId:
                  weixinPreflight?.account_id ?? claim.weixin_account_id ?? null,
                weixinContextToken,
                inboundMessageId: options.inboundMessageId ?? null,
                signal,
                refreshWeixinPreflight:
                  platformStr === "weixin"
                    ? () =>
                        this.runWeixinPreflight(
                          options.deliveryKey,
                          effectiveWeixinAttempt,
                          fileChunkIndex,
                          weixinPreflightBudget!,
                          signal,
                          weixinPreflight?.account_id ??
                            claim.weixin_account_id ??
                            null,
                        )
                    : undefined,
              });
            },
          );
          const fileSucceeded =
            sendRes.status === "sent" || sendRes.status === "linked";
          if (fileSucceeded) {
            platformMessageIds.push(sendRes.messageId);
            anyFileSucceeded = true;
            maybeNotifyFirstCurrentDelivery();
          } else {
            anyFileFailed = true;
          }
          fileOutcomes.push({
            path: fileRef.path,
            workspace_id: wsId,
            status: sendRes.status,
            reason: sendRes.reason ?? null,
          });
          if (fileSucceeded) {
            await recordProgress({
              delivery_key: options.deliveryKey,
              sent_chunks: sentChunks,
              total_chunks: totalChunks,
              platform_message_ids: [...platformMessageIds],
              files_status: [...fileOutcomes],
            });
          }
        } catch (err) {
          if (isStaleGatewayEpochError(err) || this.leaseLost) {
            throw isStaleGatewayEpochError(err)
              ? err
              : new StaleGatewayEpochError(
                  "Gateway lease lost during file delivery",
                  { cause: err },
                );
          }
          anyFileFailed = true;
          let fileEntryStatus: AgentSendFileOutcome["status"] = "failed";
          if (err instanceof ChannelDeliveryError) {
            if (err.outcome === "unknown") {
              fileUnknownError = err.message;
              fileEntryStatus = "unknown";
            }
            if (!fileFailureCode) {
              fileFailureCode = err.code;
            }
            if (!fileFailureDenyCode && err.denyCode) {
              fileFailureDenyCode = err.denyCode;
            }
            if (!fileFailureErrorDetail && err.errorDetail) {
              fileFailureErrorDetail = err.errorDetail;
            }
          } else {
            fileUnknownError =
              err instanceof Error ? err.message : String(err);
            fileEntryStatus = "unknown";
          }
          fileOutcomes.push({
            path: fileRef.path,
            workspace_id: wsId,
            status: fileEntryStatus,
            reason: err instanceof Error ? err.message : String(err),
          });
        }
      }
    } else if (filesToSend.length > 0 && !terminalNoticeCompleted) {
      for (const fileRef of filesToSend) {
        anyFileFailed = true;
        fileOutcomes.push({
          path: fileRef.path,
          workspace_id: fileRef.workspace_id || null,
          status: "failed",
          reason: "Skipped because earlier message chunk failed",
        });
      }
    }

    const somethingSucceeded = sentChunks > 0 || anyFileSucceeded;
    let finalStatus: AgentSendResponse["status"];
    let finalCode: AgentSendResponse["code"] = null;
    let finalMessage: string;

    if (chunkFailure) {
      if (chunkFailure.outcome === "unknown") {
        // Round 3 Decision #9: `unknown` overrides `partial` on Weixin or when explicitly forced
        // (e.g. unconfirmed Feishu topic location where stopping avoids a main-group leak);
        // Feishu & Discord otherwise preserve `partial` settlement when earlier chunks succeeded.
        if (chunkFailure.forceUnknown || platformStr === "weixin" || !somethingSucceeded) {
          finalStatus = "unknown";
          finalCode = chunkFailure.code;
          finalMessage = somethingSucceeded
            ? `Delivered ${sentChunks}/${totalChunks} text chunks before uncertain outcome: ${chunkFailure.message}`
            : `Delivery outcome could not be confirmed: ${chunkFailure.message}`;
        } else {
          finalStatus = "partial";
          finalCode = chunkFailure.code;
          finalMessage = `Delivered ${sentChunks}/${totalChunks} text chunks before error: ${chunkFailure.message}`;
        }
      } else if (somethingSucceeded) {
        finalStatus = "partial";
        finalCode = chunkFailure.code;
        finalMessage = `Delivered ${sentChunks}/${totalChunks} text chunks before error: ${chunkFailure.message}`;
      } else {
        finalStatus = "failed";
        finalCode = chunkFailure.code;
        finalMessage = chunkFailure.message;
      }
    } else if (anyFileFailed) {
      if (fileUnknownError) {
        if (platformStr === "weixin" || !somethingSucceeded) {
          finalStatus = "unknown";
          finalCode = "unavailable";
          finalMessage = `File delivery outcome could not be confirmed: ${fileUnknownError}`;
        } else {
          finalStatus = "partial";
          finalCode = fileFailureCode ?? "unavailable";
          finalMessage = "Message delivered, but one or more attached files failed.";
        }
      } else if (somethingSucceeded) {
        finalStatus = "partial";
        finalCode = fileFailureCode;
        finalMessage = "Message delivered, but one or more attached files failed.";
      } else {
        finalStatus = "failed";
        finalCode = fileFailureCode;
        finalMessage = "All attached files failed to deliver.";
      }
    } else {
      finalStatus = "sent";
      finalCode = null;
      finalMessage = `Delivered to ${finalAddress}.`;
    }

    const responseObj: AgentSendResponse = {
      status: finalStatus,
      code: finalCode,
      address: finalAddress,
      current: isCurrent,
      duplicate: false,
      message: finalMessage,
      files: fileOutcomes.map(({ path, status, reason }) => ({
        path,
        status,
        reason,
      })),
    };

    const recordedErrorCode =
      platformStr === "weixin"
        ? (chunkFailure?.denyCode ?? fileFailureDenyCode ?? finalCode)
        : finalCode;
    const recordedErrorDetail =
      platformStr === "weixin"
        ? (chunkFailure?.errorDetail ?? fileFailureErrorDetail)
        : undefined;

    await recordProgress({
      delivery_key: options.deliveryKey,
      sent_chunks: sentChunks,
      total_chunks: totalChunks,
      platform_message_ids: platformMessageIds,
      files_status: fileOutcomes,
      first_response_json: responseObj as unknown as Record<string, unknown>,
      status: finalStatus,
      error_code: recordedErrorCode,
      ...(recordedErrorDetail ? { error_detail: recordedErrorDetail } : {}),
      error_message: finalStatus === "sent" ? null : finalMessage,
    });

    return responseObj;
  }

  private async claimDeliveryWith503Retry(
    req: I6AuthorizeAndClaimRequest,
  ): Promise<I6AuthorizeAndClaimResponse> {
    // R5 #5 (Codex Medium #7): In-delivery I-6 503 retry applies ONLY to Weixin;
    // Feishu and Discord keep original fail-immediately behavior on 503.
    const isWeixinTarget =
      req.platform === "weixin" ||
      (typeof req.target === "string" &&
        req.target.trim().toLowerCase().startsWith("weixin:"));
    const maxAttempts = isWeixinTarget ? this.claim503MaxAttempts : 1;
    const signal = this.retryAbortController.signal;
    let delayMs = DELIVERY_CLAIM_503_INITIAL_DELAY_MS;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      if (this.leaseLost) {
        throw new StaleGatewayEpochError(
          "Gateway lease lost before I-6 authorize-and-claim",
        );
      }
      if (this.startupBlocked || signal.aborted) {
        throw new BackendHttpError(
          "Delivery claim aborted due to lease loss or shutdown",
          503,
          { code: "gateway_stopping" },
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
                "Gateway lease lost during I-6 authorize-and-claim",
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
        this.logger.warn("I-6 authorize-and-claim returned 503; retrying in delivery", {
          delivery_key: req.delivery_key,
          platform: req.platform ?? null,
          attempt,
          delay_ms: delayMs,
          ...extractSafeLogErrorFields(err),
        });
        const slept = await this.retryDelayFn(delayMs, signal);
        if (this.leaseLost) {
          throw new StaleGatewayEpochError(
            "Gateway lease lost while retrying I-6 authorize-and-claim",
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
    accountId?: string | null,
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
          "Gateway lease lost during Weixin preflight",
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
                "Gateway lease lost during Weixin preflight",
                { cause: err },
              );
        }
        throwIfAbortedOrLeaseLost();
        const isRetryable503 =
          err instanceof BackendHttpError && err.status === 503;
        const isRetryableTimeout = isPreflightTimeoutError(err);
        if (isRetryable503) {
          budget.lastRetryableDenyCode = "roster_lookup_error";
          budget.lastRetryableMessage =
            "Weixin preflight remained temporarily unavailable (503).";
        } else if (isRetryableTimeout) {
          budget.lastRetryableDenyCode =
            budget.lastRetryableDenyCode ?? "preflight_timeout";
          budget.lastRetryableMessage =
            budget.lastRetryableMessage ??
            "Weixin send preflight timed out before completing.";
        }
        if (attemptSignal.aborted || getRemainingBudgetMs() <= 0) {
          throw buildDeadlineExceededError(
            "Weixin send preflight exceeded shared delivery deadline.",
          );
        }
        if (!isRetryable503 && !isRetryableTimeout) {
          throw err;
        }
        const remainingMs = getRemainingBudgetMs();
        if (remainingMs <= 0) {
          throw buildDeadlineExceededError(
            budget.lastRetryableMessage ??
              "Weixin preflight remained temporarily unavailable.",
          );
        }
        const waitMs = Math.min(delayMs, remainingMs);
        this.logger.warn(
          isRetryable503
            ? "Weixin preflight returned 503; retrying in delivery"
            : "Weixin preflight single request timed out; retrying in delivery",
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
      // R6 #3 (Codex Low #7): Record retryable deny reason BEFORE checking whether the shared
      // deadline expired, so a retryable denial arriving right at/after the deadline still
      // reports its root cause (while a late `authorized: true` is still rejected).
      if (!pf.authorized && (pf.retryable === true || pf.decision === "defer")) {
        budget.lastRetryableDenyCode = pf.deny_code;
        budget.lastRetryableMessage =
          pf.message ??
          `Weixin send preflight remained unavailable (${pf.deny_code}).`;
      } else if (!pf.authorized && WEIXIN_ROTATION_DENY_CODES.has(pf.deny_code)) {
        this.onWeixinAccountRotated?.({
          accountId: accountId ?? null,
          reason: pf.deny_code,
        });
      }

      // R5 #2 (Codex High #4): Do NOT adopt an authorization returned after the shared deadline.
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
        const remainingMs = getRemainingBudgetMs();
        if (remainingMs <= 0) {
          throw buildDeadlineExceededError(
            budget.lastRetryableMessage ??
              `Weixin send preflight remained unavailable (${pf.deny_code}).`,
          );
        }
        const waitMs = Math.min(delayMs, remainingMs);
        this.logger.warn(
          "Weixin preflight returned retryable denial; retrying in delivery",
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

  /**
   * Store a Discord thread that create or adoption already confirmed.
   * The same progress transaction records `before_body` when no body phase
   * exists yet. A missing acknowledgment does not send. The caller writes one
   * terminal status for this claim generation. A later claim can resume only
   * when that stored phase is still `before_body`.
   */
  private async storeConfirmedDiscordThread(
    deliveryKey: string,
    address: string,
    deliveryAttempt?: number,
    botTopicRegistration?: BotTopicRegistrationPayload | null,
  ): Promise<void> {
    let stored = false;
    try {
      const ack = await this.backend.updateDeliveryProgress(
        {
          delivery_key: deliveryKey,
          confirmed_thread_address: address,
          ...(typeof deliveryAttempt === "number"
            ? { delivery_attempt: deliveryAttempt }
            : {}),
          ...(botTopicRegistration
            ? { bot_topic_registration: botTopicRegistration }
            : {}),
        },
        { platform: "discord" },
      );
      const phase = ack.discord_body_phase;
      stored =
        ack.ok === true &&
        ack.confirmed_thread_address === address &&
        (phase === "before_body" || phase === "dispatching");
    } catch {
      stored = false;
    }
    if (stored) return;
    throw new ChannelDeliveryError("Discord confirmed thread was not stored", {
      outcome: "unknown",
      code: "unavailable",
    });
  }

  /**
   * Authorize one Discord body POST.
   * The POST runs only after this acknowledgment echoes `dispatching`.
   * A lost acknowledgment does not send and does not write a second status.
   * The caller writes one terminal status for this claim generation.
   * `unknown` plus `dispatching` is not claimed again, including after startup.
   */
  private async authorizeDiscordBodyDispatch(
    deliveryKey: string,
    deliveryAttempt?: number,
  ): Promise<void> {
    let authorized = false;
    try {
      const ack = await this.backend.updateDeliveryProgress(
        {
          delivery_key: deliveryKey,
          discord_body_phase: "dispatching",
          ...(typeof deliveryAttempt === "number"
            ? { delivery_attempt: deliveryAttempt }
            : {}),
        },
        { platform: "discord" },
      );
      authorized = ack.ok === true && ack.discord_body_phase === "dispatching";
    } catch {
      authorized = false;
    }
    if (authorized) return;
    throw new ChannelDeliveryError("Discord body dispatch was not acknowledged", {
      outcome: "unknown",
      code: "unavailable",
    });
  }

  private inferPlatform(
    explicitPlatform?: string | null,
    target?: string | null,
  ): ChannelPlatform | null {
    // R4 #1: Target address platform ALWAYS takes precedence over turn/origin platform.
    if (target) {
      const [prefix] = target.trim().split(":", 1);
      if (prefix && isSupportedPlatform(prefix.toLowerCase())) {
        return prefix.toLowerCase() as ChannelPlatform;
      }
    }
    if (explicitPlatform && isSupportedPlatform(explicitPlatform.toLowerCase())) {
      return explicitPlatform.toLowerCase() as ChannelPlatform;
    }
    return null;
  }
}
