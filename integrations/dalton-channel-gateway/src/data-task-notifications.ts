import { createHash } from "node:crypto";
import type { BackendClient } from "./backend-client.js";
import {
  type ChannelAdapter,
  ChannelDeliveryError,
  type SendCardImageInput,
} from "./channels/adapter.js";
import type {
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

function isPlainRecord(val: unknown): val is Record<string, unknown> {
  return typeof val === "object" && val !== null && !Array.isArray(val);
}

function computeSha256Hex(buf: Buffer): string {
  return createHash("sha256").update(buf).digest("hex");
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

export interface DataTaskNotificationServiceOptions {
  backend: BackendClient;
  adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  mode?: "incoming" | "outgoing";
  queueTaskTimeoutMs?: number;
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

  private async fetchAndVerifyArtifact(
    runId: string,
    desc: DataTaskPlanArtifactDescriptor,
  ): Promise<{
    content: Buffer;
    mimeType: string;
    filename: string;
  }> {
    if (desc.size_bytes <= 0 || desc.size_bytes > MAX_ARTIFACT_BYTES) {
      throw new Error(
        `Artifact ${desc.artifact_id} declared size (${desc.size_bytes}) is out of bounds`,
      );
    }
    if (typeof this.backend.downloadDataTaskRunArtifact !== "function") {
      throw new Error(
        "BackendClient does not implement downloadDataTaskRunArtifact",
      );
    }
    const downloaded = await this.backend.downloadDataTaskRunArtifact(
      runId,
      desc.artifact_id,
      MAX_ARTIFACT_BYTES,
    );
    if (downloaded.content.byteLength !== desc.size_bytes) {
      throw new Error(
        `Artifact ${desc.artifact_id} byte length mismatch: expected ${desc.size_bytes}, got ${downloaded.content.byteLength}`,
      );
    }
    const actualSha = computeSha256Hex(downloaded.content);
    const expectedSha = desc.sha256.trim().toLowerCase();
    if (actualSha !== expectedSha) {
      throw new Error(`Artifact ${desc.artifact_id} SHA-256 digest mismatch`);
    }
    if (
      downloaded.sha256Header &&
      downloaded.sha256Header.trim().toLowerCase() !== expectedSha
    ) {
      throw new Error(`Artifact ${desc.artifact_id} header digest mismatch`);
    }
    if (downloaded.mimeType !== desc.mime_type.toLowerCase()) {
      throw new Error(
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
    return await this.queue.enqueue(queueKey, async () =>
      this.executeAuthorizedPlan(normalizedRunId, target),
    );
  }

  private async executeAuthorizedPlan(
    runId: string,
    target: string,
  ): Promise<{ status: number; data: DataTaskNotificationResponse }> {
    if (typeof this.backend.authorizeDataTaskNotificationPlan !== "function") {
      throw new Error(
        "BackendClient does not implement authorizeDataTaskNotificationPlan",
      );
    }
    const plan = await this.backend.authorizeDataTaskNotificationPlan({
      run_id: runId,
      target,
    });

    if (
      !plan.authorized ||
      !plan.platform ||
      !plan.canonical_target_address ||
      !plan.recipient_id ||
      !Array.isArray(plan.items)
    ) {
      const denyCode = plan.code ?? "not_allowed";
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
          message: plan.message ?? "Data-task notification is not authorized.",
          run_id: runId,
          target,
          platform: plan.platform ?? null,
          items_total: 0,
          items_sent: 0,
          items: [],
        },
      };
    }

    const platform = plan.platform;
    const planItems: DataTaskPlanItem[] = plan.items;
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

    const itemOutcomes: DataTaskItemDeliveryOutcome[] = [];
    let itemsSent = 0;
    let groupLiveVerified = false;

    for (const item of planItems) {
      const textChunks =
        item.kind === "feishu_card"
          ? []
          : item.text
            ? readyAdapter.splitTextChunks(item.text)
            : [];
      const totalChunks = item.kind === "feishu_card" ? 1 : textChunks.length;
      const hasNonEmptyText =
        item.kind === "feishu_card" ? true : textChunks.length > 0;

      const claim = await this.backend.authorizeAndClaimDelivery({
        delivery_key: item.delivery_key,
        kind: "data_task",
        run_id: plan.run_id ?? runId,
        target: plan.requested_target ?? target,
        platform,
        has_non_empty_text: hasNonEmptyText,
        total_chunks: totalChunks,
      });

      if (claim.claim_status === "already_completed") {
        if (claim.status === "sent") {
          itemsSent += 1;
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "sent",
            duplicate: true,
            platform_message_ids: [],
            code: null,
          });
          continue;
        }
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "partial",
          duplicate: true,
          platform_message_ids: [],
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

      if (claim.claim_status === "in_flight_or_unknown") {
        const code = claim.code ?? "in_flight";
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "unknown",
          duplicate: false,
          platform_message_ids: [],
          code,
          message:
            claim.message ??
            "Item delivery state is uncertain or in flight; automatic resend is blocked.",
        });
        return {
          status: 200,
          data: {
            ok: false,
            status: "unknown",
            code,
            message:
              claim.message ??
              "Item delivery state is uncertain or in flight; automatic resend is blocked.",
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      if (claim.claim_status === "denied") {
        const denyCode = claim.code ?? "not_allowed";
        itemOutcomes.push({
          item_index: item.item_index,
          delivery_key: item.delivery_key,
          status: "failed",
          duplicate: false,
          platform_message_ids: [],
          code: denyCode,
          message: claim.message ?? "Item claim denied.",
        });
        const overallStatus =
          itemsSent === 0 && SKIP_CODES.has(denyCode) ? "skipped" : "failed";
        return {
          status: 200,
          data: {
            ok: false,
            status: overallStatus,
            code: denyCode,
            message: claim.message ?? "Item claim denied.",
            run_id: runId,
            target,
            platform,
            items_total: planItems.length,
            items_sent: itemsSent,
            items: itemOutcomes,
          },
        };
      }

      const targetAddress =
        claim.target_address ?? plan.canonical_target_address;
      const recipientId = claim.recipient_id ?? plan.recipient_id;

      // Verify live group membership once before sending the first unsent item to a group.
      if (
        plan.target_kind === "group" &&
        !groupLiveVerified &&
        typeof readyAdapter.checkTargetLive === "function"
      ) {
        const liveCheck = await readyAdapter.checkTargetLive({
          userId: plan.owner_user_id ?? "",
          platformUserId: plan.owner_platform_user_id ?? null,
          address: targetAddress,
          purpose: "data_task_notification",
        });
        if (!liveCheck.ok) {
          const denyMsg =
            liveCheck.message ?? `Group target ${targetAddress} is no longer accessible.`;
          await this.backend.updateDeliveryProgress({
            delivery_key: item.delivery_key,
            sent_chunks: 0,
            total_chunks: totalChunks,
            status: "failed",
            error_code: "not_allowed",
            error_message: denyMsg,
          });
          itemOutcomes.push({
            item_index: item.item_index,
            delivery_key: item.delivery_key,
            status: "failed",
            duplicate: false,
            platform_message_ids: [],
            code: "not_allowed",
            message: denyMsg,
          });
          return {
            status: 200,
            data: {
              ok: false,
              status: "failed",
              code: "not_allowed",
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
              throw new Error(
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
          const artMsg =
            artErr instanceof Error ? artErr.message : String(artErr);
          await this.backend.updateDeliveryProgress({
            delivery_key: item.delivery_key,
            sent_chunks: 0,
            total_chunks: 1,
            status: "failed",
            error_code: "artifact_invalid",
            error_message: artMsg,
          });
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
            recipientPlatformUserId: recipientId,
            card: item.card,
            images: cardImages,
          });
          const filesStatus = item.images.map((img) => ({
            path: img.filename,
            status: "sent" as const,
            reason: null,
          }));
          try {
            await this.backend.updateDeliveryProgress({
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
            const pMsg =
              progressErr instanceof Error
                ? progressErr.message
                : String(progressErr);
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
            await this.backend.updateDeliveryProgress({
              delivery_key: item.delivery_key,
              sent_chunks: 0,
              total_chunks: 1,
              status: itemStatus,
              error_code: classified.code,
              error_message: classified.message,
            });
          } catch {
            // Row remains 'in_flight' in dalton_channel_deliveries (safe uncertain state)
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
          const artMsg =
            artErr instanceof Error ? artErr.message : String(artErr);
          await this.backend.updateDeliveryProgress({
            delivery_key: item.delivery_key,
            sent_chunks: 0,
            total_chunks: totalChunks,
            status: "failed",
            error_code: "artifact_invalid",
            error_message: artMsg,
          });
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
        try {
          const chunkRes = await readyAdapter.sendTextChunk({
            targetAddress,
            recipientPlatformUserId: recipientId,
            text: textChunks[i]!,
            chunkIndex: i,
            totalChunks: textChunks.length,
          });
          sentChunks += 1;
          messageIds.push(chunkRes.messageId);
          await this.backend.updateDeliveryProgress({
            delivery_key: item.delivery_key,
            sent_chunks: sentChunks,
            total_chunks: totalChunks,
            platform_message_ids: [...messageIds],
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
            await this.backend.updateDeliveryProgress({
              delivery_key: item.delivery_key,
              sent_chunks: sentChunks,
              total_chunks: totalChunks,
              platform_message_ids: [...messageIds],
              status: itemStatus,
              error_code: classified.code,
              error_message: classified.message,
            });
          } catch {
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
            recipientPlatformUserId: recipientId,
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
            await this.backend.updateDeliveryProgress({
              delivery_key: item.delivery_key,
              sent_chunks: sentChunks,
              total_chunks: totalChunks,
              platform_message_ids: [...messageIds],
              files_status: filesStatus,
              status: itemStatus,
              error_code: "file_not_sent",
              error_message: substMsg,
            });
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
            await this.backend.updateDeliveryProgress({
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
        await this.backend.updateDeliveryProgress({
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
        const pMsg =
          progressErr instanceof Error
            ? progressErr.message
            : String(progressErr);
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
