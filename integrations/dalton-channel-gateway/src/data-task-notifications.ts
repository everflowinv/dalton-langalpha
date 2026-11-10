import { createHash } from "node:crypto";
import {
  type BackendClient,
  BackendHttpError,
  type I7DeliveriesProgressRequest,
  type I7DeliveriesProgressResponse,
} from "./backend-client.js";
import {
  type ChannelAdapter,
  ChannelDeliveryError,
  type SendCardImageInput,
} from "./channels/adapter.js";
import type {
  DataTaskAuthorizePlanResponse,
  DataTaskItemDeliveryOutcome,
  DataTaskNotificationResponse,
  DataTaskPlanArtifactDescriptor,
  DataTaskPlanItem,
} from "./data-task-notification-types.js";
import {
  createSequentialQueue,
  type SequentialQueue,
} from "./shared/keyed-queue.js";
import type { ChannelPlatform } from "./types.js";

const MAX_ARTIFACT_BYTES = 20 * 1024 * 1024;
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ALLOWED_REQUEST_KEYS = new Set(["run_id", "target"]);
const SUPPORTED_PLATFORMS = new Set<string>(["feishu", "discord", "weixin"]);
const RECOGNIZED_DELIVERY_STATUSES = new Set<string>([
  "in_flight",
  "sent",
  "partial",
  "failed",
  "unknown",
]);
const SKIP_CODES = new Set([
  "dalton_disabled",
  "task_disabled",
  "task_notify_disabled",
  "toggle_off",
  "master_off",
  "override_off",
  "target_not_routed",
  "no_targets",
]);

class DataTaskBackendAckError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DataTaskBackendAckError";
  }
}

class DataTaskArtifactVerifyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DataTaskArtifactVerifyError";
  }
}

function isPlainRecord(val: unknown): val is Record<string, unknown> {
  return typeof val === "object" && val !== null && !Array.isArray(val);
}

function isSupportedChannelPlatform(val: unknown): val is ChannelPlatform {
  return typeof val === "string" && SUPPORTED_PLATFORMS.has(val);
}

function computeSha256Hex(buf: Buffer): string {
  return createHash("sha256").update(buf).digest("hex");
}

function safeBackendErrorMessage(err: unknown, fallback: string): string {
  if (
    err instanceof DataTaskBackendAckError ||
    err instanceof DataTaskArtifactVerifyError ||
    err instanceof BackendHttpError
  ) {
    return err.message;
  }
  return fallback;
}

function classifyAdapterError(err: unknown): {
  outcome: "failed" | "unknown";
  code: string;
  message: string;
} {
  if (err instanceof ChannelDeliveryError) {
    return {
      outcome: err.outcome === "unknown" ? "unknown" : "failed",
      code: err.code,
      message: err.message,
    };
  }
  return {
    outcome: "unknown",
    code: "unavailable",
    message: err instanceof Error ? err.message : String(err),
  };
}

type ValidatedDataTaskClaim =
  | {
      kind: "claimed";
      platform: ChannelPlatform;
      targetAddress: string;
      recipientId: string;
      isCurrent: false;
      homeWorkspaceId: string | null;
      weixinContextToken: string | null;
    }
  | {
      kind: "already_completed";
      status: "sent" | "partial";
      platformMessageIds: string[];
    }
  | {
      kind: "in_flight_or_unknown";
      code: string;
      message: string;
    }
  | {
      kind: "denied";
      status: "failed" | "unknown";
      code: string;
      message: string;
    }
  | {
      kind: "invalid";
      reason: string;
    };

