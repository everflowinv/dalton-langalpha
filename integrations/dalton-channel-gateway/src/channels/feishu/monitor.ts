/**
 * Feishu WebSocket long-connection monitor (`Lark.WSClient` + `Lark.EventDispatcher`)
 * and `FeishuChannelAdapter` (`ChannelAdapter` implementation).
 *
 * Adapted from `.agy/ref_openclaw_channels/feishu/dist/.setup/monitor.account-UbN1RhlP.mjs`
 * (lines 858–872, 5215–5309, 5343–5565, 5622–5760, 6228–6317).
 * License: MIT, © OpenClaw contributors.
 *
 * Design §4.2 row 4 (`src/channels/feishu/monitor.ts`):
 * - Uses Feishu `WSClient` long connection only (no webhook callback server).
 * - Deduplicates incoming events via `MemoryDedupeGuard` (`src/shared/dedupe.ts`).
 * - Serializes per-chat inbound handling via `createSequentialQueue` (`src/shared/keyed-queue.ts`).
 * - Excludes `FeishuStreamingSession` (streaming cards) and typing reaction lifecycle.
 */

import type * as Lark from "@larksuiteoapi/node-sdk";
import {
  createMemoryDedupe,
  type MemoryDedupeGuard,
} from "../../shared/dedupe.js";
import {
  createSequentialQueue,
  type SequentialQueue,
} from "../../shared/keyed-queue.js";
import {
  createSubsystemLogger,
  type SubsystemLogger,
} from "../../shared/logger.js";
import {
  type InboundAttachmentDescriptor,
  type InboundChannelEvent,
  MAX_ATTACHMENT_BYTES,
} from "../../types.js";
import {
  type AvailableChatsResult,
  type ChannelAdapter,
  ChannelDeliveryError,
  type CheckTargetLiveResult,
  type DownloadedAttachment,
  type SendCardImageInput,
  type SendCardOptions,
  type SendCardResult,
  type SendFileOptions,
  type SendFileResult,
  type SendTextChunkOptions,
  type SendTextChunkResult,
} from "../adapter.js";
import {
  createEventDispatcher,
  createFeishuClient,
  createFeishuWSClient,
  type FeishuCredentials,
  type FeishuDomain,
  type FeishuWSClientOptions,
} from "./client.js";
import {
  assertFeishuChatMember,
  getChatInfo,
  listFeishuDirectoryGroupsDetailed,
  MAX_FEISHU_DIRECTORY_PAGES,
} from "./directory.js";
import {
  assertFeishuCardWithinEnvelope,
  chunkFeishuOutboundText,
  shouldUseFeishuCard,
} from "./format.js";
import {
  hasUnresolvedGroupMention,
  parseFeishuInboundEvent,
  resolveFeishuMessageDedupeKey,
} from "./inbound-parse.js";
import {
  downloadMessageResourceFeishu,
  sendMediaFeishu,
  uploadImageFeishu,
} from "./media.js";
import { probeFeishu } from "./probe.js";
import {
  getMessageFeishu,
  sendCardFeishu,
  sendMessageFeishu,
  sendStructuredCardFeishu,
} from "./send.js";
import {
  buildDaltonFeishuThreadScopeKey,
  normalizeFeishuTarget,
  resolveDaltonOutboundTarget,
  stripFeishuProviderPrefix,
} from "./targets.js";

export interface FeishuWSClientLike {
  start(params: { eventDispatcher: Lark.EventDispatcher }): Promise<void> | void;
  close?(params?: { force?: boolean }): void;
  getConnectionStatus?(): {
    state?: "idle" | "connecting" | "connected" | "reconnecting" | "failed";
    reconnectAttempts?: number;
  };
}

