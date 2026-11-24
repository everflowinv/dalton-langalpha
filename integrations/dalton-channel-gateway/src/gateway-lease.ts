import { randomUUID } from "node:crypto";
import { createSubsystemLogger, type SubsystemLogger } from "./shared/logger.js";

export const DEFAULT_GATEWAY_LEASE_TTL_SECONDS = 45;
export const DEFAULT_GATEWAY_LEASE_HEARTBEAT_MS = 15_000;
export const DEFAULT_GATEWAY_LEASE_SAFETY_MARGIN_MS = 5_000;
export const DEFAULT_GATEWAY_LEASE_ACQUIRE_RETRY_MS = 3_000;

export class StaleGatewayEpochError extends Error {
  readonly code = "stale_gateway_epoch";
  readonly status?: number;
  readonly epoch: number | null;

  constructor(
    message = "Gateway lease epoch is stale (HTTP 409)",
    options?: { status?: number; epoch?: number | null; cause?: unknown },
  ) {
    super(message, options?.cause !== undefined ? { cause: options.cause } : undefined);
    this.name = "StaleGatewayEpochError";
    this.status = options?.status ?? 409;
    this.epoch = options?.epoch ?? null;
  }
}

export function isStaleGatewayEpochError(
  err: unknown,
): err is StaleGatewayEpochError {
  return (
    err instanceof StaleGatewayEpochError ||
    (typeof err === "object" &&
      err !== null &&
      "code" in err &&
      (err as { code?: unknown }).code === "stale_gateway_epoch")
  );
}

export interface GatewayLeaseBackend {
  acquireGatewayLease(req: {
    instance_id: string;
    ttl_seconds: number;
  }): Promise<{
    granted: boolean;
    epoch: number;
    expires_at: string;
    retry_after_ms?: number;
  }>;
  heartbeatGatewayLease(req: {
    instance_id: string;
    epoch: number;
    ttl_seconds: number;
  }): Promise<{
    ok: boolean;
    epoch: number;
    expires_at: string;
  }>;
  releaseGatewayLease(req: {
    instance_id: string;
    epoch: number;
  }): Promise<{
    ok: boolean;
    released: boolean;
  }>;
}

export interface GatewayLeaseManagerOptions {
  backend: GatewayLeaseBackend;
  instanceId?: string;
  ttlSeconds?: number;
  heartbeatIntervalMs?: number;
  safetyMarginMs?: number;
  acquireRetryMs?: number;
  logger?: SubsystemLogger;
  onEpochAcquired?: (epoch: number) => void;
  onLeaseLost?: (reason: string, error?: unknown) => void;
  /** Invoked when a heartbeat succeeds after the local lease had transiently dipped into the safety margin. */
  onLeaseRecovered?: (epoch: number) => void;
}

