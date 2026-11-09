/**
 * Discord channel adapter: Gateway inbound pipeline plus REST outbound.
 * License for adapted OpenClaw code: MIT, Copyright (c) 2025 Peter Steinberger
 * (see `THIRD_PARTY_NOTICES.md` for per-file source ranges).
 */

import { createMemoryDedupe, type MemoryDedupeGuard } from "../../shared/dedupe.js";
import { createSequentialQueue, type SequentialQueue } from "../../shared/keyed-queue.js";
import { createSubsystemLogger, type SubsystemLogger } from "../../shared/logger.js";
import type { SafeFetchOptions } from "../../shared/safe-fetch.js";
import { MAX_ATTACHMENT_BYTES, type InboundChannelEvent } from "../../types.js";
import {
  type AvailableChatsResult, type ChannelAdapter, ChannelDeliveryError,
  type CheckTargetLiveResult, type DownloadedAttachment, type SendFileOptions,
  type SendFileResult, type SendTextChunkOptions, type SendTextChunkResult,
} from "../adapter.js";
import { chunkDiscordText, DISCORD_MAX_CHARS } from "./chunk.js";
import {
  DiscordGatewayClient, fetchDiscordGatewayInfo, type DiscordGatewayClientOptions,
  type DiscordGatewayInfo, type DiscordWebSocketFactory,
} from "./gateway.js";
import {
  downloadDiscordAttachment, isBotMentioned, isDiscordThreadChannelType,
  parseDiscordInboundMessage, type DiscordChannelContext, type DiscordMessageLike,
} from "./inbound.js";
import { classifyDiscordDeliveryError, RequestClient, type DiscordFetch } from "./rest.js";
import { createDmChannelId, parseDiscordTarget, sendFileDiscord, sendMessageDiscord } from "./send.js";
import { ensureThreadUnarchived, getDiscordChannel } from "./threading.js";

export * from "./chunk.js";
export * from "./gateway.js";
export * from "./inbound.js";
export * from "./rest.js";
export * from "./send.js";
export * from "./threading.js";

export interface DiscordChannelAdapterOptions {
  botToken?: string;
  botUserId?: string;
  applicationId?: string;
  rest?: RequestClient;
  fetchImpl?: DiscordFetch;
  gatewayClient?: DiscordGatewayClient;
  webSocketFactory?: DiscordWebSocketFactory;
  gatewayOptions?: Partial<DiscordGatewayClientOptions>;
  fetchGatewayInfoFn?: () => Promise<DiscordGatewayInfo>;
  logger?: SubsystemLogger;
  dedupeTtlMs?: number;
  queueTaskTimeoutMs?: number;
  mediaFetchImpl?: SafeFetchOptions["fetchImpl"];
}

interface GuildChannelsResponseItem {
  id?: string; type?: number; name?: string; parent_id?: string; guild_id?: string;
}

export class DiscordChannelAdapter implements ChannelAdapter {
  readonly platform = "discord" as const;
  readonly maxChunkChars = DISCORD_MAX_CHARS;

  private readonly botToken: string;
  private readonly applicationId?: string;
  private readonly logger: SubsystemLogger;
  private readonly dedupe: MemoryDedupeGuard;
  private readonly queue: SequentialQueue;
  private readonly handlers: Array<(event: InboundChannelEvent) => Promise<void>> = [];
  private readonly channelCache = new Map<string, DiscordChannelContext>();
  private readonly dmChannelCache = new Map<string, string>();
  private restInstance?: RequestClient;
  private gatewayInstance?: DiscordGatewayClient;
  private botUserId?: string;
  private started = false;

  constructor(private readonly options: DiscordChannelAdapterOptions = {}) {
    this.botToken = options.botToken?.trim() ?? "";
    this.applicationId = options.applicationId?.trim() || undefined;
    this.logger = options.logger ?? createSubsystemLogger("discord");
    this.restInstance = options.rest;
    this.gatewayInstance = options.gatewayClient;
    this.botUserId = options.botUserId?.trim() || undefined;
    this.dedupe = createMemoryDedupe({ ttlMs: options.dedupeTtlMs });
    this.queue = createSequentialQueue({ taskTimeoutMs: options.queueTaskTimeoutMs });
  }

  isConfigured(): boolean { return Boolean(this.botToken || this.restInstance || this.gatewayInstance); }
  isConnected(): boolean { return this.started && (this.gatewayInstance ? this.gatewayInstance.isConnected() : false); }
  getBotUserId(): string | undefined { return this.botUserId; }
  getApplicationId(): string | undefined { return this.applicationId ?? this.botUserId; }