export interface MonitorFeishuWebSocketOptions {
  creds: FeishuCredentials;
  botOpenId?: string;
  resolveBotOpenId?: () => Promise<string | undefined>;
  client?: Lark.Client;
  onEvent: (event: InboundChannelEvent) => Promise<void>;
  dedupe?: MemoryDedupeGuard;
  queue?: SequentialQueue;
  logger?: SubsystemLogger;
  wsClientFactory?: (creds: FeishuWSClientOptions) => FeishuWSClientLike;
  eventDispatcherFactory?: (
    creds: Pick<FeishuCredentials, "encryptKey" | "verificationToken">,
  ) => Lark.EventDispatcher;
}

export interface MonitorFeishuWebSocketHandle {
  wsClient: FeishuWSClientLike;
  eventDispatcher: Lark.EventDispatcher;
  isConnected(): boolean;
  dispatchRawEvent(rawPayload: unknown): Promise<void>;
  stop(): Promise<void>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function extractRawMessageMeta(rawPayload: unknown): {
  chatType?: string;
  messageType?: string;
} {
  if (!isRecord(rawPayload)) {
    return {};
  }
  const eventObj = isRecord(rawPayload.event) ? rawPayload.event : rawPayload;
  const messageObj = isRecord(eventObj.message) ? eventObj.message : undefined;
  const chatType =
    typeof messageObj?.chat_type === "string"
      ? messageObj.chat_type.trim().toLowerCase()
      : undefined;
  const messageType =
    typeof messageObj?.message_type === "string"
      ? messageObj.message_type.trim().toLowerCase()
      : typeof messageObj?.msg_type === "string"
        ? messageObj.msg_type.trim().toLowerCase()
        : undefined;
  return { chatType, messageType };
}

function extractRawQueueKey(rawPayload: unknown): string {
  if (!isRecord(rawPayload)) {
    return "__default__";
  }
  const eventObj = isRecord(rawPayload.event) ? rawPayload.event : rawPayload;
  const messageObj = isRecord(eventObj.message) ? eventObj.message : undefined;
  const senderObj = isRecord(eventObj.sender) ? eventObj.sender : undefined;
  const senderIdObj = isRecord(senderObj?.sender_id)
    ? senderObj.sender_id
    : undefined;
  const chatId =
    typeof messageObj?.chat_id === "string" ? messageObj.chat_id.trim() : "";
  const senderOpenId =
    (typeof senderIdObj?.open_id === "string" && senderIdObj.open_id.trim()) ||
    (typeof senderIdObj?.user_id === "string" && senderIdObj.user_id.trim()) ||
    "";
  const messageId =
    typeof messageObj?.message_id === "string"
      ? messageObj.message_id.trim()
      : "";
  if (!chatId || !senderOpenId) {
    return messageId ? `raw:${messageId}` : "__default__";
  }
  const rawChatType =
    typeof messageObj?.chat_type === "string"
      ? messageObj.chat_type.trim().toLowerCase()
      : "p2p";
  const threadId =
    typeof messageObj?.thread_id === "string" ? messageObj.thread_id : undefined;
  return buildDaltonFeishuThreadScopeKey({
    chatType:
      rawChatType === "p2p" || rawChatType === "private" || rawChatType === "dm"
        ? "p2p"
        : "group",
    chatId,
    senderOpenId,
    threadId,
  });
}

export async function monitorWebSocket(
  options: MonitorFeishuWebSocketOptions,
): Promise<MonitorFeishuWebSocketHandle> {
  const logger = options.logger ?? createSubsystemLogger("feishu/monitor");
  const dedupe = options.dedupe ?? createMemoryDedupe();
  const queue = options.queue ?? createSequentialQueue();
  let currentBotOpenId = options.botOpenId?.trim() || undefined;
  let wsConnected = false;
  let stopped = false;

  const wsClient = (options.wsClientFactory ?? createFeishuWSClient)({
    ...options.creds,
    logger,
    onReady: () => {
      if (!stopped) {
        wsConnected = true;
      }
    },
    onReconnected: () => {
      if (!stopped) {
        wsConnected = true;
      }
    },
    onReconnecting: () => {
      wsConnected = false;
    },
    onError: () => {
      wsConnected = false;
    },
  });
  const eventDispatcher = (
    options.eventDispatcherFactory ?? createEventDispatcher
  )({
    encryptKey: options.creds.encryptKey,
    verificationToken: options.creds.verificationToken,
  });

  const dispatchRawEvent = async (rawPayload: unknown): Promise<void> => {
    const dedupeKey = resolveFeishuMessageDedupeKey(
      rawPayload,
      options.creds.accountId,
    );
    if (dedupeKey && dedupe.peek(dedupeKey)) {
      logger.debug("Skipping duplicate Feishu inbound message", { dedupeKey });
      return;
    }

    const queueKey = extractRawQueueKey(rawPayload);
    await queue.enqueue(queueKey, async () => {
      if (dedupeKey && dedupe.peek(dedupeKey)) {
        return;
      }
      const { chatType, messageType } = extractRawMessageMeta(rawPayload);
      const isGroupChat =
        chatType !== undefined &&
        chatType !== "p2p" &&
        chatType !== "private" &&
        chatType !== "dm";
      if (!currentBotOpenId && isGroupChat && options.resolveBotOpenId) {
        currentBotOpenId = await options.resolveBotOpenId();
      }
      const parsed = parseFeishuInboundEvent(rawPayload, {
        botOpenId: currentBotOpenId,
      });
      if (!parsed) {
        return;
      }
      if (hasUnresolvedGroupMention(rawPayload, currentBotOpenId)) {
        if (dedupeKey) {
          dedupe.delete(dedupeKey);
        }
        throw new Error(
          "Feishu bot identity is not resolved; a group message with structured mentions cannot be classified yet",
        );
      }
      if (
        messageType === "merge_forward" &&
        options.client &&
        parsed.text === "[Merged and Forwarded Message]"
      ) {
        const fetched = await getMessageFeishu(options.client, {
          messageId: parsed.messageId,
        });
        if (fetched?.content) {
          parsed.text = fetched.content;
        }
      }

      try {
        await options.onEvent(parsed);
        if (dedupeKey) {
          dedupe.record(dedupeKey);
        }
      } catch (err) {
        if (dedupeKey) {
          dedupe.delete(dedupeKey);
        }
        logger.error("Feishu inbound handler error", {
          messageId: parsed.messageId,
          threadScopeKey: parsed.threadScopeKey,
          error: err,
        });
        throw err;
      }
    });
  };

  eventDispatcher.register({
    "im.message.receive_v1": async (data: unknown) => {
      await dispatchRawEvent(data);
    },
  });

  await wsClient.start({ eventDispatcher });
  wsConnected = true;

  return {
    wsClient,
    eventDispatcher,
    isConnected: () => {
      if (stopped) {
        return false;
      }
      if (typeof wsClient.getConnectionStatus === "function") {
        const status = wsClient.getConnectionStatus();
        if (status && typeof status.state === "string") {
          return status.state === "connected";
        }
      }
      return wsConnected;
    },
    dispatchRawEvent,
    stop: async () => {
      stopped = true;
      wsConnected = false;
      try {
        wsClient.close?.({ force: true });
      } catch (err) {
        logger.warn("Error closing Feishu WSClient", { error: err });
      }
    },
  };
}

export interface FeishuChannelAdapterOptions {
  appId?: string;
  appSecret?: string;
  domain?: FeishuDomain;
  encryptKey?: string;
  verificationToken?: string;
  botOpenId?: string;
  outgoingOnly?: boolean;
  maxChunkChars?: number;
  client?: Lark.Client;
  clientFactory?: (creds: FeishuCredentials) => Lark.Client;
  wsClientFactory?: (creds: FeishuWSClientOptions) => FeishuWSClientLike;
  eventDispatcherFactory?: (
    creds: Pick<FeishuCredentials, "encryptKey" | "verificationToken">,
  ) => Lark.EventDispatcher;
  dedupeTtlMs?: number;
  queueTaskTimeoutMs?: number;
  logger?: SubsystemLogger;
}

function collectFeishuCardPlaceholderPaths(node: unknown, out: string[]): void {
  if (!node || typeof node !== "object") {
    return;
  }
  if (Array.isArray(node)) {
    for (const item of node) {
      collectFeishuCardPlaceholderPaths(item, out);
    }
    return;
  }
  const rec = node as Record<string, unknown>;
  if (rec.tag === "__img__") {
    out.push(typeof rec.path === "string" ? rec.path.trim() : "");
    return;
  }
  for (const val of Object.values(rec)) {
    collectFeishuCardPlaceholderPaths(val, out);
  }
}

function replaceFeishuCardPlaceholdersInPlace(
  node: unknown,
  uploadedKeys: ReadonlyMap<string, string>,
): unknown {
  if (!node || typeof node !== "object") {
    return node;
  }
  if (Array.isArray(node)) {
    for (let i = 0; i < node.length; i += 1) {
      node[i] = replaceFeishuCardPlaceholdersInPlace(node[i], uploadedKeys);
    }
    return node;
  }
  const rec = node as Record<string, unknown>;
  if (rec.tag === "__img__") {
    const ph = typeof rec.path === "string" ? rec.path.trim() : "";
    const imageKey = uploadedKeys.get(ph);
    if (!ph || !imageKey) {
      throw new ChannelDeliveryError(
        `Unresolved __img__ placeholder in Feishu Card 2.0: "${ph}"`,
        { outcome: "failed", code: "not_allowed" },
      );
    }
    const rawAlt = rec.alt;
    const altObj =
      rawAlt && typeof rawAlt === "object" && !Array.isArray(rawAlt)
        ? structuredClone(rawAlt as Record<string, unknown>)
        : {
            tag: "plain_text",
            content: typeof rawAlt === "string" ? rawAlt : "",
          };
    return {
      tag: "img",
      img_key: imageKey,
      alt: altObj,
      mode: "fit_horizontal",
      preview: true,
    };
  }
  for (const [k, v] of Object.entries(rec)) {
    if (v && typeof v === "object") {
      rec[k] = replaceFeishuCardPlaceholdersInPlace(v, uploadedKeys);
    }
  }
  return rec;
}

export class FeishuChannelAdapter implements ChannelAdapter {
  readonly platform = "feishu" as const;
  readonly maxChunkChars: number;

