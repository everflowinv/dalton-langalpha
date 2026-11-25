// Portions adapted from @tencent-weixin/openclaw-weixin (MIT License, Copyright (c) 2026 Tencent).

import {
  type BackendClient,
  BackendHttpError,
  isValidWeixinAccountsReconcileResponse,
  type WeixinAccountSnapshot,
  type WeixinPreflightAuthorizedData,
} from "../../backend-client.js";
import {
  type GatewayLeaseManager,
  isLeaseSafetyMarginPauseError,
  isStaleGatewayEpochError,
  StaleGatewayEpochError,
} from "../../gateway-lease.js";
import {
  createSubsystemLogger,
  extractSafeLogErrorFields,
  type SubsystemLogger,
} from "../../shared/logger.js";
import type { HostIpResolver } from "../../shared/safe-fetch.js";
import type {
  InboundAttachmentDescriptor,
  InboundChannelEvent,
  OneTimeWeixinReplyEnvelope,
} from "../../types.js";
import {
  type AvailableChatsResult,
  type ChannelAdapter,
  ChannelDeliveryError,
  type CheckTargetLiveResult,
  type DownloadedAttachment,
  type ProcessingFeedbackContext,
  type ProcessingFeedbackHandle,
  type ProcessingFeedbackOutcome,
  type SendFileOptions,
  type SendFileResult,
  type SendTextChunkOptions,
  type SendTextChunkResult,
  splitTextByLimit,
  type WeixinQrStartResult,
  type WeixinQrWaitResult,
} from "../adapter.js";
import {
  getWeixinConfig,
  normalizeAndValidateIlinkBaseUrl,
  resolveValidatedRedirectBaseUrl,
  sendWeixinTyping,
  WeixinApiError,
} from "./api.js";
import { downloadWeixinCdnAttachment } from "./cdn.js";
import {
  WeixinHttpPoolManager,
  type WeixinHttpAgentFactory,
} from "./http-pool.js";
import { resolvePersistedAttachmentMeta } from "./inbound.js";
import {
  type ConfirmedWeixinAccount,
  WeixinQrLoginManager,
} from "./login-qr.js";
import { sleepWithAbort, WeixinMonitor } from "./monitor.js";
import {
  normalizeRecipientUserId,
  sendWeixinMediaAttachment,
  sendWeixinTextMessage,
} from "./send.js";
import {
  ILINK_DEFAULT_BASE_URL,
  ILINK_DEFAULT_BOT_TYPE,
  ILINK_DEFAULT_CDN_BASE_URL,
  TypingStatus,
} from "./types.js";

export * from "./types.js";
export * from "./api.js";
export * from "./cdn.js";
export * from "./http-pool.js";
export * from "./inbound.js";
export * from "./send.js";
export * from "./markdown-filter.js";
export * from "./login-qr.js";
export * from "./monitor.js";

export const WEIXIN_MAX_CHUNK_CHARS = 2_000;
export const WEIXIN_TYPING_INTERVAL_MS = 10_000;
export const WEIXIN_TYPING_MAX_DURATION_MS = 120_000;
export const WEIXIN_TYPING_TICKET_TTL_MS = 24 * 60 * 60 * 1_000;
export const WEIXIN_DEFAULT_RECONCILE_INTERVAL_MS = 15_000;
export const WEIXIN_DEFAULT_STARTUP_SNAPSHOT_RETRY_MS = 1_000;
export const WEIXIN_LAYER2_PROBE_INTERVAL_MS = 5 * 60_000;
export const WEIXIN_DEFAULT_STAGGER_DELAY_MS = 100; // <= 10 accounts/sec
export const WEIXIN_DEFAULT_STAGGER_JITTER_MS = 500;
export const WEIXIN_DEFAULT_RATE_LIMIT_WINDOW_MS = 10_000;
export const WEIXIN_DEFAULT_RATE_LIMIT_THRESHOLD = 3;
export const WEIXIN_DEFAULT_RATE_LIMIT_COOLDOWN_MS = 20_000;

/**
 * Allowlist of iLink business error codes confirmed to mean `context_token`
 * expired / invalid (§5.2, Decision #3). Intentionally empty until verified
 * against live iLink traffic (§7.4 #6); unknown errors only fail the current
 * delivery without poisoning valid tokens.
 */
export const CONTEXT_TOKEN_STALE_ERRCODES: readonly number[] = [];
export const WEIXIN_STALE_CONTEXT_TOKEN_ERRCODES = CONTEXT_TOKEN_STALE_ERRCODES;

const MAX_CACHED_TYPING_TICKETS = 2_000;
const MAX_ONE_TIME_REPLY_KEYS = 2_000;

export interface WeixinChannelAdapterOptions {
  backend?: BackendClient;
  leaseManager?: GatewayLeaseManager;
  baseUrl?: string;
  cdnBaseUrl?: string;
  botType?: string;
  /** Optional standalone/test single-account seed when no backend reconcile is used. */
  accountId?: string;
  botToken?: string;
  ilinkBotId?: string;
  botGeneration?: number;
  pollTimeoutMs?: number;
  retryDelayMs?: number;
  reconcileIntervalMs?: number;
  startupSnapshotRetryMs?: number;
  probeInterval404Ms?: number;
  staggerDelayMs?: number;
  staggerJitterMs?: number;
  rateLimitWindowMs?: number;
  rateLimitAccountThreshold?: number;
  rateLimitCooldownMs?: number;
  staleContextTokenErrorCodes?: readonly number[];
  allowedApiDomains?: readonly string[];
  allowedCdnDomains?: readonly string[];
  resolveHostIps?: HostIpResolver;
  checkResolvedIps?: boolean;
  fetchImpl?: typeof fetch;
  /** Optional custom undici Agent factory for isolated poll/control pools (§3.3). */
  httpAgentFactory?: WeixinHttpAgentFactory;
  logger?: SubsystemLogger;
  typingIntervalMs?: number;
  typingMaxDurationMs?: number;
  typingTicketTtlMs?: number;
  nowFn?: () => number;
}

export interface ManagedWeixinAccount {
  accountId: string;
  userId: string;
  ilinkBotId: string;
  ilinkUserId: string | null;
  botToken: string;
  baseUrl: string;
  cdnBaseUrl: string;
  botGeneration: number;
  getUpdatesBuf: string;
  status: string;
  monitorController: AbortController | null;
  monitorTask: Promise<void> | null;
}

function isPollableAccountStatus(status: string): boolean {
  return status === "active" || status === "pending_verification";
}

export class WeixinChannelAdapter implements ChannelAdapter {
  readonly platform = "weixin" as const;
  readonly maxChunkChars = WEIXIN_MAX_CHUNK_CHARS;

  private readonly opts: WeixinChannelAdapterOptions;
  private readonly logger: SubsystemLogger;
  private readonly typingIntervalMs: number;
  private readonly typingMaxDurationMs: number;
  private readonly typingTicketTtlMs: number;
  private readonly reconcileIntervalMs: number;
  private readonly startupSnapshotRetryMs: number;
  private readonly probeInterval404Ms: number;
  private readonly staggerDelayMs: number;
  private readonly staggerJitterMs: number;
  private readonly rateLimitWindowMs: number;
  private readonly rateLimitAccountThreshold: number;
  private readonly rateLimitCooldownMs: number;
  private readonly staleContextTokenCodes: ReadonlySet<number>;
  private readonly nowFn: () => number;

  private readonly accounts = new Map<string, ManagedWeixinAccount>();
  private readonly typingTicketCache = new Map<
    string,
    { ticket: string; cachedAtMs: number }
  >();
  private readonly typingSeqByUser = new Map<string, number>();
  private readonly activeFeedbackCleanups = new Set<() => void>();
  private readonly sentOneTimeReplyKeys = new Set<string>();
  private readonly inFlightControllers = new Set<AbortController>();
  private readonly accountInFlightControllers = new Map<
    string,
    Set<AbortController>
  >();
  private readonly recentRateLimitEvents = new Map<string, number>();
  private readonly httpPools: WeixinHttpPoolManager;

  private backend?: BackendClient;
  private leaseManager?: GatewayLeaseManager;
  private leaseRecoveredUnsubscribe: (() => void) | null = null;
  private qrManager: WeixinQrLoginManager | null = null;
  private inboundHandler: ((event: InboundChannelEvent) => Promise<void>) | null =
    null;
  private accountInvalidatedHandler:
    | ((info: {
        accountId: string;
        reason: string;
        activeBotGeneration?: number;
      }) => void)
    | null = null;

  private started = false;
  private hasAppliedSnapshot = false;
  private backendEnabled = true;
  private backendNotFound404 = false;
  private lastRevision: number | null = null;
  private maxConcurrentAccounts = 50;
  private reconcileTimer: ReturnType<typeof setTimeout> | null = null;
  private reconcileInFlight: Promise<void> | null = null;
  private reconcilePending = false;
  private reconcileAbortController: AbortController | null = null;
  private egressCooldownUntilMs = 0;
  private readonly pendingStaggerIds: string[] = [];
  private readonly pendingStaggerSet = new Set<string>();
  private staggerDrainTask: Promise<void> | null = null;
  private lastMonitorStartAtMs: number | null = null;

