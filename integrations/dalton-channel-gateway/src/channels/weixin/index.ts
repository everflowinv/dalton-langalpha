// Portions adapted from @tencent-weixin/openclaw-weixin (MIT License, Copyright (c) 2026 Tencent).

import type {
  BackendClient,
  I9WeixinStateGetResponse,
} from "../../backend-client.js";
import type { HostIpResolver } from "../../shared/safe-fetch.js";
import type {
  InboundAttachmentDescriptor,
  InboundChannelEvent,
} from "../../types.js";
import {
  type AvailableChatsResult,
  type ChannelAdapter,
  ChannelDeliveryError,
  type CheckTargetLiveResult,
  type DownloadedAttachment,
  type SendFileOptions,
  type SendFileResult,
  type SendTextChunkOptions,
  type SendTextChunkResult,
  splitTextByLimit,
  type WeixinQrStartResult,
  type WeixinQrWaitResult,
} from "../adapter.js";
import {
  normalizeAndValidateIlinkBaseUrl,
  resolveValidatedRedirectBaseUrl,
  WeixinApiError,
} from "./api.js";
import { downloadWeixinCdnAttachment } from "./cdn.js";
import { resolvePersistedAttachmentMeta } from "./inbound.js";
import {
  type ConfirmedWeixinAccount,
  WeixinQrLoginManager,
} from "./login-qr.js";
import { WeixinMonitor } from "./monitor.js";
import {
  normalizeRecipientUserId,
  sendWeixinMediaAttachment,
  sendWeixinTextMessage,
} from "./send.js";
import {
  ILINK_DEFAULT_BASE_URL,
  ILINK_DEFAULT_BOT_TYPE,
  ILINK_DEFAULT_CDN_BASE_URL,
} from "./types.js";

export * from "./types.js";
export * from "./api.js";
export * from "./cdn.js";
export * from "./inbound.js";
export * from "./send.js";
export * from "./markdown-filter.js";
export * from "./login-qr.js";
export * from "./monitor.js";

export const WEIXIN_MAX_CHUNK_CHARS = 2_000;

export interface WeixinChannelAdapterOptions {
  backend?: BackendClient;
  baseUrl?: string;
  cdnBaseUrl?: string;
  botType?: string;
  botToken?: string;
  ilinkBotId?: string;
  botGeneration?: number;
  pollTimeoutMs?: number;
  retryDelayMs?: number;
  allowedApiDomains?: readonly string[];
  allowedCdnDomains?: readonly string[];
  resolveHostIps?: HostIpResolver;
  checkResolvedIps?: boolean;
  fetchImpl?: typeof fetch;
}

interface ActiveWeixinAccountState {
  ilinkBotId: string;
  botToken: string;
  baseUrl: string;
  cdnBaseUrl: string;
  botGeneration: number;
  getUpdatesBuf: string;
  status: "active" | "expired" | "unconfigured";
}

export class WeixinChannelAdapter implements ChannelAdapter {
  readonly platform = "weixin" as const;
  readonly maxChunkChars = WEIXIN_MAX_CHUNK_CHARS;

  private readonly opts: WeixinChannelAdapterOptions;
  private backend?: BackendClient;
  private account: ActiveWeixinAccountState;
  private started = false;
  private inboundHandler: ((event: InboundChannelEvent) => Promise<void>) | null =
    null;
  private monitorController: AbortController | null = null;
  private monitorTask: Promise<void> | null = null;
  private monitorGeneration = 0;
  private readonly inFlightControllers = new Set<AbortController>();
  private qrManager: WeixinQrLoginManager | null = null;

  constructor(opts: WeixinChannelAdapterOptions = {}) {
    this.opts = opts;
    this.backend = opts.backend;

    const initialBaseUrl = (opts.baseUrl || ILINK_DEFAULT_BASE_URL).trim();
    const initialCdnBaseUrl = (
      opts.cdnBaseUrl || ILINK_DEFAULT_CDN_BASE_URL
    ).trim();
    const initialToken = (opts.botToken || "").trim();
    const initialBotId = (
      opts.ilinkBotId || (initialToken ? "weixin:env_bot" : "")
    ).trim();

    this.account = {
      ilinkBotId: initialBotId,
      botToken: initialToken,
      baseUrl: initialBaseUrl,
      cdnBaseUrl: initialCdnBaseUrl,
      botGeneration: opts.botGeneration ?? 1,
      getUpdatesBuf: "",
      status: initialToken && initialBotId ? "active" : "unconfigured",
    };

    if (this.backend) {
      this.initQrManager(this.backend);
    }
  }