  private readonly appId: string;
  private readonly appSecret: string;
  private readonly domain?: FeishuDomain;
  private readonly encryptKey?: string;
  private readonly verificationToken?: string;
  private readonly outgoingOnly: boolean;
  private botOpenId?: string;
  private clientInstance?: Lark.Client;
  private readonly clientFactory?: (creds: FeishuCredentials) => Lark.Client;
  private readonly wsClientFactory?: (
    creds: FeishuWSClientOptions,
  ) => FeishuWSClientLike;
  private readonly eventDispatcherFactory?: (
    creds: Pick<FeishuCredentials, "encryptKey" | "verificationToken">,
  ) => Lark.EventDispatcher;
  private readonly dedupe: MemoryDedupeGuard;
  private readonly queue: SequentialQueue;
  private readonly logger: SubsystemLogger;
  private readonly handlers: Array<(event: InboundChannelEvent) => Promise<void>> =
    [];
  private monitorHandle: MonitorFeishuWebSocketHandle | null = null;
  private connected = false;

  constructor(options: FeishuChannelAdapterOptions = {}) {
    this.appId = options.appId?.trim() ?? "";
    this.appSecret = options.appSecret?.trim() ?? "";
    this.domain = options.domain;
    this.encryptKey = options.encryptKey;
    this.verificationToken = options.verificationToken;
    this.outgoingOnly = options.outgoingOnly ?? false;
    this.botOpenId = options.botOpenId?.trim() || undefined;
    this.maxChunkChars = options.maxChunkChars ?? 4000;
    this.clientInstance = options.client;
    this.clientFactory = options.clientFactory;
    this.wsClientFactory = options.wsClientFactory;
    this.eventDispatcherFactory = options.eventDispatcherFactory;
    this.logger = options.logger ?? createSubsystemLogger("feishu");
    this.dedupe = createMemoryDedupe({ ttlMs: options.dedupeTtlMs });
    this.queue = createSequentialQueue({
      taskTimeoutMs: options.queueTaskTimeoutMs,
    });
  }