  private getRest(): RequestClient {
    if (this.restInstance) return this.restInstance;
    if (!this.botToken) {
      throw new ChannelDeliveryError("Discord adapter is not configured", { outcome: "failed", code: "unavailable" });
    }
    this.restInstance = new RequestClient(this.botToken, { fetchImpl: this.options.fetchImpl });
    return this.restInstance;
  }

  private getGateway(): DiscordGatewayClient {
    if (this.gatewayInstance) return this.gatewayInstance;
    const rest = this.getRest();
    this.gatewayInstance = new DiscordGatewayClient({
      token: this.botToken,
      webSocketFactory: this.options.webSocketFactory,
      logger: this.logger,
      fetchGatewayInfo: this.options.fetchGatewayInfoFn ?? (() => fetchDiscordGatewayInfo(rest)),
      ...this.options.gatewayOptions,
      onDispatch: (name, data) => this.handleDispatch(name, data),
      onReady: (info) => {
        if (info.botUserId) this.botUserId = info.botUserId;
        this.options.gatewayOptions?.onReady?.(info);
      },
      onError: (err) => {
        this.logger.error("Discord gateway error", { error: err });
        this.options.gatewayOptions?.onError?.(err);
      },
    });
    return this.gatewayInstance;
  }

  async start(): Promise<void> {
    if (!this.isConfigured() || this.started) return;
    // Resolve the bot identity first so group mentions can be classified.
    try {
      const me = (await this.getRest().get("/users/@me")) as { id?: string } | undefined;
      if (me?.id) this.botUserId = me.id;
    } catch (err) {
      this.logger.warn("Discord bot identity lookup failed; group mentions need gateway READY", { error: err });
    }
    const gateway = this.getGateway();
    // Re-wire dispatch in case a gateway was injected without handlers.
    gateway.onDispatch = (name, data) => this.handleDispatch(name, data);
    const previousReady = gateway.onReady;
    gateway.onReady = (info) => {
      if (info.botUserId) this.botUserId = info.botUserId;
      previousReady?.(info);
    };
    this.started = true;
    await gateway.start();
  }

  async stop(): Promise<void> {
    this.started = false;
    if (this.gatewayInstance) await this.gatewayInstance.stop();
    this.restInstance?.abortAllRequests();
  }

  onInboundEvent(handler: (event: InboundChannelEvent) => Promise<void>): void {
    this.handlers.push(handler);
  }

  /** Handles one gateway dispatch; exposed for tests with a fake gateway/socket. */
  async handleDispatch(eventName: string, data: unknown): Promise<void> {
    if (eventName === "GUILD_CREATE" || eventName === "CHANNEL_CREATE" || eventName === "CHANNEL_UPDATE" || eventName === "THREAD_CREATE" || eventName === "THREAD_UPDATE") {
      this.cacheChannelPayload(data);
      if (eventName === "GUILD_CREATE" && data && typeof data === "object") {
        const channels = (data as { channels?: unknown[] }).channels;
        if (Array.isArray(channels)) for (const ch of channels) this.cacheChannelPayload(ch, (data as { id?: string }).id);
        const threads = (data as { threads?: unknown[] }).threads;
        if (Array.isArray(threads)) for (const th of threads) this.cacheChannelPayload(th, (data as { id?: string }).id);
      }
      return;
    }
    if (eventName !== "MESSAGE_CREATE") return;
    await this.dispatchMessage(data as DiscordMessageLike);
  }

  private cacheChannelPayload(data: unknown, guildId?: string): void {
    if (!data || typeof data !== "object") return;
    const ch = data as GuildChannelsResponseItem;
    if (!ch.id) return;
    this.channelCache.set(ch.id, {
      channelId: ch.id,
      type: ch.type,
      guildId: ch.guild_id ?? guildId,
      parentId: ch.parent_id ?? undefined,
      name: ch.name,
    });
  }