  constructor(opts: WeixinChannelAdapterOptions = {}) {
    this.opts = opts;
    this.logger = opts.logger ?? createSubsystemLogger("weixin");
    this.typingIntervalMs = opts.typingIntervalMs ?? WEIXIN_TYPING_INTERVAL_MS;
    this.typingMaxDurationMs =
      opts.typingMaxDurationMs ?? WEIXIN_TYPING_MAX_DURATION_MS;
    this.typingTicketTtlMs =
      opts.typingTicketTtlMs ?? WEIXIN_TYPING_TICKET_TTL_MS;
    this.reconcileIntervalMs =
      opts.reconcileIntervalMs ?? WEIXIN_DEFAULT_RECONCILE_INTERVAL_MS;
    this.startupSnapshotRetryMs =
      opts.startupSnapshotRetryMs ?? WEIXIN_DEFAULT_STARTUP_SNAPSHOT_RETRY_MS;
    this.probeInterval404Ms =
      opts.probeInterval404Ms ?? WEIXIN_LAYER2_PROBE_INTERVAL_MS;
    this.staggerDelayMs =
      opts.staggerDelayMs ?? WEIXIN_DEFAULT_STAGGER_DELAY_MS;
    this.staggerJitterMs =
      opts.staggerJitterMs ?? WEIXIN_DEFAULT_STAGGER_JITTER_MS;
    this.rateLimitWindowMs =
      opts.rateLimitWindowMs ?? WEIXIN_DEFAULT_RATE_LIMIT_WINDOW_MS;
    this.rateLimitAccountThreshold =
      opts.rateLimitAccountThreshold ?? WEIXIN_DEFAULT_RATE_LIMIT_THRESHOLD;
    this.rateLimitCooldownMs =
      opts.rateLimitCooldownMs ?? WEIXIN_DEFAULT_RATE_LIMIT_COOLDOWN_MS;
    this.staleContextTokenCodes = new Set(
      opts.staleContextTokenErrorCodes ?? WEIXIN_STALE_CONTEXT_TOKEN_ERRCODES,
    );
    this.nowFn = opts.nowFn ?? (() => Date.now());
    this.backend = opts.backend;
    this.httpPools = new WeixinHttpPoolManager({
      maxConcurrentAccounts: this.maxConcurrentAccounts,
      agentFactory: opts.httpAgentFactory,
    });
    if (opts.leaseManager) {
      this.setLeaseManager(opts.leaseManager);
    }

    const initialToken = (opts.botToken || "").trim();
    const initialBotId = (
      opts.ilinkBotId || (initialToken ? "weixin:env_bot" : "")
    ).trim();
    if (initialToken && initialBotId) {
      const accountId = opts.accountId?.trim() || "standalone";
      if (!this.backend) {
        this.hasAppliedSnapshot = true;
      }
      this.accounts.set(accountId, {
        accountId,
        userId: "",
        ilinkBotId: initialBotId,
        ilinkUserId: null,
        botToken: initialToken,
        baseUrl: (opts.baseUrl || ILINK_DEFAULT_BASE_URL).trim(),
        cdnBaseUrl: (opts.cdnBaseUrl || ILINK_DEFAULT_CDN_BASE_URL).trim(),
        botGeneration: opts.botGeneration ?? 1,
        getUpdatesBuf: "",
        status: "active",
        monitorController: null,
        monitorTask: null,
      });
    }

    if (this.backend) {
      this.initQrManager(this.backend);
    }
  }

  setBackendClient(backend: BackendClient): void {
    this.backend = backend;
    this.initQrManager(backend);
  }

  setLeaseManager(leaseManager: GatewayLeaseManager): void {
    this.leaseRecoveredUnsubscribe?.();
    this.leaseManager = leaseManager;
    this.leaseRecoveredUnsubscribe = leaseManager.addLeaseRecoveredListener(
      () => {
        this.ensurePollableMonitorsRunning();
      },
    );
    if (this.backend) {
      this.initQrManager(this.backend);
    }
  }

  getHttpPoolManager(): WeixinHttpPoolManager {
    return this.httpPools;
  }

  onWeixinAccountInvalidated(
    handler: (info: {
      accountId: string;
      reason: string;
      activeBotGeneration?: number;
    }) => void,
  ): void {
    this.accountInvalidatedHandler = handler;
  }

  private assertLeaseForOutbound(operation: string): void {
    this.leaseManager?.assertLeaseForOutbound(operation);
  }

  private assertLeaseForDelivery(operation: string): void {
    try {
      this.leaseManager?.assertLeaseForOutbound(operation);
    } catch (err) {
      if (isLeaseSafetyMarginPauseError(err)) {
        throw new ChannelDeliveryError(err.message, {
          outcome: "failed",
          code: "unavailable",
          retryable: true,
          denyCode: err.code,
        });
      }
      throw err;
    }
  }

  /**
   * Round 3 Decision #2 & R4 #5 (Codex #9): After `preflight-send` returns and before
   * `sendmessage` / CDN upload, verify that the initial managed snapshot has been applied
   * and that the preflight's `account_id`, `ilink_bot_id`, and `bot_generation` match the
   * current managed snapshot. Without an applied snapshot, never send outbound.
   */
  private assertPreflightMatchesSnapshot(
    preflight: WeixinPreflightAuthorizedData,
  ): void {
    if (this.backend && !this.hasAppliedSnapshot) {
      throw new ChannelDeliveryError(
        "Weixin managed account snapshot has not been applied yet.",
        {
          outcome: "failed",
          code: "unavailable",
          retryable: false,
          denyCode: "weixin_snapshot_not_ready",
        },
      );
    }
    const snap = this.accounts.get(preflight.account_id);
    if (
      !snap ||
      !isPollableAccountStatus(snap.status) ||
      snap.ilinkBotId !== preflight.ilink_bot_id ||
      snap.botGeneration !== preflight.bot_generation
    ) {
      throw new ChannelDeliveryError(
        "Weixin preflight account or bot generation does not match current managed snapshot.",
        {
          outcome: "failed",
          code: "not_allowed",
          retryable: false,
          denyCode: "weixin_bot_generation_mismatch",
        },
      );
    }
  }

  private initQrManager(backend: BackendClient): void {
    this.qrManager = new WeixinQrLoginManager({
      backend,
      baseUrl: this.opts.baseUrl || ILINK_DEFAULT_BASE_URL,
      cdnBaseUrl: this.opts.cdnBaseUrl || ILINK_DEFAULT_CDN_BASE_URL,
      botType: this.opts.botType || ILINK_DEFAULT_BOT_TYPE,
      allowedApiDomains: this.opts.allowedApiDomains,
      resolveHostIps: this.opts.resolveHostIps,
      checkResolvedIps: this.opts.checkResolvedIps,
      fetchImpl: this.opts.fetchImpl,
      getDispatcher: () => this.httpPools.getControlDispatcher(),
      isLeaseValid: this.leaseManager
        ? () => this.leaseManager!.isLeaseLocallyValid()
        : undefined,
      assertLeaseValid: this.leaseManager
        ? (operation) => this.leaseManager!.assertLeaseForOutbound(operation)
        : undefined,
      onAccountConfirmed: async (confirmed) => {
        await this.handleConfirmedAccount(confirmed);
      },
    });
  }

  isConfigured(): boolean {
    if (this.backend && !this.backendNotFound404 && this.backendEnabled) {
      return true;
    }
    return this.getActiveAccountCount() > 0;
  }

  isConnected(): boolean {
    if (!this.started) {
      return false;
    }
    if (this.backend && (this.backendNotFound404 || !this.backendEnabled)) {
      return false;
    }
    return this.getActiveAccountCount() > 0 || Boolean(this.backend && this.backendEnabled);
  }

  isReadyForOutbound(): boolean {
    return this.isConfigured();
  }

  getActiveAccountCount(): number {
    let count = 0;
    for (const acc of this.accounts.values()) {
      if (isPollableAccountStatus(acc.status) && acc.botToken && acc.ilinkBotId) {
        count += 1;
      }
    }
    return count;
  }

  getManagedAccounts(): Array<{
    accountId: string;
    userId: string;
    ilinkBotId: string;
    botGeneration: number;
    status: string;
    getUpdatesBuf: string;
    polling: boolean;
  }> {
    return [...this.accounts.values()].map((acc) => ({
      accountId: acc.accountId,
      userId: acc.userId,
      ilinkBotId: acc.ilinkBotId,
      botGeneration: acc.botGeneration,
      status: acc.status,
      getUpdatesBuf: acc.getUpdatesBuf,
      polling: acc.monitorTask !== null,
    }));
  }

  onInboundEvent(handler: (event: InboundChannelEvent) => Promise<void>): void {
    this.inboundHandler = handler;
  }

  splitTextChunks(text: string): string[] {
    return splitTextByLimit(text, this.maxChunkChars);
  }

  private createOperationSignal(
    initialAccountId?: string | null,
    externalSignal?: AbortSignal,
  ): {
    signal: AbortSignal;
    attachAccount: (accountId: string | null | undefined) => void;
    release: () => void;
  } {
    const controller = new AbortController();
    this.inFlightControllers.add(controller);
    let attachedAccountId: string | null = null;
    const onExternalAbort = (): void => {
      controller.abort();
    };
    if (externalSignal) {
      if (externalSignal.aborted) {
        controller.abort();
      } else {
        externalSignal.addEventListener("abort", onExternalAbort, { once: true });
      }
    }

    const attachAccount = (accountId: string | null | undefined): void => {
      const clean = accountId?.trim() || null;
      if (!clean || clean === attachedAccountId) {
        return;
      }
      if (attachedAccountId) {
        const prevSet = this.accountInFlightControllers.get(attachedAccountId);
        prevSet?.delete(controller);
        if (prevSet && prevSet.size === 0) {
          this.accountInFlightControllers.delete(attachedAccountId);
        }
      }
      attachedAccountId = clean;
      let set = this.accountInFlightControllers.get(clean);
      if (!set) {
        set = new Set();
        this.accountInFlightControllers.set(clean, set);
      }
      set.add(controller);
    };

    attachAccount(initialAccountId);

    return {
      signal: controller.signal,
      attachAccount,
      release: () => {
        if (externalSignal) {
          externalSignal.removeEventListener("abort", onExternalAbort);
        }
        this.inFlightControllers.delete(controller);
        if (attachedAccountId) {
          const set = this.accountInFlightControllers.get(attachedAccountId);
          set?.delete(controller);
          if (set && set.size === 0) {
            this.accountInFlightControllers.delete(attachedAccountId);
          }
        }
      },
    };
  }

  async runWithAccountOperationSignal<T>(
    accountId: string | null | undefined,
    fn: (ctx: {
      signal: AbortSignal;
      attachAccount: (id: string | null | undefined) => void;
    }) => Promise<T>,
  ): Promise<T> {
    const op = this.createOperationSignal(accountId);
    try {
      return await fn({
        signal: op.signal,
        attachAccount: op.attachAccount,
      });
    } finally {
      op.release();
    }
  }

  private abortAccountInFlight(accountId: string): void {
    const set = this.accountInFlightControllers.get(accountId);
    if (!set) return;
    this.accountInFlightControllers.delete(accountId);
    for (const ctrl of [...set]) {
      this.inFlightControllers.delete(ctrl);
      ctrl.abort();
    }
  }

  /**
   * Synchronously aborts ALL in-flight sends, uploads, QR calls, reconcile passes,
   * and account poll loops without awaiting handlers (Decision #1 / §3.2).
   */
  abortAllInFlight(): void {
    this.started = false;
    this.clearReconcileTimer();
    this.reconcilePending = false;
    this.pendingStaggerIds.length = 0;
    this.pendingStaggerSet.clear();
    this.lastMonitorStartAtMs = null;
    const recCtrl = this.reconcileAbortController;
    this.reconcileAbortController = null;
    recCtrl?.abort();

    for (const cleanup of [...this.activeFeedbackCleanups]) {
      cleanup();
    }
    this.activeFeedbackCleanups.clear();

    for (const ctrl of [...this.inFlightControllers]) {
      ctrl.abort();
    }
    this.inFlightControllers.clear();
    for (const set of this.accountInFlightControllers.values()) {
      for (const ctrl of [...set]) {
        ctrl.abort();
      }
    }
    this.accountInFlightControllers.clear();

    for (const acc of this.accounts.values()) {
      acc.monitorController?.abort();
    }
  }