  isConfigured(): boolean {
    return Boolean(this.appId && this.appSecret);
  }

  isConnected(): boolean {
    if (!this.connected || !this.monitorHandle) {
      return false;
    }
    return this.monitorHandle.isConnected();
  }

  isReadyForOutbound(): boolean {
    if (!this.isConfigured()) {
      return false;
    }
    return this.outgoingOnly ? true : this.isConnected();
  }

  getBotOpenId(): string | undefined {
    return this.botOpenId;
  }

  private getClient(): Lark.Client {
    if (this.clientInstance) {
      return this.clientInstance;
    }
    if (!this.isConfigured()) {
      throw new ChannelDeliveryError("Feishu adapter is not configured", {
        outcome: "failed",
        code: "unavailable",
      });
    }
    const creds: FeishuCredentials = {
      appId: this.appId,
      appSecret: this.appSecret,
      domain: this.domain,
      encryptKey: this.encryptKey,
      verificationToken: this.verificationToken,
      logger: this.logger,
    };
    this.clientInstance = this.clientFactory
      ? this.clientFactory(creds)
      : createFeishuClient(creds);
    return this.clientInstance;
  }

  private async ensureBotOpenId(): Promise<string | undefined> {
    if (this.botOpenId) {
      return this.botOpenId;
    }
    if (!this.isConfigured()) {
      return undefined;
    }
    const creds: FeishuCredentials = {
      appId: this.appId,
      appSecret: this.appSecret,
      domain: this.domain,
      encryptKey: this.encryptKey,
      verificationToken: this.verificationToken,
      logger: this.logger,
    };
    const probe = await probeFeishu(creds, {
      client: this.clientInstance,
      skipCache: true,
      forceRefresh: true,
    });
    if (probe.ok && probe.botOpenId) {
      this.botOpenId = probe.botOpenId;
      return this.botOpenId;
    }
    if (!probe.ok) {
      this.logger.warn("Feishu bot probe did not return botOpenId", {
        error: probe.error,
      });
    }
    return undefined;
  }

