// Portions adapted from @tencent-weixin/openclaw-weixin (MIT License, Copyright (c) 2026 Tencent).

import type { BackendClient } from "../../backend-client.js";
import {
  isStaleGatewayEpochError,
  type StaleGatewayEpochError,
} from "../../gateway-lease.js";
import {
  createSubsystemLogger,
  type SubsystemLogger,
} from "../../shared/logger.js";
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
  accountId: string;
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
  logger?: SubsystemLogger;
  /**
   * Local lease double-check evaluated on every `getupdates` return before processing
   * inbound messages or committing the cursor (D1 / §3.5).
   */
  isLeaseValid?: () => boolean;
  onInboundEvent: (event: InboundChannelEvent) => Promise<void>;
  onSessionExpired?: (err: WeixinApiError) => Promise<void> | void;
  onCursorCommitted?: (newCursor: string) => void;
  onRateLimited?: (err: WeixinApiError) => Promise<number> | number;
  onStaleEpoch?: (err: StaleGatewayEpochError) => void;
  signal: AbortSignal;
}

export class WeixinMonitor {
  private readonly opts: WeixinMonitorOptions;
  private readonly logger: SubsystemLogger;
  private currentCursor: string;
  private pollTimeoutMs: number;
  private runningPromise: Promise<void> | null = null;

  constructor(opts: WeixinMonitorOptions) {
    this.opts = opts;
    this.logger = opts.logger ?? createSubsystemLogger("weixin/monitor");
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
    const { backend, accountId, botGeneration, ilinkBotId, signal } = this.opts;
    if (signal.aborted) return "retry";
    if (this.opts.isLeaseValid && !this.opts.isLeaseValid()) {
      return "stale";
    }

    try {
      const res = await backend.updateWeixinAccountCursor(accountId, {
        expected_bot_generation: botGeneration,
        expected_ilink_bot_id: ilinkBotId,
        expected_get_updates_buf: this.currentCursor,
        get_updates_buf: nextCursor,
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
    } catch (err) {
      if (isStaleGatewayEpochError(err)) {
        this.opts.onStaleEpoch?.(err);
        throw err;
      }
      if (signal.aborted) return "retry";
      // Network drop after POST .../cursor: check account detail for generation switch;
      // otherwise let commitCursorUntilSettled retry the CAS write idempotently
      // (a retry after an already-committed write returns cas_mismatch with authoritative cursor).
      try {
        const detail = await backend.getWeixinAccountDetail(accountId);
        if (
          detail.bot_generation !== botGeneration ||
          detail.ilink_bot_id !== ilinkBotId ||
          (detail.status !== "active" && detail.status !== "pending_verification")
        ) {
          return "stale";
        }
        if (detail.get_updates_buf === nextCursor) {
          this.currentCursor = nextCursor;
          this.opts.onCursorCommitted?.(nextCursor);
          return "committed";
        }
      } catch (detailErr) {
        if (isStaleGatewayEpochError(detailErr)) {
          this.opts.onStaleEpoch?.(detailErr);
          throw detailErr;
        }
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
      if (this.opts.isLeaseValid && !this.opts.isLeaseValid()) {
        return "stopped";
      }
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
    const { backend, accountId, botGeneration, ilinkBotId } = this.opts;
    try {
      await backend.updateWeixinAccountStatus(accountId, {
        action: "mark_session_expired",
        expected_bot_generation: botGeneration,
        expected_ilink_bot_id: ilinkBotId,
        error_code: err.errcode ?? err.ret ?? -14,
      });
    } catch (statusErr) {
      if (isStaleGatewayEpochError(statusErr)) {
        this.opts.onStaleEpoch?.(statusErr);
        throw statusErr;
      }
      // Best-effort persistence on -14; adapter also pauses local state.
    }
    await this.opts.onSessionExpired?.(err);
  }

  private async runLoop(): Promise<void> {
    const {
      signal,
      accountId,
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
      if (this.opts.isLeaseValid && !this.opts.isLeaseValid()) {
        this.logger.warn(
          "Stopping Weixin poll loop because gateway lease is no longer locally valid",
          {
            account_id: accountId,
            ilink_bot_id: ilinkBotId,
            bot_generation: botGeneration,
          },
        );
        break;
      }

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

        // D1 double-check on every getupdates return before processing inbound messages or advancing cursor.
        if (this.opts.isLeaseValid && !this.opts.isLeaseValid()) {
          this.logger.warn(
            "Discarding getupdates batch because gateway lease expired during long poll",
            {
              account_id: accountId,
              ilink_bot_id: ilinkBotId,
              bot_generation: botGeneration,
            },
          );
          break;
        }

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
          if (this.opts.isLeaseValid && !this.opts.isLeaseValid()) {
            return;
          }
          const event = parseWeixinInboundMessage(msg, {
            accountId,
            ilinkBotId,
            botGeneration,
          });
          try {
            await this.opts.onInboundEvent(event);
          } catch (inboundErr) {
            if (isStaleGatewayEpochError(inboundErr)) {
              this.opts.onStaleEpoch?.(inboundErr);
              return;
            }
            // Inbound registration (I-1) failed (e.g. 503 roster_lookup_error); do not advance cursor so batch is retried.
            batchAllRegistered = false;
            break;
          }
        }

        if (!batchAllRegistered || signal.aborted) {
          if (signal.aborted) break;
          consecutiveFailures += 1;
          const backoffMs = Math.min(
            baseRetryMs * Math.pow(2, Math.min(consecutiveFailures - 1, 4)),
            MAX_RETRY_DELAY_MS,
          );
          const ok = await sleepWithAbort(backoffMs, signal);
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
        if (isStaleGatewayEpochError(err)) {
          this.opts.onStaleEpoch?.(err);
          break;
        }

        if (err instanceof WeixinApiError) {
          if (err.kind === "session_expired") {
            try {
              await this.markExpiredInBackend(err);
            } catch (statusErr) {
              if (isStaleGatewayEpochError(statusErr)) {
                break;
              }
            }
            break;
          }
          if (err.kind === "rate_limited") {
            const delayMs = this.opts.onRateLimited
              ? await this.opts.onRateLimited(err)
              : (err.retryAfterMs ?? baseRetryMs);
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