  private clearReconcileTimer(): void {
    if (this.reconcileTimer !== null) {
      clearTimeout(this.reconcileTimer);
      this.reconcileTimer = null;
    }
  }

  private scheduleNextReconcile(delayMs: number): void {
    this.clearReconcileTimer();
    if (!this.started || !this.backend) {
      return;
    }
    this.reconcileTimer = setTimeout(() => {
      this.reconcileTimer = null;
      void this.runSingleFlightReconcile("periodic");
    }, Math.max(10, delayMs));
    (this.reconcileTimer as { unref?: () => void }).unref?.();
  }

  /**
   * R4 #5 & R5 #4 (Codex #6, Grok High): Loads and applies the initial managed account
   * snapshot BEFORE the gateway lifts startup write restrictions and before long-polling begins.
   * Retries transient failures (5xx / network / lease safety margin) until an authoritative
   * snapshot is applied; immediately throws on lease loss (409 / expired) or stop so write
   * routes are NEVER opened before the initial snapshot succeeds.
   */
  async prepareStartupSnapshot(): Promise<void> {
    if (!this.backend || this.hasAppliedSnapshot) {
      return;
    }
    if (!this.reconcileAbortController || this.reconcileAbortController.signal.aborted) {
      this.reconcileAbortController = new AbortController();
    }
    const signal = this.reconcileAbortController.signal;

    while (!this.hasAppliedSnapshot) {
      if (signal.aborted) {
        throw new Error("Weixin startup snapshot aborted");
      }
      if (this.leaseManager?.isLeaseExpired()) {
        throw new StaleGatewayEpochError(
          "Gateway lease lost during Weixin startup snapshot",
          { status: 409, epoch: this.leaseManager.epoch },
        );
      }
      if (this.leaseManager && !this.leaseManager.isLeaseLocallyValid()) {
        const ok = await sleepWithAbort(this.startupSnapshotRetryMs, signal);
        if (!ok) {
          throw new Error("Weixin startup snapshot aborted while waiting for lease");
        }
        continue;
      }

      try {
        await this.executeSingleReconcilePass("startup_snapshot", {
          startMonitors: false,
          throwOnFailure: true,
        });
      } catch (err) {
        if (isStaleGatewayEpochError(err)) {
          throw err;
        }
        if (signal.aborted || this.leaseManager?.isLeaseExpired()) {
          throw new StaleGatewayEpochError(
            "Gateway lease lost during Weixin startup snapshot",
            { status: 409, epoch: this.leaseManager?.epoch ?? null, cause: err },
          );
        }
        this.logger.warn(
          "Weixin initial startup snapshot failed; retrying before opening write routes",
          {
            retry_ms: this.startupSnapshotRetryMs,
            ...extractSafeLogErrorFields(err),
          },
        );
        const ok = await sleepWithAbort(this.startupSnapshotRetryMs, signal);
        if (!ok) {
          throw new Error("Weixin startup snapshot aborted during retry wait");
        }
      }
    }
  }

  async start(): Promise<void> {
    if (this.started) {
      return;
    }
    this.started = true;
    if (!this.reconcileAbortController || this.reconcileAbortController.signal.aborted) {
      this.reconcileAbortController = new AbortController();
    }

    if (this.backend) {
      if (this.hasAppliedSnapshot) {
        const pendingToStart = [...this.accounts.values()].filter(
          (acc) =>
            !acc.monitorTask &&
            isPollableAccountStatus(acc.status) &&
            Boolean(acc.botToken) &&
            Boolean(acc.ilinkBotId),
        );
        if (
          this.backendEnabled &&
          !this.backendNotFound404 &&
          (!this.leaseManager || this.leaseManager.isLeaseLocallyValid())
        ) {
          await this.enqueueMonitorsStaggered(pendingToStart);
        }
        const nextDelay = this.backendNotFound404
          ? this.probeInterval404Ms
          : this.reconcileIntervalMs;
        this.scheduleNextReconcile(nextDelay);
        return;
      }
      await this.runSingleFlightReconcile("startup");
      return;
    }

    // Standalone/test mode without backend: start any seeded local account monitors.
    for (const acc of this.accounts.values()) {
      if (isPollableAccountStatus(acc.status) && acc.botToken && acc.ilinkBotId) {
        this.startAccountMonitor(acc);
      }
    }
  }

  async stop(): Promise<void> {
    // Decision #1: Synchronously abort EVERY in-flight send, upload, QR, and poll request FIRST,
    // before awaiting monitor tasks, so nothing can still hit iLink while shutting down.
    this.abortAllInFlight();
    this.typingTicketCache.clear();
    this.typingSeqByUser.clear();
    this.leaseRecoveredUnsubscribe?.();
    this.leaseRecoveredUnsubscribe = null;

    await this.stopAllAccountMonitors();
    await this.httpPools.close();
  }

  private async stopAccountMonitor(acc: ManagedWeixinAccount): Promise<void> {
    const ctrl = acc.monitorController;
    const task = acc.monitorTask;
    acc.monitorController = null;
    acc.monitorTask = null;
    ctrl?.abort();
    if (task) {
      try {
        await task;
      } catch {
        // Ignore abort errors during shutdown.
      }
    }
  }

  private async stopAllAccountMonitors(): Promise<void> {
    const tasks: Promise<void>[] = [];
    for (const acc of this.accounts.values()) {
      tasks.push(this.stopAccountMonitor(acc));
    }
    await Promise.all(tasks);
  }

  private clearTypingCacheForAccount(accountId: string): void {
    const prefix = `${accountId}:`;
    for (const key of [...this.typingTicketCache.keys()]) {
      if (key.startsWith(prefix)) {
        this.typingTicketCache.delete(key);
      }
    }
    for (const key of [...this.typingSeqByUser.keys()]) {
      if (key.startsWith(prefix)) {
        this.typingSeqByUser.delete(key);
      }
    }
  }

  private async resolveSafeBaseUrl(
    candidateUrl: string | undefined | null,
  ): Promise<string> {
    const defaultBase = this.opts.baseUrl || ILINK_DEFAULT_BASE_URL;
    if (!candidateUrl || !candidateUrl.trim()) {
      return normalizeAndValidateIlinkBaseUrl(
        defaultBase,
        this.opts.allowedApiDomains,
      );
    }
    try {
      return await resolveValidatedRedirectBaseUrl({
        redirectHost: candidateUrl.trim(),
        baseUrl: defaultBase,
        allowedDomains: this.opts.allowedApiDomains,
        resolveHostIps: this.opts.resolveHostIps,
        checkResolvedIps: this.opts.checkResolvedIps,
      });
    } catch {
      return normalizeAndValidateIlinkBaseUrl(
        defaultBase,
        this.opts.allowedApiDomains,
      );
    }
  }

  private getSharedCooldownRemainingMs(): number {
    return Math.max(0, this.egressCooldownUntilMs - this.nowFn());
  }

  private recordRateLimitAndComputeDelay(
    accountId: string,
    err: WeixinApiError,
  ): number {
    const now = this.nowFn();
    this.recentRateLimitEvents.set(accountId, now);
    for (const [accId, ts] of this.recentRateLimitEvents.entries()) {
      if (now - ts > this.rateLimitWindowMs) {
        this.recentRateLimitEvents.delete(accId);
      }
    }

    if (this.recentRateLimitEvents.size >= this.rateLimitAccountThreshold) {
      const cooldownUntil = now + this.rateLimitCooldownMs;
      if (cooldownUntil > this.egressCooldownUntilMs) {
        this.egressCooldownUntilMs = cooldownUntil;
        this.logger.warn(
          "Shared Weixin egress IP rate-limit cooldown triggered across accounts",
          {
            affected_accounts: this.recentRateLimitEvents.size,
            cooldown_ms: this.rateLimitCooldownMs,
          },
        );
      }
    }

    const sharedRemaining = Math.max(0, this.egressCooldownUntilMs - now);
    return Math.max(err.retryAfterMs ?? 5_000, sharedRemaining);
  }

  private startAccountMonitor(acc: ManagedWeixinAccount): void {
    if (!this.started || !this.inboundHandler || !this.backend) {
      return;
    }
    if (acc.monitorTask || !isPollableAccountStatus(acc.status)) {
      return;
    }
    if (!acc.botToken || !acc.ilinkBotId) {
      return;
    }

    const controller = new AbortController();
    acc.monitorController = controller;
    const capturedGen = acc.botGeneration;
    const capturedBotId = acc.ilinkBotId;
    const capturedAccountId = acc.accountId;

    const monitor = new WeixinMonitor({
      accountId: capturedAccountId,
      baseUrl: acc.baseUrl,
      botToken: acc.botToken,
      ilinkBotId: capturedBotId,
      botGeneration: capturedGen,
      initialGetUpdatesBuf: acc.getUpdatesBuf,
      pollTimeoutMs: this.opts.pollTimeoutMs,
      retryDelayMs: this.opts.retryDelayMs,
      allowedApiDomains: this.opts.allowedApiDomains,
      resolveHostIps: this.opts.resolveHostIps,
      checkResolvedIps: this.opts.checkResolvedIps,
      fetchImpl: this.opts.fetchImpl,
      getPollDispatcher: () => this.httpPools.getPollDispatcher(),
      backend: this.backend,
      logger: this.logger.child("monitor"),
      signal: controller.signal,
      isLeaseValid: this.leaseManager
        ? () => this.leaseManager!.isLeaseLocallyValid()
        : undefined,
      assertLeaseValid: this.leaseManager
        ? () => this.leaseManager!.assertLeaseForOutbound("getupdates")
        : undefined,
      getSharedCooldownRemainingMs: () => this.getSharedCooldownRemainingMs(),
      onInboundEvent: async (ev) => {
        if (!this.inboundHandler) return;
        await this.inboundHandler(ev);
      },
      onCursorCommitted: (newCursor) => {
        const current = this.accounts.get(capturedAccountId);
        if (
          current &&
          current.botGeneration === capturedGen &&
          current.ilinkBotId === capturedBotId
        ) {
          current.getUpdatesBuf = newCursor;
        }
      },
      onSessionExpired: async () => {
        const current = this.accounts.get(capturedAccountId);
        if (
          current &&
          current.botGeneration === capturedGen &&
          current.ilinkBotId === capturedBotId
        ) {
          current.status = "expired";
          this.accountInvalidatedHandler?.({
            accountId: capturedAccountId,
            reason: "session_expired",
          });
        }
      },
      onRateLimited: (err) =>
        this.recordRateLimitAndComputeDelay(capturedAccountId, err),
      onAccountGenerationSuperseded: ({ accountId, activeBotGeneration }) => {
        this.abortAccountInFlight(accountId);
        this.accountInvalidatedHandler?.({
          accountId,
          reason: "monitor_generation_superseded",
          activeBotGeneration,
        });
        void this.triggerAccountsSync(
          undefined,
          "monitor_generation_superseded",
        );
      },
      onStaleEpoch: (err) => {
        this.leaseManager?.notifyLeaseLost("monitor_stale_epoch", err);
      },
    });

    const task = monitor.start().finally(() => {
      if (acc.monitorTask === task) {
        acc.monitorTask = null;
      }
      if (acc.monitorController === controller) {
        acc.monitorController = null;
      }
    });
    acc.monitorTask = task;
  }