  async start(): Promise<void> {
    if (!this.isConfigured() || this.outgoingOnly) {
      return;
    }
    if (this.connected) {
      return;
    }

    const creds: FeishuCredentials = {
      appId: this.appId,
      appSecret: this.appSecret,
      domain: this.domain,
      encryptKey: this.encryptKey,
      verificationToken: this.verificationToken,
      logger: this.logger,
    };

    if (!this.botOpenId) {
      await this.ensureBotOpenId();
    }

    this.monitorHandle = await monitorWebSocket({
      creds,
      botOpenId: this.botOpenId,
      resolveBotOpenId: () => this.ensureBotOpenId(),
      client: this.clientInstance ?? (this.isConfigured() ? this.getClient() : undefined),
      dedupe: this.dedupe,
      queue: this.queue,
      logger: this.logger,
      wsClientFactory: this.wsClientFactory,
      eventDispatcherFactory: this.eventDispatcherFactory,
      onEvent: async (event) => {
        for (const handler of this.handlers) {
          await handler(event);
        }
      },
    });

    this.connected = true;
  }

  async stop(): Promise<void> {
    if (this.monitorHandle) {
      await this.monitorHandle.stop();
      this.monitorHandle = null;
    }
    this.connected = false;
  }

  onInboundEvent(handler: (event: InboundChannelEvent) => Promise<void>): void {
    this.handlers.push(handler);
  }

