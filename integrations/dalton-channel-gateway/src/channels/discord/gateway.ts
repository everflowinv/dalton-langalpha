/**
 * Standalone Discord Bot Gateway client over `ws` (v10, JSON encoding).
 *
 * Adapted from `.agy/ref_openclaw_channels/discord/dist/.setup/provider-CbmXLYrW.mjs`
 * (lines 4383-4461, 4499-4592, 4680-4978, 8174-8191, 8304-8314: `isFatalGatewayCloseCode`,
 * `canResumeAfterGatewayClose`, `GatewayIdentifyLimiter`, `GatewayHeartbeatTimers`,
 * `GatewaySendLimiter`, `GatewayPlugin`, `fetchDiscordGatewayInfoWithTimeout`,
 * `resolveDiscordGatewayIntents`). License: MIT, Copyright (c) 2025 Peter Steinberger.
 * The Carbon `Plugin` base class, voice state cache, and voice dispatch are removed;
 * only Hello/heartbeat, Identify/Resume, session recovery, and dispatch delivery remain.
 */

import type { APIGatewayBotInfo } from "discord-api-types/v10";
import WebSocket from "ws";
import { createSubsystemLogger, type SubsystemLogger } from "../../shared/logger.js";
import { RequestClient } from "./rest.js";

export const DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json";
/** Guilds | GuildMessages | GuildMessageReactions | DirectMessages | DirectMessageReactions | MessageContent */
export const DISCORD_GATEWAY_INTENTS = 1 | 512 | 1024 | 4096 | 8192 | 32768;

export const FATAL_GATEWAY_CLOSE_CODES = new Set([4004, 4010, 4011, 4012, 4013, 4014]);
export const NON_RESUMABLE_GATEWAY_CLOSE_CODES = new Set([4007, 4009, 4003, 4015]);
/**
 * Discord invalidates the gateway session when a client closes with 1000 or 1001.
 * Reconnects that intend to `Resume` (zombie heartbeat ACK timeout, OP.Reconnect,
 * resumable InvalidSession) close the previous socket with 4000 instead.
 */
export const RESUMABLE_GATEWAY_CLOSE_CODE = 4000;

export function isFatalGatewayCloseCode(code: number): boolean {
  return FATAL_GATEWAY_CLOSE_CODES.has(code);
}
export function canResumeAfterGatewayClose(code: number): boolean {
  return !NON_RESUMABLE_GATEWAY_CLOSE_CODES.has(code);
}

const IDENTIFY_WINDOW_MS = 5000;
const INVALID_SESSION_MIN_DELAY_MS = 1000;
const INVALID_SESSION_JITTER_MS = 4000;
const RESUME_FAILURE_THRESHOLD = 3;
const MAX_PENDING_DISPATCHES = 1000;
const MAX_DISPATCH_RETRY_ATTEMPTS = 8;
const DISPATCH_RETRY_BASE_DELAY_MS = 500;
const DISPATCH_RETRY_MAX_DELAY_MS = 15_000;
export const MAX_RECONNECT_ATTEMPTS = 50;

export interface DiscordWebSocketLike {
  readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  on(event: string, listener: (...args: never[]) => void): unknown;
}

export type DiscordWebSocketFactory = (url: string) => DiscordWebSocketLike;

export interface GatewayTimerDeps {
  setTimeoutFn?: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimeoutFn?: (t: ReturnType<typeof setTimeout>) => void;
  now?: () => number;
  random?: () => number;
}

export class GatewayIdentifyLimiter {
  private state: { lastObservedAt: number; nextAllowedAt: number } | undefined;
  private readonly activeWaiters = new Set<{ timer: ReturnType<typeof setTimeout>; cancel: () => void }>();

  constructor(private readonly deps: GatewayTimerDeps = {}) {}

  async wait(): Promise<boolean> {
    const nowFn = this.deps.now ?? Date.now;
    const setTimeoutFn = this.deps.setTimeoutFn ?? ((fn, ms) => setTimeout(fn, ms));
    const clearTimeoutFn = this.deps.clearTimeoutFn ?? ((t) => clearTimeout(t));
    const now = nowFn();
    const clockMovedBackward = this.state !== undefined && now < this.state.lastObservedAt;
    const nextAllowedAt =
      this.state === undefined
        ? now
        : clockMovedBackward
          ? now + IDENTIFY_WINDOW_MS
          : this.state.nextAllowedAt;
    const waitMs = Math.max(0, nextAllowedAt - now);
    this.state = { lastObservedAt: now, nextAllowedAt: Math.max(now, nextAllowedAt) + IDENTIFY_WINDOW_MS };
    if (waitMs <= 0) return true;
    return new Promise<boolean>((resolve) => {
      const waiter = {
        timer: undefined as unknown as ReturnType<typeof setTimeout>,
        cancel: () => {
          clearTimeoutFn(waiter.timer);
          this.activeWaiters.delete(waiter);
          resolve(false);
        },
      };
      waiter.timer = setTimeoutFn(() => {
        this.activeWaiters.delete(waiter);
        resolve(true);
      }, waitMs);
      (waiter.timer as { unref?: () => void }).unref?.();
      this.activeWaiters.add(waiter);
    });
  }