async function sleepAbortable(ms: number, signal?: AbortSignal): Promise<void> {
  if (ms <= 0) {
    return;
  }
  if (signal?.aborted) {
    throw new Error("Gateway lease acquisition aborted");
  }
  await new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      reject(new Error("Gateway lease acquisition aborted"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function parseExpiresAtMs(expiresAt: string | undefined, ttlSeconds: number): number {
  if (expiresAt) {
    const parsed = Date.parse(expiresAt);
    if (Number.isFinite(parsed) && parsed > 0) {
      return parsed;
    }
  }
  return Date.now() + ttlSeconds * 1000;
}

export class GatewayLeaseManager {
  readonly instanceId: string;
  private readonly backend: GatewayLeaseBackend;
  private readonly ttlSeconds: number;
  private readonly heartbeatIntervalMs: number;
  private readonly safetyMarginMs: number;
  private readonly acquireRetryMs: number;
  private readonly logger: SubsystemLogger;
  private readonly onEpochAcquired?: (epoch: number) => void;
  private readonly onLeaseLost?: (reason: string, error?: unknown) => void;
  private readonly onLeaseRecovered?: (epoch: number) => void;
  private readonly leaseRecoveredListeners = new Set<(epoch: number) => void>();

  private currentEpoch: number | null = null;
  private expiresAtMs = 0;
  private lastHeartbeatOk = false;
  private leaseLost = false;
  private stopped = false;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private heartbeatInFlight = false;

  constructor(options: GatewayLeaseManagerOptions) {
    this.backend = options.backend;
    this.instanceId = options.instanceId?.trim() || `gw-${randomUUID()}`;
    this.ttlSeconds = options.ttlSeconds ?? DEFAULT_GATEWAY_LEASE_TTL_SECONDS;
    this.heartbeatIntervalMs =
      options.heartbeatIntervalMs ?? DEFAULT_GATEWAY_LEASE_HEARTBEAT_MS;
    this.safetyMarginMs =
      options.safetyMarginMs ?? DEFAULT_GATEWAY_LEASE_SAFETY_MARGIN_MS;
    this.acquireRetryMs =
      options.acquireRetryMs ?? DEFAULT_GATEWAY_LEASE_ACQUIRE_RETRY_MS;
    this.logger = options.logger ?? createSubsystemLogger("gateway-lease");
    this.onEpochAcquired = options.onEpochAcquired;
    this.onLeaseLost = options.onLeaseLost;
    this.onLeaseRecovered = options.onLeaseRecovered;
  }

  addLeaseRecoveredListener(listener: (epoch: number) => void): () => void {
    this.leaseRecoveredListeners.add(listener);
    return () => {
      this.leaseRecoveredListeners.delete(listener);
    };
  }

  get epoch(): number | null {
    return this.currentEpoch;
  }

  get isLost(): boolean {
    return this.leaseLost;
  }

  get leaseExpiresAtMs(): number {
    return this.expiresAtMs;
  }

  /**
   * Local double-check before processing a `getupdates` batch or committing side effects (D1 / §3.5):
   * requires an acquired epoch, healthy heartbeat state, and remaining TTL > safety margin (5s).
   */
  isLeaseLocallyValid(nowMs: number = Date.now()): boolean {
    return (
      !this.stopped &&
      !this.leaseLost &&
      this.currentEpoch !== null &&
      this.lastHeartbeatOk &&
      this.expiresAtMs - nowMs > this.safetyMarginMs
    );
  }

  async acquireUntilGranted(signal?: AbortSignal): Promise<number> {
    this.stopped = false;
    this.leaseLost = false;
    let attempt = 0;

    while (!this.stopped && !signal?.aborted) {
      attempt += 1;
      try {
        const res = await this.backend.acquireGatewayLease({
          instance_id: this.instanceId,
          ttl_seconds: this.ttlSeconds,
        });
        if (res.granted && Number.isInteger(res.epoch) && res.epoch > 0) {
          this.currentEpoch = res.epoch;
          this.expiresAtMs = parseExpiresAtMs(res.expires_at, this.ttlSeconds);
          this.lastHeartbeatOk = true;
          this.leaseLost = false;
          this.onEpochAcquired?.(res.epoch);
          this.startHeartbeat();
          this.logger.info("Gateway deployment lease acquired", {
            instance_id: this.instanceId,
            epoch: res.epoch,
            ttl_seconds: this.ttlSeconds,
          });
          return res.epoch;
        }

        const waitMs = Math.max(
          50,
          res.retry_after_ms ?? this.acquireRetryMs,
        );
        this.logger.info(
          "Gateway deployment lease currently held by another instance; waiting to retry",
          {
            instance_id: this.instanceId,
            attempt,
            retry_after_ms: waitMs,
          },
        );
        await sleepAbortable(waitMs, signal);
      } catch (err) {
        if (signal?.aborted || this.stopped) {
          throw err;
        }
        this.logger.warn("Gateway lease acquire attempt failed; retrying", {
          instance_id: this.instanceId,
          attempt,
          error: err instanceof Error ? err.message : String(err),
        });
        await sleepAbortable(this.acquireRetryMs, signal);
      }
    }

    throw new Error("Gateway lease acquisition aborted");
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      void this.runHeartbeatOnce();
    }, this.heartbeatIntervalMs);
    this.heartbeatTimer.unref?.();
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  async runHeartbeatOnce(): Promise<boolean> {
    if (this.stopped || this.leaseLost || this.currentEpoch === null) {
      return false;
    }
    if (this.heartbeatInFlight) {
      return this.lastHeartbeatOk;
    }
    this.heartbeatInFlight = true;
    const epoch = this.currentEpoch;
    try {
      const res = await this.backend.heartbeatGatewayLease({
        instance_id: this.instanceId,
        epoch,
        ttl_seconds: this.ttlSeconds,
      });
      if (this.stopped || this.leaseLost) {
        return false;
      }
      if (!res.ok) {
        this.lastHeartbeatOk = false;
        this.notifyLeaseLost("heartbeat_rejected");
        return false;
      }
      const wasLocallyValid = this.isLeaseLocallyValid();
      this.expiresAtMs = parseExpiresAtMs(res.expires_at, this.ttlSeconds);
      this.lastHeartbeatOk = true;
      if (!wasLocallyValid && this.isLeaseLocallyValid()) {
        this.onLeaseRecovered?.(epoch);
        for (const listener of this.leaseRecoveredListeners) {
          try {
            listener(epoch);
          } catch {
            // Ignore listener errors during lease recovery notification
          }
        }
      }
      return true;
    } catch (err) {
      if (this.stopped || this.leaseLost) {
        return false;
      }
      if (isStaleGatewayEpochError(err)) {
        this.lastHeartbeatOk = false;
        this.notifyLeaseLost("heartbeat_stale_epoch", err);
        return false;
      }
      const remainingMs = this.expiresAtMs - Date.now();
      this.lastHeartbeatOk = remainingMs > this.safetyMarginMs;
      this.logger.warn("Gateway lease heartbeat failed", {
        instance_id: this.instanceId,
        epoch,
        remaining_ms: Math.max(0, remainingMs),
        error: err instanceof Error ? err.message : String(err),
      });
      if (remainingMs <= 0) {
        this.notifyLeaseLost("heartbeat_expired", err);
      }
      return false;
    } finally {
      this.heartbeatInFlight = false;
    }
  }

  /**
   * Idempotently marks the lease lost (triggered by any HTTP 409 `stale_gateway_epoch`
   * response or expired heartbeat) and invokes `onLeaseLost` once.
   */
  notifyLeaseLost(reason: string, error?: unknown): void {
    if (this.leaseLost) {
      return;
    }
    this.leaseLost = true;
    this.lastHeartbeatOk = false;
    this.stopHeartbeat();
    this.logger.error("Gateway deployment lease lost; stopping instance", {
      instance_id: this.instanceId,
      epoch: this.currentEpoch,
      reason,
    });
    this.onLeaseLost?.(reason, error);
  }

  async stopAndRelease(): Promise<void> {
    this.stopped = true;
    this.stopHeartbeat();
    const epoch = this.currentEpoch;
    this.currentEpoch = null;
    this.lastHeartbeatOk = false;
    if (epoch !== null && !this.leaseLost) {
      try {
        await this.backend.releaseGatewayLease({
          instance_id: this.instanceId,
          epoch,
        });
      } catch (err) {
        this.logger.warn("Best-effort gateway lease release failed", {
          instance_id: this.instanceId,
          epoch,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }
  }
}