  /**
   * Dispatches a raw `im.message.receive_v1` event payload through the
   * adapter's deduplication, parsing, and per-chat sequential queue pipeline.
   */
  async receiveRawEvent(rawPayload: unknown): Promise<void> {
    if (this.monitorHandle) {
      await this.monitorHandle.dispatchRawEvent(rawPayload);
      return;
    }
    const dedupeKey = resolveFeishuMessageDedupeKey(rawPayload);
    if (dedupeKey && this.dedupe.peek(dedupeKey)) {
      return;
    }

    const queueKey = extractRawQueueKey(rawPayload);
    await this.queue.enqueue(queueKey, async () => {
      if (dedupeKey && this.dedupe.peek(dedupeKey)) {
        return;
      }
      const { chatType, messageType } = extractRawMessageMeta(rawPayload);
      const isGroupChat =
        chatType !== undefined &&
        chatType !== "p2p" &&
        chatType !== "private" &&
        chatType !== "dm";
      if (!this.botOpenId && isGroupChat) {
        await this.ensureBotOpenId();
      }
      const parsed = parseFeishuInboundEvent(rawPayload, {
        botOpenId: this.botOpenId,
      });
      if (!parsed) {
        return;
      }
      if (hasUnresolvedGroupMention(rawPayload, this.botOpenId)) {
        if (dedupeKey) {
          this.dedupe.delete(dedupeKey);
        }
        throw new Error(
          "Feishu bot identity is not resolved; a group message with structured mentions cannot be classified yet",
        );
      }
      if (
        messageType === "merge_forward" &&
        this.clientInstance &&
        parsed.text === "[Merged and Forwarded Message]"
      ) {
        const fetched = await getMessageFeishu(this.clientInstance, {
          messageId: parsed.messageId,
        });
        if (fetched?.content) {
          parsed.text = fetched.content;
        }
      }

      try {
        for (const handler of this.handlers) {
          await handler(parsed);
        }
        if (dedupeKey) {
          this.dedupe.record(dedupeKey);
        }
      } catch (err) {
        if (dedupeKey) {
          this.dedupe.delete(dedupeKey);
        }
        this.logger.error("Feishu inbound handler error", {
          messageId: parsed.messageId,
          threadScopeKey: parsed.threadScopeKey,
          error: err,
        });
        throw err;
      }
    });
  }

  splitTextChunks(text: string): string[] {
    if (!text) {
      return [];
    }
    return chunkFeishuOutboundText(text);
  }

  async sendTextChunk(
    options: SendTextChunkOptions,
  ): Promise<SendTextChunkResult> {
    const client = this.getClient();
    const target = resolveDaltonOutboundTarget({
      targetAddress: options.targetAddress,
      recipientPlatformUserId: options.recipientPlatformUserId,
    });
    if (!target) {
      throw new ChannelDeliveryError(
        `Unable to resolve Feishu target for address "${options.targetAddress}"`,
        {
          outcome: "failed",
          code: "target_not_found",
        },
      );
    }

    if (shouldUseFeishuCard(options.text)) {
      const res = await sendStructuredCardFeishu(client, {
        to: target,
        text: options.text,
        replyToMessageId: options.replyToMessageId,
      });
      return {
        messageId: res.messageId,
        resolvedAddress: options.targetAddress,
      };
    }

    const res = await sendMessageFeishu(client, {
      to: target,
      text: options.text,
      replyToMessageId: options.replyToMessageId,
    });
    return {
      messageId: res.messageId,
      resolvedAddress: options.targetAddress,
    };
  }

  async sendFile(options: SendFileOptions): Promise<SendFileResult> {
    const client = this.getClient();
    const target = resolveDaltonOutboundTarget({
      targetAddress: options.targetAddress,
      recipientPlatformUserId: options.recipientPlatformUserId,
    });
    if (!target) {
      throw new ChannelDeliveryError(
        `Unable to resolve Feishu target for address "${options.targetAddress}"`,
        {
          outcome: "failed",
          code: "target_not_found",
        },
      );
    }

    const res = await sendMediaFeishu(client, {
      to: target,
      buffer: options.content,
      fileName: options.filename,
      mimeType: options.mimeType,
    });

    return {
      messageId: res.messageId,
      status: "sent",
      reason: null,
    };
  }

