import type { AutomationNotificationService } from "./automation-notifications.js";
import type {
  BackendClient,
  DeferredDeliveryDueRow,
} from "./backend-client.js";
import type { DataTaskNotificationService } from "./data-task-notifications.js";
import type { DeliveryService } from "./delivery-service.js";
import {
  type GatewayLeaseManager,
  isLeaseSafetyMarginPauseError,
  isStaleGatewayEpochError,
} from "./gateway-lease.js";
import { createSubsystemLogger, type SubsystemLogger } from "./shared/logger.js";

/** Contract §7.3 / Batch B Round 3 Supplement Decision #4: poll due deferred deliveries every 30s while lease is held. */
export const DEFAULT_DEFERRED_DELIVERY_POLL_INTERVAL_MS = 30_000;
export const DEFAULT_DEFERRED_DELIVERY_POLL_LIMIT = 50;

export interface DeferredDeliveryPollerOptions {
  backend: BackendClient;
  deliveryService: Pick<
    DeliveryService,
    "hasDeferredPayload" | "replayDeferredDelivery"
  >;
  dataTaskNotifications: Pick<
    DataTaskNotificationService,
    "handleDataTaskNotification"
  >;
  automationNotifications: Pick<AutomationNotificationService, "handle">;
  leaseManager?: GatewayLeaseManager;
  intervalMs?: number;
  limit?: number;
  onFallbackReplay?: (row: DeferredDeliveryDueRow) => Promise<void>;
  logger?: SubsystemLogger;
}

/**
 * Periodically polls `GET /internal/deliveries/deferred-due` while this gateway instance
 * holds a valid deployment lease and replays due `deferred` / `partial_deferred` deliveries
 * under their original `delivery_key` (sending only remaining unsent chunks/files).
 *
 * Stops immediately on lease loss or explicit `stop()`.
 */
export class DeferredDeliveryPoller {
  private readonly backend: BackendClient;
  private readonly deliveryService: Pick<
    DeliveryService,
    "hasDeferredPayload" | "replayDeferredDelivery"
  >;
  private readonly dataTaskNotifications: Pick<
    DataTaskNotificationService,
    "handleDataTaskNotification"
  >;
  private readonly automationNotifications: Pick<
    AutomationNotificationService,
    "handle"
  >;
  private readonly leaseManager?: GatewayLeaseManager;
  private readonly intervalMs: number;
  private readonly limit: number;
  private readonly onFallbackReplay?: (row: DeferredDeliveryDueRow) => Promise<void>;
  private readonly logger: SubsystemLogger;

  private timer: ReturnType<typeof setInterval> | null = null;
  private stopped = true;
  private inFlight = false;
  private readonly warnedUnreplayableKeys = new Set<string>();

  constructor(options: DeferredDeliveryPollerOptions) {
    this.backend = options.backend;
    this.deliveryService = options.deliveryService;
    this.dataTaskNotifications = options.dataTaskNotifications;
    this.automationNotifications = options.automationNotifications;
    this.leaseManager = options.leaseManager;
    this.intervalMs =
      typeof options.intervalMs === "number" &&
      Number.isFinite(options.intervalMs) &&
      options.intervalMs > 0
        ? Math.trunc(options.intervalMs)
        : DEFAULT_DEFERRED_DELIVERY_POLL_INTERVAL_MS;
    this.limit =
      typeof options.limit === "number" &&
      Number.isFinite(options.limit) &&
      options.limit >= 1
        ? Math.min(200, Math.trunc(options.limit))
        : DEFAULT_DEFERRED_DELIVERY_POLL_LIMIT;
    this.onFallbackReplay = options.onFallbackReplay;
    this.logger =
      options.logger ?? createSubsystemLogger("deferred-delivery-poller");
  }

  get isRunning(): boolean {
    return !this.stopped && this.timer !== null;
  }