  /**
   * Single-flight account reconcile triggered by periodic 15s timer,
   * `POST /internal/weixin/accounts/sync` nudge, or QR login confirmation.
   */
  async triggerAccountsSync(
    targetRevision?: number | null,
    reason = "nudge",
  ): Promise<{ ok: boolean; accepted_revision: number }> {
    const accepted = Math.max(this.lastRevision ?? 0, targetRevision ?? 0);
    if (!this.backend) {
      return { ok: true, accepted_revision: accepted };
    }
    if (this.reconcileInFlight) {
      this.reconcilePending = true;
      return { ok: true, accepted_revision: accepted };
    }
    if (this.started) {
      void this.runSingleFlightReconcile(reason);
    }
    return { ok: true, accepted_revision: accepted };
  }

  async reconcileNow(reason = "manual"): Promise<void> {
    await this.runSingleFlightReconcile(reason);
  }

  private async runSingleFlightReconcile(reason: string): Promise<void> {
    if (!this.backend) {
      return;
    }
    if (this.reconcileInFlight) {
      this.reconcilePending = true;
      await this.reconcileInFlight;
      return;
    }
    this.clearReconcileTimer();

    const task = (async () => {
      do {
        this.reconcilePending = false;
        if (!this.started) {
          return;
        }
        if (this.leaseManager && !this.leaseManager.isLeaseLocallyValid()) {
          await this.stopAllAccountMonitors();
          return;
        }
        await this.executeSingleReconcilePass(reason);
      } while (this.reconcilePending && this.started);
    })().finally(() => {
      if (this.reconcileInFlight === task) {
        this.reconcileInFlight = null;
      }
      if (this.started && this.backend) {
        const nextDelay = this.backendNotFound404
          ? this.probeInterval404Ms
          : this.reconcileIntervalMs;
        this.scheduleNextReconcile(nextDelay);
      }
    });

    this.reconcileInFlight = task;
    await task;
  }

  private async executeSingleReconcilePass(
    reason: string,
    options?: { startMonitors?: boolean; throwOnFailure?: boolean },
  ): Promise<void> {
    if (!this.backend) return;
    try {
      const resp = await this.backend.reconcileWeixinAccounts(
        this.lastRevision,
        { signal: this.reconcileAbortController?.signal },
      );
      if (!isValidWeixinAccountsReconcileResponse(resp)) {
        throw new BackendHttpError(
          "Weixin accounts reconcile returned an invalid snapshot payload",
          502,
          { code: "invalid_reconcile_snapshot" },
        );
      }
      this.backendNotFound404 = false;
      this.maxConcurrentAccounts = Math.max(
        1,
        Number.isFinite(resp.max_concurrent_accounts) &&
          resp.max_concurrent_accounts > 0
          ? Math.trunc(resp.max_concurrent_accounts)
          : 50,
      );
      this.httpPools.updateMaxConcurrentAccounts(this.maxConcurrentAccounts);

      if (!resp.enabled) {
        this.backendEnabled = false;
        this.hasAppliedSnapshot = true;
        this.lastRevision = resp.accounts_revision;
        if (this.accounts.size > 0) {
          this.logger.info(
            "Weixin runtime toggle disabled by backend; stopping all poll loops",
            {
              reason,
              accounts_revision: resp.accounts_revision,
              stopped_count: this.accounts.size,
            },
          );
          const removedIds = [...this.accounts.keys()];
          for (const accountId of removedIds) {
            this.abortAccountInFlight(accountId);
          }
          await this.stopAllAccountMonitors();
          this.accounts.clear();
          this.typingTicketCache.clear();
          this.typingSeqByUser.clear();
          for (const accountId of removedIds) {
            this.accountInvalidatedHandler?.({
              accountId,
              reason: "feature_disabled",
            });
          }
        }
        return;
      }

      this.backendEnabled = true;
      if (!resp.changed) {
        this.hasAppliedSnapshot = true;
        if (this.lastRevision === null) {
          this.lastRevision = resp.accounts_revision;
        }
        // Decision #12: self-heal any pollable monitor whose coroutine exited
        // (e.g. after a transient lease dip) even when accounts_revision did not change.
        if (options?.startMonitors !== false) {
          this.ensurePollableMonitorsRunning();
        }
        return;
      }

      this.lastRevision = resp.accounts_revision;
      await this.applyAccountSnapshots(resp.accounts ?? [], {
        startMonitors: options?.startMonitors,
      });
    } catch (err) {
      if (isStaleGatewayEpochError(err)) {
        this.abortAllInFlight();
        this.leaseManager?.notifyLeaseLost("reconcile_stale_epoch", err);
        await this.stopAllAccountMonitors();
        if (options?.throwOnFailure) {
          throw err;
        }
        return;
      }
      if (err instanceof BackendHttpError && err.status === 404) {
        // Layer 2 disabled: backend Weixin routes unmounted -> stop all pollers, probe every 5m.
        this.backendNotFound404 = true;
        this.backendEnabled = false;
        this.hasAppliedSnapshot = true;
        this.lastRevision = null;
        const removedIds = [...this.accounts.keys()];
        for (const accountId of removedIds) {
          this.abortAccountInFlight(accountId);
        }
        await this.stopAllAccountMonitors();
        this.accounts.clear();
        this.typingTicketCache.clear();
        this.typingSeqByUser.clear();
        for (const accountId of removedIds) {
          this.accountInvalidatedHandler?.({
            accountId,
            reason: "routes_disabled_404",
          });
        }
        this.logger.info(
          "Weixin multi-account backend endpoint returned 404; pausing pollers and probing periodically",
          {
            probe_interval_ms: this.probeInterval404Ms,
          },
        );
        return;
      }
      if (options?.throwOnFailure) {
        throw err;
      }
      this.logger.warn("Weixin accounts reconcile failed; retaining current pollers", {
        reason,
        ...extractSafeLogErrorFields(err),
      });
    }
  }

  /**
   * Self-heals any pollable managed account whose polling coroutine is missing
   * (Decision #12 & R6 #5: routes through the shared staggered startup queue so
   * `changed: false` sync nudges and lease recovery never bypass startup stagger).
   */
  ensurePollableMonitorsRunning(): void {
    if (
      !this.started ||
      !this.backendEnabled ||
      this.backendNotFound404 ||
      (this.leaseManager && !this.leaseManager.isLeaseLocallyValid())
    ) {
      return;
    }
    const missing: ManagedWeixinAccount[] = [];
    for (const acc of this.accounts.values()) {
      if (
        !acc.monitorTask &&
        isPollableAccountStatus(acc.status) &&
        acc.botToken &&
        acc.ilinkBotId
      ) {
        missing.push(acc);
      }
    }
    if (missing.length > 0) {
      void this.enqueueMonitorsStaggered(missing);
    }
  }

  private async applyAccountSnapshots(
    snapshots: readonly WeixinAccountSnapshot[],
    options?: { startMonitors?: boolean },
  ): Promise<void> {
    const startMonitors = options?.startMonitors !== false && this.started;
    this.hasAppliedSnapshot = true;
    const pollableRaw = snapshots.filter(
      (s) =>
        isPollableAccountStatus(s.status) &&
        Boolean(s.account_id?.trim()) &&
        Boolean(s.ilink_bot_id?.trim()) &&
        Boolean(s.bot_token?.trim()),
    );

    if (pollableRaw.length > this.maxConcurrentAccounts) {
      this.logger.warn(
        "Weixin active accounts exceed max_concurrent_accounts; capping local pollers",
        {
          active_accounts: pollableRaw.length,
          max_concurrent_accounts: this.maxConcurrentAccounts,
        },
      );
    }
    const pollable = pollableRaw.slice(0, this.maxConcurrentAccounts);
    const desiredIds = new Set(pollable.map((s) => s.account_id));

    // 1. Stop & remove accounts no longer in the pollable snapshot.
    // Decision #10: abort all in-flight sends/uploads for the account BEFORE deleting it.
    for (const [accountId, existing] of [...this.accounts.entries()]) {
      if (!desiredIds.has(accountId)) {
        this.abortAccountInFlight(accountId);
        await this.stopAccountMonitor(existing);
        this.accounts.delete(accountId);
        this.clearTypingCacheForAccount(accountId);
        this.accountInvalidatedHandler?.({
          accountId,
          reason: "removed_or_inactive",
        });
      }
    }

    // 2. Diff remaining / new accounts and collect those needing monitor start.
    const toStart: ManagedWeixinAccount[] = [];
    for (const snap of pollable) {
      if (startMonitors && !this.started) return;
      if (this.reconcileAbortController?.signal.aborted) return;
      const safeBaseUrl = await this.resolveSafeBaseUrl(snap.base_url);
      const cdnBaseUrl = (
        snap.cdn_base_url ||
        this.opts.cdnBaseUrl ||
        ILINK_DEFAULT_CDN_BASE_URL
      ).trim();

      const existing = this.accounts.get(snap.account_id);
      if (existing) {
        const credentialsChanged =
          existing.ilinkBotId !== snap.ilink_bot_id ||
          existing.botGeneration !== snap.bot_generation ||
          existing.botToken !== snap.bot_token ||
          existing.baseUrl !== safeBaseUrl;

        if (credentialsChanged) {
          // Decision #10: abort all in-flight requests using old credentials BEFORE applying new ones.
          this.abortAccountInFlight(snap.account_id);
          await this.stopAccountMonitor(existing);
          this.clearTypingCacheForAccount(snap.account_id);
          existing.userId = snap.user_id;
          existing.ilinkBotId = snap.ilink_bot_id;
          existing.ilinkUserId = snap.ilink_user_id ?? null;
          existing.botToken = snap.bot_token;
          existing.baseUrl = safeBaseUrl;
          existing.cdnBaseUrl = cdnBaseUrl;
          existing.botGeneration = snap.bot_generation;
          existing.getUpdatesBuf = snap.get_updates_buf ?? "";
          existing.status = snap.status;
          this.accountInvalidatedHandler?.({
            accountId: snap.account_id,
            reason: "generation_or_credentials_changed",
            activeBotGeneration: snap.bot_generation,
          });
          toStart.push(existing);
        } else {
          existing.userId = snap.user_id;
          existing.ilinkUserId = snap.ilink_user_id ?? null;
          existing.cdnBaseUrl = cdnBaseUrl;
          existing.status = snap.status;
          if (!existing.getUpdatesBuf && snap.get_updates_buf) {
            existing.getUpdatesBuf = snap.get_updates_buf;
          }
          if (!existing.monitorTask) {
            toStart.push(existing);
          }
        }
      } else {
        const created: ManagedWeixinAccount = {
          accountId: snap.account_id,
          userId: snap.user_id,
          ilinkBotId: snap.ilink_bot_id,
          ilinkUserId: snap.ilink_user_id ?? null,
          botToken: snap.bot_token,
          baseUrl: safeBaseUrl,
          cdnBaseUrl,
          botGeneration: snap.bot_generation,
          getUpdatesBuf: snap.get_updates_buf ?? "",
          status: snap.status,
          monitorController: null,
          monitorTask: null,
        };
        this.accounts.set(snap.account_id, created);
        toStart.push(created);
      }
    }

    // 3. Staggered startup (<= 10 accounts/sec + 0..500ms jitter) when starting multiple pollers (§3.4).
    if (!startMonitors) {
      return;
    }
    await this.enqueueMonitorsStaggered(toStart);
  }