function validateDataTaskClaimResponse(
  rawClaim: unknown,
  expected: {
    platform: ChannelPlatform;
    canonicalTargetAddress: string;
    recipientId: string;
  },
): ValidatedDataTaskClaim {
  if (!isPlainRecord(rawClaim)) {
    return {
      kind: "invalid",
      reason: "I-6 authorize-and-claim response is not a JSON object.",
    };
  }
  const claimStatus = rawClaim.claim_status;
  if (typeof claimStatus !== "string") {
    return {
      kind: "invalid",
      reason: "I-6 authorize-and-claim response is missing a string claim_status.",
    };
  }

  if (claimStatus === "claimed") {
    if (rawClaim.platform !== expected.platform) {
      return {
        kind: "invalid",
        reason: "I-6 claimed platform does not match the authorized plan.",
      };
    }
    if (
      typeof rawClaim.recipient_id !== "string" ||
      rawClaim.recipient_id.trim().length === 0 ||
      rawClaim.recipient_id !== expected.recipientId
    ) {
      return {
        kind: "invalid",
        reason: "I-6 claimed recipient_id is missing or does not match the authorized plan.",
      };
    }
    if (
      typeof rawClaim.target_address !== "string" ||
      rawClaim.target_address.trim().length === 0 ||
      rawClaim.target_address !== expected.canonicalTargetAddress
    ) {
      return {
        kind: "invalid",
        reason: "I-6 claimed target_address is missing or does not match the authorized plan.",
      };
    }
    if (rawClaim.is_current !== false) {
      return {
        kind: "invalid",
        reason: "I-6 claimed response must confirm is_current=false.",
      };
    }
    if (
      !("home_workspace_id" in rawClaim) ||
      (rawClaim.home_workspace_id !== null &&
        (typeof rawClaim.home_workspace_id !== "string" ||
          rawClaim.home_workspace_id.trim().length === 0))
    ) {
      return {
        kind: "invalid",
        reason: "I-6 claimed response has missing or invalid home_workspace_id.",
      };
    }
    if (
      !("weixin_context_token" in rawClaim) ||
      (rawClaim.weixin_context_token !== null &&
        typeof rawClaim.weixin_context_token !== "string")
    ) {
      return {
        kind: "invalid",
        reason: "I-6 claimed response has missing or invalid weixin_context_token.",
      };
    }
    return {
      kind: "claimed",
      platform: expected.platform,
      targetAddress: rawClaim.target_address,
      recipientId: rawClaim.recipient_id,
      isCurrent: false,
      homeWorkspaceId: rawClaim.home_workspace_id,
      weixinContextToken: rawClaim.weixin_context_token,
    };
  }

  if (claimStatus === "already_completed") {
    if (rawClaim.status !== "sent" && rawClaim.status !== "partial") {
      return {
        kind: "invalid",
        reason: "I-6 already_completed response has missing or unrecognized status.",
      };
    }
    if (rawClaim.platform !== expected.platform) {
      return {
        kind: "invalid",
        reason: "I-6 already_completed platform does not match the authorized plan.",
      };
    }
    if (rawClaim.target_address !== expected.canonicalTargetAddress) {
      return {
        kind: "invalid",
        reason: "I-6 already_completed target_address does not match the authorized plan.",
      };
    }
    if (rawClaim.is_current !== undefined && rawClaim.is_current !== false) {
      return {
        kind: "invalid",
        reason: "I-6 already_completed response has invalid is_current.",
      };
    }
    if (
      rawClaim.first_response_json !== undefined &&
      rawClaim.first_response_json !== null &&
      !isPlainRecord(rawClaim.first_response_json)
    ) {
      return {
        kind: "invalid",
        reason: "I-6 already_completed response has invalid first_response_json.",
      };
    }
    const recordedMsgIds =
      isPlainRecord(rawClaim.first_response_json) &&
      Array.isArray(rawClaim.first_response_json.platform_message_ids) &&
      rawClaim.first_response_json.platform_message_ids.every(
        (id) => typeof id === "string" && id.length > 0,
      )
        ? (rawClaim.first_response_json.platform_message_ids as string[])
        : [];
    return {
      kind: "already_completed",
      status: rawClaim.status,
      platformMessageIds: recordedMsgIds,
    };
  }

  if (claimStatus === "in_flight_or_unknown") {
    if (rawClaim.status !== "unknown") {
      return {
        kind: "invalid",
        reason: "I-6 in_flight_or_unknown response must carry status=unknown.",
      };
    }
    if (rawClaim.platform !== expected.platform) {
      return {
        kind: "invalid",
        reason: "I-6 in_flight_or_unknown platform does not match the authorized plan.",
      };
    }
    if (rawClaim.target_address !== expected.canonicalTargetAddress) {
      return {
        kind: "invalid",
        reason: "I-6 in_flight_or_unknown target_address does not match the authorized plan.",
      };
    }
    if (rawClaim.is_current !== undefined && rawClaim.is_current !== false) {
      return {
        kind: "invalid",
        reason: "I-6 in_flight_or_unknown response has invalid is_current.",
      };
    }
    if (
      rawClaim.code !== undefined &&
      rawClaim.code !== null &&
      (typeof rawClaim.code !== "string" || rawClaim.code.trim().length === 0)
    ) {
      return {
        kind: "invalid",
        reason: "I-6 in_flight_or_unknown response has invalid code.",
      };
    }
    if (rawClaim.message !== undefined && typeof rawClaim.message !== "string") {
      return {
        kind: "invalid",
        reason: "I-6 in_flight_or_unknown response has invalid message.",
      };
    }
    return {
      kind: "in_flight_or_unknown",
      code: rawClaim.code ?? "in_flight",
      message:
        rawClaim.message ??
        "Item delivery state is uncertain or in flight; automatic resend is blocked.",
    };
  }

  if (claimStatus === "denied") {
    if (rawClaim.status !== "failed" && rawClaim.status !== "unknown") {
      return {
        kind: "invalid",
        reason: "I-6 denied response has missing or unrecognized status.",
      };
    }
    if (typeof rawClaim.code !== "string" || rawClaim.code.trim().length === 0) {
      return {
        kind: "invalid",
        reason: "I-6 denied response is missing a non-empty code.",
      };
    }
    if (rawClaim.message !== undefined && typeof rawClaim.message !== "string") {
      return {
        kind: "invalid",
        reason: "I-6 denied response has invalid message.",
      };
    }
    return {
      kind: "denied",
      status: rawClaim.status,
      code: rawClaim.code,
      message: rawClaim.message ?? "Item claim denied.",
    };
  }

  return {
    kind: "invalid",
    reason: "I-6 authorize-and-claim returned an unrecognized claim_status.",
  };
}

export interface DataTaskNotificationServiceOptions {
  backend: BackendClient;
  adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  mode?: "incoming" | "outgoing";
  queueTaskTimeoutMs?: number;
}