  cancelAll(): void {
    for (const waiter of [...this.activeWaiters]) {
      waiter.cancel();
    }
  }

  reset(): void {
    this.cancelAll();
    this.state = undefined;
  }
}

export type DiscordGatewayInfo = Pick<APIGatewayBotInfo, "url" | "session_start_limit">;

export function isValidSessionStartLimit(
  v: unknown,
): v is APIGatewayBotInfo["session_start_limit"] {
  if (!isRecord(v)) return false;
  return (
    typeof v.total === "number" && Number.isFinite(v.total) && v.total >= 0 &&
    typeof v.remaining === "number" && Number.isFinite(v.remaining) && v.remaining >= 0 &&
    typeof v.reset_after === "number" && Number.isFinite(v.reset_after) && v.reset_after >= 0 &&
    typeof v.max_concurrency === "number" && Number.isFinite(v.max_concurrency) && v.max_concurrency >= 1
  );
}

export function isValidDiscordGatewayInfo(v: unknown): v is DiscordGatewayInfo {
  if (!isRecord(v)) return false;
  if (typeof v.url !== "string" || !v.url.trim()) return false;
  return isValidSessionStartLimit(v.session_start_limit);
}

export async function fetchDiscordGatewayInfo(
  rest: Pick<RequestClient, "get">,
): Promise<DiscordGatewayInfo> {
  const parsed = await rest.get("/gateway/bot");
  if (!isValidDiscordGatewayInfo(parsed)) {
    throw new Error("Discord /gateway/bot returned an invalid gateway info payload");
  }
  return {
    url: parsed.url.trim(),
    session_start_limit: {
      total: parsed.session_start_limit.total,
      remaining: parsed.session_start_limit.remaining,
      reset_after: parsed.session_start_limit.reset_after,
      max_concurrency: parsed.session_start_limit.max_concurrency,
    },
  };
}

export interface DiscordGatewayPayload {
  op: number;
  d?: unknown;
  s?: number | null;
  t?: string | null;
}

export interface DiscordGatewayClientOptions {
  token: string;
  intents?: number;
  gatewayUrl?: string;
  gatewayInfo?: DiscordGatewayInfo;
  fetchGatewayInfo?: () => Promise<DiscordGatewayInfo>;
  webSocketFactory?: DiscordWebSocketFactory;
  identifyLimiter?: GatewayIdentifyLimiter;
  logger?: SubsystemLogger;
  onDispatch?: (eventName: string, data: unknown, seq: number | null) => Promise<void> | void;
  onReady?: (data: { sessionId: string; resumeGatewayUrl: string | null; botUserId?: string }) => void;
  onError?: (err: Error) => void;
  timerDeps?: GatewayTimerDeps;
}

const OP = { Dispatch: 0, Heartbeat: 1, Identify: 2, Resume: 6, Reconnect: 7, InvalidSession: 9, Hello: 10, HeartbeatAck: 11 } as const;

interface PendingDispatch {
  eventName: string;
  data: unknown;
  seq: number | null;
  sessionEpoch: number;
  messageId: string | null;
  attempts: number;
}

function extractDispatchMessageId(eventName: string, data: unknown): string | null {
  if (eventName !== "MESSAGE_CREATE" || !isRecord(data)) return null;
  return typeof data.id === "string" && data.id.trim().length > 0 ? data.id.trim() : null;
}