  /**
   * Single shared staggered monitor-start queue (§3.4 / R5 #4 / R6 #5).
   * Used by preloaded `start()`, `applyAccountSnapshots()`, and `ensurePollableMonitorsRunning()`
   * so concurrent sync nudges (`changed: false` or `changed: true`) never launch remaining
   * accounts simultaneously.
   */
  private enqueueMonitorsStaggered(
    accounts: readonly ManagedWeixinAccount[],
  ): Promise<void> {
    for (const acc of accounts) {
      if (
        !acc.monitorTask &&
        !this.pendingStaggerSet.has(acc.accountId) &&
        isPollableAccountStatus(acc.status) &&
        acc.botToken &&
        acc.ilinkBotId
      ) {
        this.pendingStaggerSet.add(acc.accountId);
        this.pendingStaggerIds.push(acc.accountId);
      }
    }
    if (this.pendingStaggerIds.length === 0) {
      return this.staggerDrainTask ?? Promise.resolve();
    }
    if (this.staggerDrainTask) {
      return this.staggerDrainTask;
    }

    const drainTask = (async () => {
      const signal = this.reconcileAbortController?.signal;
      while (this.pendingStaggerIds.length > 0) {
        if (
          !this.started ||
          signal?.aborted ||
          !this.backendEnabled ||
          this.backendNotFound404 ||
          (this.leaseManager && !this.leaseManager.isLeaseLocallyValid())
        ) {
          this.pendingStaggerIds.length = 0;
          this.pendingStaggerSet.clear();
          return;
        }
        const accountId = this.pendingStaggerIds.shift()!;
        this.pendingStaggerSet.delete(accountId);
        const acc = this.accounts.get(accountId);
        if (
          !acc ||
          acc.monitorTask ||
          !isPollableAccountStatus(acc.status) ||
          !acc.botToken ||
          !acc.ilinkBotId
        ) {
          continue;
        }
        if (
          this.lastMonitorStartAtMs !== null &&
          (this.staggerDelayMs > 0 || this.staggerJitterMs > 0)
        ) {
          const jitter =
            this.staggerJitterMs > 0
              ? Math.floor(Math.random() * this.staggerJitterMs)
              : 0;
          const targetAtMs =
            this.lastMonitorStartAtMs + this.staggerDelayMs + jitter;
          const waitMs = targetAtMs - this.nowFn();
          if (waitMs > 0) {
            const ok = await sleepWithAbort(waitMs, signal);
            if (!ok) {
              this.pendingStaggerIds.length = 0;
              this.pendingStaggerSet.clear();
              return;
            }
          }
        }
        if (
          !this.started ||
          signal?.aborted ||
          !this.backendEnabled ||
          this.backendNotFound404 ||
          (this.leaseManager && !this.leaseManager.isLeaseLocallyValid())
        ) {
          this.pendingStaggerIds.length = 0;
          this.pendingStaggerSet.clear();
          return;
        }
        const latest = this.accounts.get(accountId);
        if (
          latest &&
          !latest.monitorTask &&
          isPollableAccountStatus(latest.status) &&
          latest.botToken &&
          latest.ilinkBotId
        ) {
          this.startAccountMonitor(latest);
          this.lastMonitorStartAtMs = this.nowFn();
        }
      }
    })().finally(() => {
      if (this.staggerDrainTask === drainTask) {
        this.staggerDrainTask = null;
      }
    });

    this.staggerDrainTask = drainTask;
    return drainTask;
  }

  private async handleConfirmedAccount(
    confirmed: ConfirmedWeixinAccount,
  ): Promise<void> {
    this.abortAccountInFlight(confirmed.accountId);
    this.accountInvalidatedHandler?.({
      accountId: confirmed.accountId,
      reason: "qr_confirmed",
      activeBotGeneration: confirmed.botGeneration,
    });
    if (this.backend && this.started) {
      await this.triggerAccountsSync(
        confirmed.accountsRevision ?? undefined,
        "qr_confirmed",
      );
      return;
    }
    const safeBaseUrl = await this.resolveSafeBaseUrl(confirmed.baseUrl);
    const existing = this.accounts.get(confirmed.accountId);
    if (existing) {
      await this.stopAccountMonitor(existing);
      this.clearTypingCacheForAccount(confirmed.accountId);
    }
    const next: ManagedWeixinAccount = {
      accountId: confirmed.accountId,
      userId: confirmed.ownerUserId,
      ilinkBotId: confirmed.ilinkBotId,
      ilinkUserId: confirmed.ilinkUserId ?? null,
      botToken: confirmed.botToken,
      baseUrl: safeBaseUrl,
      cdnBaseUrl: (
        confirmed.cdnBaseUrl ||
        this.opts.cdnBaseUrl ||
        ILINK_DEFAULT_CDN_BASE_URL
      ).trim(),
      botGeneration: confirmed.botGeneration,
      getUpdatesBuf: confirmed.getUpdatesBuf ?? "",
      status: confirmed.accountStatus ?? "active",
      monitorController: null,
      monitorTask: null,
    };
    this.hasAppliedSnapshot = true;
    this.accounts.set(confirmed.accountId, next);
    if (this.started) {
      this.startAccountMonitor(next);
    }
  }

  private async markAccountSessionExpired(
    accountId: string,
    ilinkBotId: string,
    botGeneration: number,
    err?: WeixinApiError,
  ): Promise<void> {
    const existing = this.accounts.get(accountId);
    if (
      existing &&
      existing.ilinkBotId === ilinkBotId &&
      existing.botGeneration === botGeneration
    ) {
      this.abortAccountInFlight(accountId);
      existing.status = "expired";
      existing.monitorController?.abort();
      this.accountInvalidatedHandler?.({
        accountId,
        reason: "session_expired",
      });
    }
    if (!this.backend || !accountId || !ilinkBotId) {
      return;
    }
    try {
      const statusRes = await this.backend.updateWeixinAccountStatus(accountId, {
        action: "mark_session_expired",
        expected_bot_generation: botGeneration,
        expected_ilink_bot_id: ilinkBotId,
        error_code: String(err?.errcode ?? err?.ret ?? -14),
      });
      if (statusRes?.stale_generation) {
        this.abortAccountInFlight(accountId);
        this.accountInvalidatedHandler?.({
          accountId,
          reason: "status_stale_generation",
        });
        void this.triggerAccountsSync(undefined, "status_stale_generation");
      }
    } catch (statusErr) {
      if (isStaleGatewayEpochError(statusErr)) {
        this.leaseManager?.notifyLeaseLost("send_status_stale_epoch", statusErr);
        throw statusErr;
      }
    }
  }

  private async maybeMarkContextTokenStale(
    preflight: WeixinPreflightAuthorizedData,
    err: WeixinApiError,
  ): Promise<void> {
    if (!this.backend) return;
    const code = err.errcode ?? err.ret;
    if (typeof code !== "number" || !this.staleContextTokenCodes.has(code)) {
      return;
    }
    try {
      const statusRes = await this.backend.updateWeixinAccountStatus(
        preflight.account_id,
        {
          action: "mark_context_token_stale",
          expected_bot_generation: preflight.bot_generation,
          expected_ilink_bot_id: preflight.ilink_bot_id,
          error_code: String(code),
          recipient_platform_user_id: preflight.recipient_id,
          expected_token_version: preflight.weixin_context_token_version,
        },
      );
      if (statusRes?.stale_generation) {
        this.abortAccountInFlight(preflight.account_id);
        this.accountInvalidatedHandler?.({
          accountId: preflight.account_id,
          reason: "token_status_stale_generation",
        });
        void this.triggerAccountsSync(undefined, "token_status_stale_generation");
      }
    } catch (statusErr) {
      if (isStaleGatewayEpochError(statusErr)) {
        this.leaseManager?.notifyLeaseLost("token_stale_epoch", statusErr);
        throw statusErr;
      }
    }
  }

  /**
   * R5 #1 (Codex High #2): Returns true iff the Weixin account referenced by `event`
   * still exists in the current snapshot, is pollable, and matches `ilinkBotId` + `botGeneration`.
   */
  isWeixinAccountGenerationActive(
    event: Pick<
      InboundChannelEvent,
      "weixinAccountId" | "weixinBotId" | "weixinBotGeneration"
    >,
  ): boolean {
    const account = this.resolveAccountForEvent(event as InboundChannelEvent);
    return Boolean(account && isPollableAccountStatus(account.status));
  }