  async sendCard(options: SendCardOptions): Promise<SendCardResult> {
    const client = this.getClient();
    const target = resolveDaltonOutboundTarget({
      targetAddress: options.targetAddress,
      recipientPlatformUserId: options.recipientPlatformUserId,
    });
    if (!target) {
      throw new ChannelDeliveryError(
        `Unable to resolve Feishu target for address "${options.targetAddress}"`,
        {
          outcome: "failed",
          code: "target_not_found",
        },
      );
    }

    const resolvedCard = structuredClone(options.card);
    const imagesByPlaceholder = new Map<string, SendCardImageInput>();
    for (const img of options.images ?? []) {
      const ph = img.placeholder.trim();
      if (!ph) {
        throw new ChannelDeliveryError(
          "Feishu card image input cannot have an empty placeholder",
          { outcome: "failed", code: "not_allowed" },
        );
      }
      imagesByPlaceholder.set(ph, img);
    }

    const foundPlaceholders: string[] = [];
    collectFeishuCardPlaceholderPaths(resolvedCard, foundPlaceholders);
    const uniquePlaceholders = new Set<string>();
    for (const ph of foundPlaceholders) {
      if (!ph || !imagesByPlaceholder.has(ph)) {
        throw new ChannelDeliveryError(
          `Unresolved __img__ placeholder in Feishu Card 2.0: "${ph}"`,
          { outcome: "failed", code: "not_allowed" },
        );
      }
      uniquePlaceholders.add(ph);
    }
    if (uniquePlaceholders.size !== imagesByPlaceholder.size) {
      throw new ChannelDeliveryError(
        "Feishu Card 2.0 image inputs contain unreferenced placeholders",
        { outcome: "failed", code: "not_allowed" },
      );
    }

    const uploadedKeys = new Map<string, string>();
    for (const ph of uniquePlaceholders) {
      const imgInput = imagesByPlaceholder.get(ph)!;
      const uploaded = await uploadImageFeishu(client, {
        image: imgInput.content,
        imageType: "message",
      });
      uploadedKeys.set(ph, uploaded.imageKey);
    }

    replaceFeishuCardPlaceholdersInPlace(resolvedCard, uploadedKeys);

    const remainingPlaceholders: string[] = [];
    collectFeishuCardPlaceholderPaths(resolvedCard, remainingPlaceholders);
    if (remainingPlaceholders.length > 0) {
      throw new ChannelDeliveryError(
        "Unresolved __img__ placeholder remained after image upload",
        { outcome: "failed", code: "not_allowed" },
      );
    }

    try {
      assertFeishuCardWithinEnvelope(resolvedCard, "Feishu outbound card");
    } catch (err) {
      throw new ChannelDeliveryError(
        err instanceof Error ? err.message : String(err),
        { outcome: "failed", code: "not_allowed" },
      );
    }

    const res = await sendCardFeishu(client, {
      to: target,
      card: resolvedCard,
      replyToMessageId: options.replyToMessageId,
    });
    return {
      messageId: res.messageId,
      resolvedAddress: options.targetAddress,
    };
  }