  /** Dispatches a raw MESSAGE_CREATE payload through per-conversation queue + dedupe. */
  async dispatchMessage(message: DiscordMessageLike): Promise<void> {
    if (!message || typeof message !== "object") return;
    const author = message.author;
    const authorId = typeof author?.id === "string" ? author.id.trim() : "";
    if (!authorId || author?.bot || message.webhook_id) return;
    if (this.botUserId && authorId === this.botUserId) return;

    const messageId = typeof message.id === "string" ? message.id.trim() : "";
    const channelId = typeof message.channel_id === "string" ? message.channel_id.trim() : "";
    if (!messageId || !channelId) return;

    const dedupeKey = `discord:${messageId}`;
    if (this.dedupe.peek(dedupeKey)) return;

    const guildId =
      typeof message.guild_id === "string" && message.guild_id.trim()
        ? message.guild_id.trim()
        : undefined;
    const rawQueueKey = guildId ? `chat:${guildId}/${channelId}` : `dm:${authorId}`;

    await this.queue.enqueue(rawQueueKey, async () => {
      if (this.dedupe.peek(dedupeKey)) return;

      if (guildId && !this.botUserId) {
        try {
          const me = (await this.getRest().get("/users/@me")) as { id?: string } | undefined;
          if (typeof me?.id === "string" && me.id.trim()) {
            this.botUserId = me.id.trim();
          }
        } catch (err) {
          this.logger.warn("Discord bot identity lookup failed during guild dispatch", { error: err });
          throw classifyDiscordDeliveryError(err, {
            stage: "read",
            message: "Discord bot identity lookup failed",
          });
        }
      }

      if (guildId && !isBotMentioned(message, this.botUserId)) {
        return;
      }

      let ctx = this.channelCache.get(channelId);
      const hintThread = Boolean(message.thread?.id && message.thread.id === channelId);
      const needsChannelLookup =
        Boolean(guildId) &&
        (!ctx || ((isDiscordThreadChannelType(ctx.type) || hintThread) && !ctx.parentId));

      if (needsChannelLookup && guildId) {
        const fetched = await getDiscordChannel(this.getRest(), channelId);
        const resolvedChannelId =
          typeof fetched?.id === "string" && fetched.id.trim() ? fetched.id.trim() : channelId;
        const resolvedType = fetched?.type ?? ctx?.type;
        const resolvedParentId = fetched?.parent_id ?? ctx?.parentId ?? undefined;
        const isThread = isDiscordThreadChannelType(resolvedType) || hintThread;
        if (isThread && !resolvedParentId) {
          throw classifyDiscordDeliveryError(
            new Error(`Discord thread ${channelId} lookup did not return parent_id`),
            { stage: "read", message: "Discord channel lookup failed" },
          );
        }
        ctx = {
          channelId: resolvedChannelId,
          type: resolvedType,
          guildId: fetched?.guild_id ?? ctx?.guildId ?? guildId,
          parentId: resolvedParentId,
          name: fetched?.name ?? ctx?.name,
        };
        this.channelCache.set(channelId, ctx);
        this.channelCache.set(resolvedChannelId, ctx);
      }

      const parsed = parseDiscordInboundMessage(message, {
        botUserId: this.botUserId,
        channelContext: ctx,
      });
      if (!parsed) return;
      if (parsed.chatType === "group" && !parsed.isGroupTrigger) return;

      try {
        for (const handler of this.handlers) await handler(parsed);
        // Short-term dedupe is recorded only after the inbound callback
        // succeeds; a failure stays eligible for a later redelivery/recovery.
        this.dedupe.record(dedupeKey);
      } catch (err) {
        this.dedupe.delete(dedupeKey);
        this.logger.error("Discord inbound handler error", { messageId: parsed.messageId, error: err });
        throw err;
      }
    });
  }

  splitTextChunks(text: string): string[] {
    return chunkDiscordText(text, { maxChars: this.maxChunkChars });
  }

  private async resolveDmChannelId(recipientUserId: string): Promise<string> {
    const cached = this.dmChannelCache.get(recipientUserId);
    if (cached) return cached;
    const id = await createDmChannelId(this.getRest(), recipientUserId);
    this.dmChannelCache.set(recipientUserId, id);
    return id;
  }

  async sendTextChunk(options: SendTextChunkOptions): Promise<SendTextChunkResult> {
    const res = await sendMessageDiscord(this.getRest(), {
      targetAddress: options.targetAddress,
      recipientPlatformUserId: options.recipientPlatformUserId,
      text: options.text,
      replyToMessageId: options.replyToMessageId,
      newThread: options.newThread,
      threadScopeKey: options.threadScopeKey,
      resolveDmChannelId: (id) => this.resolveDmChannelId(id),
    });
    return { messageId: res.messageId, resolvedAddress: res.resolvedAddress };
  }