  /**
   * Decision #10: strictly match account_id, ilink_bot_id, and bot_generation.
   * Never fall back to a different account or generation when identity fields are present.
   */
  private resolveAccountForEvent(
    event: InboundChannelEvent,
  ): ManagedWeixinAccount | undefined {
    const reqAccountId = event.weixinAccountId?.trim() || "";
    const reqBotId = event.weixinBotId?.trim() || "";
    const reqGen = event.weixinBotGeneration;

    if (reqAccountId) {
      const byId = this.accounts.get(reqAccountId);
      if (!byId) {
        return undefined;
      }
      if (reqBotId && byId.ilinkBotId !== reqBotId) {
        return undefined;
      }
      if (reqGen !== undefined && byId.botGeneration !== reqGen) {
        return undefined;
      }
      return byId;
    }

    if (reqBotId) {
      for (const acc of this.accounts.values()) {
        if (
          acc.ilinkBotId === reqBotId &&
          (reqGen === undefined || acc.botGeneration === reqGen)
        ) {
          return acc;
        }
      }
      return undefined;
    }

    // Standalone single-account compatibility only when event carries no multi-account identity
    if (this.accounts.size === 1) {
      const only = this.accounts.values().next().value;
      if (
        only &&
        only.accountId === "standalone" &&
        (reqGen === undefined || only.botGeneration === reqGen)
      ) {
        return only;
      }
    }
    return undefined;
  }

  private resolveStandaloneAccount(
    accountId?: string | null,
  ): ManagedWeixinAccount | undefined {
    if (accountId) {
      return this.accounts.get(accountId);
    }
    if (this.accounts.size === 1) {
      return this.accounts.values().next().value;
    }
    return undefined;
  }

  private rememberTypingTicket(cacheKey: string, ticket: string): void {
    if (
      this.typingTicketCache.size >= MAX_CACHED_TYPING_TICKETS &&
      !this.typingTicketCache.has(cacheKey)
    ) {
      const oldestKey = this.typingTicketCache.keys().next().value;
      if (oldestKey !== undefined) {
        this.typingTicketCache.delete(oldestKey);
      }
    }
    this.typingTicketCache.set(cacheKey, {
      ticket,
      cachedAtMs: this.nowFn(),
    });
  }

  /**
   * Multi-account Weixin processing status feedback (#499):
   * resolves the exact account (`bot_token`, `base_url`, `ilink_bot_id`, `bot_generation`)
   * for the inbound event and caches `typing_ticket` per `(accountId, ilinkBotId, botGeneration, ilinkUserId)`.
   */
  beginProcessingFeedback(
    context: ProcessingFeedbackContext,
  ): ProcessingFeedbackHandle | null {
    const { event } = context;
    if (event.chatType === "group") {
      return null;
    }
    if (this.leaseManager && !this.leaseManager.isLeaseLocallyValid()) {
      return null;
    }
    const account = this.resolveAccountForEvent(event);
    if (
      !account ||
      !isPollableAccountStatus(account.status) ||
      !account.botToken ||
      !account.ilinkBotId
    ) {
      return null;
    }
    const ilinkUserId = event.platformUserId.trim();
    if (!ilinkUserId) {
      return null;
    }

    const capturedAccountId = account.accountId;
    const capturedBotId = account.ilinkBotId;
    const capturedGen = account.botGeneration;
    const capturedBaseUrl = account.baseUrl;
    const capturedBotToken = account.botToken;
    const cacheKey = `${capturedAccountId}:${capturedBotId}:${capturedGen}:${ilinkUserId}`;
    const startedAtMs = this.nowFn();
    const mySeq = (this.typingSeqByUser.get(cacheKey) ?? 0) + 1;
    this.typingSeqByUser.set(cacheKey, mySeq);

    let settled = false;
    let typingStarted = false;
    let activeTicket: string | null = null;
    let settlePromise: Promise<void> | null = null;
    let renewTimer: ReturnType<typeof setTimeout> | null = null;
    let activeRenewalTask: Promise<void> = Promise.resolve();

    const isSameActiveAccount = (): boolean => {
      const current = this.accounts.get(capturedAccountId);
      return Boolean(
        current &&
          isPollableAccountStatus(current.status) &&
          current.ilinkBotId === capturedBotId &&
          current.botGeneration === capturedGen,
      );
    };

    const isCurrentTypingOwner = (): boolean =>
      this.typingSeqByUser.get(cacheKey) === mySeq;

    const stopTimer = (): void => {
      if (renewTimer !== null) {
        clearTimeout(renewTimer);
        renewTimer = null;
      }
      this.activeFeedbackCleanups.delete(stopTimer);
    };

    const scheduleRenewal = (): void => {
      if (
        settled ||
        !activeTicket ||
        !isSameActiveAccount() ||
        !isCurrentTypingOwner()
      ) {
        stopTimer();
        return;
      }
      const elapsedMs = this.nowFn() - startedAtMs;
      if (elapsedMs + this.typingIntervalMs >= this.typingMaxDurationMs) {
        stopTimer();
        return;
      }
      renewTimer = setTimeout(() => {
        renewTimer = null;
        if (
          settled ||
          !activeTicket ||
          !isSameActiveAccount() ||
          !isCurrentTypingOwner()
        ) {
          stopTimer();
          return;
        }
        if (this.nowFn() - startedAtMs >= this.typingMaxDurationMs) {
          stopTimer();
          return;
        }
        const ticket = activeTicket;
        const { signal, release } = this.createOperationSignal(capturedAccountId);
        const renewalPromise = sendWeixinTyping({
          baseUrl: capturedBaseUrl,
          botToken: capturedBotToken,
          ilinkUserId,
          typingTicket: ticket,
          status: TypingStatus.TYPING,
          signal,
          fetchImpl: this.opts.fetchImpl,
          dispatcher: this.httpPools.getControlDispatcher(),
          allowedDomains: this.opts.allowedApiDomains,
          resolveHostIps: this.opts.resolveHostIps,
          checkResolvedIps: this.opts.checkResolvedIps,
          assertLeaseValid: () => this.assertLeaseForOutbound("sendtyping.renew"),
        })
          .then(() => {
            if (!settled && isCurrentTypingOwner()) {
              scheduleRenewal();
            }
          })
          .catch((err) => {
            if (err instanceof WeixinApiError && err.kind === "business_error") {
              this.typingTicketCache.delete(cacheKey);
            }
            stopTimer();
            this.logger.debug("Weixin typing renewal skipped", {
              account_id: capturedAccountId,
              ilink_bot_id: capturedBotId,
              error_kind: err instanceof WeixinApiError ? err.kind : "error",
              status: err instanceof WeixinApiError ? err.status : undefined,
            });
          })
          .finally(() => {
            release();
          });
        activeRenewalTask = renewalPromise;
        void renewalPromise;
      }, this.typingIntervalMs);
      (renewTimer as { unref?: () => void }).unref?.();
    };

    this.activeFeedbackCleanups.add(stopTimer);

    const startTask = (async () => {
      const cached = this.typingTicketCache.get(cacheKey);
      let ticket: string | undefined;
      if (
        cached &&
        this.nowFn() - cached.cachedAtMs < this.typingTicketTtlMs
      ) {
        ticket = cached.ticket;
      } else {
        // Round 3 Decision #13: create & register abort signal BEFORE `getWeixinAccountDetail`
        // so lease loss or account invalidation during the lookup immediately cancels `getconfig`.
        const { signal, release } = this.createOperationSignal(capturedAccountId);
        try {
          let contextToken = event.weixinContextToken?.trim() || undefined;
          if (!contextToken && this.backend && capturedAccountId !== "standalone") {
            try {
              const detail = await this.backend.getWeixinAccountDetail(
                capturedAccountId,
                {
                  recipient_platform_user_id: ilinkUserId,
                  inbound_message_id: event.messageId,
                  signal,
                },
              );
              if (
                detail.ilink_bot_id !== capturedBotId ||
                detail.bot_generation !== capturedGen ||
                !isPollableAccountStatus(detail.status)
              ) {
                stopTimer();
                return;
              }
              contextToken = detail.weixin_context_token?.trim() || undefined;
            } catch {
              // Proceed best-effort without context_token if unavailable.
            }
          }
          if (
            settled ||
            signal.aborted ||
            !isSameActiveAccount() ||
            !isCurrentTypingOwner()
          ) {
            stopTimer();
            return;
          }
          const cfg = await getWeixinConfig({
            baseUrl: capturedBaseUrl,
            botToken: capturedBotToken,
            ilinkUserId,
            contextToken,
            signal,
            fetchImpl: this.opts.fetchImpl,
            dispatcher: this.httpPools.getControlDispatcher(),
            allowedDomains: this.opts.allowedApiDomains,
            resolveHostIps: this.opts.resolveHostIps,
            checkResolvedIps: this.opts.checkResolvedIps,
            assertLeaseValid: () => this.assertLeaseForOutbound("getconfig"),
          });
          ticket = cfg.typing_ticket?.trim() || undefined;
          if (ticket) {
            this.rememberTypingTicket(cacheKey, ticket);
          } else {
            stopTimer();
            this.logger.debug("Weixin getconfig returned no typing_ticket", {
              account_id: capturedAccountId,
              ilink_bot_id: capturedBotId,
            });
            return;
          }
        } catch (err) {
          stopTimer();
          this.logger.debug("Weixin getconfig for typing_ticket failed; skipping", {
            account_id: capturedAccountId,
            ilink_bot_id: capturedBotId,
            error_kind: err instanceof WeixinApiError ? err.kind : "error",
            status: err instanceof WeixinApiError ? err.status : undefined,
          });
          return;
        } finally {
          release();
        }
      }

      if (!ticket || settled || !isSameActiveAccount() || !isCurrentTypingOwner()) {
        stopTimer();
        return;
      }

      const { signal, release } = this.createOperationSignal(capturedAccountId);
      try {
        await sendWeixinTyping({
          baseUrl: capturedBaseUrl,
          botToken: capturedBotToken,
          ilinkUserId,
          typingTicket: ticket,
          status: TypingStatus.TYPING,
          signal,
          fetchImpl: this.opts.fetchImpl,
          dispatcher: this.httpPools.getControlDispatcher(),
          allowedDomains: this.opts.allowedApiDomains,
          resolveHostIps: this.opts.resolveHostIps,
          checkResolvedIps: this.opts.checkResolvedIps,
          assertLeaseValid: () => this.assertLeaseForOutbound("sendtyping.start"),
        });
        typingStarted = true;
        activeTicket = ticket;
        if (!settled && isCurrentTypingOwner()) {
          scheduleRenewal();
        } else {
          stopTimer();
        }
      } catch (err) {
        stopTimer();
        if (err instanceof WeixinApiError && err.kind === "business_error") {
          this.typingTicketCache.delete(cacheKey);
        }
        this.logger.debug("Weixin sendtyping(start) skipped", {
          account_id: capturedAccountId,
          ilink_bot_id: capturedBotId,
          error_kind: err instanceof WeixinApiError ? err.kind : "error",
          status: err instanceof WeixinApiError ? err.status : undefined,
        });
      } finally {
        release();
      }
    })().catch(() => {
      stopTimer();
      this.logger.warn("Weixin processing feedback start unexpected error", {
        account_id: capturedAccountId,
        ilink_bot_id: capturedBotId,
      });
    });

    return {
      settle: (_outcome: ProcessingFeedbackOutcome): Promise<void> => {
        if (settled) {
          return settlePromise ?? Promise.resolve();
        }
        settled = true;
        stopTimer();
        settlePromise = (async () => {
          await startTask;
          await activeRenewalTask;
          if (
            !typingStarted ||
            !activeTicket ||
            !isSameActiveAccount() ||
            !isCurrentTypingOwner()
          ) {
            return;
          }
          const { signal, release } = this.createOperationSignal(capturedAccountId);
          try {
            await sendWeixinTyping({
              baseUrl: capturedBaseUrl,
              botToken: capturedBotToken,
              ilinkUserId,
              typingTicket: activeTicket,
              status: TypingStatus.CANCEL,
              signal,
              fetchImpl: this.opts.fetchImpl,
              dispatcher: this.httpPools.getControlDispatcher(),
              allowedDomains: this.opts.allowedApiDomains,
              resolveHostIps: this.opts.resolveHostIps,
              checkResolvedIps: this.opts.checkResolvedIps,
              assertLeaseValid: () => this.assertLeaseForOutbound("sendtyping.cancel"),
            });
          } catch (err) {
            // Decision #15 (§4.3): log only whitelisted fields, never raw error/message text.
            this.logger.warn("Weixin sendtyping(cancel) failed", {
              account_id: capturedAccountId,
              ilink_bot_id: capturedBotId,
              error_kind: err instanceof WeixinApiError ? err.kind : "error",
              status: err instanceof WeixinApiError ? err.status : undefined,
              ret: err instanceof WeixinApiError ? err.ret : undefined,
              errcode: err instanceof WeixinApiError ? err.errcode : undefined,
            });
          } finally {
            release();
          }
        })().catch(() => {
          this.logger.warn("Weixin processing feedback settle error", {
            account_id: capturedAccountId,
            ilink_bot_id: capturedBotId,
          });
        });
        return settlePromise;
      },
    };
  }