interface ExecutionStateTracker {
  claimedAnyItem: boolean;
  resolvedPlatform: ChannelPlatform | null;
  itemsTotal: number;
  itemsSent: number;
  outcomes: DataTaskItemDeliveryOutcome[];
}

export class DataTaskNotificationService {
  private readonly backend: BackendClient;
  private readonly adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  private readonly mode: "incoming" | "outgoing";
  private readonly queue: SequentialQueue;

  constructor(options: DataTaskNotificationServiceOptions) {
    this.backend = options.backend;
    this.adapters = options.adapters;
    this.mode = options.mode ?? "incoming";
    this.queue = createSequentialQueue({
      taskTimeoutMs: options.queueTaskTimeoutMs ?? 180_000,
    });
  }

  private isAdapterOutboundReady(adapter: ChannelAdapter | undefined): boolean {
    if (!adapter || !adapter.isConfigured()) {
      return false;
    }
    if (typeof adapter.isReadyForOutbound === "function") {
      return adapter.isReadyForOutbound();
    }
    return this.mode === "outgoing" ? true : adapter.isConnected();
  }

  private async recordValidatedDeliveryProgress(
    req: I7DeliveriesProgressRequest,
  ): Promise<I7DeliveriesProgressResponse> {
    const expectedStatus = req.status ?? "in_flight";
    if (!RECOGNIZED_DELIVERY_STATUSES.has(expectedStatus)) {
      throw new DataTaskBackendAckError(
        "Submitted I-7 delivery status is not recognized.",
      );
    }
    let rawAck: unknown;
    try {
      rawAck = await this.backend.updateDeliveryProgress(req);
    } catch (err) {
      throw new DataTaskBackendAckError(
        safeBackendErrorMessage(
          err,
          "I-7 deliveries/progress request failed without a confirmed acknowledgement.",
        ),
      );
    }
    if (!isPlainRecord(rawAck)) {
      throw new DataTaskBackendAckError(
        "I-7 deliveries/progress acknowledgement is not a JSON object.",
      );
    }
    if (rawAck.ok !== true) {
      throw new DataTaskBackendAckError(
        "I-7 deliveries/progress acknowledgement did not confirm ok=true.",
      );
    }
    if (
      typeof rawAck.delivery_key !== "string" ||
      rawAck.delivery_key !== req.delivery_key
    ) {
      throw new DataTaskBackendAckError(
        "I-7 deliveries/progress acknowledgement delivery_key mismatch.",
      );
    }
    if (
      typeof rawAck.status !== "string" ||
      !RECOGNIZED_DELIVERY_STATUSES.has(rawAck.status) ||
      rawAck.status !== expectedStatus
    ) {
      throw new DataTaskBackendAckError(
        "I-7 deliveries/progress acknowledgement status is unrecognized or inconsistent with submitted transition.",
      );
    }
    if (
      typeof rawAck.sent_to_current_count !== "number" ||
      !Number.isInteger(rawAck.sent_to_current_count) ||
      rawAck.sent_to_current_count < 0
    ) {
      throw new DataTaskBackendAckError(
        "I-7 deliveries/progress acknowledgement has an invalid sent_to_current_count.",
      );
    }
    return {
      ok: true,
      delivery_key: rawAck.delivery_key,
      status: rawAck.status,
      sent_to_current_count: rawAck.sent_to_current_count,
    };
  }

  private async fetchAndVerifyArtifact(
    runId: string,
    desc: DataTaskPlanArtifactDescriptor,
  ): Promise<{
    content: Buffer;
    mimeType: string;
    filename: string;
  }> {
    if (desc.size_bytes <= 0 || desc.size_bytes > MAX_ARTIFACT_BYTES) {
      throw new DataTaskArtifactVerifyError(
        `Artifact ${desc.artifact_id} declared size (${desc.size_bytes}) is out of bounds`,
      );
    }
    if (typeof this.backend.downloadDataTaskRunArtifact !== "function") {
      throw new DataTaskArtifactVerifyError(
        "BackendClient does not implement downloadDataTaskRunArtifact",
      );
    }
    let downloaded;
    try {
      downloaded = await this.backend.downloadDataTaskRunArtifact(
        runId,
        desc.artifact_id,
        MAX_ARTIFACT_BYTES,
      );
    } catch (err) {
      throw new DataTaskArtifactVerifyError(
        safeBackendErrorMessage(
          err,
          `Artifact ${desc.artifact_id} download failed.`,
        ),
      );
    }
    if (downloaded.content.byteLength !== desc.size_bytes) {
      throw new DataTaskArtifactVerifyError(
        `Artifact ${desc.artifact_id} byte length mismatch: expected ${desc.size_bytes}, got ${downloaded.content.byteLength}`,
      );
    }
    const actualSha = computeSha256Hex(downloaded.content);
    const expectedSha = desc.sha256.trim().toLowerCase();
    if (actualSha !== expectedSha) {
      throw new DataTaskArtifactVerifyError(
        `Artifact ${desc.artifact_id} SHA-256 digest mismatch`,
      );
    }
    if (
      downloaded.sha256Header &&
      downloaded.sha256Header.trim().toLowerCase() !== expectedSha
    ) {
      throw new DataTaskArtifactVerifyError(
        `Artifact ${desc.artifact_id} header digest mismatch`,
      );
    }
    if (downloaded.mimeType !== desc.mime_type.toLowerCase()) {
      throw new DataTaskArtifactVerifyError(
        `Artifact ${desc.artifact_id} MIME mismatch: expected ${desc.mime_type}, got ${downloaded.mimeType}`,
      );
    }
    return {
      content: downloaded.content,
      mimeType: desc.mime_type,
      filename: desc.filename,
    };
  }