export class DiscordGatewayClient {
  private readonly token: string;
  private readonly intents: number;
  private readonly logger: SubsystemLogger;
  private readonly timerDeps: Required<Pick<GatewayTimerDeps, "setTimeoutFn" | "clearTimeoutFn" | "now" | "random">>;
  private readonly identifyLimiter: GatewayIdentifyLimiter;
  private readonly requiresGatewayInfo: boolean;
  private ws: DiscordWebSocketLike | null = null;
  private lastReceivedSequence: number | null = null;
  private ackedSequence: number | null = null;
  private sessionId: string | null = null;
  private resumeGatewayUrl: string | null = null;
  private gatewayUrl: string;
  private sessionStartLimit: DiscordGatewayInfo["session_start_limit"] | null = null;
  private sessionStartResetAt: number | null = null;
  private gatewayInfoLoaded = false;
  private lastHeartbeatAck = true;
  private heartbeatTimer: ReturnType<typeof setTimeout> | undefined;
  private firstHeartbeatTimer: ReturnType<typeof setTimeout> | undefined;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private dispatchRetryTimer: ReturnType<typeof setTimeout> | undefined;
  private readonly activeWaitTimers = new Set<{ timer: ReturnType<typeof setTimeout>; cancel: () => void }>();
  private pendingDispatches: PendingDispatch[] = [];
  private isDrainingDispatches = false;
  private wakeDrainImmediately = false;
  private stopGeneration = 0;
  private refreshGeneration = 0;
  private protocolSessionEpoch = 0;
  private shouldReconnect = false;
  private isConnecting = false;
  private connected = false;
  private reconnectAttempts = 0;
  private consecutiveResumeFailures = 0;

  onDispatch: DiscordGatewayClientOptions["onDispatch"];
  onReady: DiscordGatewayClientOptions["onReady"];
  onError: DiscordGatewayClientOptions["onError"];

  constructor(private readonly options: DiscordGatewayClientOptions) {
    this.token = options.token.replace(/^Bot\s+/i, "");
    this.intents = options.intents ?? DISCORD_GATEWAY_INTENTS;
    this.logger = options.logger ?? createSubsystemLogger("discord/gateway");
    this.onDispatch = options.onDispatch;
    this.onReady = options.onReady;
    this.onError = options.onError;
    this.timerDeps = {
      setTimeoutFn: options.timerDeps?.setTimeoutFn ?? ((fn, ms) => setTimeout(fn, ms)),
      clearTimeoutFn: options.timerDeps?.clearTimeoutFn ?? ((t) => clearTimeout(t)),
      now: options.timerDeps?.now ?? Date.now,
      random: options.timerDeps?.random ?? Math.random,
    };
    this.identifyLimiter = options.identifyLimiter ?? new GatewayIdentifyLimiter(options.timerDeps);
    this.requiresGatewayInfo = Boolean(options.fetchGatewayInfo || options.gatewayInfo !== undefined);
    if (options.gatewayInfo !== undefined && isValidDiscordGatewayInfo(options.gatewayInfo)) {
      this.gatewayUrl = options.gatewayUrl ?? options.gatewayInfo.url.trim();
      this.sessionStartLimit = { ...options.gatewayInfo.session_start_limit };
      this.sessionStartResetAt = this.timerDeps.now() + options.gatewayInfo.session_start_limit.reset_after;
      this.gatewayInfoLoaded = true;
    } else {
      this.gatewayUrl = options.gatewayUrl ?? DISCORD_GATEWAY_URL;
      this.gatewayInfoLoaded = !this.requiresGatewayInfo;
    }
  }

  isConnected(): boolean { return this.connected && this.ws?.readyState === 1; }
  getSessionId(): string | null { return this.sessionId; }
  /** Returns the highest application-acknowledged sequence used for `Resume`. */
  getSequence(): number | null { return this.ackedSequence; }
  /** Returns the latest gateway sequence number received on the socket (used for `Heartbeat`). */
  getLastReceivedSequence(): number | null { return this.lastReceivedSequence; }
  getSessionStartLimit(): DiscordGatewayInfo["session_start_limit"] | null {
    return this.sessionStartLimit ? { ...this.sessionStartLimit } : null;
  }

  private async refreshGatewayInfo(
    expectedStopGen = this.stopGeneration,
  ): Promise<"ok" | "failed" | "stale"> {
    if (!this.options.fetchGatewayInfo) {
      return !this.shouldReconnect || expectedStopGen !== this.stopGeneration ? "stale" : "failed";
    }
    const refreshGen = ++this.refreshGeneration;
    try {
      const info = await this.options.fetchGatewayInfo();
      if (
        !this.shouldReconnect ||
        expectedStopGen !== this.stopGeneration ||
        refreshGen !== this.refreshGeneration
      ) {
        return "stale";
      }
      if (!isValidDiscordGatewayInfo(info)) {
        throw new Error("Discord /gateway/bot returned an invalid gateway info payload");
      }
      if (!this.options.gatewayUrl) {
        this.gatewayUrl = info.url.trim();
      }
      this.sessionStartLimit = { ...info.session_start_limit };
      this.sessionStartResetAt = this.timerDeps.now() + info.session_start_limit.reset_after;
      this.gatewayInfoLoaded = true;
      return "ok";
    } catch (err) {
      if (
        !this.shouldReconnect ||
        expectedStopGen !== this.stopGeneration ||
        refreshGen !== this.refreshGeneration
      ) {
        return "stale";
      }
      this.gatewayInfoLoaded = false;
      const error = err instanceof Error ? err : new Error(String(err));
      this.logger.warn("Discord gateway info lookup failed", { error });
      this.emitError(error);
      return "failed";
    }
  }

