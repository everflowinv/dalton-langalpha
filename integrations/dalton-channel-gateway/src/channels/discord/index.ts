/**
 * Discord channel adapter: Gateway inbound pipeline plus REST outbound.
 * License for adapted OpenClaw code: MIT, Copyright (c) 2025 Peter Steinberger
 * (see `THIRD_PARTY_NOTICES.md` for per-file source ranges).
 */

import { createMemoryDedupe, type MemoryDedupeGuard } from "../../shared/dedupe.js";
import { createSequentialQueue, type SequentialQueue } from "../../shared/keyed-queue.js";
import { createSubsystemLogger, type SubsystemLogger } from "../../shared/logger.js";
import type { SafeFetchOptions } from "../../shared/safe-fetch.js";
import {
  MAX_ATTACHMENT_BYTES,
  type InboundChannelEvent,
  type NativeAutocompleteResponder,
  type NativeCommandResponder,
  type NativeInteractionEvent,
} from "../../types.js";
import {
  canViewPlatformChat,
  type AvailableChatsResult, type ChannelAdapter, ChannelDeliveryError,
  type CheckTargetLiveResult, type DownloadedAttachment, type ListAvailableChatsOptions,
  type SendFileOptions, type SendFileResult, type SendTextChunkOptions, type SendTextChunkResult,
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
import {
  DiscordInteractionHttpClient,
  type DiscordInteractionAckResult,
  type DiscordInteractionHttpOptions,
} from "./interaction-response.js";
import {
  parseDiscordInteractionCreate,
  type ParsedDiscordCommandInteraction,
} from "./interactions.js";
import { classifyDiscordDeliveryError, RequestClient, type DiscordFetch } from "./rest.js";
import { createDmChannelId, parseDiscordTarget, sendFileDiscord, sendMessageDiscord } from "./send.js";
import { ensureThreadUnarchived, getDiscordChannel, type DiscordChannelInfo } from "./threading.js";

export * from "./chunk.js";
export * from "./gateway.js";
export * from "./inbound.js";
export * from "./interaction-response.js";
export * from "./interactions.js";
export * from "./rest.js";
export * from "./send.js";
export * from "./threading.js";

export interface DiscordChannelAdapterOptions {
  botToken?: string;
  botUserId?: string;
  applicationId?: string;
  rest?: RequestClient;
  fetchImpl?: DiscordFetch;
  interactionHttpClient?: DiscordInteractionHttpClient;
  interactionHttpOptions?: DiscordInteractionHttpOptions;
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

interface StoredInteractionAckState {
  ackResult: DiscordInteractionAckResult;
  receivedAtMs: number;
  expiresAtMs: number;
}

const MAX_TRACKED_INTERACTION_ACKS = 2_000;

export class DiscordChannelAdapter implements ChannelAdapter {
  readonly platform = "discord" as const;
  readonly maxChunkChars = DISCORD_MAX_CHARS;

  private readonly botToken: string;
  private readonly applicationId?: string;
  private readonly logger: SubsystemLogger;
  private readonly dedupe: MemoryDedupeGuard;
  private readonly queue: SequentialQueue;
  private readonly handlers: Array<(event: InboundChannelEvent) => Promise<void>> = [];
  private readonly nativeInteractionHandlers: Array<(event: NativeInteractionEvent) => Promise<void>> = [];
  private readonly channelCache = new Map<string, DiscordChannelContext>();
  private readonly dmChannelCache = new Map<string, string>();
  private readonly interactionAckStates = new Map<string, StoredInteractionAckState>();
  private readonly interactionAckInFlight = new Map<string, Promise<StoredInteractionAckState>>();
  private readonly interactionClient: DiscordInteractionHttpClient;
  private restInstance?: RequestClient;
  private gatewayInstance?: DiscordGatewayClient;
  private readonly baseGatewayOnReady?: DiscordGatewayClientOptions["onReady"];
  private botUserId?: string;
  private verifiedApplicationId?: string;
  private readyApplicationId?: string;
  private applicationIdentityState: "pending" | "verified" | "mismatch" | "unconfigured" = "pending";
  private verifyAppPromise: Promise<string | undefined> | null = null;
  private started = false;
  private lifecycleEpoch = 0;
  private startPromise: Promise<void> | null = null;
  private lifecycleAbortController = new AbortController();

  constructor(private readonly options: DiscordChannelAdapterOptions = {}) {
    this.botToken = options.botToken?.trim() ?? "";
    this.applicationId = options.applicationId?.trim() || undefined;
    if (!this.applicationId) {
      this.applicationIdentityState = "unconfigured";
    }
    this.logger = options.logger ?? createSubsystemLogger("discord");
    this.restInstance = options.rest;
    this.gatewayInstance = options.gatewayClient;
    this.baseGatewayOnReady = options.gatewayClient?.onReady ?? options.gatewayOptions?.onReady;
    this.botUserId = options.botUserId?.trim() || undefined;
    this.dedupe = createMemoryDedupe({ ttlMs: options.dedupeTtlMs });
    this.queue = createSequentialQueue({ taskTimeoutMs: options.queueTaskTimeoutMs });
    this.interactionClient =
      options.interactionHttpClient ??
      new DiscordInteractionHttpClient({
        fetchImpl: options.interactionHttpOptions?.fetchImpl ?? (options.fetchImpl as typeof fetch | undefined),
        ...options.interactionHttpOptions,
      });
  }

  isConfigured(): boolean { return Boolean(this.botToken || this.restInstance || this.gatewayInstance); }
  isConnected(): boolean { return this.started && (this.gatewayInstance ? this.gatewayInstance.isConnected() : false); }
  getBotUserId(): string | undefined { return this.botUserId; }
  /**
   * Returns the verified application ID (never falls back to `botUserId`).
   */
  getApplicationId(): string | undefined { return this.verifiedApplicationId; }
  getConfiguredApplicationId(): string | undefined { return this.applicationId; }
  getVerifiedApplicationId(): string | undefined { return this.verifiedApplicationId; }

  private getRest(): RequestClient {
    if (this.restInstance) return this.restInstance;
    if (!this.botToken) {
      throw new ChannelDeliveryError("Discord adapter is not configured", { outcome: "failed", code: "unavailable" });
    }
    this.restInstance = new RequestClient(this.botToken, { fetchImpl: this.options.fetchImpl });
    return this.restInstance;
  }

  private applyReadyApplicationId(readyAppId?: string): void {
    const trimmed = readyAppId?.trim();
    if (!trimmed) return;
    this.readyApplicationId = trimmed;
    if (this.applicationId && trimmed !== this.applicationId) {
      this.verifiedApplicationId = undefined;
      this.applicationIdentityState = "mismatch";
      this.logger.warn("Discord gateway READY application.id does not match configured DALTON_DISCORD_APPLICATION_ID; native interactions disabled", {
        configuredApplicationId: this.applicationId,
        readyApplicationId: trimmed,
      });
    }
  }

  /**
   * Verifies `DALTON_DISCORD_APPLICATION_ID` against `/api/v10/oauth2/applications/@me` using the same Bot token.
   * Never falls back to `botUserId`. On mismatch or lookup failure, native interactions are rejected while
   * regular `MESSAGE_CREATE` handling continues to operate.
   */
  async ensureApplicationIdentityVerified(): Promise<string | undefined> {
    if (!this.applicationId) {
      this.verifiedApplicationId = undefined;
      this.applicationIdentityState = "unconfigured";
      return undefined;
    }
    if (this.readyApplicationId && this.readyApplicationId !== this.applicationId) {
      this.verifiedApplicationId = undefined;
      this.applicationIdentityState = "mismatch";
      return undefined;
    }
    if (this.applicationIdentityState === "verified") {
      return this.verifiedApplicationId;
    }
    if (this.applicationIdentityState === "mismatch") {
      return undefined;
    }
    if (this.verifyAppPromise) {
      return this.verifyAppPromise;
    }
    const runVerify = async (): Promise<string | undefined> => {
      try {
        const app = (await this.getRest().get("/oauth2/applications/@me")) as { id?: unknown } | undefined;
        const fetchedAppId = typeof app?.id === "string" ? app.id.trim() : "";
        if (!fetchedAppId || fetchedAppId !== this.applicationId) {
          this.verifiedApplicationId = undefined;
          this.applicationIdentityState = "mismatch";
          this.logger.warn("Discord /oauth2/applications/@me identity does not match DALTON_DISCORD_APPLICATION_ID; native interactions rejected", {
            configuredApplicationId: this.applicationId,
            fetchedApplicationId: fetchedAppId || null,
          });
          return undefined;
        }
        if (this.readyApplicationId && this.readyApplicationId !== this.applicationId) {
          this.verifiedApplicationId = undefined;
          this.applicationIdentityState = "mismatch";
          return undefined;
        }
        this.verifiedApplicationId = this.applicationId;
        this.applicationIdentityState = "verified";
        return this.verifiedApplicationId;
      } catch (err) {
        this.verifiedApplicationId = undefined;
        this.applicationIdentityState = "mismatch";
        this.logger.warn("Discord /oauth2/applications/@me identity check failed; native interactions rejected", {
          configuredApplicationId: this.applicationId,
          error: err,
        });
        return undefined;
      }
    };
    const p = runVerify().finally(() => {
      if (this.verifyAppPromise === p) {
        this.verifyAppPromise = null;
      }
    });
    this.verifyAppPromise = p;
    return p;
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
        this.applyReadyApplicationId(info.applicationId);
        this.baseGatewayOnReady?.(info);
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
    if (this.startPromise) {
      await this.startPromise;
      return;
    }
    const epoch = ++this.lifecycleEpoch;
    if (this.lifecycleAbortController.signal.aborted) {
      this.lifecycleAbortController = new AbortController();
    }
    const runStart = async (): Promise<void> => {
      // Resolve the bot identity first so group mentions can be classified.
      try {
        const me = (await this.getRest().get("/users/@me")) as { id?: string } | undefined;
        if (epoch !== this.lifecycleEpoch) return;
        if (me?.id) this.botUserId = me.id;
      } catch (err) {
        if (epoch !== this.lifecycleEpoch) return;
        this.logger.warn("Discord bot identity lookup failed; group mentions need gateway READY", { error: err });
      }
      if (epoch !== this.lifecycleEpoch) return;
      if (this.applicationId) {
        await this.ensureApplicationIdentityVerified();
        if (epoch !== this.lifecycleEpoch) return;
      }
      const gateway = this.getGateway();
      // Re-wire dispatch and ready handlers without nesting across restarts.
      gateway.onDispatch = (name, data) => {
        if (epoch !== this.lifecycleEpoch) return;
        return this.handleDispatch(name, data);
      };
      gateway.onReady = (info) => {
        if (epoch !== this.lifecycleEpoch) return;
        if (info.botUserId) this.botUserId = info.botUserId;
        this.applyReadyApplicationId(info.applicationId);
        this.baseGatewayOnReady?.(info);
      };
      this.started = true;
      await gateway.start();
      if (epoch !== this.lifecycleEpoch) return;
    };
    const p = runStart().finally(() => {
      if (this.startPromise === p) {
        this.startPromise = null;
      }
    });
    this.startPromise = p;
    await p;
  }

  async stop(): Promise<void> {
    this.lifecycleEpoch += 1;
    this.startPromise = null;
    this.started = false;
    this.lifecycleAbortController.abort();
    if (this.gatewayInstance) await this.gatewayInstance.stop();
    this.restInstance?.abortAllRequests();
  }

  onInboundEvent(handler: (event: InboundChannelEvent) => Promise<void>): void {
    this.handlers.push(handler);
  }

  onNativeInteraction(handler: (event: NativeInteractionEvent) => Promise<void>): void {
    this.nativeInteractionHandlers.push(handler);
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
    if (eventName === "INTERACTION_CREATE") {
      await this.dispatchInteraction(data);
      return;
    }
    if (eventName !== "MESSAGE_CREATE") return;
    await this.dispatchMessage(data as DiscordMessageLike);
  }

  private rememberInteractionAckState(interactionId: string, state: StoredInteractionAckState): void {
    if (this.interactionAckStates.size >= MAX_TRACKED_INTERACTION_ACKS && !this.interactionAckStates.has(interactionId)) {
      const oldestKey = this.interactionAckStates.keys().next().value;
      if (oldestKey !== undefined) {
        this.interactionAckStates.delete(oldestKey);
      }
    }
    this.interactionAckStates.set(interactionId, state);
  }

  private async acquireOrSendInitialDefer(
    interactionId: string,
    interactionToken: string,
    receivedAtMs: number,
  ): Promise<StoredInteractionAckState> {
    const existing = this.interactionAckStates.get(interactionId);
    if (existing) {
      return existing;
    }
    const inFlight = this.interactionAckInFlight.get(interactionId);
    if (inFlight) {
      return inFlight;
    }
    const task = (async (): Promise<StoredInteractionAckState> => {
      try {
        const rawAck = await this.interactionClient.deferEphemeralCommand({
          interactionId,
          interactionToken,
          receivedAtMs,
        });
        const state: StoredInteractionAckState = {
          ackResult: rawAck,
          receivedAtMs,
          expiresAtMs: receivedAtMs + this.interactionClient.getTokenTtlMs(),
        };
        this.rememberInteractionAckState(interactionId, state);
        return state;
      } finally {
        this.interactionAckInFlight.delete(interactionId);
      }
    })();
    this.interactionAckInFlight.set(interactionId, task);
    return task;
  }

  private async fetchAndCacheInteractionChannel(
    channelId: string,
    guildId?: string,
  ): Promise<DiscordChannelContext | null> {
    const cached = this.channelCache.get(channelId);
    if (
      cached &&
      typeof cached.type === "number" &&
      (!guildId || cached.guildId === guildId) &&
      (!isDiscordThreadChannelType(cached.type) ||
        (Boolean(cached.parentId) && cached.parentId !== channelId))
    ) {
      return cached;
    }
    try {
      const fetched = await getDiscordChannel(this.getRest(), channelId);
      if (!fetched || typeof fetched !== "object") {
        return null;
      }
      const fetchedId =
        typeof fetched.id === "string" && fetched.id.trim() ? fetched.id.trim() : undefined;
      if (!fetchedId) {
        return null;
      }
      const resolvedType = typeof fetched.type === "number" ? fetched.type : undefined;
      const fetchedGuildId =
        typeof fetched.guild_id === "string" && fetched.guild_id.trim()
          ? fetched.guild_id.trim()
          : undefined;
      const resolvedParentId =
        typeof fetched.parent_id === "string" && fetched.parent_id.trim()
          ? fetched.parent_id.trim()
          : undefined;
      const resolvedName =
        typeof fetched.name === "string" && fetched.name.trim()
          ? fetched.name.trim()
          : cached?.name;
      const ctx: DiscordChannelContext = {
        channelId: fetchedId,
        type: resolvedType,
        guildId: fetchedGuildId ?? (guildId ? undefined : cached?.guildId),
        parentId: resolvedParentId,
        name: resolvedName,
      };
      const guildMatches = !guildId || fetchedGuildId === guildId;
      const threadValid =
        !isDiscordThreadChannelType(resolvedType) ||
        (Boolean(resolvedParentId) && resolvedParentId !== channelId);
      if (
        fetchedId === channelId &&
        typeof resolvedType === "number" &&
        guildMatches &&
        threadValid
      ) {
        this.channelCache.set(channelId, ctx);
      }
      return ctx;
    } catch {
      return null;
    }
  }

  /**
   * Validates a Gateway `INTERACTION_CREATE` payload, performs the bounded-timeout
   * initial response before any session queue/database/cancel/channel-REST work,
   * and forwards to the registered native interaction handlers.
   */
  async dispatchInteraction(data: unknown): Promise<void> {
    const receivedAtMs = this.interactionClient.getNow();
    const verifiedAppId = await this.ensureApplicationIdentityVerified();
    if (!verifiedAppId) {
      this.logger.warn("Rejecting Discord INTERACTION_CREATE: application identity is unverified", {
        configuredApplicationId: this.applicationId ?? null,
        identityState: this.applicationIdentityState,
      });
      return;
    }

    const parseResult = await parseDiscordInteractionCreate(data, {
      verifiedApplicationId: verifiedAppId,
      botUserId: this.botUserId,
      resolveChannelInfo: async (chId) => this.channelCache.get(chId) ?? null,
      allowUnresolvedThreadParent: true,
    });

    if (!parseResult.ok) {
      this.logger.warn("Rejecting Discord INTERACTION_CREATE", {
        reason: parseResult.reason,
        detail: parseResult.detail ?? null,
      });
      return;
    }

    const initialInteraction = parseResult.interaction;

    if (initialInteraction.kind === "autocomplete") {
      const autocompleteDeadlineMs = Math.min(
        receivedAtMs + 1_800,
        receivedAtMs + this.interactionClient.getInitialCallbackTimeoutMs(),
      );
      let responded = false;
      const responder: NativeAutocompleteResponder = {
        receivedAtMs,
        respondChoices: async (choices) => {
          if (responded) {
            return { outcome: "acknowledged", status: 204 };
          }
          responded = true;
          return this.interactionClient.respondAutocompleteChoices({
            interactionId: initialInteraction.interactionId,
            interactionToken: initialInteraction.interactionToken,
            choices,
            receivedAtMs,
          });
        },
      };

      let autocompleteInteraction = initialInteraction;
      if (autocompleteInteraction.needsChannelResolution) {
        const remainingForChannelMs = Math.max(
          1,
          autocompleteDeadlineMs - this.interactionClient.getNow(),
        );
        let channelTimedOut = false;
        const resolvedAutoChannel = await Promise.race([
          this.fetchAndCacheInteractionChannel(
            autocompleteInteraction.channelId,
            autocompleteInteraction.guildId,
          ),
          new Promise<null>((resolve) => {
            const timer = setTimeout(() => {
              channelTimedOut = true;
              resolve(null);
            }, remainingForChannelMs);
            if (typeof timer === "object" && timer !== null && "unref" in timer) {
              (timer as { unref: () => void }).unref();
            }
          }),
        ]);
        if (channelTimedOut || this.interactionClient.getNow() >= autocompleteDeadlineMs) {
          await responder.respondChoices([]);
          return;
        }
        const enriched = await parseDiscordInteractionCreate(data, {
          verifiedApplicationId: verifiedAppId,
          botUserId: this.botUserId,
          resolveChannelInfo: async (id) =>
            (id === autocompleteInteraction.channelId ? resolvedAutoChannel : null) ??
            this.channelCache.get(id) ??
            null,
          allowUnresolvedThreadParent: false,
        });
        if (!enriched.ok || enriched.interaction.kind !== "autocomplete") {
          await responder.respondChoices([]);
          return;
        }
        autocompleteInteraction = enriched.interaction;
      }

      const event: NativeInteractionEvent = {
        kind: "autocomplete",
        platform: "discord",
        interactionId: autocompleteInteraction.interactionId,
        applicationId: autocompleteInteraction.applicationId,
        commandName: autocompleteInteraction.commandName as "workspace",
        focusedOptionName: autocompleteInteraction.focusedOptionName,
        query: autocompleteInteraction.query,
        platformUserId: autocompleteInteraction.platformUserId,
        platformUsername: autocompleteInteraction.platformUsername,
        chatType: autocompleteInteraction.chatType,
        chatAddress: autocompleteInteraction.chatAddress,
        chatId: autocompleteInteraction.chatId,
        chatName: autocompleteInteraction.chatName,
        threadScopeKey: autocompleteInteraction.threadScopeKey,
        rawPayload: autocompleteInteraction.sanitizedRawPayload,
        deadlineMs: autocompleteDeadlineMs,
        responder,
      };

      try {
        for (const handler of this.nativeInteractionHandlers) {
          await handler(event);
        }
      } catch (err) {
        this.logger.warn("Discord native autocomplete handler failed", {
          interactionId: autocompleteInteraction.interactionId,
          error: err,
        });
      }
      if (!responded) {
        await responder.respondChoices([]);
      }
      return;
    }

    // Command interaction (type = 2):
    // Send initial ephemeral defer callback (`type=5, flags=64`) BEFORE waiting on
    // channel REST lookups, session queues, database transactions, or `/stop` cancellation.
    const ackState = await this.acquireOrSendInitialDefer(
      initialInteraction.interactionId,
      initialInteraction.interactionToken,
      receivedAtMs,
    );

    let commandInteraction: ParsedDiscordCommandInteraction = initialInteraction;
    if (commandInteraction.needsChannelResolution) {
      const resolvedCmdChannel = await this.fetchAndCacheInteractionChannel(
        commandInteraction.channelId,
        commandInteraction.guildId,
      );
      const enriched = await parseDiscordInteractionCreate(data, {
        verifiedApplicationId: verifiedAppId,
        botUserId: this.botUserId,
        resolveChannelInfo: async (id) =>
          (id === commandInteraction.channelId ? resolvedCmdChannel : null) ??
          this.channelCache.get(id) ??
          null,
        allowUnresolvedThreadParent: false,
      });
      if (!enriched.ok || enriched.interaction.kind !== "command") {
        this.logger.warn("Rejecting Discord command interaction after channel context verification", {
          interactionId: commandInteraction.interactionId,
          reason: !enriched.ok ? enriched.reason : "unexpected_kind",
        });
        if (ackState.ackResult.outcome === "acknowledged") {
          await this.interactionClient.editOriginalEphemeralResponse({
            applicationId: verifiedAppId,
            interactionId: initialInteraction.interactionId,
            interactionToken: initialInteraction.interactionToken,
            content:
              "Unable to verify the Discord channel or thread context for this command. Please try again.",
            expiresAtMs: ackState.expiresAtMs,
          });
        }
        return;
      }
      commandInteraction = enriched.interaction;
    }

    const finalAckState = ackState;
    const interactionToken = commandInteraction.interactionToken;
    const responder: NativeCommandResponder = {
      ackResult: finalAckState.ackResult,
      receivedAtMs: finalAckState.receivedAtMs,
      expiresAtMs: finalAckState.expiresAtMs,
      isExpired: () => this.interactionClient.getNow() >= finalAckState.expiresAtMs,
      sendPrivateReply: async (text: string) => {
        if (finalAckState.ackResult.outcome !== "acknowledged") {
          return {
            outcome: "failed",
            reason: `cannot_send_reply_when_ack_${finalAckState.ackResult.outcome}`,
          };
        }
        return this.interactionClient.editOriginalEphemeralResponse({
          applicationId: verifiedAppId,
          interactionId: commandInteraction.interactionId,
          interactionToken,
          content: text,
          expiresAtMs: finalAckState.expiresAtMs,
        });
      },
    };

    const event: NativeInteractionEvent = {
      kind: "command",
      platform: "discord",
      interactionId: commandInteraction.interactionId,
      requestKey: commandInteraction.requestKey,
      applicationId: commandInteraction.applicationId,
      commandName: commandInteraction.commandName,
      cmd: commandInteraction.parsedCommand.cmd,
      targetKind: commandInteraction.parsedCommand.targetKind,
      workspaceId: commandInteraction.parsedCommand.workspaceId,
      workspaceQuery: commandInteraction.parsedCommand.workspaceQuery,
      platformUserId: commandInteraction.platformUserId,
      platformUsername: commandInteraction.platformUsername,
      chatType: commandInteraction.chatType,
      chatAddress: commandInteraction.chatAddress,
      chatId: commandInteraction.chatId,
      chatName: commandInteraction.chatName,
      threadScopeKey: commandInteraction.threadScopeKey,
      rawPayload: commandInteraction.sanitizedRawPayload,
      responder,
    };

    try {
      for (const handler of this.nativeInteractionHandlers) {
        await handler(event);
      }
    } catch (err) {
      this.logger.error("Discord native command interaction handler error", {
        interactionId: commandInteraction.interactionId,
        commandName: commandInteraction.commandName,
        error: err,
      });
    }
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
        let fetched: DiscordChannelInfo;
        try {
          fetched = await getDiscordChannel(this.getRest(), channelId);
        } catch (err) {
          // 403/404 cannot be processed later. Acknowledge the event so it
          // does not stay at the head of the gateway queue. Transport and 5xx
          // errors still propagate and keep their retry.
          if (
            err instanceof ChannelDeliveryError &&
            (err.code === "target_not_found" || err.code === "not_allowed")
          ) {
            this.logger.warn("Discord channel lookup cannot be processed; acknowledging the event", {
              messageId,
              channelId,
              code: err.code,
            });
            return;
          }
          throw err;
        }
        const resolvedChannelId =
          typeof fetched?.id === "string" && fetched.id.trim() ? fetched.id.trim() : channelId;
        const resolvedType = fetched?.type ?? ctx?.type;
        const resolvedParentId = fetched?.parent_id ?? ctx?.parentId ?? undefined;
        const isThread = isDiscordThreadChannelType(resolvedType) || hintThread;
        if (isThread && !resolvedParentId) {
          this.logger.warn("Discord thread lookup confirmed no parent channel; acknowledging the event", {
            messageId,
            channelId,
          });
          return;
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
      deliveryKey: options.deliveryKey,
      botUserId: this.botUserId,
      confirmedThreadAddress: options.confirmedThreadAddress,
      onConfirmedThread: options.onConfirmedThread,
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
      signal: this.lifecycleAbortController.signal,
    });
    return { content: res.content, filename: res.filename, mimeType: res.mimeType };
  }

  async listAvailableChats(params: ListAvailableChatsOptions): Promise<AvailableChatsResult> {
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
              const address = `discord:${guild.id}/${ch.id}`;
              if (!canViewPlatformChat({
                userTier: params.userTier,
                address,
                viewableAddresses: params.viewableAddresses,
              })) {
                continue;
              }
              chats.push({
                address,
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