  async handleDataTaskNotification(
    rawBody: unknown,
  ): Promise<{ status: number; data: DataTaskNotificationResponse }> {
    if (!isPlainRecord(rawBody)) {
      return {
        status: 400,
        data: {
          ok: false,
          status: "failed",
          code: "invalid_request",
          message: "Request body must be a JSON object.",
          run_id: "",
          target: "",
          platform: null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const extraKeys = Object.keys(rawBody).filter(
      (k) => !ALLOWED_REQUEST_KEYS.has(k),
    );
    if (extraKeys.length > 0) {
      return {
        status: 400,
        data: {
          ok: false,
          status: "failed",
          code: "invalid_request",
          message: `Forbidden caller fields in data-task notification request: ${extraKeys.join(", ")}`,
          run_id: typeof rawBody.run_id === "string" ? rawBody.run_id : "",
          target: typeof rawBody.target === "string" ? rawBody.target : "",
          platform: null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const runId = typeof rawBody.run_id === "string" ? rawBody.run_id.trim() : "";
    const target = typeof rawBody.target === "string" ? rawBody.target.trim() : "";

    if (
      !runId ||
      !UUID_RE.test(runId) ||
      !target ||
      target.includes("://") ||
      target.includes("/") ||
      target.includes("\\") ||
      /\s/.test(target)
    ) {
      return {
        status: 400,
        data: {
          ok: false,
          status: "failed",
          code: "invalid_request",
          message: "run_id must be a valid UUID and target must be a canonical channel address.",
          run_id: runId,
          target,
          platform: null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const normalizedRunId = runId.toLowerCase();
    const queueKey = `${normalizedRunId}::${target}`;
    const tracker: ExecutionStateTracker = {
      claimedAnyItem: false,
      resolvedPlatform: null,
      itemsTotal: 0,
      itemsSent: 0,
      outcomes: [],
    };
    let capturedResult:
      | { status: number; data: DataTaskNotificationResponse }
      | undefined;

    try {
      const queueOutcome = await this.queue.enqueue(queueKey, async () => {
        capturedResult = await this.executeAuthorizedPlan(
          normalizedRunId,
          target,
          tracker,
        );
      });

      if (queueOutcome === "completed" && capturedResult !== undefined) {
        return capturedResult;
      }

      const isUncertain = tracker.claimedAnyItem || tracker.itemsSent > 0;
      return {
        status: 200,
        data: {
          ok: false,
          status: isUncertain ? "unknown" : "failed",
          code: isUncertain ? "delivery_uncertain" : "unavailable",
          message: isUncertain
            ? "Data-task notification timed out after claiming delivery; state is uncertain."
            : "Data-task notification timed out before claiming any item.",
          run_id: normalizedRunId,
          target,
          platform: tracker.resolvedPlatform,
          items_total: tracker.itemsTotal,
          items_sent: tracker.itemsSent,
          items: [...tracker.outcomes],
        },
      };
    } catch (err) {
      const isUncertain = tracker.claimedAnyItem || tracker.itemsSent > 0;
      const msg = safeBackendErrorMessage(
        err,
        isUncertain
          ? "Backend communication failed after claiming delivery; state is uncertain."
          : "Backend communication failed before claiming delivery.",
      );
      return {
        status: 200,
        data: {
          ok: false,
          status: isUncertain ? "unknown" : "failed",
          code: isUncertain ? "delivery_uncertain" : "unavailable",
          message: msg,
          run_id: normalizedRunId,
          target,
          platform: tracker.resolvedPlatform,
          items_total: tracker.itemsTotal,
          items_sent: tracker.itemsSent,
          items: [...tracker.outcomes],
        },
      };
    }
  }

  private async executeAuthorizedPlan(
    runId: string,
    target: string,
    tracker: ExecutionStateTracker,
  ): Promise<{ status: number; data: DataTaskNotificationResponse }> {
    if (typeof this.backend.authorizeDataTaskNotificationPlan !== "function") {
      throw new DataTaskBackendAckError(
        "BackendClient does not implement authorizeDataTaskNotificationPlan",
      );
    }
    const rawPlan: unknown = await this.backend.authorizeDataTaskNotificationPlan({
      run_id: runId,
      target,
    });

    if (!isPlainRecord(rawPlan) || typeof rawPlan.authorized !== "boolean") {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "unavailable",
          message: "Malformed authorization plan response from backend.",
          run_id: runId,
          target,
          platform: null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const plan = rawPlan as unknown as DataTaskAuthorizePlanResponse;
    if (!plan.authorized) {
      const denyCode =
        typeof plan.code === "string" && plan.code.trim().length > 0
          ? plan.code
          : "not_allowed";
      const mappedStatus =
        plan.status === "skipped" || SKIP_CODES.has(denyCode)
          ? "skipped"
          : "failed";
      return {
        status: 200,
        data: {
          ok: false,
          status: mappedStatus,
          code: denyCode,
          message:
            typeof plan.message === "string"
              ? plan.message
              : "Data-task notification is not authorized.",
          run_id: runId,
          target,
          platform: isSupportedChannelPlatform(plan.platform)
            ? plan.platform
            : null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    if (
      !isSupportedChannelPlatform(plan.platform) ||
      typeof plan.canonical_target_address !== "string" ||
      plan.canonical_target_address.trim().length === 0 ||
      typeof plan.recipient_id !== "string" ||
      plan.recipient_id.trim().length === 0 ||
      (plan.target_kind !== "dm" && plan.target_kind !== "group") ||
      !Array.isArray(plan.items)
    ) {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "unavailable",
          message: "Authorized delivery plan from backend is missing required fields.",
          run_id: runId,
          target,
          platform: isSupportedChannelPlatform(plan.platform)
            ? plan.platform
            : null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const platform = plan.platform;
    const canonicalPlanTargetAddress = plan.canonical_target_address;
    const expectedRecipientId = plan.recipient_id;
    const planItems: DataTaskPlanItem[] = plan.items;
    tracker.resolvedPlatform = platform;
    tracker.itemsTotal = planItems.length;
    if (planItems.length === 0) {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "empty_payload",
          message: "Authorized delivery plan contains zero items.",
          run_id: runId,
          target,
          platform,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const adapter = this.adapters.get(platform);
    if (!this.isAdapterOutboundReady(adapter)) {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "unavailable",
          message: `The ${platform} channel adapter is unavailable; pending notification work is preserved.`,
          run_id: runId,
          target,
          platform,
          items_total: planItems.length,
          items_sent: 0,
          items: [],
        },
      };
    }
    const readyAdapter = adapter!;

    const needsSendCard = planItems.some((it) => it.kind === "feishu_card");
    if (needsSendCard && typeof readyAdapter.sendCard !== "function") {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "unsupported_item_kind",
          message: `Adapter ${platform} does not support structured Card 2.0 delivery.`,
          run_id: runId,
          target,
          platform,
          items_total: planItems.length,
          items_sent: 0,
          items: [],
        },
      };
    }

    if (
      plan.target_kind === "group" &&
      typeof readyAdapter.checkTargetLive !== "function"
    ) {
      return {
        status: 200,
        data: {
          ok: false,
          status: "failed",
          code: "unavailable",
          message: `Adapter ${platform} cannot verify live group target ${canonicalPlanTargetAddress} before sending.`,
          run_id: runId,
          target,
          platform,
          items_total: planItems.length,
          items_sent: 0,
          items: [],
        },
      };
    }

    const itemOutcomes: DataTaskItemDeliveryOutcome[] = tracker.outcomes;
    let itemsSent = 0;
    let groupLiveVerified = false;

    for (const item of planItems) {
      if (
        plan.target_kind === "group" &&
        !groupLiveVerified &&
        item.existing_status !== "sent"
      ) {
        const preClaimLiveCheck = await readyAdapter.checkTargetLive!({
          userId: plan.owner_user_id ?? "",
          address: canonicalPlanTargetAddress,
          purpose: "data_task_notification",
        });
        if (!preClaimLiveCheck.ok) {
          const failCode = preClaimLiveCheck.code ?? "not_allowed";
          const denyMsg =
            preClaimLiveCheck.message ??
            `Group target ${canonicalPlanTargetAddress} is not accessible.`;
          return {
            status: 200,
            data: {
              ok: false,
              status: "failed",
              code: failCode,
              message: denyMsg,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }
        groupLiveVerified = true;
      }

      const textChunks =
        item.kind === "feishu_card"
          ? []
          : item.text
            ? readyAdapter.splitTextChunks(item.text)
            : [];
      const totalChunks = item.kind === "feishu_card" ? 1 : textChunks.length;
      const hasNonEmptyText =
        item.kind === "feishu_card" ? true : textChunks.length > 0;

      const hadPriorClaim = tracker.claimedAnyItem;
      tracker.claimedAnyItem = true;
      let rawClaim: unknown;
      try {
        rawClaim = await this.backend.authorizeAndClaimDelivery({
          delivery_key: item.delivery_key,
          kind: "data_task",
          run_id: plan.run_id ?? runId,
          target: plan.requested_target ?? target,
          platform,
          has_non_empty_text: hasNonEmptyText,
          total_chunks: totalChunks,
        });
      } catch (claimErr) {
        const cMsg = safeBackendErrorMessage(
          claimErr,
          "I-6 authorize-and-claim request failed without a confirmed response.",
        );
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "unknown",
          duplicate: false,
          platform_message_ids: [],
          code: "delivery_uncertain",
          message: cMsg,
        });
        return {
          status: 200,
          data: {
            ok: false,
            status: "unknown",
            code: "delivery_uncertain",
            message: cMsg,
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      const validatedClaim = validateDataTaskClaimResponse(rawClaim, {
        platform,
        canonicalTargetAddress: canonicalPlanTargetAddress,
        recipientId: expectedRecipientId,
      });

      if (validatedClaim.kind === "invalid") {
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "unknown",
          duplicate: false,
          platform_message_ids: [],
          code: "delivery_uncertain",
          message: validatedClaim.reason,
        });
        return {
          status: 200,
          data: {
            ok: false,
            status: "unknown",
            code: "delivery_uncertain",
            message: validatedClaim.reason,
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      if (validatedClaim.kind === "already_completed") {
        if (validatedClaim.status === "sent") {
          tracker.claimedAnyItem = hadPriorClaim;
          itemsSent += 1;
          tracker.itemsSent = itemsSent;
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "sent",
            duplicate: true,
            platform_message_ids: validatedClaim.platformMessageIds,
            code: null,
          });
          continue;
        }
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "partial",
          duplicate: true,
          platform_message_ids: validatedClaim.platformMessageIds,
          code: "partial",
          message: "Earlier attempt partially delivered this item; automatic resend is blocked.",
        });
        return {
          status: 200,
          data: {
            ok: false,
            status: "unknown",
            code: "partial",
            message: "Earlier attempt partially delivered an item; automatic resend is blocked.",
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      if (validatedClaim.kind === "in_flight_or_unknown") {
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "unknown",
          duplicate: false,
          platform_message_ids: [],
          code: validatedClaim.code,
          message: validatedClaim.message,
        });
        return {
          status: 200,
          data: {
            ok: false,
            status: "unknown",
            code: validatedClaim.code,
            message: validatedClaim.message,
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      if (validatedClaim.kind === "denied") {
        tracker.claimedAnyItem = hadPriorClaim;
        const denyCode = validatedClaim.code;
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: validatedClaim.status,
          duplicate: false,
          platform_message_ids: [],
          code: denyCode,
          message: validatedClaim.message,
        });
        const overallStatus =
          validatedClaim.status === "unknown"
            ? "unknown"
            : itemsSent === 0 && SKIP_CODES.has(denyCode)
              ? "skipped"
              : "failed";
        return {
          status: 200,
          data: {
            ok: false,
            status: overallStatus,
            code: denyCode,
            message: validatedClaim.message,
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      // Strictly validated `claim_status === "claimed"`: never substitute plan fields.
      const targetAddress = validatedClaim.targetAddress;
      const recipientId = validatedClaim.recipientId;
      const recipientPlatformUserId =
        plan.target_kind === "dm" ? recipientId : undefined;

      // Safety fallback if `existing_status` was stale and `claim` transitioned to `claimed`.
      if (plan.target_kind === "group" && !groupLiveVerified) {
        const liveCheck = await readyAdapter.checkTargetLive!({
          userId: plan.owner_user_id ?? "",
          address: targetAddress,
          purpose: "data_task_notification",
        });
        if (!liveCheck.ok) {
          const failCode = liveCheck.code ?? "not_allowed";
          const denyMsg =
            liveCheck.message ?? `Group target ${targetAddress} is no longer accessible.`;
          try {
            await this.recordValidatedDeliveryProgress({
              delivery_key: item.delivery_key,
              sent_chunks: 0,
              total_chunks: totalChunks,
              status: "failed",
              error_code: failCode,
              error_message: denyMsg,
            });
          } catch (progressErr) {
            const pMsg = safeBackendErrorMessage(
              progressErr,
              "Failed to record group target denial progress.",
            );
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [],
              code: "delivery_uncertain",
              message: pMsg,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: pMsg,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "failed",
            duplicate: false,
            platform_message_ids: [],
            code: failCode,
            message: denyMsg,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: "failed",
              code: failCode,
              message: denyMsg,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }
        groupLiveVerified = true;
      }

      if (item.kind === "feishu_card") {
        const cardImages: SendCardImageInput[] = [];
        try {
          for (const imgDesc of item.images) {
            if (!imgDesc.placeholder) {
              throw new DataTaskArtifactVerifyError(
                `Card image artifact ${imgDesc.artifact_id} is missing placeholder`,
              );
            }
            const verified = await this.fetchAndVerifyArtifact(runId, imgDesc);
            cardImages.push({
              placeholder: imgDesc.placeholder,
              content: verified.content,
              mimeType: verified.mimeType,
              filename: verified.filename,
            });
          }
        } catch (artErr) {
          const artMsg = safeBackendErrorMessage(
            artErr,
            "Card image artifact validation failed.",
          );
          try {
            await this.recordValidatedDeliveryProgress({
              delivery_key: item.delivery_key,
              sent_chunks: 0,
              total_chunks: 1,
              status: "failed",
              error_code: "artifact_invalid",
              error_message: artMsg,
            });
          } catch (progressErr) {
            const pMsg = safeBackendErrorMessage(
              progressErr,
              "Failed to record artifact validation failure progress.",
            );
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [],
              code: "delivery_uncertain",
              message: pMsg,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: pMsg,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "failed",
            duplicate: false,
            platform_message_ids: [],
            code: "artifact_invalid",
            message: artMsg,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: "failed",
              code: "artifact_invalid",
              message: artMsg,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }

        try {
          const sendRes = await readyAdapter.sendCard!({
            targetAddress,
            recipientPlatformUserId,
            card: item.card,
            images: cardImages,
          });
          const filesStatus = item.images.map((img) => ({
            path: img.filename,
            status: "sent" as const,
            reason: null,
          }));
          try {
            await this.recordValidatedDeliveryProgress({
              delivery_key: item.delivery_key,
              sent_chunks: 1,
              total_chunks: 1,
              platform_message_ids: [sendRes.messageId],
              files_status: filesStatus,
              status: "sent",
              first_response_json: {
                status: "sent",
                platform_message_ids: [sendRes.messageId],
              },
            });
          } catch (progressErr) {
            const pMsg = safeBackendErrorMessage(
              progressErr,
              "I-7 progress acknowledgement failed after card delivery; state is uncertain.",
            );
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [sendRes.messageId],
              code: "delivery_uncertain",
              message: pMsg,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: pMsg,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }

          itemsSent += 1;
          tracker.itemsSent = itemsSent;
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "sent",
            duplicate: false,
            platform_message_ids: [sendRes.messageId],
            code: null,
          });
          continue;
        } catch (sendErr) {
          const classified = classifyAdapterError(sendErr);
          const itemStatus =
            classified.outcome === "unknown" ? "unknown" : "failed";
          try {
            await this.recordValidatedDeliveryProgress({
              delivery_key: item.delivery_key,
              sent_chunks: 0,
              total_chunks: 1,
              status: itemStatus,
              error_code: classified.code,
              error_message: classified.message,
            });
          } catch {
            // Row remains 'in_flight' in dalton_channel_deliveries (safe uncertain state)
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [],
              code: "delivery_uncertain",
              message: classified.message,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: classified.message,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: itemStatus,
            duplicate: false,
            platform_message_ids: [],
            code: classified.code,
            message: classified.message,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: itemStatus,
              code: classified.code,
              message: classified.message,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }
      }

      // `text_and_file` (e.g. T26) or `text`
      let verifiedAttachment: {
        content: Buffer;
        mimeType: string;
        filename: string;
      } | null = null;
      if (item.kind === "text_and_file") {
        try {
          verifiedAttachment = await this.fetchAndVerifyArtifact(
            runId,
            item.artifact,
          );
        } catch (artErr) {
          const artMsg = safeBackendErrorMessage(
            artErr,
            "Attachment artifact validation failed.",
          );
          try {
            await this.recordValidatedDeliveryProgress({
              delivery_key: item.delivery_key,
              sent_chunks: 0,
              total_chunks: totalChunks,
              status: "failed",
              error_code: "artifact_invalid",
              error_message: artMsg,
            });
          } catch (progressErr) {
            const pMsg = safeBackendErrorMessage(
              progressErr,
              "Failed to record attachment validation failure progress.",
            );
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [],
              code: "delivery_uncertain",
              message: pMsg,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: pMsg,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "failed",
            duplicate: false,
            platform_message_ids: [],
            code: "artifact_invalid",
            message: artMsg,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: "failed",
              code: "artifact_invalid",
              message: artMsg,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }
      }

      const messageIds: string[] = [];
      let sentChunks = 0;

      for (let i = 0; i < textChunks.length; i += 1) {
        let chunkRes: { messageId: string };
        try {
          chunkRes = await readyAdapter.sendTextChunk({
            targetAddress,
            recipientPlatformUserId,
            text: textChunks[i]!,
            chunkIndex: i,
            totalChunks: textChunks.length,
          });
        } catch (chunkErr) {
          const classified = classifyAdapterError(chunkErr);
          const anySent = sentChunks > 0;
          const itemStatus: "partial" | "unknown" | "failed" = anySent
            ? "partial"
            : classified.outcome === "unknown"
              ? "unknown"
              : "failed";
          const overallStatus = itemStatus === "failed" ? "failed" : "unknown";
          try {
            await this.recordValidatedDeliveryProgress({
              delivery_key: item.delivery_key,
              sent_chunks: sentChunks,
              total_chunks: totalChunks,
              platform_message_ids: [...messageIds],
              status: itemStatus,
              error_code: classified.code,
              error_message: classified.message,
            });
          } catch {
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [...messageIds],
              code: "delivery_uncertain",
              message: classified.message,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: classified.message,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: itemStatus,
            duplicate: false,
            platform_message_ids: [...messageIds],
            code: classified.code,
            message: classified.message,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: overallStatus,
              code: itemStatus === "partial" ? "partial" : classified.code,
              message: classified.message,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }

        sentChunks += 1;
        messageIds.push(chunkRes.messageId);
        try {
          await this.recordValidatedDeliveryProgress({
            delivery_key: item.delivery_key,
            sent_chunks: sentChunks,
            total_chunks: totalChunks,
            platform_message_ids: [...messageIds],
          });
        } catch (progressErr) {
          const pMsg = safeBackendErrorMessage(
            progressErr,
            "I-7 chunk progress acknowledgement failed after platform send; state is uncertain.",
          );
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "unknown",
            duplicate: false,
            platform_message_ids: [...messageIds],
            code: "delivery_uncertain",
            message: pMsg,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: "unknown",
              code: "delivery_uncertain",
              message: pMsg,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }
      }

      const filesStatus: Array<{
        path: string;
        status: "sent" | "linked" | "failed";
        reason: string | null;
      }> = [];

      if (item.kind === "text_and_file" && verifiedAttachment) {
        try {
          const fileRes = await readyAdapter.sendFile({
            targetAddress,
            recipientPlatformUserId,
            path: verifiedAttachment.filename,
            filename: verifiedAttachment.filename,
            content: verifiedAttachment.content,
            mimeType: verifiedAttachment.mimeType,
          });
          if (fileRes.status !== "sent") {
            const substMsg =
              fileRes.reason ??
              "Chart file must be uploaded directly; text-only link substitution is not permitted.";
            const itemStatus: "partial" | "failed" =
              sentChunks > 0 ? "partial" : "failed";
            filesStatus.push({
              path: verifiedAttachment.filename,
              status: "failed",
              reason: substMsg,
            });
            try {
              await this.recordValidatedDeliveryProgress({
                delivery_key: item.delivery_key,
                sent_chunks: sentChunks,
                total_chunks: totalChunks,
                platform_message_ids: [...messageIds],
                files_status: filesStatus,
                status: itemStatus,
                error_code: "file_not_sent",
                error_message: substMsg,
              });
            } catch (progressErr) {
              const pMsg = safeBackendErrorMessage(
                progressErr,
                "Failed to record file delivery failure progress; state is uncertain.",
              );
              itemOutcomes.push({
                item_index: item.item_index,
                delivery_key: item.delivery_key,
                status: "unknown",
                duplicate: false,
                platform_message_ids: [...messageIds],
                code: "delivery_uncertain",
                message: pMsg,
              });
              return {
                status: 200,
                data: {
                  ok: false,
                  status: "unknown",
                  code: "delivery_uncertain",
                  message: pMsg,
                  run_id: runId,
                  target,
                  platform,
                  items_total: planItems.length,
                  items_sent: itemsSent,
                  items: itemOutcomes,
                },
              };
            }
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: itemStatus,
              duplicate: false,
              platform_message_ids: [...messageIds],
              code: itemStatus === "partial" ? "partial" : "file_not_sent",
              message: substMsg,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: itemStatus === "failed" ? "failed" : "unknown",
                code: itemStatus === "partial" ? "partial" : "file_not_sent",
                message: substMsg,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          messageIds.push(fileRes.messageId);
          filesStatus.push({
            path: verifiedAttachment.filename,
            status: "sent",
            reason: null,
          });
        } catch (fileErr) {
          const classified = classifyAdapterError(fileErr);
          const anySent = sentChunks > 0;
          const itemStatus: "partial" | "unknown" | "failed" = anySent
            ? "partial"
            : classified.outcome === "unknown"
              ? "unknown"
              : "failed";
          filesStatus.push({
            path: verifiedAttachment.filename,
            status: "failed",
            reason: classified.message,
          });
          try {
            await this.recordValidatedDeliveryProgress({
              delivery_key: item.delivery_key,
              sent_chunks: sentChunks,
              total_chunks: totalChunks,
              platform_message_ids: [...messageIds],
              files_status: filesStatus,
              status: itemStatus,
              error_code: classified.code,
              error_message: classified.message,
            });
          } catch {
            itemOutcomes.push({
              item_index: item.item_index,
              delivery_key: item.delivery_key,
              status: "unknown",
              duplicate: false,
              platform_message_ids: [...messageIds],
              code: "delivery_uncertain",
              message: classified.message,
            });
            return {
              status: 200,
              data: {
                ok: false,
                status: "unknown",
                code: "delivery_uncertain",
                message: classified.message,
                run_id: runId,
                target,
                platform,
                items_total: planItems.length,
                items_sent: itemsSent,
                items: itemOutcomes,
              },
            };
          }
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: itemStatus,
            duplicate: false,
            platform_message_ids: [...messageIds],
            code: itemStatus === "partial" ? "partial" : classified.code,
            message: classified.message,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: itemStatus === "failed" ? "failed" : "unknown",
              code: itemStatus === "partial" ? "partial" : classified.code,
              message: classified.message,
              run_id: runId,
              target,
              platform,
              items_total: planItems.length,
              items_sent: itemsSent,
              items: itemOutcomes,
            },
          };
        }
      }

      try {
        await this.recordValidatedDeliveryProgress({
          delivery_key: item.delivery_key,
          sent_chunks: sentChunks,
          total_chunks: totalChunks,
          platform_message_ids: [...messageIds],
          files_status: filesStatus,
          status: "sent",
          first_response_json: {
            status: "sent",
            platform_message_ids: [...messageIds],
          },
        });
      } catch (progressErr) {
        const pMsg = safeBackendErrorMessage(
          progressErr,
          "I-7 progress acknowledgement failed after platform delivery; state is uncertain.",
        );
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "unknown",
          duplicate: false,
          platform_message_ids: [...messageIds],
          code: "delivery_uncertain",
          message: pMsg,
        });
        return {
          status: 200,
          data: {
            ok: false,
            status: "unknown",
            code: "delivery_uncertain",
            message: pMsg,
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      itemsSent += 1;
      tracker.itemsSent = itemsSent;
      itemOutcomes.push({
        item_index: item.item_index,
        delivery_key: item.delivery_key,
        status: "sent",
        duplicate: false,
        platform_message_ids: [...messageIds],
        code: null,
      });
    }

    return {
      status: 200,
      data: {
        ok: true,
        status: "sent",
        code: null,
        message: `Delivered ${itemsSent} item(s).`,
        run_id: runId,
        target,
        platform,
        items_total: planItems.length,
        items_sent: itemsSent,
        items: itemOutcomes,
      },
    };
  }
}