  async sendFile(options: SendFileOptions): Promise<SendFileResult> {
    const res = await sendFileDiscord(this.getRest(), {
      targetAddress: options.targetAddress,
      recipientPlatformUserId: options.recipientPlatformUserId,
      filename: options.filename,
      content: options.content,
      mimeType: options.mimeType,
      threadScopeKey: options.threadScopeKey,
      resolveDmChannelId: (id) => this.resolveDmChannelId(id),
    });
    return { messageId: res.messageId, status: "sent", reason: null };
  }

  async downloadAttachment(
    descriptor: Parameters<ChannelAdapter["downloadAttachment"]>[0],
    event: InboundChannelEvent,
  ): Promise<DownloadedAttachment> {
    const res = await downloadDiscordAttachment(this.getRest(), descriptor, event, {
      fetchImpl: this.options.mediaFetchImpl,
      maxBytes: MAX_ATTACHMENT_BYTES,
    });
    return { content: res.content, filename: res.filename, mimeType: res.mimeType };
  }

  async listAvailableChats(_params: { userId: string; platformUserId: string }): Promise<AvailableChatsResult> {
    const rest = this.getRest();
    const chats: AvailableChatsResult["chats"] = [];
    let complete = true;
    let error: string | null = null;
    try {
      let after: string | undefined;
      for (;;) {
        const guilds = (await rest.get("/users/@me/guilds", { limit: 200, ...(after ? { after } : {}) })) as Array<{ id?: string; name?: string }>;
        if (!Array.isArray(guilds)) { complete = false; error = "Discord guild list returned an unexpected shape"; break; }
        for (const guild of guilds) {
          if (!guild.id) continue;
          const channels = (await rest.get(`/guilds/${encodeURIComponent(guild.id)}/channels`)) as GuildChannelsResponseItem[];
          if (!Array.isArray(channels)) { complete = false; error = "Discord channel list returned an unexpected shape"; continue; }
          for (const ch of channels) {
            if (!ch.id) continue;
            this.cacheChannelPayload(ch, guild.id);
            if (ch.type === 0 || ch.type === 5) {
              chats.push({
                address: `discord:${guild.id}/${ch.id}`,
                name: ch.name ? `#${ch.name}` : ch.id,
                kind: "channel",
              });
            }
          }
        }
        if (guilds.length < 200) break;
        const lastId = guilds[guilds.length - 1]?.id;
        if (!lastId || lastId === after) { complete = false; error = "Discord guild pagination did not advance"; break; }
        after = lastId;
      }
    } catch (err) {
      // A partial directory must never be presented as a complete empty list.
      complete = false;
      error = err instanceof Error ? err.message : String(err);
    }
    return { chats, complete, error };
  }

  async checkTargetLive(params: {
    userId: string; platformUserId?: string | null; address: string; purpose: string;
  }): Promise<CheckTargetLiveResult> {
    const parsed = parseDiscordTarget(params.address);
    if (!parsed) return { ok: false, address: null, message: "Target address cannot be empty." };
    if (parsed.kind === "dm") {
      const recipient = params.platformUserId?.trim();
      if (!recipient) return { ok: false, address: null, message: "Discord DM target has no linked recipient." };
      try {
        await this.resolveDmChannelId(recipient);
        return { ok: true, address: "discord:@me", name: "Direct Message", message: null };
      } catch (err) {
        return { ok: false, address: null, message: err instanceof Error ? err.message : String(err) };
      }
    }
    const channelId = parsed.threadId ?? parsed.channelId;
    try {
      const channel = await getDiscordChannel(this.getRest(), channelId);
      if (parsed.guildId && channel.guild_id && channel.guild_id !== parsed.guildId) {
        return { ok: false, address: null, message: "Discord channel does not belong to the expected guild." };
      }
      if (parsed.threadId) {
        if (parsed.parentChannelId && channel.parent_id && channel.parent_id !== parsed.parentChannelId) {
          return { ok: false, address: null, message: "Discord thread does not belong to the expected parent channel." };
        }
        await ensureThreadUnarchived(this.getRest(), channel);
      }
      return {
        ok: true,
        address: params.address,
        name: channel.name ?? channelId,
        message: null,
      };
    } catch (err) {
      const classified = err instanceof ChannelDeliveryError ? err : classifyDiscordDeliveryError(err, { stage: "read" });
      return { ok: false, address: null, message: classified.message };
    }
  }
}