  start(): void {
    if (!this.stopped && this.timer !== null) {
      return;
    }
    this.stopped = false;
    this.timer = setInterval(() => {
      void this.pollOnce();
    }, this.intervalMs);
    this.timer.unref?.();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  async pollOnce(): Promise<number> {
    if (this.stopped || this.inFlight) {
      return 0;
    }
    if (this.leaseManager) {
      if (this.leaseManager.isLeaseExpired()) {
        this.stop();
        return 0;
      }
      if (!this.leaseManager.isLeaseLocallyValid()) {
        // Within 5s safety margin: skip this tick without exiting (Round 3 Decision #1).
        return 0;
      }
    }
    if (typeof this.backend.getDueDeferredDeliveries !== "function") {
      return 0;
    }

    this.inFlight = true;
    try {
      const resp = await this.backend.getDueDeferredDeliveries({
        limit: this.limit,
      });
      if (this.stopped) {
        return 0;
      }
      if (this.leaseManager) {
        if (this.leaseManager.isLeaseExpired()) {
          this.stop();
          return 0;
        }
        if (!this.leaseManager.isLeaseLocallyValid()) {
          return 0;
        }
      }
      const rows = Array.isArray(resp?.deliveries) ? resp.deliveries : [];
      if (rows.length === 0) {
        return 0;
      }
      return await this.replayRows(rows);
    } catch (err) {
      if (isStaleGatewayEpochError(err)) {
        this.stop();
        this.leaseManager?.notifyLeaseLost("deferred_poll_stale_epoch", err);
        return 0;
      }
      if (isLeaseSafetyMarginPauseError(err)) {
        return 0;
      }
      this.logger.warn("Deferred delivery poll failed; will retry on next tick", {
        error: err instanceof Error ? err.message : String(err),
      });
      return 0;
    } finally {
      this.inFlight = false;
    }
  }

  async replayRows(rows: readonly DeferredDeliveryDueRow[]): Promise<number> {
    let replayedCount = 0;
    const seenKeys = new Set<string>();
    const seenDataTasks = new Set<string>();
    const seenAutomations = new Set<string>();

    for (const row of rows) {
      if (this.stopped) {
        break;
      }
      if (this.leaseManager) {
        if (this.leaseManager.isLeaseExpired()) {
          this.stop();
          break;
        }
        if (!this.leaseManager.isLeaseLocallyValid()) {
          break;
        }
      }

      const key =
        typeof row?.delivery_key === "string" ? row.delivery_key.trim() : "";
      if (!key || seenKeys.has(key)) {
        continue;
      }
      seenKeys.add(key);

      try {
        // 1) In-process DeliveryService payloads (`agent_send`, `fallback`, `interrupt`, `automation`, `notice`)
        if (this.deliveryService.hasDeferredPayload(key)) {
          await this.deliveryService.replayDeferredDelivery(key);
          this.warnedUnreplayableKeys.delete(key);
          replayedCount += 1;
          continue;
        }

        // 2) `data_task` notifications: re-fetch authorized plan and resume via original `delivery_key`
        if (row.kind === "data_task") {
          const runId = typeof row.run_id === "string" ? row.run_id.trim() : "";
          const target =
            typeof row.target_address === "string"
              ? row.target_address.trim()
              : "";
          if (runId && target) {
            const dedupeKey = `${runId}:${target}`;
            if (seenDataTasks.has(dedupeKey)) {
              continue;
            }
            seenDataTasks.add(dedupeKey);
            await this.dataTaskNotifications.handleDataTaskNotification({
              run_id: runId,
              target,
            });
            this.warnedUnreplayableKeys.delete(key);
            replayedCount += 1;
            continue;
          }
        }

        // 3) `automation_notify`: re-fetch authorized plan by delivery_id (`execution_id` / `run_id`)
        if (row.kind === "automation_notify") {
          const deliveryId = (
            (typeof row.execution_id === "string" ? row.execution_id.trim() : "") ||
            (typeof row.run_id === "string" ? row.run_id.trim() : "")
          );
          if (deliveryId) {
            if (seenAutomations.has(deliveryId)) {
              continue;
            }
            seenAutomations.add(deliveryId);
            await this.automationNotifications.handle({
              delivery_id: deliveryId,
            });
            this.warnedUnreplayableKeys.delete(key);
            replayedCount += 1;
            continue;
          }
        }

        // 4) Post-restart `fallback` rows can reconstruct final turn text via replay fallback
        if (
          row.kind === "fallback" &&
          row.user_id &&
          row.thread_id &&
          this.onFallbackReplay
        ) {
          await this.onFallbackReplay(row);
          this.warnedUnreplayableKeys.delete(key);
          replayedCount += 1;
          continue;
        }

        if (!this.warnedUnreplayableKeys.has(key)) {
          if (this.warnedUnreplayableKeys.size >= 500) {
            const oldest = this.warnedUnreplayableKeys.values().next().value;
            if (oldest !== undefined) {
              this.warnedUnreplayableKeys.delete(oldest);
            }
          }
          this.warnedUnreplayableKeys.add(key);
          this.logger.warn(
            "Skipping due deferred delivery without local cached payload",
            {
              delivery_key: key,
              kind: row.kind,
              platform: row.platform,
              status: row.status,
            },
          );
        }
      } catch (err) {
        if (isStaleGatewayEpochError(err)) {
          this.stop();
          this.leaseManager?.notifyLeaseLost("deferred_replay_stale_epoch", err);
          break;
        }
        if (isLeaseSafetyMarginPauseError(err)) {
          break;
        }
        this.logger.warn("Failed to replay due deferred delivery", {
          delivery_key: key,
          kind: row.kind,
          platform: row.platform,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }

    return replayedCount;
  }
}