  async downloadAttachment(
    descriptor: InboundAttachmentDescriptor,
    event: InboundChannelEvent,
  ): Promise<DownloadedAttachment> {
    const client = this.getClient();
    const rawMsg =
      event.rawPayload &&
      typeof event.rawPayload === "object" &&
      "message" in event.rawPayload &&
      event.rawPayload.message &&
      typeof event.rawPayload.message === "object"
        ? (event.rawPayload.message as {
            message_type?: string;
            msg_type?: string;
          })
        : undefined;
    const rawMsgType = (rawMsg?.message_type ?? rawMsg?.msg_type ?? "")
      .trim()
      .toLowerCase();
    const resourceType: "image" | "file" =
      rawMsgType === "file" ||
      rawMsgType === "audio" ||
      rawMsgType === "media" ||
      rawMsgType === "video" ||
      rawMsgType === "sticker"
        ? "file"
        : rawMsgType === "image" ||
            descriptor.mimeType.toLowerCase().startsWith("image/")
          ? "image"
          : "file";

    const downloaded = await downloadMessageResourceFeishu(client, {
      messageId: event.messageId,
      fileKey: descriptor.id,
      type: resourceType,
      maxBytes: MAX_ATTACHMENT_BYTES,
      originalFilename: descriptor.filename,
      originalMimeType: descriptor.mimeType,
    });
    return {
      content: downloaded.buffer,
      filename: downloaded.filename,
      mimeType: downloaded.mimeType,
    };
  }

  async listAvailableChats(_params: {
    userId: string;
    platformUserId: string;
  }): Promise<AvailableChatsResult> {
    const client = this.getClient();
    const listing = await listFeishuDirectoryGroupsDetailed(client);
    return {
      chats: listing.groups.map((group) => ({
        address: `feishu:${group.id}`,
        name: group.name || group.id,
        kind: "group" as const,
      })),
      complete: listing.complete,
      error: listing.error,
    };
  }

  async checkTargetLive(params: {
    userId: string;
    platformUserId?: string | null;
    address: string;
    purpose: string;
  }): Promise<CheckTargetLiveResult> {
    const normalized = normalizeFeishuTarget(params.address);
    if (!normalized) {
      return {
        ok: false,
        address: null,
        message: "Target address cannot be empty.",
      };
    }

    if (normalized === "@me") {
      return {
        ok: true,
        address: "feishu:@me",
        name: "Direct Message",
        message: null,
      };
    }

    const chatId = stripFeishuProviderPrefix(normalized);
    if (!chatId.startsWith("oc_")) {
      return {
        ok: false,
        address: null,
        message: `Unsupported Feishu target address "${params.address}". Expected "feishu:@me" or "feishu:oc_...".`,
      };
    }

    const client = this.getClient();
    const senderOpenId = params.platformUserId?.trim();
    if (params.purpose === "data_task_notification" || !senderOpenId) {
      try {
        const listing = await listFeishuDirectoryGroupsDetailed(client, {
          limit: MAX_FEISHU_DIRECTORY_PAGES * 100,
          pageSize: 100,
          maxPages: MAX_FEISHU_DIRECTORY_PAGES,
        });
        if (!listing.complete || listing.error !== null) {
          return {
            ok: false,
            address: null,
            code: "unavailable",
            message:
              listing.error ??
              "Feishu bot group enumeration was incomplete; refusing unconfirmed group access.",
          };
        }
        const matchedGroup = listing.groups.find(
          (group) => group.id === chatId,
        );
        if (!matchedGroup) {
          return {
            ok: false,
            address: null,
            code: "not_allowed",
            message: `Dedicated Feishu bot is not a member of group ${chatId}.`,
          };
        }
        const chatInfo = await getChatInfo(client, chatId);
        return {
          ok: true,
          address: `feishu:${chatId}`,
          name: chatInfo.name || matchedGroup.name || chatId,
          message: null,
        };
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        const isNotAllowed =
          /permission denied|not a member|not exist|not found|no permission/i.test(
            msg,
          );
        return {
          ok: false,
          address: null,
          code: isNotAllowed ? "not_allowed" : "unavailable",
          message: msg,
        };
      }
    }

    try {
      const chatInfo = await getChatInfo(client, chatId);
      await assertFeishuChatMember(client, chatId, senderOpenId);
      return {
        ok: true,
        address: `feishu:${chatId}`,
        name: chatInfo.name || chatId,
        message: null,
      };
    } catch (err) {
      return {
        ok: false,
        address: null,
        message: err instanceof Error ? err.message : String(err),
      };
    }
  }
}