  async start(): Promise<void> {
    const stopGen = ++this.stopGeneration;
    this.shouldReconnect = true;
    if (this.options.fetchGatewayInfo) {
      const status = await this.refreshGatewayInfo(stopGen);
      if (!this.shouldReconnect || stopGen !== this.stopGeneration || status === "stale") return;
      if (status !== "ok") {
        this.scheduleReconnect({ preferResume: false, reason: "gateway-info-error" });
        return;
      }
    } else if (this.requiresGatewayInfo && !this.gatewayInfoLoaded) {
      this.emitError(new Error("Discord gateway info is invalid"));
      this.scheduleReconnect({ preferResume: false, reason: "gateway-info-error" });
      return;
    }
    if (!this.shouldReconnect || stopGen !== this.stopGeneration) return;
    this.connect(false);
  }

  /** Stops the socket, heartbeat, and any pending reconnect/retry/quota waits; no further reconnects occur. */
  async stop(): Promise<void> {
    this.shouldReconnect = false;
    this.stopGeneration += 1;
    this.refreshGeneration += 1;
    this.protocolSessionEpoch += 1;
    this.wakeDrainImmediately = false;
    this.stopHeartbeat();
    if (this.reconnectTimer) {
      this.timerDeps.clearTimeoutFn(this.reconnectTimer);
      this.reconnectTimer = undefined;
    }
    this.clearDispatchRetryTimer();
    this.cancelAllWaitTimers();
    this.identifyLimiter.cancelAll();
    this.pendingDispatches = [];
    this.connected = false;
    this.isConnecting = false;
    const ws = this.ws;
    this.ws = null;
    try { ws?.close(1000, "Client disconnect"); } catch { /* socket already closed */ }
  }

  private waitCancellable(ms: number): Promise<boolean> {
    const delayMs = Math.max(0, ms);
    if (delayMs <= 0) return Promise.resolve(this.shouldReconnect);
    return new Promise<boolean>((resolve) => {
      const entry = {
        timer: undefined as unknown as ReturnType<typeof setTimeout>,
        cancel: () => {
          this.timerDeps.clearTimeoutFn(entry.timer);
          this.activeWaitTimers.delete(entry);
          resolve(false);
        },
      };
      entry.timer = this.timerDeps.setTimeoutFn(() => {
        this.activeWaitTimers.delete(entry);
        resolve(this.shouldReconnect);
      }, delayMs);
      (entry.timer as { unref?: () => void }).unref?.();
      this.activeWaitTimers.add(entry);
    });
  }

  private cancelAllWaitTimers(): void {
    for (const entry of [...this.activeWaitTimers]) {
      entry.cancel();
    }
  }

  private clearDispatchRetryTimer(): void {
    if (this.dispatchRetryTimer) {
      this.timerDeps.clearTimeoutFn(this.dispatchRetryTimer);
      this.dispatchRetryTimer = undefined;
    }
  }

  private connect(resume: boolean): void {
    if (!this.shouldReconnect || this.isConnecting) return;
    if (!resume && this.requiresGatewayInfo && !this.gatewayInfoLoaded) {
      void this.connectAfterGatewayInfo();
      return;
    }
    this.stopHeartbeat();
    if (this.reconnectTimer) {
      this.timerDeps.clearTimeoutFn(this.reconnectTimer);
      this.reconnectTimer = undefined;
    }
    const previousWs = this.ws;
    this.ws = null;
    try { previousWs?.close(RESUMABLE_GATEWAY_CLOSE_CODE, "Reconnecting"); } catch { /* ignore */ }
    const base = resume && this.resumeGatewayUrl ? this.resumeGatewayUrl : this.gatewayUrl;
    const url = ensureGatewayParams(base);
    const factory =
      this.options.webSocketFactory ??
      ((u: string) => new WebSocket(u, { maxPayload: 16 * 1024 * 1024, handshakeTimeout: 30_000 }) as unknown as DiscordWebSocketLike);
    const socket = factory(url);
    this.ws = socket;
    this.isConnecting = true;
    this.connected = false;
    this.lastHeartbeatAck = true;
    this.setupSocket(socket, resume);
  }

