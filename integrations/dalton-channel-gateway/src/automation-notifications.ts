/**
 * Generic automation notifications.
 *
 * The caller sends only `delivery_id`. This module loads the backend plan and
 * hands it to the shared data-task send loop. It does not send by itself.
 */

import type { BackendClient } from "./backend-client.js";
import type { DataTaskNotificationResponse } from "./data-task-notification-types.js";
import {
  DataTaskNotificationService,
  type ExecutionStateTracker,
} from "./data-task-notifications.js";
import { createSequentialQueue, type SequentialQueue } from "./shared/keyed-queue.js";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ALLOWED_REQUEST_KEYS = new Set(["delivery_id"]);

function isPlainRecord(val: unknown): val is Record<string, unknown> {
  return typeof val === "object" && val !== null && !Array.isArray(val);
}

function failedBody(
  deliveryId: string,
  code: string,
  message: string,
  status: DataTaskNotificationResponse["status"] = "failed",
): DataTaskNotificationResponse {
  return {
    ok: false,
    status,
    code,
    message,
    run_id: deliveryId,
    target: "",
    platform: null,
    items_total: 0,
    items_sent: 0,
    items: [],
  };
}

export class AutomationNotificationService {
  private readonly queue: SequentialQueue;

  constructor(
    private readonly backend: BackendClient,
    private readonly deliveries: DataTaskNotificationService,
    queueTaskTimeoutMs = 180_000,
  ) {
    this.queue = createSequentialQueue({ taskTimeoutMs: queueTaskTimeoutMs });
  }

  async handle(rawBody: unknown): Promise<{
    status: number;
    data: DataTaskNotificationResponse;
  }> {
    if (!isPlainRecord(rawBody)) {
      return {
        status: 400,
        data: failedBody("", "invalid_request", "Request body must be a JSON object."),
      };
    }
    const extraKeys = Object.keys(rawBody).filter(
      (key) => !ALLOWED_REQUEST_KEYS.has(key),
    );
    if (extraKeys.length > 0) {
      return {
        status: 400,
        data: failedBody(
          typeof rawBody.delivery_id === "string" ? rawBody.delivery_id : "",
          "invalid_request",
          `Forbidden caller fields in automation notification request: ${extraKeys.join(", ")}`,
        ),
      };
    }
    const deliveryId =
      typeof rawBody.delivery_id === "string" ? rawBody.delivery_id.trim() : "";
    if (!deliveryId || !UUID_RE.test(deliveryId)) {
      return {
        status: 400,
        data: failedBody(
          deliveryId,
          "invalid_request",
          "delivery_id must be a UUID.",
        ),
      };
    }
    const normalizedId = deliveryId.toLowerCase();
    const tracker: ExecutionStateTracker = {
      claimedAnyItem: false,
      resolvedPlatform: null,
      itemsTotal: 0,
      itemsSent: 0,
      outcomes: [],
    };
    let captured:
      | { status: number; data: DataTaskNotificationResponse }
      | undefined;
    try {
      const queueOutcome = await this.queue.enqueue(normalizedId, async () => {
        if (
          typeof this.backend.authorizeAutomationNotificationPlan !== "function"
        ) {
          throw new Error(
            "BackendClient does not implement authorizeAutomationNotificationPlan",
          );
        }
        const rawPlan = await this.backend.authorizeAutomationNotificationPlan({
          delivery_id: normalizedId,
        });
        const requested =
          isPlainRecord(rawPlan) && typeof rawPlan.requested_target === "string"
            ? rawPlan.requested_target
            : "";
        captured = await this.deliveries.deliverAuthorizedPlan(
          normalizedId,
          requested,
          tracker,
          {
            rawPlan,
            claimKind: "automation_notify",
            liveCheckPurpose: "automation_notification",
          },
        );
      });
      if (queueOutcome === "completed" && captured !== undefined) {
        return captured;
      }
      const uncertain = tracker.claimedAnyItem || tracker.itemsSent > 0;
      return {
        status: 200,
        data: {
          ok: false,
          status: uncertain ? "unknown" : "failed",
          code: uncertain ? "delivery_uncertain" : "unavailable",
          message: uncertain
            ? "Automation notification timed out after claiming delivery; state is uncertain."
            : "Automation notification timed out before claiming any item.",
          run_id: normalizedId,
          target: "",
          platform: tracker.resolvedPlatform,
          items_total: tracker.itemsTotal,
          items_sent: tracker.itemsSent,
          items: [...tracker.outcomes],
        },
      };
    } catch (err) {
      const uncertain = tracker.claimedAnyItem || tracker.itemsSent > 0;
      const message =
        err instanceof Error && err.message
          ? err.message
          : uncertain
            ? "Backend communication failed after claiming delivery; state is uncertain."
            : "Backend communication failed before claiming delivery.";
      return {
        status: 200,
        data: {
          ok: false,
          status: uncertain ? "unknown" : "failed",
          code: uncertain ? "delivery_uncertain" : "unavailable",
          message,
          run_id: normalizedId,
          target: "",
          platform: tracker.resolvedPlatform,
          items_total: tracker.itemsTotal,
          items_sent: tracker.itemsSent,
          items: [...tracker.outcomes],
        },
      };
    }
  }
}
