// Portions adapted from @tencent-weixin/openclaw-weixin (MIT License, Copyright (c) 2026 Tencent).

import type { BackendClient } from "../../backend-client.js";
import type { HostIpResolver } from "../../shared/safe-fetch.js";
import type { InboundChannelEvent } from "../../types.js";
import {
  DEFAULT_LONG_POLL_TIMEOUT_MS,
  getWeixinUpdates,
  MAX_RETRY_AFTER_MS,
  WeixinApiError,
} from "./api.js";
import { parseWeixinInboundMessage } from "./inbound.js";

const MIN_POLL_TIMEOUT_MS = 1_000;
const MAX_POLL_TIMEOUT_MS = 60_000;
const BASE_RETRY_DELAY_MS = 2_000;
const MAX_RETRY_DELAY_MS = 30_000;

export function sleepWithAbort(
  ms: number,
  signal?: AbortSignal,
): Promise<boolean> {
  if (signal?.aborted) return Promise.resolve(false);
  const clamped = Math.max(0, Math.min(ms, MAX_RETRY_AFTER_MS));
  return new Promise<boolean>((resolve) => {
    const onAbort = () => {
      clearTimeout(timer);
      resolve(false);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve(!signal?.aborted);
    }, clamped);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export interface WeixinMonitorOptions {
  baseUrl: string;
  botToken: string;
  ilinkBotId: string;
  botGeneration: number;
  initialGetUpdatesBuf?: string;
  pollTimeoutMs?: number;
  retryDelayMs?: number;
  allowedApiDomains?: readonly string[];
  resolveHostIps?: HostIpResolver;
  checkResolvedIps?: boolean;
  fetchImpl?: typeof fetch;
  backend: BackendClient;
  onInboundEvent: (event: InboundChannelEvent) => Promise<void>;
  onSessionExpired?: (err: WeixinApiError) => Promise<void> | void;
  onCursorCommitted?: (newCursor: string) => void;
  signal: AbortSignal;
}

export class WeixinMonitor {
  private readonly opts: WeixinMonitorOptions;
  private currentCursor: string;
  private pollTimeoutMs: number;
  private runningPromise: Promise<void> | null = null;

  constructor(opts: WeixinMonitorOptions) {
    this.opts = opts;
    this.currentCursor = opts.initialGetUpdatesBuf ?? "";
    this.pollTimeoutMs = opts.pollTimeoutMs ?? DEFAULT_LONG_POLL_TIMEOUT_MS;
  }

  getCursor(): string {
    return this.currentCursor;
  }

  start(): Promise<void> {
    if (!this.runningPromise) {
      this.runningPromise = this.runLoop();
    }
    return this.runningPromise;
  }

  private async commitCursorWithRecovery(
    nextCursor: string,
  ): Promise<"committed" | "stale" | "retry"> {
    const { backend, botGeneration, ilinkBotId, signal } = this.opts;
    if (signal.aborted) return "retry";

    try {
      const res = await backend.putWeixinState({
        get_updates_buf: nextCursor,
        expected_get_updates_buf: this.currentCursor,
        expected_bot_generation: botGeneration,
        expected_ilink_bot_id: ilinkBotId,
      });
      if (res.stale_generation) {
        return "stale";
      }
      if (res.updated) {
        this.currentCursor = nextCursor;
        this.opts.onCursorCommitted?.(nextCursor);
        return "committed";
      }
      if (res.cas_mismatch) {
        if (typeof res.get_updates_buf === "string") {
          this.currentCursor = res.get_updates_buf;
          this.opts.onCursorCommitted?.(res.get_updates_buf);
        }
        return "committed";
      }
      return "retry";
    } catch {
      // I-9 response may have been lost after commit; read back current state from Postgres.
      if (signal.aborted) return "retry";
      try {
        const current = await backend.getWeixinState({ include_token: false });
        if (
          (current.bot_generation !== undefined &&
            current.bot_generation !== null &&
            current.bot_generation !== botGeneration) ||
          (current.ilink_bot_id && current.ilink_bot_id !== ilinkBotId)
        ) {
          return "stale";
        }
        if (current.get_updates_buf === nextCursor) {
          this.currentCursor = nextCursor;
          this.opts.onCursorCommitted?.(nextCursor);
          return "committed";
        }
      } catch {
        // Ignore read-back error; next poll will safely replay batch from currentCursor.
      }
      return "retry";
    }
  }

  private async commitCursorUntilSettled(
    nextBuf: string,
  ): Promise<"committed" | "stale" | "stopped"> {
    const baseRetryMs = this.opts.retryDelayMs ?? BASE_RETRY_DELAY_MS;
    let attempt = 0;
    while (!this.opts.signal.aborted) {
      const status = await this.commitCursorWithRecovery(nextBuf);
      if (status === "committed" || status === "stale") {
        return status;
      }
      attempt += 1;
      const delayMs = Math.min(
        baseRetryMs * Math.pow(2, Math.min(attempt - 1, 4)),
        MAX_RETRY_DELAY_MS,
      );
      const ok = await sleepWithAbort(delayMs, this.opts.signal);
      if (!ok) return "stopped";
    }
    return "stopped";
  }

  private async markExpiredInBackend(err: WeixinApiError): Promise<void> {
    const { backend, botGeneration, ilinkBotId } = this.opts;
    try {
      await backend.putWeixinState({
        status: "expired",
        expected_bot_generation: botGeneration,
        expected_ilink_bot_id: ilinkBotId,
      });
    } catch {
      // Best-effort persistence on -14; adapter also pauses local state.
    }
    await this.opts.onSessionExpired?.(err);
  }

  private async runLoop(): Promise<void> {
    const {
      signal,
      baseUrl,
      botToken,
      ilinkBotId,
      botGeneration,
      allowedApiDomains,
      resolveHostIps,
      checkResolvedIps,
      fetchImpl,
    } = this.opts;
    const baseRetryMs = this.opts.retryDelayMs ?? BASE_RETRY_DELAY_MS;
    let consecutiveFailures = 0;

    while (!signal.aborted) {
      try {
        const resp = await getWeixinUpdates({
          baseUrl,
          botToken,
          getUpdatesBuf: this.currentCursor,
          timeoutMs: this.pollTimeoutMs,
          signal,
          fetchImpl,
          allowedDomains: allowedApiDomains,
          resolveHostIps,
          checkResolvedIps,
        });

        if (signal.aborted) break;
        consecutiveFailures = 0;

        if (
          typeof resp.longpolling_timeout_ms === "number" &&
          Number.isFinite(resp.longpolling_timeout_ms) &&
          resp.longpolling_timeout_ms > 0
        ) {
          this.pollTimeoutMs = Math.max(
            MIN_POLL_TIMEOUT_MS,
            Math.min(
              Math.trunc(resp.longpolling_timeout_ms),
              MAX_POLL_TIMEOUT_MS,
            ),
          );
        }

        const msgs = Array.isArray(resp.msgs) ? resp.msgs : [];
        let batchAllRegistered = true;

        for (const msg of msgs) {
          if (signal.aborted) {
            batchAllRegistered = false;
            break;
          }
          const event = parseWeixinInboundMessage(msg, {
            ilinkBotId,
            botGeneration,
          });
          try {
            await this.opts.onInboundEvent(event);
          } catch {
            // Inbound registration (I-1) failed; do not advance cursor so batch is retried.
            batchAllRegistered = false;
            break;
          }
        }

        if (!batchAllRegistered || signal.aborted) {
          if (signal.aborted) break;
          consecutiveFailures += 1;
          const ok = await sleepWithAbort(baseRetryMs, signal);
          if (!ok) break;
          continue;
        }

        const nextBuf =
          typeof resp.get_updates_buf === "string" &&
          resp.get_updates_buf.length > 0
            ? resp.get_updates_buf
            : "";

        if (nextBuf && nextBuf !== this.currentCursor) {
          const commitStatus = await this.commitCursorUntilSettled(nextBuf);
          if (commitStatus === "stale" || commitStatus === "stopped") {
            break;
          }
        }
      } catch (err) {
        if (signal.aborted) break;

        if (err instanceof WeixinApiError) {
          if (err.kind === "session_expired") {
            await this.markExpiredInBackend(err);
            break;
          }
          if (err.kind === "rate_limited") {
            const delayMs = err.retryAfterMs ?? baseRetryMs;
            const ok = await sleepWithAbort(delayMs, signal);
            if (!ok) break;
            continue;
          }
        }

        consecutiveFailures += 1;
        const backoffMs = Math.min(
          baseRetryMs * Math.pow(2, Math.min(consecutiveFailures - 1, 4)),
          MAX_RETRY_DELAY_MS,
        );
        const ok = await sleepWithAbort(backoffMs, signal);
        if (!ok) break;
      }
    }
  }
}