  private async connectAfterGatewayInfo(): Promise<void> {
    if (!this.shouldReconnect || this.isConnecting) return;
    const stopGen = this.stopGeneration;
    const status = await this.refreshGatewayInfo(stopGen);
    if (!this.shouldReconnect || stopGen !== this.stopGeneration || status === "stale") return;
    if (status !== "ok") {
      if (!this.options.fetchGatewayInfo) {
        this.emitError(new Error("Discord gateway info is invalid"));
      }
      this.scheduleReconnect({ preferResume: false, reason: "gateway-info-error" });
      return;
    }
    this.connect(false);
  }

  private setupSocket(socket: DiscordWebSocketLike, resume: boolean): void {
    socket.on("open", () => {
      if (socket !== this.ws) return;
      this.isConnecting = false;
    });
    socket.on("message", (incoming: never) => {
      if (socket !== this.ws) return;
      const payload = decodePayload(incoming as unknown);
      if (!payload) {
        this.emitError(new Error("Invalid Discord gateway payload"));
        return;
      }
      void this.handlePayload(payload, resume, socket).catch((err: unknown) => {
        if (socket !== this.ws) return;
        this.emitError(err instanceof Error ? err : new Error(String(err)));
      });
    });
    socket.on("close", ((code: number) => {
      if (socket !== this.ws) return;
      this.handleClose(typeof code === "number" ? code : 1006);
    }) as (...args: never[]) => void);
    socket.on("error", ((err: Error) => {
      if (socket !== this.ws) return;
      this.emitError(err instanceof Error ? err : new Error(String(err)));
    }) as (...args: never[]) => void);
  }

  private handleClose(code: number): void {
    this.stopHeartbeat();
    this.clearDispatchRetryTimer();
    this.cancelAllWaitTimers();
    this.isConnecting = false;
    this.connected = false;
    this.ws = null;
    if (!this.shouldReconnect) return;
    if (isFatalGatewayCloseCode(code)) {
      this.shouldReconnect = false;
      this.resetSession();
      this.emitError(new Error(`Fatal Discord gateway close code: ${code}`));
      return;
    }
    const canResume = canResumeAfterGatewayClose(code);
    if (!canResume) this.resetSession();
    this.scheduleReconnect({ preferResume: canResume, reason: "close" });
  }

  private async handlePayload(payload: DiscordGatewayPayload, resume: boolean, socket: DiscordWebSocketLike): Promise<void> {
    if (socket !== this.ws || !this.shouldReconnect) return;
    if (payload.s !== null && payload.s !== undefined) {
      this.lastReceivedSequence = payload.s;
    }
    switch (payload.op) {
      case OP.Hello: {
        const interval = isRecord(payload.d) && typeof payload.d.heartbeat_interval === "number" ? payload.d.heartbeat_interval : 45_000;
        this.startHeartbeat(interval);
        const resumeState = resume ? this.getResumeState() : null;
        if (resumeState) {
          this.send({ op: OP.Resume, d: { token: this.token, session_id: resumeState.sessionId, seq: resumeState.sequence } });
        } else {
          await this.identify(socket);
        }
        break;
      }
      case OP.HeartbeatAck:
        this.lastHeartbeatAck = true;
        break;
      case OP.Heartbeat:
        this.sendHeartbeat();
        break;
      case OP.Dispatch:
        await this.handleDispatch(payload, socket);
        break;
      case OP.InvalidSession: {
        const resumable = payload.d === true;
        if (!resumable) this.resetSession();
        this.scheduleReconnect({
          preferResume: resumable,
          reason: "invalid-session",
          minDelayMs: INVALID_SESSION_MIN_DELAY_MS + Math.floor(this.timerDeps.random() * INVALID_SESSION_JITTER_MS),
        });
        break;
      }
      case OP.Reconnect:
        this.scheduleReconnect({ preferResume: true, reason: "reconnect-opcode" });
        break;
      default:
        break;
    }
  }