  /**
   * D16 one-time Weixin reply sent directly using the inbound message's `context_token`
   * without calling `preflight-send` (used for conflict auto-unbind and `/unbind`).
   */
  async sendOneTimeReply(
    envelope: OneTimeWeixinReplyEnvelope,
    dedupeKey?: string,
  ): Promise<{ sent: boolean; duplicate?: boolean; messageId?: string }> {
    this.assertLeaseForOutbound("sendOneTimeReply");
    if (dedupeKey && this.sentOneTimeReplyKeys.has(dedupeKey)) {
      return { sent: false, duplicate: true };
    }
    const { signal, release } = this.createOperationSignal(null);
    try {
      const safeBaseUrl = await this.resolveSafeBaseUrl(envelope.base_url);
      this.assertLeaseForOutbound("sendOneTimeReply.dispatch");
      if (signal.aborted) {
        throw new ChannelDeliveryError("Weixin one-time reply aborted", {
          outcome: "failed",
          code: "unavailable",
        });
      }
      const cdnBaseUrl = (
        this.opts.cdnBaseUrl || ILINK_DEFAULT_CDN_BASE_URL
      ).trim();
      const res = await sendWeixinTextMessage(
        {
          baseUrl: safeBaseUrl,
          cdnBaseUrl,
          botToken: envelope.bot_token,
          contextToken: envelope.context_token,
          toUserId: envelope.recipient_id,
          signal,
          fetchImpl: this.opts.fetchImpl,
          dispatcher: this.httpPools.getControlDispatcher(),
          allowedApiDomains: this.opts.allowedApiDomains,
          allowedCdnDomains: this.opts.allowedCdnDomains,
          resolveHostIps: this.opts.resolveHostIps,
          checkResolvedIps: this.opts.checkResolvedIps,
          assertLeaseValid: () => this.assertLeaseForOutbound("sendOneTimeReply.http"),
        },
        envelope.text,
      );
      if (dedupeKey) {
        if (this.sentOneTimeReplyKeys.size >= MAX_ONE_TIME_REPLY_KEYS) {
          const oldest = this.sentOneTimeReplyKeys.values().next().value;
          if (oldest !== undefined) {
            this.sentOneTimeReplyKeys.delete(oldest);
          }
        }
        this.sentOneTimeReplyKeys.add(dedupeKey);
      }
      return { sent: true, messageId: res.messageId };
    } finally {
      release();
    }
  }

  private async resolvePreflightOrStandaloneCredentials(options: {
    targetAddress: string;
    recipientPlatformUserId?: string | null;
    weixinContextToken?: string | null;
    weixinPreflight?: WeixinPreflightAuthorizedData | null;
    weixinAccountId?: string | null;
    deliveryKey?: string | null;
    deliveryAttempt?: number;
    chunkIndex?: number;
    signal?: AbortSignal;
  }): Promise<{
    preflight: WeixinPreflightAuthorizedData | null;
    accountId: string;
    ilinkBotId: string;
    botGeneration: number;
    baseUrl: string;
    cdnBaseUrl: string;
    botToken: string;
    contextToken: string;
    toUserId: string;
  }> {
    let preflight = options.weixinPreflight ?? null;
    if (
      !preflight &&
      this.backend &&
      options.deliveryKey &&
      typeof options.deliveryAttempt === "number" &&
      options.deliveryAttempt > 0
    ) {
      const pf = await this.backend.preflightWeixinSend(
        options.deliveryKey,
        {
          delivery_attempt: options.deliveryAttempt,
          chunk_index: options.chunkIndex ?? 0,
        },
        { signal: options.signal },
      );
      if (!pf.authorized) {
        const isTransient = pf.decision === "defer" || pf.retryable;
        throw new ChannelDeliveryError(
          pf.message ?? `Weixin preflight ${pf.decision}: ${pf.deny_code}`,
          {
            outcome:
              pf.deny_code === "source_generation_unknown"
                ? "unknown"
                : "failed",
            code: isTransient ? "unavailable" : "not_allowed",
            retryable: pf.retryable,
            denyCode: pf.deny_code,
          },
        );
      }
      preflight = pf;
    }

    if (preflight) {
      const safeBaseUrl = await this.resolveSafeBaseUrl(preflight.base_url);
      return {
        preflight,
        accountId: preflight.account_id,
        ilinkBotId: preflight.ilink_bot_id,
        botGeneration: preflight.bot_generation,
        baseUrl: safeBaseUrl,
        cdnBaseUrl: (
          preflight.cdn_base_url ||
          this.opts.cdnBaseUrl ||
          ILINK_DEFAULT_CDN_BASE_URL
        ).trim(),
        botToken: preflight.bot_token,
        contextToken: preflight.weixin_context_token,
        toUserId: preflight.recipient_id,
      };
    }

    // Standalone / direct test mode without DeliveryService preflight
    const acc = this.resolveStandaloneAccount(options.weixinAccountId);
    if (!acc) {
      throw new ChannelDeliveryError("Weixin bot is not logged in", {
        outcome: "failed",
        code: "unavailable",
      });
    }
    if (acc.status === "expired") {
      throw new ChannelDeliveryError(
        "Weixin bot session has expired (-14); please re-scan QR code in settings.",
        {
          outcome: "failed",
          code: "unavailable",
        },
      );
    }
    if (!isPollableAccountStatus(acc.status) || !acc.botToken) {
      throw new ChannelDeliveryError("Weixin bot is not logged in", {
        outcome: "failed",
        code: "unavailable",
      });
    }
    const toUserId = normalizeRecipientUserId(
      options.targetAddress,
      options.recipientPlatformUserId,
    );
    if (!toUserId || toUserId === "@me") {
      throw new ChannelDeliveryError("Invalid Weixin recipient user ID", {
        outcome: "failed",
        code: "target_not_found",
      });
    }
    const contextToken = options.weixinContextToken?.trim() ?? "";
    if (!contextToken) {
      throw new ChannelDeliveryError(
        "Missing active Weixin context_token for recipient",
        {
          outcome: "failed",
          code: "unavailable",
        },
      );
    }
    return {
      preflight: null,
      accountId: acc.accountId,
      ilinkBotId: acc.ilinkBotId,
      botGeneration: acc.botGeneration,
      baseUrl: acc.baseUrl,
      cdnBaseUrl: acc.cdnBaseUrl,
      botToken: acc.botToken,
      contextToken,
      toUserId,
    };
  }

  async sendTextChunk(
    options: SendTextChunkOptions,
  ): Promise<SendTextChunkResult> {
    this.assertLeaseForDelivery("sendTextChunk");
    const initialAccountId =
      options.weixinPreflight?.account_id ?? options.weixinAccountId ?? null;
    const { signal, attachAccount, release } = this.createOperationSignal(
      initialAccountId,
      options.signal,
    );
    try {
      const creds = await this.resolvePreflightOrStandaloneCredentials({
        targetAddress: options.targetAddress,
        recipientPlatformUserId: options.recipientPlatformUserId,
        weixinContextToken: options.weixinContextToken,
        weixinPreflight: options.weixinPreflight,
        weixinAccountId: options.weixinAccountId,
        deliveryKey: options.deliveryKey,
        deliveryAttempt: options.deliveryAttempt,
        chunkIndex: options.chunkIndex,
        signal,
      });
      attachAccount(creds.accountId);
      if (creds.preflight) {
        this.assertPreflightMatchesSnapshot(creds.preflight);
      }
      this.assertLeaseForDelivery("sendTextChunk.dispatch");
      if (signal.aborted) {
        throw new ChannelDeliveryError("Weixin text send aborted", {
          outcome: "failed",
          code: "unavailable",
        });
      }
      return await sendWeixinTextMessage(
        {
          baseUrl: creds.baseUrl,
          cdnBaseUrl: creds.cdnBaseUrl,
          botToken: creds.botToken,
          contextToken: creds.contextToken,
          toUserId: creds.toUserId,
          signal,
          fetchImpl: this.opts.fetchImpl,
          dispatcher: this.httpPools.getControlDispatcher(),
          allowedApiDomains: this.opts.allowedApiDomains,
          allowedCdnDomains: this.opts.allowedCdnDomains,
          resolveHostIps: this.opts.resolveHostIps,
          checkResolvedIps: this.opts.checkResolvedIps,
          assertLeaseValid: () => this.assertLeaseForOutbound("sendTextChunk.http"),
          onSessionExpired: async (err) => {
            await this.markAccountSessionExpired(
              creds.accountId,
              creds.ilinkBotId,
              creds.botGeneration,
              err,
            );
          },
          onBusinessError: creds.preflight
            ? async (err) => {
                await this.maybeMarkContextTokenStale(creds.preflight!, err);
              }
            : undefined,
        },
        options.text,
      );
    } finally {
      release();
    }
  }

