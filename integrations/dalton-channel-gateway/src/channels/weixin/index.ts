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
  private monitorDepth = 0;
  private accountEpoch = 0;
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

  private abortMonitorOnly(): void {
    const ctrl = this.monitorController;
    this.monitorController = null;
    ctrl?.abort();
  }

  private async stopMonitorLoop(): Promise<void> {
    const task = this.monitorTask;
    this.abortMonitorOnly();
    // The poll is awaiting this call stack. Abort it and let the loop exit on its own.
    if (this.monitorDepth > 0) {
      return;
    }
    this.monitorTask = null;
    if (task) {
      try {
        await task;
      } catch {
        // Ignore abort errors on monitor shutdown.
      }
    }
  }

  private async ensureSingleMonitorLoop(): Promise<void> {
    const epoch = this.accountEpoch;
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

    if (this.monitorDepth > 0 && this.monitorTask) {
      this.abortMonitorOnly();
      const detached = this.monitorTask;
      void detached.finally(() => {
        if (this.monitorTask !== detached) return;
        this.monitorTask = null;
        if (this.accountEpoch !== epoch || !this.started) return;
        void this.ensureSingleMonitorLoop();
      });
      return;
    }

    await this.stopMonitorLoop();
    if (this.accountEpoch !== epoch || !this.started || !this.inboundHandler) {
      return;
    }
    if (
      this.account.status !== "active" ||
      !this.account.botToken ||
      !this.account.ilinkBotId ||
      !this.backend
    ) {
      return;
    }

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
        this.monitorDepth += 1;
        try {
          await this.inboundHandler(ev);
        } finally {
          this.monitorDepth -= 1;
        }
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

  private cursorAfterAccountSwitch(
    sameAccount: boolean,
    incomingCursor: string,
  ): string {
    if (sameAccount) {
      return incomingCursor || this.account.getUpdatesBuf;
    }
    return incomingCursor;
  }

  private async handleConfirmedAccount(
    confirmed: ConfirmedWeixinAccount,
  ): Promise<void> {
    if (confirmed.botGeneration < this.account.botGeneration) {
      return;
    }
    const epoch = ++this.accountEpoch;
    const sameAccount =
      this.account.ilinkBotId === confirmed.ilinkBotId &&
      this.account.botGeneration === confirmed.botGeneration;
    const incomingCursor = confirmed.getUpdatesBuf ?? "";
    await this.stopMonitorLoop();
    if (this.accountEpoch !== epoch) return;
    const safeBaseUrl = await this.resolveSafeBaseUrl(confirmed.baseUrl);
    if (this.accountEpoch !== epoch) return;
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
      getUpdatesBuf: this.cursorAfterAccountSwitch(sameAccount, incomingCursor),
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
    const epoch = ++this.accountEpoch;
    const stillCurrent = (): boolean => this.accountEpoch === epoch;

    if (!state.configured || !state.ilink_bot_id) {
      await this.stopMonitorLoop();
      if (!stillCurrent()) return;
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
      if (!stillCurrent()) return;
      const sameAccount =
        this.account.ilinkBotId === state.ilink_bot_id &&
        this.account.botGeneration === incomingGen;
      this.account = {
        ...this.account,
        ilinkBotId: state.ilink_bot_id,
        botGeneration: incomingGen,
        getUpdatesBuf: this.cursorAfterAccountSwitch(
          sameAccount,
          state.get_updates_buf ?? "",
        ),
        status: "expired",
      };
      return;
    }

    const nextToken = (state.bot_token ?? this.account.botToken).trim();
    if (!nextToken) {
      await this.stopMonitorLoop();
      if (!stillCurrent()) return;
      this.account = {
        ...this.account,
        ilinkBotId: state.ilink_bot_id,
        botGeneration: incomingGen,
        status: "unconfigured",
      };
      return;
    }

    const nextBaseUrl = await this.resolveSafeBaseUrl(state.base_url);
    if (!stillCurrent()) return;
    const nextCdnBaseUrl = (
      state.cdn_base_url ||
      this.opts.cdnBaseUrl ||
      ILINK_DEFAULT_CDN_BASE_URL
    ).trim();
    const sameAccount =
      this.account.ilinkBotId === state.ilink_bot_id &&
      this.account.botGeneration === incomingGen;
    const nextCursor = this.cursorAfterAccountSwitch(
      sameAccount,
      state.get_updates_buf ?? "",
    );

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

  private async markSessionExpiredFromSend(
    capturedBotId: string,
    capturedGen: number,
  ): Promise<void> {
    if (
      this.account.ilinkBotId !== capturedBotId ||
      this.account.botGeneration !== capturedGen
    ) {
      return;
    }
    const epoch = this.accountEpoch;
    this.account.status = "expired";
    this.abortMonitorOnly();
    if (!this.backend || !capturedBotId) return;
    try {
      await this.backend.putWeixinState({
        status: "expired",
        expected_bot_generation: capturedGen,
        expected_ilink_bot_id: capturedBotId,
      });
    } catch {
      return;
    }
    if (
      this.accountEpoch !== epoch ||
      this.account.ilinkBotId !== capturedBotId ||
      this.account.botGeneration !== capturedGen
    ) {
      return;
    }
  }

  private async resolveFreshContextToken(
    recipientUserId: string,
    fallbackToken?: string | null,
    inboundMessageId?: string | null,
  ): Promise<string> {
    const missingToken = (message: string): ChannelDeliveryError =>
      new ChannelDeliveryError(message, {
        outcome: "failed",
        code: "unavailable",
      });

    if (!this.backend) {
      const trimmedFallback = fallbackToken?.trim();
      if (!trimmedFallback) {
        throw missingToken("Missing active Weixin context_token for recipient");
      }
      return trimmedFallback;
    }

    const state = await this.backend.getWeixinState({
      include_token: false,
      recipient_platform_user_id: recipientUserId,
      inbound_message_id: inboundMessageId ?? null,
    });
    if (state.status === "expired") {
      const stateBot = state.ilink_bot_id ?? "";
      const stateGen = state.bot_generation;
      const matchesLocal =
        (!stateBot || stateBot === this.account.ilinkBotId) &&
        (typeof stateGen !== "number" || stateGen === this.account.botGeneration);
      if (matchesLocal) {
        this.account.status = "expired";
        this.abortMonitorOnly();
      }
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
        inbound_message_id: inboundMessageId ?? null,
      });
      if (
        typeof fullState.bot_generation === "number" &&
        fullState.bot_generation > this.account.botGeneration
      ) {
        await this.applyPersistedState(fullState);
      }
      const switchedToken = fullState.weixin_context_token?.trim();
      if (
        switchedToken &&
        fullState.ilink_bot_id === this.account.ilinkBotId &&
        fullState.bot_generation === this.account.botGeneration
      ) {
        return switchedToken;
      }
      throw missingToken(
        "Missing active Weixin context_token for current bot generation",
      );
    }
    const currentToken = state.weixin_context_token?.trim();
    const stateBot = state.ilink_bot_id ?? "";
    const stateGen = state.bot_generation;
    const tokenMatchesAccount =
      (!stateBot || stateBot === this.account.ilinkBotId) &&
      (typeof stateGen !== "number" || stateGen === this.account.botGeneration);
    if (currentToken && tokenMatchesAccount) {
      return currentToken;
    }
    throw missingToken("Missing active Weixin context_token for recipient");
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
      options.inboundMessageId,
    );
    const capturedBotId = this.account.ilinkBotId;
    const capturedGen = this.account.botGeneration;
    const capturedBaseUrl = this.account.baseUrl;
    const capturedCdnBaseUrl = this.account.cdnBaseUrl;
    const capturedBotToken = this.account.botToken;
    const { signal, release } = this.createOperationSignal();
    try {
      return await sendWeixinTextMessage(
        {
          baseUrl: capturedBaseUrl,
          cdnBaseUrl: capturedCdnBaseUrl,
          botToken: capturedBotToken,
          contextToken,
          toUserId,
          signal,
          fetchImpl: this.opts.fetchImpl,
          allowedApiDomains: this.opts.allowedApiDomains,
          allowedCdnDomains: this.opts.allowedCdnDomains,
          resolveHostIps: this.opts.resolveHostIps,
          checkResolvedIps: this.opts.checkResolvedIps,
          onSessionExpired: async () => {
            await this.markSessionExpiredFromSend(capturedBotId, capturedGen);
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
      options.inboundMessageId,
    );
    const capturedBotId = this.account.ilinkBotId;
    const capturedGen = this.account.botGeneration;
    const capturedBaseUrl = this.account.baseUrl;
    const capturedCdnBaseUrl = this.account.cdnBaseUrl;
    const capturedBotToken = this.account.botToken;
    const { signal, release } = this.createOperationSignal();
    try {
      return await sendWeixinMediaAttachment(
        {
          baseUrl: capturedBaseUrl,
          cdnBaseUrl: capturedCdnBaseUrl,
          botToken: capturedBotToken,
          contextToken,
          toUserId,
          signal,
          fetchImpl: this.opts.fetchImpl,
          allowedApiDomains: this.opts.allowedApiDomains,
          allowedCdnDomains: this.opts.allowedCdnDomains,
          resolveHostIps: this.opts.resolveHostIps,
          checkResolvedIps: this.opts.checkResolvedIps,
          onSessionExpired: async () => {
            await this.markSessionExpiredFromSend(capturedBotId, capturedGen);
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