  private async handleDispatch(payload: DiscordGatewayPayload, socket: DiscordWebSocketLike): Promise<void> {
    if (socket !== this.ws || !this.shouldReconnect) return;
    if (!payload.t) return;
    const seq = payload.s ?? null;

    if (payload.t === "READY" && isRecord(payload.d)) {
      this.protocolSessionEpoch += 1;
      for (const item of this.pendingDispatches) {
        item.attempts = 0;
      }
      this.clearDispatchRetryTimer();
      if (this.isDrainingDispatches) {
        this.wakeDrainImmediately = true;
      }
      this.sessionId = typeof payload.d.session_id === "string" ? payload.d.session_id : null;
      this.resumeGatewayUrl = typeof payload.d.resume_gateway_url === "string" ? payload.d.resume_gateway_url : null;
      this.ackedSequence = seq;
      this.reconnectAttempts = 0;
      this.consecutiveResumeFailures = 0;
      this.connected = true;
      const user = isRecord(payload.d.user) ? payload.d.user : undefined;
      this.onReady?.({
        sessionId: this.sessionId ?? "",
        resumeGatewayUrl: this.resumeGatewayUrl,
        botUserId: user && typeof user.id === "string" ? user.id : undefined,
      });
    } else if (payload.t === "RESUMED") {
      this.reconnectAttempts = 0;
      this.consecutiveResumeFailures = 0;
      this.connected = true;
      for (const item of this.pendingDispatches) {
        item.attempts = 0;
      }
      this.clearDispatchRetryTimer();
      if (this.isDrainingDispatches) {
        this.wakeDrainImmediately = true;
      }
    }

    const currentEpoch = this.protocolSessionEpoch;
    const messageId = extractDispatchMessageId(payload.t, payload.d);

    if (payload.t !== "READY" && payload.t !== "RESUMED") {
      if (
        seq !== null &&
        this.ackedSequence !== null &&
        seq <= this.ackedSequence &&
        !this.pendingDispatches.some(
          (item) =>
            (item.sessionEpoch === currentEpoch && item.seq === seq) ||
            (messageId !== null && item.messageId === messageId),
        )
      ) {
        return;
      }
      const existing = this.pendingDispatches.find(
        (item) =>
          (seq !== null && item.sessionEpoch === currentEpoch && item.seq === seq) ||
          (messageId !== null && item.messageId === messageId),
      );
      if (existing) {
        if (seq !== null && existing.sessionEpoch === currentEpoch) {
          existing.seq = seq;
        }
        existing.attempts = 0;
        this.clearDispatchRetryTimer();
        if (this.isDrainingDispatches) {
          this.wakeDrainImmediately = true;
        }
        await this.drainPendingDispatches();
        return;
      }
    }

    if (this.pendingDispatches.length >= MAX_PENDING_DISPATCHES) {
      this.emitError(new Error(`Discord gateway pending dispatch queue overflow (${MAX_PENDING_DISPATCHES})`));
      this.scheduleReconnect({ preferResume: true, reason: "dispatch-queue-overflow" });
      return;
    }

    this.pendingDispatches.push({
      eventName: payload.t,
      data: payload.d,
      seq,
      sessionEpoch: currentEpoch,
      messageId,
      attempts: 0,
    });

    if (this.pendingDispatches[0] && this.pendingDispatches[0].sessionEpoch !== currentEpoch) {
      this.clearDispatchRetryTimer();
    }

    await this.drainPendingDispatches();
  }

  private async drainPendingDispatches(): Promise<void> {
    if (this.isDrainingDispatches || !this.shouldReconnect || this.dispatchRetryTimer) return;
    this.isDrainingDispatches = true;
    const stopGen = this.stopGeneration;
    try {
      while (this.shouldReconnect && stopGen === this.stopGeneration && this.pendingDispatches.length > 0) {
        const current = this.pendingDispatches[0]!;
        this.wakeDrainImmediately = false;
        try {
          await this.onDispatch?.(current.eventName, current.data, current.seq ?? this.ackedSequence);
        } catch (err) {
          if (!this.shouldReconnect || stopGen !== this.stopGeneration) return;
          this.emitError(err instanceof Error ? err : new Error(String(err)));
          if (this.wakeDrainImmediately) {
            this.wakeDrainImmediately = false;
            current.attempts = 0;
            continue;
          }
          current.attempts += 1;
          if (current.attempts >= MAX_DISPATCH_RETRY_ATTEMPTS) {
            current.attempts = 0;
            this.scheduleReconnect({ preferResume: true, reason: "dispatch-retry-exhausted" });
            return;
          }
          const retryDelayMs = Math.min(
            DISPATCH_RETRY_MAX_DELAY_MS,
            DISPATCH_RETRY_BASE_DELAY_MS * 2 ** (current.attempts - 1),
          );
          this.scheduleDispatchRetry(retryDelayMs, stopGen);
          return;
        }
        if (!this.shouldReconnect || stopGen !== this.stopGeneration) return;
        if (this.pendingDispatches[0] === current) {
          this.pendingDispatches.shift();
        }
        if (
          current.sessionEpoch === this.protocolSessionEpoch &&
          current.seq !== null &&
          (this.ackedSequence === null || current.seq > this.ackedSequence)
        ) {
          this.ackedSequence = current.seq;
        }
      }
    } finally {
      this.isDrainingDispatches = false;
    }
  }