  async sendFile(options: SendFileOptions): Promise<SendFileResult> {
    this.assertLeaseForDelivery("sendFile");
    const initialAccountId =
      options.weixinPreflight?.account_id ?? options.weixinAccountId ?? null;
    const { signal, attachAccount, release } = this.createOperationSignal(
      initialAccountId,
      options.signal,
    );
    try {
      const creds = await this.resolvePreflightOrStandaloneCredentials({
        targetAddress: options.targetAddress,
        recipientPlatformUserId: options.recipientPlatformUserId,
        weixinContextToken: options.weixinContextToken,
        weixinPreflight: options.weixinPreflight,
        weixinAccountId: options.weixinAccountId,
        deliveryKey: options.deliveryKey,
        deliveryAttempt: options.deliveryAttempt,
        chunkIndex: 0,
        signal,
      });
      attachAccount(creds.accountId);
      if (creds.preflight) {
        this.assertPreflightMatchesSnapshot(creds.preflight);
      }
      this.assertLeaseForDelivery("sendFile.dispatch");
      if (signal.aborted) {
        throw new ChannelDeliveryError("Weixin file send aborted", {
          outcome: "failed",
          code: "unavailable",
        });
      }
      return await sendWeixinMediaAttachment(
        {
          baseUrl: creds.baseUrl,
          cdnBaseUrl: creds.cdnBaseUrl,
          botToken: creds.botToken,
          contextToken: creds.contextToken,
          toUserId: creds.toUserId,
          signal,
          fetchImpl: this.opts.fetchImpl,
          dispatcher: this.httpPools.getControlDispatcher(),
          allowedApiDomains: this.opts.allowedApiDomains,
          allowedCdnDomains: this.opts.allowedCdnDomains,
          resolveHostIps: this.opts.resolveHostIps,
          checkResolvedIps: this.opts.checkResolvedIps,
          assertLeaseValid: () => this.assertLeaseForOutbound("sendFile.http"),
          onSessionExpired: async (err) => {
            await this.markAccountSessionExpired(
              creds.accountId,
              creds.ilinkBotId,
              creds.botGeneration,
              err,
            );
          },
          onBusinessError: creds.preflight
            ? async (err) => {
                await this.maybeMarkContextTokenStale(creds.preflight!, err);
              }
            : undefined,
          uploadedBy: {
            accountId: creds.accountId,
            botId: creds.ilinkBotId,
            botGeneration: creds.botGeneration,
          },
          refreshAfterUpload: options.refreshWeixinPreflight
            ? async () => {
                this.assertLeaseForDelivery("sendFile.refreshAfterUpload");
                if (signal.aborted) {
                  throw new ChannelDeliveryError(
                    "Weixin file send aborted before sendmessage",
                    {
                      outcome: "failed",
                      code: "unavailable",
                    },
                  );
                }
                const refreshed = await options.refreshWeixinPreflight!();
                if (!refreshed.authorized) {
                  const isTransient =
                    refreshed.decision === "defer" || refreshed.retryable;
                  throw new ChannelDeliveryError(
                    refreshed.message ??
                      `Weixin preflight ${refreshed.decision} after upload: ${refreshed.deny_code}`,
                    {
                      outcome:
                        refreshed.deny_code === "source_generation_unknown"
                          ? "unknown"
                          : "failed",
                      code: isTransient ? "unavailable" : "not_allowed",
                      retryable: refreshed.retryable,
                      denyCode: refreshed.deny_code,
                    },
                  );
                }
                this.assertPreflightMatchesSnapshot(refreshed);
                const safeRefreshedBase = await this.resolveSafeBaseUrl(
                  refreshed.base_url,
                );
                this.assertLeaseForDelivery("sendFile.sendmessage");
                if (signal.aborted) {
                  throw new ChannelDeliveryError(
                    "Weixin file send aborted before sendmessage",
                    {
                      outcome: "failed",
                      code: "unavailable",
                    },
                  );
                }
                return {
                  accountId: refreshed.account_id,
                  contextToken: refreshed.weixin_context_token,
                  botToken: refreshed.bot_token,
                  botId: refreshed.ilink_bot_id,
                  botGeneration: refreshed.bot_generation,
                  baseUrl: safeRefreshedBase,
                };
              }
            : undefined,
        },
        {
          filename: options.filename,
          content: options.content,
          mimeType: options.mimeType,
        },
      );
    } finally {
      release();
    }
  }

  async downloadAttachment(
    descriptor: InboundAttachmentDescriptor,
    event: InboundChannelEvent,
    options?: { signal?: AbortSignal },
  ): Promise<DownloadedAttachment> {
    this.assertLeaseForOutbound("downloadAttachment");
    const account = this.resolveAccountForEvent(event);
    // R5 #1 (Codex High #2): Never fall back to a default CDN base URL when a matching active
    // account/generation cannot be resolved; abort immediately if the account rotated or vanished.
    const requiresMatchedAccount =
      Boolean(this.backend) ||
      Boolean(event.weixinAccountId?.trim()) ||
      Boolean(event.weixinBotId?.trim()) ||
      event.weixinBotGeneration !== undefined;
    if (!account && requiresMatchedAccount) {
      throw new Error(
        "Weixin account not found or bot generation superseded; aborting attachment download",
      );
    }
    if (account && !isPollableAccountStatus(account.status)) {
      throw new Error(
        account.status === "expired"
          ? "Weixin bot session has expired (-14)"
          : `Weixin account status "${account.status}" is not active`,
      );
    }
    const meta = resolvePersistedAttachmentMeta(descriptor, event);
    if (!meta) {
      throw new Error(
        `Weixin attachment metadata not found for id=${descriptor.id}`,
      );
    }

    const cdnBaseUrl = account
      ? account.cdnBaseUrl
      : (this.opts.cdnBaseUrl || ILINK_DEFAULT_CDN_BASE_URL).trim();

    const { signal, release } = this.createOperationSignal(
      account?.accountId ?? event.weixinAccountId,
      options?.signal,
    );
    try {
      if (signal.aborted) {
        throw new Error("Weixin attachment download aborted");
      }
      const downloaded = await downloadWeixinCdnAttachment(meta, {
        cdnBaseUrl,
        allowedCdnDomains: this.opts.allowedCdnDomains,
        resolveHostIps: this.opts.resolveHostIps,
        checkResolvedIps: this.opts.checkResolvedIps,
        signal,
        fetchImpl: this.opts.fetchImpl,
        dispatcher: this.httpPools.getControlDispatcher(),
        assertLeaseValid: () => this.assertLeaseForOutbound("downloadAttachment.http"),
      });
      return {
        content: downloaded.buffer,
        filename: downloaded.filename,
        mimeType: downloaded.mimeType,
      };
    } finally {
      release();
    }
  }

  async listAvailableChats(_options: {
    userId: string;
    platformUserId: string;
  }): Promise<AvailableChatsResult> {
    return {
      chats: [],
      complete: true,
      error: null,
    };
  }

  async checkTargetLive(options: {
    userId: string;
    platformUserId?: string | null;
    address: string;
    purpose: string;
  }): Promise<CheckTargetLiveResult> {
    const addr = options.address.trim();
    if (addr !== "weixin:@me") {
      return {
        ok: false,
        address: addr,
        message: "Personal WeChat supports direct messages (weixin:@me) only.",
      };
    }
    if (!this.isConnected()) {
      return {
        ok: false,
        address: addr,
        message: "WeChat bot is not currently connected.",
      };
    }
    const userHasManagedAccount = [...this.accounts.values()].some(
      (acc) =>
        isPollableAccountStatus(acc.status) &&
        (!acc.userId || acc.userId === options.userId),
    );
    if (this.accounts.size > 0 && !userHasManagedAccount) {
      return {
        ok: false,
        address: addr,
        message: "User WeChat account is not currently active.",
      };
    }
    return {
      ok: true,
      address: "weixin:@me",
      name: "WeChat DM",
      message: null,
    };
  }

  async startQrLogin(options: {
    userId: string;
    sessionKey?: string;
    loginVersion?: number;
    expectedBotGeneration?: number;
  }): Promise<WeixinQrStartResult> {
    this.assertLeaseForOutbound("startQrLogin");
    if (!this.qrManager) {
      throw new Error(
        "Weixin QR login manager is not initialized with a BackendClient",
      );
    }
    const { signal, release } = this.createOperationSignal();
    try {
      return await this.qrManager.startQrLogin(options.userId, {
        sessionKey: options.sessionKey,
        loginVersion: options.loginVersion,
        expectedBotGeneration: options.expectedBotGeneration,
        signal,
      });
    } finally {
      release();
    }
  }

  async waitQrLogin(options: {
    userId: string;
    sessionKey: string;
  }): Promise<WeixinQrWaitResult> {
    this.assertLeaseForOutbound("waitQrLogin");
    if (!this.qrManager) {
      throw new Error(
        "Weixin QR login manager is not initialized with a BackendClient",
      );
    }
    const { signal, release } = this.createOperationSignal();
    try {
      return await this.qrManager.waitQrLogin(
        options.userId,
        options.sessionKey,
        { signal },
      );
    } finally {
      release();
    }
  }

  async submitVerifyCode(options: {
    userId: string;
    sessionKey: string;
    code: string;
  }): Promise<WeixinQrWaitResult> {
    this.assertLeaseForOutbound("submitVerifyCode");
    if (!this.qrManager) {
      throw new Error(
        "Weixin QR login manager is not initialized with a BackendClient",
      );
    }
    const { signal, release } = this.createOperationSignal();
    try {
      return await this.qrManager.verifyQrCode(
        options.userId,
        options.sessionKey,
        options.code,
        { signal },
      );
    } finally {
      release();
    }
  }
}
