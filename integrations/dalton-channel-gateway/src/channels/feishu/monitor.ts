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
} from "./client.js";
import {
  assertFeishuChatMember,
  getChatInfo,
  listFeishuDirectoryGroupsLive,
} from "./directory.js";
import {
  chunkFeishuCardMarkdown,
  chunkFeishuOutboundText,
  shouldUseFeishuCard,
  withinCardTableLimit,
} from "./format.js";
import {
  parseFeishuInboundEvent,
  resolveFeishuMessageDedupeKey,
} from "./inbound-parse.js";
import { downloadMessageResourceFeishu, sendMediaFeishu } from "./media.js";
import { probeFeishu } from "./probe.js";
import { sendMessageFeishu, sendStructuredCardFeishu } from "./send.js";
import {
  normalizeFeishuTarget,
  resolveDaltonOutboundTarget,
  stripFeishuProviderPrefix,
} from "./targets.js";

export interface FeishuWSClientLike {
  start(params: { eventDispatcher: Lark.EventDispatcher }): Promise<void> | void;
  close?(params?: { force?: boolean }): void;
}

export interface MonitorFeishuWebSocketOptions {
  creds: FeishuCredentials;
  botOpenId?: string;
  onEvent: (event: InboundChannelEvent) => Promise<void>;
  dedupe?: MemoryDedupeGuard;
  queue?: SequentialQueue;
  logger?: SubsystemLogger;
  wsClientFactory?: (creds: FeishuCredentials) => FeishuWSClientLike;
  eventDispatcherFactory?: (
    creds: Pick<FeishuCredentials, "encryptKey" | "verificationToken">,
  ) => Lark.EventDispatcher;
}

export interface MonitorFeishuWebSocketHandle {
  wsClient: FeishuWSClientLike;
  eventDispatcher: Lark.EventDispatcher;
  dispatchRawEvent(rawPayload: unknown): Promise<void>;
  stop(): Promise<void>;
}

export async function monitorWebSocket(
  options: MonitorFeishuWebSocketOptions,
): Promise<MonitorFeishuWebSocketHandle> {
  const logger = options.logger ?? createSubsystemLogger("feishu/monitor");
  const dedupe = options.dedupe ?? createMemoryDedupe();
  const queue = options.queue ?? createSequentialQueue();
  const wsClient = (options.wsClientFactory ?? createFeishuWSClient)(
    options.creds,
  );
  const eventDispatcher = (
    options.eventDispatcherFactory ?? createEventDispatcher
  )({
    encryptKey: options.creds.encryptKey,
    verificationToken: options.creds.verificationToken,
  });

  const dispatchRawEvent = async (rawPayload: unknown): Promise<void> => {
    const dedupeKey = resolveFeishuMessageDedupeKey(rawPayload);
    if (dedupeKey && dedupe.check(dedupeKey)) {
      logger.debug("Skipping duplicate Feishu inbound message", { dedupeKey });
      return;
    }

    const parsed = parseFeishuInboundEvent(rawPayload, {
      botOpenId: options.botOpenId,
    });
    if (!parsed) {
      return;
    }

    await queue.enqueue(parsed.threadScopeKey, async () => {
      try {
        await options.onEvent(parsed);
      } catch (err) {
        logger.error("Feishu inbound handler error", {
          messageId: parsed.messageId,
          threadScopeKey: parsed.threadScopeKey,
          error: err,
        });
      }
    });
  };

  eventDispatcher.register({
    "im.message.receive_v1": async (data: unknown) => {
      await dispatchRawEvent(data);
    },
  });

  await wsClient.start({ eventDispatcher });

  return {
    wsClient,
    eventDispatcher,
    dispatchRawEvent,
    stop: async () => {
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
  maxChunkChars?: number;
  client?: Lark.Client;
  wsClientFactory?: (creds: FeishuCredentials) => FeishuWSClientLike;
  eventDispatcherFactory?: (
    creds: Pick<FeishuCredentials, "encryptKey" | "verificationToken">,
  ) => Lark.EventDispatcher;
  dedupeTtlMs?: number;
  queueTaskTimeoutMs?: number;
  logger?: SubsystemLogger;
}

export class FeishuChannelAdapter implements ChannelAdapter {
  readonly platform = "feishu" as const;
  readonly maxChunkChars: number;

  private readonly appId: string;
  private readonly appSecret: string;
  private readonly domain?: FeishuDomain;
  private readonly encryptKey?: string;
  private readonly verificationToken?: string;
  private botOpenId?: string;
  private clientInstance?: Lark.Client;
  private readonly wsClientFactory?: (
    creds: FeishuCredentials,
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
    this.botOpenId = options.botOpenId?.trim() || undefined;
    this.maxChunkChars = options.maxChunkChars ?? 4000;
    this.clientInstance = options.client;
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
    return this.connected;
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
    this.clientInstance = createFeishuClient({
      appId: this.appId,
      appSecret: this.appSecret,
      domain: this.domain,
      encryptKey: this.encryptKey,
      verificationToken: this.verificationToken,
    });
    return this.clientInstance;
  }

  async start(): Promise<void> {
    if (!this.isConfigured()) {
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
    };

    if (!this.botOpenId) {
      const probe = await probeFeishu(creds, {
        client: this.clientInstance,
      });
      if (probe.ok && probe.botOpenId) {
        this.botOpenId = probe.botOpenId;
      } else if (!probe.ok) {
        this.logger.warn("Feishu bot probe did not return botOpenId", {
          appId: this.appId,
          error: probe.error,
        });
      }
    }

    this.monitorHandle = await monitorWebSocket({
      creds,
      botOpenId: this.botOpenId,
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
    if (dedupeKey && this.dedupe.check(dedupeKey)) {
      return;
    }
    const parsed = parseFeishuInboundEvent(rawPayload, {
      botOpenId: this.botOpenId,
    });
    if (!parsed) {
      return;
    }
    await this.queue.enqueue(parsed.threadScopeKey, async () => {
      for (const handler of this.handlers) {
        await handler(parsed);
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
      if (withinCardTableLimit(options.text)) {
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

      const subChunks = chunkFeishuCardMarkdown(options.text);
      let lastMessageId = "unknown";
      for (const subChunk of subChunks) {
        const res = await sendStructuredCardFeishu(client, {
          to: target,
          text: subChunk,
          replyToMessageId: options.replyToMessageId,
        });
        lastMessageId = res.messageId;
      }
      return {
        messageId: lastMessageId,
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
    const groups = await listFeishuDirectoryGroupsLive(client);
    return {
      chats: groups.map((group) => ({
        address: `feishu:${group.id}`,
        name: group.name || group.id,
        kind: "group" as const,
      })),
      complete: true,
      error: null,
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
    try {
      const chatInfo = await getChatInfo(client, chatId);
      const senderOpenId = params.platformUserId?.trim();
      if (senderOpenId) {
        await assertFeishuChatMember(client, chatId, senderOpenId);
      }
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