  private scheduleDispatchRetry(delayMs: number, stopGen: number): void {
    this.clearDispatchRetryTimer();
    if (!this.shouldReconnect || stopGen !== this.stopGeneration) return;
    this.dispatchRetryTimer = this.timerDeps.setTimeoutFn(() => {
      this.dispatchRetryTimer = undefined;
      if (!this.shouldReconnect || stopGen !== this.stopGeneration) return;
      void this.drainPendingDispatches();
    }, Math.max(0, delayMs));
    (this.dispatchRetryTimer as { unref?: () => void }).unref?.();
  }

  private startHeartbeat(intervalMs: number): void {
    this.stopHeartbeat();
    const scheduleCycle = (): void => {
      this.heartbeatTimer = this.timerDeps.setTimeoutFn(() => {
        this.heartbeatTimer = undefined;
        if (!this.lastHeartbeatAck) {
          this.emitError(new Error("Discord gateway heartbeat ACK timeout"));
          this.scheduleReconnect({ preferResume: true, reason: "zombie" });
          return;
        }
        this.sendHeartbeat();
        scheduleCycle();
      }, intervalMs);
      (this.heartbeatTimer as { unref?: () => void }).unref?.();
    };
    this.firstHeartbeatTimer = this.timerDeps.setTimeoutFn(() => {
      this.firstHeartbeatTimer = undefined;
      this.sendHeartbeat();
      scheduleCycle();
    }, Math.max(0, Math.floor(intervalMs * this.timerDeps.random())));
    (this.firstHeartbeatTimer as { unref?: () => void }).unref?.();
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) { this.timerDeps.clearTimeoutFn(this.heartbeatTimer); this.heartbeatTimer = undefined; }
    if (this.firstHeartbeatTimer) { this.timerDeps.clearTimeoutFn(this.firstHeartbeatTimer); this.firstHeartbeatTimer = undefined; }
  }

  private sendHeartbeat(): void {
    if (!this.ws || this.ws.readyState !== 1) return;
    this.lastHeartbeatAck = false;
    this.send({ op: OP.Heartbeat, d: this.lastReceivedSequence });
  }

  private async identify(socket: DiscordWebSocketLike): Promise<void> {
    const stopGen = this.stopGeneration;
    if (socket !== this.ws || !this.shouldReconnect) return;

    if (this.requiresGatewayInfo && !this.gatewayInfoLoaded) {
      const status = await this.refreshGatewayInfo(stopGen);
      if (socket !== this.ws || !this.shouldReconnect || stopGen !== this.stopGeneration || status === "stale") return;
      if (status !== "ok") {
        this.scheduleReconnect({ preferResume: false, reason: "gateway-info-error" });
        return;
      }
    }

    if (this.sessionStartLimit && this.sessionStartLimit.remaining <= 0) {
      const now = this.timerDeps.now();
      const waitMs =
        this.sessionStartResetAt !== null
          ? Math.max(0, this.sessionStartResetAt - now)
          : Math.max(0, this.sessionStartLimit.reset_after);
      if (waitMs > 0) {
        this.logger.warn("Discord session_start_limit exhausted; waiting for reset_after before Identify", {
          resetAfterMs: waitMs,
        });
        const waited = await this.waitCancellable(waitMs);
        if (!waited || socket !== this.ws || !this.shouldReconnect || stopGen !== this.stopGeneration) return;
      }

      if (!this.options.fetchGatewayInfo) {
        this.emitError(new Error("Discord session_start_limit exhausted and no fetchGatewayInfo source is configured"));
        this.scheduleReconnect({
          preferResume: false,
          reason: "session-start-limit-exhausted",
        });
        return;
      }

      const status = await this.refreshGatewayInfo(stopGen);
      if (socket !== this.ws || !this.shouldReconnect || stopGen !== this.stopGeneration || status === "stale") return;
      if (status !== "ok") {
        this.scheduleReconnect({ preferResume: false, reason: "gateway-info-error" });
        return;
      }

      if (this.sessionStartLimit && this.sessionStartLimit.remaining <= 0) {
        const remainingResetMs =
          this.sessionStartResetAt !== null
            ? Math.max(1000, this.sessionStartResetAt - this.timerDeps.now())
            : Math.max(1000, this.sessionStartLimit.reset_after);
        this.scheduleReconnect({
          preferResume: false,
          reason: "session-start-limit-exhausted",
          minDelayMs: remainingResetMs,
        });
        return;
      }
    }

    const allowed = await this.identifyLimiter.wait();
    if (!allowed || socket !== this.ws || !this.shouldReconnect || stopGen !== this.stopGeneration) return;
    if (socket.readyState !== 1) {
      this.scheduleReconnect({ preferResume: false, reason: "identify" });
      return;
    }

    if (this.sessionStartLimit) {
      this.sessionStartLimit.remaining = Math.max(0, this.sessionStartLimit.remaining - 1);
    }

    this.send({
      op: OP.Identify,
      d: {
        token: this.token,
        intents: this.intents,
        properties: { os: process.platform, browser: "dalton-channel-gateway", device: "dalton-channel-gateway" },
      },
    });
  }

  private send(payload: DiscordGatewayPayload): void {
    if (!this.ws || this.ws.readyState !== 1) throw new Error("Discord gateway socket is not open");
    this.ws.send(JSON.stringify(payload));
  }

  private getResumeState(): { sessionId: string; sequence: number } | null {
    return this.sessionId && this.ackedSequence !== null
      ? { sessionId: this.sessionId, sequence: this.ackedSequence }
      : null;
  }

  private resetSession(): void {
    this.protocolSessionEpoch += 1;
    this.clearDispatchRetryTimer();
    this.sessionId = null;
    this.resumeGatewayUrl = null;
    this.lastReceivedSequence = null;
    this.ackedSequence = null;
    this.consecutiveResumeFailures = 0;
  }

  private scheduleReconnect(options: { preferResume: boolean; reason: string; minDelayMs?: number }): void {
    if (!this.shouldReconnect) return;
    this.stopHeartbeat();
    this.clearDispatchRetryTimer();
    this.cancelAllWaitTimers();
    if (this.reconnectTimer) this.timerDeps.clearTimeoutFn(this.reconnectTimer);
    const ws = this.ws;
    this.ws = null;
    this.isConnecting = false;
    this.connected = false;
    try { ws?.close(RESUMABLE_GATEWAY_CLOSE_CODE, "Reconnecting"); } catch { /* ignore */ }
    this.reconnectAttempts += 1;
    if (this.reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
      this.shouldReconnect = false;
      this.emitError(new Error(`Max Discord gateway reconnect attempts (${MAX_RECONNECT_ATTEMPTS}) reached (${options.reason})`));
      return;
    }
    let shouldResume = options.preferResume && this.getResumeState() !== null;
    if (shouldResume && this.consecutiveResumeFailures >= RESUME_FAILURE_THRESHOLD) {
      this.resetSession();
      shouldResume = false;
    }
    if (shouldResume) this.consecutiveResumeFailures += 1;
    else this.consecutiveResumeFailures = 0;
    const backoff = Math.min(30_000, 1000 * 2 ** Math.min(this.reconnectAttempts, 5));
    const delay = Math.max(options.minDelayMs ?? 0, backoff);
    this.reconnectTimer = this.timerDeps.setTimeoutFn(() => {
      this.reconnectTimer = undefined;
      this.connect(shouldResume);
    }, delay);
    (this.reconnectTimer as { unref?: () => void }).unref?.();
  }

  private emitError(err: Error): void {
    try { this.onError?.(err); } catch { /* listener errors must not crash gateway */ }
    this.logger.error("Discord gateway error", { error: err });
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function ensureGatewayParams(url: string): string {
  const parsed = new URL(url);
  if (!parsed.searchParams.get("v")) parsed.searchParams.set("v", "10");
  if (!parsed.searchParams.get("encoding")) parsed.searchParams.set("encoding", "json");
  return parsed.toString();
}

function decodePayload(incoming: unknown): DiscordGatewayPayload | null {
  let text: string;
  if (typeof incoming === "string") text = incoming;
  else if (Buffer.isBuffer(incoming)) text = incoming.toString("utf8");
  else if (incoming instanceof ArrayBuffer) text = Buffer.from(incoming).toString("utf8");
  else if (Array.isArray(incoming)) text = Buffer.concat(incoming.map((e) => Buffer.from(e as ArrayBuffer))).toString("utf8");
  else text = String(incoming);
  try { return JSON.parse(text) as DiscordGatewayPayload; } catch { return null; }
}