  setBackendClient(backend: BackendClient): void {
    this.backend = backend;
    this.initQrManager(backend);
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
      onAccountConfirmed: async (confirmed) => {
        await this.handleConfirmedAccount(confirmed);
      },
    });
  }

  isConfigured(): boolean {
    return (
      this.account.status !== "unconfigured" &&
      Boolean(this.account.ilinkBotId && this.account.botToken)
    );
  }

  isConnected(): boolean {
    return (
      this.started &&
      this.account.status === "active" &&
      Boolean(this.account.ilinkBotId && this.account.botToken)
    );
  }

  getAccountStatus(): {
    status: "active" | "expired" | "unconfigured";
    ilinkBotId: string;
    botGeneration: number;
    getUpdatesBuf: string;
  } {
    return {
      status: this.account.status,
      ilinkBotId: this.account.ilinkBotId,
      botGeneration: this.account.botGeneration,
      getUpdatesBuf: this.account.getUpdatesBuf,
    };
  }

  onInboundEvent(handler: (event: InboundChannelEvent) => Promise<void>): void {
    this.inboundHandler = handler;
  }

  splitTextChunks(text: string): string[] {
    return splitTextByLimit(text, this.maxChunkChars);
  }

  private createOperationSignal(): { signal: AbortSignal; release: () => void } {
    const controller = new AbortController();
    this.inFlightControllers.add(controller);
    return {
      signal: controller.signal,
      release: () => {
        this.inFlightControllers.delete(controller);
      },
    };
  }

  async start(): Promise<void> {
    this.started = true;

    if (this.backend) {
      try {
        const persisted = await this.backend.getWeixinState({
          include_token: true,
        });
        await this.applyPersistedState(persisted);
        return;
      } catch {
        // Fall through to current in-memory state if initial backend query fails.
      }
    }

    if (
      this.account.status === "active" &&
      this.account.botToken &&
      this.account.ilinkBotId
    ) {
      await this.ensureSingleMonitorLoop();
    }
  }

  async stop(): Promise<void> {
    this.started = false;
    await this.stopMonitorLoop();
    for (const ctrl of [...this.inFlightControllers]) {
      ctrl.abort();
    }
    this.inFlightControllers.clear();
  }

  private async stopMonitorLoop(): Promise<void> {
    const ctrl = this.monitorController;
    const task = this.monitorTask;
    this.monitorController = null;
    this.monitorTask = null;
    if (ctrl) {
      ctrl.abort();
    }
    if (task) {
      try {
        await task;
      } catch {
        // Ignore abort errors on monitor shutdown.
      }
    }
  }

  private async ensureSingleMonitorLoop(): Promise<void> {
    if (!this.started || !this.inboundHandler) {
      return;
    }
    if (
      this.account.status !== "active" ||
      !this.account.botToken ||
      !this.account.ilinkBotId ||
      !this.backend
    ) {
      await this.stopMonitorLoop();
      return;
    }

    await this.stopMonitorLoop();

    const controller = new AbortController();
    this.monitorController = controller;
    const capturedGen = this.account.botGeneration;
    const capturedBotId = this.account.ilinkBotId;
    this.monitorGeneration = capturedGen;

    const monitor = new WeixinMonitor({
      baseUrl: this.account.baseUrl,
      botToken: this.account.botToken,
      ilinkBotId: capturedBotId,
      botGeneration: capturedGen,
      initialGetUpdatesBuf: this.account.getUpdatesBuf,
      pollTimeoutMs: this.opts.pollTimeoutMs,
      retryDelayMs: this.opts.retryDelayMs,
      allowedApiDomains: this.opts.allowedApiDomains,
      resolveHostIps: this.opts.resolveHostIps,
      checkResolvedIps: this.opts.checkResolvedIps,
      fetchImpl: this.opts.fetchImpl,
      backend: this.backend,
      signal: controller.signal,
      onInboundEvent: async (ev) => {
        if (!this.inboundHandler) return;
        await this.inboundHandler(ev);
      },
      onCursorCommitted: (newCursor) => {
        if (
          this.account.botGeneration === capturedGen &&
          this.account.ilinkBotId === capturedBotId
        ) {
          this.account.getUpdatesBuf = newCursor;
        }
      },
      onSessionExpired: async () => {
        if (
          this.account.botGeneration === capturedGen &&
          this.account.ilinkBotId === capturedBotId
        ) {
          this.account.status = "expired";
        }
      },
    });

    this.monitorTask = monitor.start();
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

  private async handleConfirmedAccount(
    confirmed: ConfirmedWeixinAccount,
  ): Promise<void> {
    if (confirmed.botGeneration < this.account.botGeneration) {
      return;
    }
    await this.stopMonitorLoop();
    const safeBaseUrl = await this.resolveSafeBaseUrl(confirmed.baseUrl);
    this.account = {
      ilinkBotId: confirmed.ilinkBotId,
      botToken: confirmed.botToken,
      baseUrl: safeBaseUrl,
      cdnBaseUrl: (
        confirmed.cdnBaseUrl ||
        this.opts.cdnBaseUrl ||
        ILINK_DEFAULT_CDN_BASE_URL
      ).trim(),
      botGeneration: confirmed.botGeneration,
      getUpdatesBuf: "",
      status: "active",
    };
    await this.ensureSingleMonitorLoop();
  }

  /**
   * Synchronize in-memory adapter credentials and poll loop with Postgres state (I-9).
   * Never writes plaintext credentials to disk.
   */
  async applyPersistedState(state: I9WeixinStateGetResponse): Promise<void> {
    const incomingGen = state.bot_generation ?? 1;
    if (
      incomingGen < this.account.botGeneration &&
      this.account.status !== "unconfigured"
    ) {
      return;
    }

    if (!state.configured || !state.ilink_bot_id) {
      await this.stopMonitorLoop();
      this.account = {
        ...this.account,
        ilinkBotId: "",
        botToken: "",
        getUpdatesBuf: "",
        botGeneration: incomingGen,
        status: "unconfigured",
      };
      return;
    }

    const normalizedStatus =
      state.status === "expired"
        ? "expired"
        : state.status === "unconfigured"
          ? "unconfigured"
          : "active";

    if (normalizedStatus === "expired") {
      await this.stopMonitorLoop();
      this.account = {
        ...this.account,
        ilinkBotId: state.ilink_bot_id,
        botGeneration: incomingGen,
        getUpdatesBuf: state.get_updates_buf ?? this.account.getUpdatesBuf,
        status: "expired",
      };
      return;
    }

    const nextToken = (state.bot_token ?? this.account.botToken).trim();
    if (!nextToken) {
      await this.stopMonitorLoop();
      this.account = {
        ...this.account,
        ilinkBotId: state.ilink_bot_id,
        botGeneration: incomingGen,
        status: "unconfigured",
      };
      return;
    }

    const nextBaseUrl = await this.resolveSafeBaseUrl(state.base_url);
    const nextCdnBaseUrl = (
      state.cdn_base_url ||
      this.opts.cdnBaseUrl ||
      ILINK_DEFAULT_CDN_BASE_URL
    ).trim();
    const nextCursor = state.get_updates_buf ?? "";

    const needsMonitorRestart =
      this.account.status !== "active" ||
      this.account.botGeneration !== incomingGen ||
      this.account.ilinkBotId !== state.ilink_bot_id ||
      this.account.botToken !== nextToken ||
      this.account.baseUrl !== nextBaseUrl ||
      !this.monitorTask ||
      this.monitorGeneration !== incomingGen;

    this.account = {
      ilinkBotId: state.ilink_bot_id,
      botToken: nextToken,
      baseUrl: nextBaseUrl,
      cdnBaseUrl: nextCdnBaseUrl,
      botGeneration: incomingGen,
      getUpdatesBuf: nextCursor,
      status: "active",
    };

    if (needsMonitorRestart && this.started) {
      await this.ensureSingleMonitorLoop();
    }
  }

  private async markSessionExpiredFromSend(_err: WeixinApiError): Promise<void> {
    const capturedGen = this.account.botGeneration;
    const capturedBotId = this.account.ilinkBotId;
    this.account.status = "expired";
    await this.stopMonitorLoop();
    if (this.backend && capturedBotId) {
      try {
        await this.backend.putWeixinState({
          status: "expired",
          expected_bot_generation: capturedGen,
          expected_ilink_bot_id: capturedBotId,
        });
      } catch {
        // Best-effort backend status update.
      }
    }
  }

  private async resolveFreshContextToken(
    recipientUserId: string,
    fallbackToken?: string | null,
  ): Promise<string> {
    if (this.backend) {
      const state = await this.backend.getWeixinState({
        include_token: false,
        recipient_platform_user_id: recipientUserId,
      });
      if (state.status === "expired") {
        this.account.status = "expired";
        await this.stopMonitorLoop();
        throw new ChannelDeliveryError(
          "Weixin bot session has expired (-14); please re-scan QR code in settings.",
          {
            outcome: "failed",
            code: "unavailable",
          },
        );
      }
      if (
        typeof state.bot_generation === "number" &&
        state.bot_generation > this.account.botGeneration
      ) {
        const fullState = await this.backend.getWeixinState({
          include_token: true,
          recipient_platform_user_id: recipientUserId,
        });
        await this.applyPersistedState(fullState);
        if (fullState.weixin_context_token?.trim()) {
          return fullState.weixin_context_token.trim();
        }
        throw new ChannelDeliveryError(
          "Missing active Weixin context_token for current bot generation",
          {
            outcome: "failed",
            code: "unavailable",
          },
        );
      }
      if (state.weixin_context_token?.trim()) {
        return state.weixin_context_token.trim();
      }
      if (state.configured && state.weixin_context_token === null) {
        throw new ChannelDeliveryError(
          "Missing active Weixin context_token for recipient",
          {
            outcome: "failed",
            code: "unavailable",
          },
        );
      }
    }

    const trimmedFallback = fallbackToken?.trim();
    if (!trimmedFallback) {
      throw new ChannelDeliveryError(
        "Missing active Weixin context_token for recipient",
        {
          outcome: "failed",
          code: "unavailable",
        },
      );
    }
    return trimmedFallback;
  }

  async sendTextChunk(
    options: SendTextChunkOptions,
  ): Promise<SendTextChunkResult> {
    if (this.account.status === "expired") {
      throw new ChannelDeliveryError(
        "Weixin bot session has expired (-14); please re-scan QR code in settings.",
        {
          outcome: "failed",
          code: "unavailable",
        },
      );
    }
    if (this.account.status !== "active" || !this.account.botToken) {
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

    const contextToken = await this.resolveFreshContextToken(
      toUserId,
      options.weixinContextToken,
    );
    const { signal, release } = this.createOperationSignal();
    try {
      return await sendWeixinTextMessage(
        {
          baseUrl: this.account.baseUrl,
          cdnBaseUrl: this.account.cdnBaseUrl,
          botToken: this.account.botToken,
          contextToken,
          toUserId,
          signal,
          fetchImpl: this.opts.fetchImpl,
          allowedApiDomains: this.opts.allowedApiDomains,
          allowedCdnDomains: this.opts.allowedCdnDomains,
          resolveHostIps: this.opts.resolveHostIps,
          checkResolvedIps: this.opts.checkResolvedIps,
          onSessionExpired: async (err) => {
            await this.markSessionExpiredFromSend(err);
          },
        },
        options.text,
      );
    } finally {
      release();
    }
  }

  async sendFile(options: SendFileOptions): Promise<SendFileResult> {
    if (this.account.status === "expired") {
      throw new ChannelDeliveryError(
        "Weixin bot session has expired (-14); please re-scan QR code in settings.",
        {
          outcome: "failed",
          code: "unavailable",
        },
      );
    }
    if (this.account.status !== "active" || !this.account.botToken) {
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

    const contextToken = await this.resolveFreshContextToken(
      toUserId,
      options.weixinContextToken,
    );
    const { signal, release } = this.createOperationSignal();
    try {
      return await sendWeixinMediaAttachment(
        {
          baseUrl: this.account.baseUrl,
          cdnBaseUrl: this.account.cdnBaseUrl,
          botToken: this.account.botToken,
          contextToken,
          toUserId,
          signal,
          fetchImpl: this.opts.fetchImpl,
          allowedApiDomains: this.opts.allowedApiDomains,
          allowedCdnDomains: this.opts.allowedCdnDomains,
          resolveHostIps: this.opts.resolveHostIps,
          checkResolvedIps: this.opts.checkResolvedIps,
          onSessionExpired: async (err) => {
            await this.markSessionExpiredFromSend(err);
          },
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
  ): Promise<DownloadedAttachment> {
    if (this.account.status === "expired") {
      throw new Error("Weixin bot session has expired (-14)");
    }
    const meta = resolvePersistedAttachmentMeta(descriptor, event);
    if (!meta) {
      throw new Error(
        `Weixin attachment metadata not found for id=${descriptor.id}`,
      );
    }

    const { signal, release } = this.createOperationSignal();
    try {
      const downloaded = await downloadWeixinCdnAttachment(meta, {
        cdnBaseUrl: this.account.cdnBaseUrl,
        allowedCdnDomains: this.opts.allowedCdnDomains,
        resolveHostIps: this.opts.resolveHostIps,
        checkResolvedIps: this.opts.checkResolvedIps,
        signal,
        fetchImpl: this.opts.fetchImpl,
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
    // Weixin iLink Phase 1 supports direct messages only (`weixin:@me`), no group chats.
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
    return {
      ok: true,
      address: "weixin:@me",
      name: "WeChat DM",
      message: null,
    };
  }

  async startQrLogin(options: { userId: string }): Promise<WeixinQrStartResult> {
    if (!this.qrManager) {
      throw new Error(
        "Weixin QR login manager is not initialized with a BackendClient",
      );
    }
    const { signal, release } = this.createOperationSignal();
    try {
      return await this.qrManager.startQrLogin(options.userId, { signal });
    } finally {
      release();
    }
  }

  async waitQrLogin(options: {
    userId: string;
    sessionKey: string;
  }): Promise<WeixinQrWaitResult> {
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
