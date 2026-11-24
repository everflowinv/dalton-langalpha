import { createHash } from "node:crypto";
import { basename } from "node:path";
import type {
  BackendClient,
  DeliveryActivityOrigin,
  WeixinPreflightAuthorizedData,
} from "./backend-client.js";
import { ChannelDeliveryError, type ChannelAdapter } from "./channels/adapter.js";
import { isStaleGatewayEpochError } from "./gateway-lease.js";
import {
  type AgentSendFileOutcome,
  type AgentSendFileRef,
  type AgentSendRequest,
  type AgentSendResponse,
  type ChannelPlatform,
  isSupportedPlatform,
  MAX_ATTACHMENT_BYTES,
  MAX_ATTACHMENT_COUNT,
  MAX_SEND_TEXT_CHARS,
  MAX_TOTAL_ATTACHMENT_BYTES,
} from "./types.js";

export interface ExecuteDeliveryOptions {
  deliveryKey: string;
  kind: "agent_send" | "fallback" | "interrupt" | "automation" | "notice";
  userId?: string | null;
  threadId?: string | null;
  runId?: string | null;
  executionId?: string | null;
  automationExecutionId?: string | null;
  platform?: string | null;
  target?: string | null;
  inboundMessageId?: string | null;
  text: string;
  files?: AgentSendFileRef[];
  replyToMessageId?: string | null;
  newThread?: boolean;
  threadScopeKey?: string | null;
  sourceScopeKey?: string | null;
  sourceGeneration?: number | null;
  activityOrigin?: DeliveryActivityOrigin | null;
}

export interface FirstCurrentDeliveryInfo {
  platform: ChannelPlatform;
  runId?: string | null;
  inboundMessageId?: string | null;
  kind: "agent_send" | "fallback" | "interrupt";
}

export class DeliveryService {
  private readonly backend: BackendClient;
  private readonly adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  private readonly firstCurrentDeliveryListeners = new Set<
    (info: FirstCurrentDeliveryInfo) => void
  >();
  private readonly inFlightCurrentDeliveriesByRun = new Map<
    string,
    Set<Promise<AgentSendResponse>>
  >();
  private startupBlocked = false;

  constructor(options: {
    backend: BackendClient;
    adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
    onFirstCurrentDelivery?: (info: FirstCurrentDeliveryInfo) => void;
  }) {
    this.backend = options.backend;
    this.adapters = options.adapters;
    if (options.onFirstCurrentDelivery) {
      this.firstCurrentDeliveryListeners.add(options.onFirstCurrentDelivery);
    }
  }

  /**
   * Blocks new I-6 delivery claims while startup `mode=startup` recovery is still running (D1 / §3.5).
   */
  setStartupBlocked(blocked: boolean): void {
    this.startupBlocked = blocked;
  }

  addFirstCurrentDeliveryListener(
    listener: (info: FirstCurrentDeliveryInfo) => void,
  ): void {
    this.firstCurrentDeliveryListeners.add(listener);
  }

  hasInFlightCurrentDelivery(runId: string | null | undefined): boolean {
    const trimmed = runId?.trim();
    if (!trimmed) {
      return false;
    }
    const set = this.inFlightCurrentDeliveriesByRun.get(trimmed);
    return Boolean(set && set.size > 0);
  }

  /**
   * Awaits all currently in-flight current-conversation content deliveries
   * (`agent_send` / `fallback`) tracked for `runId` and returns their final outcomes.
   */
  async waitForInFlightCurrentDeliveries(
    runId: string | null | undefined,
  ): Promise<AgentSendResponse[]> {
    const trimmed = runId?.trim();
    if (!trimmed) {
      return [];
    }
    const outcomes: AgentSendResponse[] = [];
    const seen = new Set<Promise<AgentSendResponse>>();
    for (;;) {
      const set = this.inFlightCurrentDeliveriesByRun.get(trimmed);
      if (!set || set.size === 0) {
        break;
      }
      const pending = [...set].filter((p) => !seen.has(p));
      if (pending.length === 0) {
        break;
      }
      for (const p of pending) {
        seen.add(p);
      }
      const settled = await Promise.allSettled(pending);
      for (const item of settled) {
        if (item.status === "fulfilled") {
          outcomes.push(item.value);
        } else {
          outcomes.push({
            status: "unknown",
            code: "unavailable",
            address: null,
            current: true,
            duplicate: false,
            message: "In-flight delivery rejected unexpectedly.",
            files: [],
          });
        }
      }
    }
    return outcomes;
  }

  private notifyFirstCurrentDelivery(info: FirstCurrentDeliveryInfo): void {
    for (const listener of this.firstCurrentDeliveryListeners) {
      try {
        listener(info);
      } catch {
        // Best-effort status feedback hook must never disrupt delivery.
      }
    }
  }

  /**
   * Compute `delivery_key` for `POST /agent/send` (§1.2.1):
   * - If `tool_call_id` is non-empty: `${run_id}:${tool_call_id}`
   * - Otherwise fallback hash: `${run_id}:notcid:${sha256(canonical_json({target, text, files, reply, new_thread}))}`
   */
  static makeAgentSendDeliveryKey(req: AgentSendRequest): string {
    const tcid = (req.tool_call_id ?? "").trim();
    if (tcid.length > 0) {
      return `${req.run_id}:${tcid}`;
    }
    const canonical = JSON.stringify({
      target: req.target ?? null,
      text: req.text ?? "",
      files: (req.files ?? []).map((f) => ({
        path: f.path,
        workspace_id: f.workspace_id,
      })),
      reply: Boolean(req.reply),
      new_thread: Boolean(req.new_thread),
    });
    const digest = createHash("sha256").update(canonical, "utf8").digest("hex");
    return `${req.run_id}:notcid:${digest}`;
  }

  /**
   * Execute an outbound delivery through `I-6` (`authorize-and-claim`) and `I-7` (`deliveries/progress`).
   */
  executeDelivery(options: ExecuteDeliveryOptions): Promise<AgentSendResponse> {
    const trackRunId =
      (options.kind === "agent_send" || options.kind === "fallback") &&
      typeof options.runId === "string" &&
      options.runId.trim().length > 0
        ? options.runId.trim()
        : null;

    if (!trackRunId) {
      return this.executeDeliveryCore(options);
    }

    let detached = false;
    let trackedPromise!: Promise<AgentSendResponse>;
    const untrack = (): void => {
      if (detached) return;
      detached = true;
      const set = this.inFlightCurrentDeliveriesByRun.get(trackRunId);
      if (set) {
        set.delete(trackedPromise);
        if (set.size === 0) {
          this.inFlightCurrentDeliveriesByRun.delete(trackRunId);
        }
      }
    };

    trackedPromise = this.executeDeliveryCore(options, untrack).finally(untrack);
    let set = this.inFlightCurrentDeliveriesByRun.get(trackRunId);
    if (!set) {
      set = new Set<Promise<AgentSendResponse>>();
      this.inFlightCurrentDeliveriesByRun.set(trackRunId, set);
    }
    set.add(trackedPromise);
    return trackedPromise;
  }

  private async executeDeliveryCore(
    options: ExecuteDeliveryOptions,
    untrackIfNotCurrent?: () => void,
  ): Promise<AgentSendResponse> {
    if (this.startupBlocked) {
      untrackIfNotCurrent?.();
      return {
        status: "failed",
        code: "unavailable",
        address: options.target ?? null,
        current: options.target === undefined || options.target === null,
        duplicate: false,
        message: "Gateway startup recovery is still in progress.",
        files: [],
      };
    }

    const rawText = options.text ?? "";
    const trimmedText = rawText.trim();
    const hasNonEmptyText = trimmedText.length > 0;
    const files = options.files ?? [];

    if (!hasNonEmptyText && files.length === 0) {
      return {
        status: "failed",
        code: null,
        address: options.target ?? null,
        current: options.target === undefined || options.target === null,
        duplicate: false,
        message: "Both text and files are empty; nothing to send.",
        files: [],
      };
    }

    if (rawText.length > MAX_SEND_TEXT_CHARS) {
      return {
        status: "failed",
        code: null,
        address: options.target ?? null,
        current: options.target === undefined || options.target === null,
        duplicate: false,
        message: `Message text exceeds the ${MAX_SEND_TEXT_CHARS}-character limit.`,
        files: [],
      };
    }

    if (files.length > MAX_ATTACHMENT_COUNT) {
      return {
        status: "failed",
        code: null,
        address: options.target ?? null,
        current: options.target === undefined || options.target === null,
        duplicate: false,
        message: `Too many files attached (max ${MAX_ATTACHMENT_COUNT}).`,
        files: files.map((f) => ({
          path: f.path,
          status: "failed",
          reason: `Exceeds maximum of ${MAX_ATTACHMENT_COUNT} files per send`,
        })),
      };
    }

    // Preliminary chunk count (refined once platform is known from I-6)
    const preliminaryPlat = this.inferPlatform(options.platform, options.target);
    const preliminaryAdapter = preliminaryPlat ? this.adapters.get(preliminaryPlat) : undefined;
    const preliminaryChunks = hasNonEmptyText
      ? preliminaryAdapter
        ? preliminaryAdapter.splitTextChunks(rawText)
        : [rawText]
      : [];

    const claim = await this.backend.authorizeAndClaimDelivery({
      delivery_key: options.deliveryKey,
      kind: options.kind,
      user_id: options.userId ?? null,
      thread_id: options.threadId ?? null,
      run_id: options.runId ?? null,
      execution_id: options.executionId ?? null,
      automation_execution_id: options.automationExecutionId ?? null,
      platform: options.platform ?? preliminaryPlat ?? null,
      target: options.target ?? null,
      inbound_message_id: options.inboundMessageId ?? null,
      has_non_empty_text: hasNonEmptyText,
      total_chunks: Math.max(preliminaryChunks.length, hasNonEmptyText ? 1 : 0),
      source_scope_key: options.sourceScopeKey ?? options.threadScopeKey ?? null,
      source_generation: options.sourceGeneration ?? null,
      activity_origin: options.activityOrigin ?? null,
    });

    if (claim.claim_status === "already_completed") {
      if (!claim.is_current) {
        untrackIfNotCurrent?.();
      }
      const prev = (claim.first_response_json ?? {}) as Partial<AgentSendResponse>;
      return {
        status: (prev.status ?? claim.status ?? "sent") as AgentSendResponse["status"],
        code: (prev.code ?? null) as AgentSendResponse["code"],
        address: (prev.address ?? claim.target_address ?? null) as string | null,
        current: Boolean(prev.current ?? claim.is_current),
        duplicate: true,
        message:
          prev.message ??
          "An earlier attempt of this same call already delivered it; nothing was sent twice.",
        files: Array.isArray(prev.files) ? prev.files : [],
      };
    }

    if (claim.claim_status === "in_flight_or_unknown") {
      if (!claim.is_current) {
        untrackIfNotCurrent?.();
      }
      return {
        status: "unknown",
        code: "in_flight",
        address: claim.target_address ?? options.target ?? null,
        current: Boolean(claim.is_current),
        duplicate: false,
        message:
          claim.message ??
          "An earlier attempt of this same call is still delivering it, and it will arrive or fail on its own. Do not send it again, and do not tell the user it was sent.",
        files: [],
      };
    }

    if (claim.claim_status === "denied") {
      if (!claim.is_current) {
        untrackIfNotCurrent?.();
      }
      const mappedCode =
        claim.code === "unknown_delivery_blocks_fallback"
          ? null
          : ((claim.code ?? "not_allowed") as AgentSendResponse["code"]);
      return {
        status: "failed",
        code: mappedCode,
        address: claim.target_address ?? options.target ?? null,
        current: Boolean(claim.is_current),
        duplicate: false,
        message: claim.message ?? "Delivery was refused.",
        files: [],
      };
    }

    // `claim_status === "claimed"`
    if (!claim.is_current) {
      untrackIfNotCurrent?.();
    }
    const deliveryAttempt =
      typeof claim.delivery_attempt === "number" ? claim.delivery_attempt : undefined;
    const recordProgress = (
      body: Parameters<BackendClient["updateDeliveryProgress"]>[0],
    ) =>
      this.backend.updateDeliveryProgress(
        deliveryAttempt === undefined
          ? body
          : { ...body, delivery_attempt: deliveryAttempt },
      );
    // Note (`PROTOCOL.md` §9 I-6): `claim.recipient_id` is the platform recipient ID
    // (`platform_user_id` for DM or `chat_id` for group), whereas `options.userId` is the Dalton user UUID!
    const platformStr = (claim.platform ?? preliminaryPlat ?? "").toLowerCase();
    const targetAddress = claim.target_address ?? options.target ?? null;
    const isCurrent = Boolean(claim.is_current);
    const effectiveThreadScopeKey =
      claim.thread_scope_key && claim.thread_scope_key.trim().length > 0
        ? claim.thread_scope_key.trim()
        : options.target && !isCurrent
          ? null
          : (options.threadScopeKey ?? null);
    const daltonUserId = (options.userId ?? "").trim();

    if (!isSupportedPlatform(platformStr) || !targetAddress) {
      const failResp: AgentSendResponse = {
        status: "failed",
        code: "unavailable",
        address: targetAddress,
        current: isCurrent,
        duplicate: false,
        message: `Platform ${platformStr || "unknown"} is not supported or target address is missing.`,
        files: [],
      };
      await recordProgress({
        delivery_key: options.deliveryKey,
        status: "failed",
        error_code: "unavailable",
        error_message: failResp.message,
        first_response_json: failResp as unknown as Record<string, unknown>,
      });
      return failResp;
    }

    const adapter = this.adapters.get(platformStr);
    if (!adapter || !adapter.isConfigured() || !adapter.isConnected()) {
      const failResp: AgentSendResponse = {
        status: "failed",
        code: "unavailable",
        address: targetAddress,
        current: isCurrent,
        duplicate: false,
        message: `The ${platformStr} channel adapter is not connected.`,
        files: [],
      };
      await recordProgress({
        delivery_key: options.deliveryKey,
        status: "failed",
        error_code: "unavailable",
        error_message: failResp.message,
        first_response_json: failResp as unknown as Record<string, unknown>,
      });
      return failResp;
    }

    const chunks = hasNonEmptyText ? adapter.splitTextChunks(rawText) : [];
    const totalChunks = chunks.length;
    if (totalChunks !== preliminaryChunks.length) {
      await recordProgress({
        delivery_key: options.deliveryKey,
        total_chunks: totalChunks,
      });
    }

    // `claim.recipient_id` already carries the platform recipient ID from `I-6`.
    // Only fall back to `I-4` lookup (with Dalton `user_id`) if `claim.recipient_id` was omitted on a DM.
    let recipientPlatformUserId: string | null =
      claim.recipient_id && claim.recipient_id.trim().length > 0
        ? claim.recipient_id.trim()
        : null;
    if (!recipientPlatformUserId && targetAddress.endsWith(":@me") && daltonUserId) {
      try {
        const state = await this.backend.getChannelState({
          user_id: daltonUserId,
          platform: platformStr,
        });
        recipientPlatformUserId =
          state.identities?.[platformStr]?.platform_user_id ?? null;
      } catch {
        // Proceed with targetAddress if lookup fails
      }
    }

    const platformMessageIds: string[] = [];
    let sentChunks = 0;
    let firstCurrentDeliveryNotified = false;
    const isFeedbackTrackedKind =
      options.kind === "agent_send" ||
      options.kind === "fallback" ||
      options.kind === "interrupt";
    const maybeNotifyFirstCurrentDelivery = (): void => {
      if (firstCurrentDeliveryNotified || !isCurrent || !isFeedbackTrackedKind) {
        return;
      }
      firstCurrentDeliveryNotified = true;
      this.notifyFirstCurrentDelivery({
        platform: platformStr,
        runId: options.runId ?? null,
        inboundMessageId: options.inboundMessageId ?? null,
        kind: options.kind as "agent_send" | "fallback" | "interrupt",
      });
    };
    let finalAddress = targetAddress;
    const confirmedThreadAddress = (claim.confirmed_thread_address ?? "").trim();
    const reuseConfirmedThread =
      platformStr === "discord" && confirmedThreadAddress.length > 0;
    let chunkFailure: {
      outcome: "failed" | "unknown";
      code: AgentSendResponse["code"];
      denyCode?: string;
      retryable?: boolean;
      message: string;
    } | null = null;
    const effectiveWeixinAttempt =
      typeof deliveryAttempt === "number" && deliveryAttempt > 0
        ? deliveryAttempt
        : 1;

    for (let i = 0; i < chunks.length; i++) {
      const chunkText = chunks[i]!;
      try {
        let weixinPreflight: WeixinPreflightAuthorizedData | null = null;
        let weixinContextToken: string | null = claim.weixin_context_token ?? null;
        let effectiveRecipientId = recipientPlatformUserId;
        if (platformStr === "weixin") {
          weixinPreflight = await this.runWeixinPreflight(
            options.deliveryKey,
            effectiveWeixinAttempt,
            i,
          );
          weixinContextToken = weixinPreflight.weixin_context_token;
          effectiveRecipientId = weixinPreflight.recipient_id;
        }

        const creatingDiscordThread =
          i === 0 &&
          platformStr === "discord" &&
          options.newThread === true &&
          !reuseConfirmedThread;
        if (i === 0 && reuseConfirmedThread) {
          await this.authorizeDiscordBodyDispatch(options.deliveryKey, deliveryAttempt);
        }
        const res = await adapter.sendTextChunk({
          targetAddress: finalAddress,
          recipientPlatformUserId: effectiveRecipientId,
          text: chunkText,
          chunkIndex: i,
          totalChunks,
          replyToMessageId: i === 0 ? options.replyToMessageId : null,
          newThread: i === 0 ? Boolean(options.newThread) && !reuseConfirmedThread : false,
          threadScopeKey: effectiveThreadScopeKey,
          deliveryKey: options.deliveryKey,
          deliveryAttempt: effectiveWeixinAttempt,
          weixinPreflight,
          weixinAccountId: weixinPreflight?.account_id ?? claim.weixin_account_id ?? null,
          weixinContextToken,
          inboundMessageId: options.inboundMessageId ?? null,
          confirmedThreadAddress:
            i === 0 && reuseConfirmedThread ? confirmedThreadAddress : null,
          onConfirmedThread: creatingDiscordThread
            ? async (confirmed) => {
                await this.storeConfirmedDiscordThread(
                  options.deliveryKey,
                  confirmed.address,
                  deliveryAttempt,
                );
                finalAddress = confirmed.address;
                await this.authorizeDiscordBodyDispatch(
                  options.deliveryKey,
                  deliveryAttempt,
                );
              }
            : undefined,
        });
        sentChunks += 1;
        platformMessageIds.push(res.messageId);
        if (res.resolvedAddress) {
          finalAddress = res.resolvedAddress;
        }
        maybeNotifyFirstCurrentDelivery();
        await recordProgress({
          delivery_key: options.deliveryKey,
          sent_chunks: sentChunks,
          total_chunks: totalChunks,
          platform_message_ids: platformMessageIds,
        });
      } catch (err) {
        if (isStaleGatewayEpochError(err)) {
          // D1 / §3.5: stale epoch MUST stop locally without writing I-7 settlement.
          throw err;
        }
        if (err instanceof ChannelDeliveryError) {
          chunkFailure = {
            outcome: err.outcome === "unknown" ? "unknown" : "failed",
            code: err.code,
            denyCode: err.denyCode,
            retryable: err.retryable,
            message: err.message,
          };
        } else {
          chunkFailure = {
            outcome: "unknown",
            code: "unavailable",
            message: err instanceof Error ? err.message : String(err),
          };
        }
        break;
      }
    }

    // Process files if text chunks did not fail before any progress
    const fileOutcomes: AgentSendFileOutcome[] = [];
    let totalDownloadedBytes = 0;
    let anyFileSucceeded = false;
    let anyFileFailed = false;
    let fileUnknownError: string | null = null;
    let fileFailureCode: AgentSendResponse["code"] = null;
    let fileFailureDenyCode: string | undefined;

    if (!chunkFailure) {
      for (let fileIdx = 0; fileIdx < files.length; fileIdx += 1) {
        const fileRef = files[fileIdx]!;
        const wsId = fileRef.workspace_id || claim.home_workspace_id || "";
        if (!wsId) {
          anyFileFailed = true;
          fileOutcomes.push({
            path: fileRef.path,
            status: "failed",
            reason: "Missing workspace_id for file attachment",
          });
          continue;
        }
        let downloaded: { content: Buffer; mimeType: string };
        try {
          // Always pass Dalton user UUID (`daltonUserId`) as `X-User-Id` (`PROTOCOL.md` §8)
          downloaded = await this.backend.downloadWorkspaceFile(
            daltonUserId,
            wsId,
            fileRef.path,
          );
        } catch (err) {
          anyFileFailed = true;
          fileOutcomes.push({
            path: fileRef.path,
            status: "failed",
            reason: err instanceof Error ? err.message : "Failed to download file from workspace",
          });
          continue;
        }

        const byteLen = downloaded.content.byteLength;
        if (byteLen > MAX_ATTACHMENT_BYTES) {
          anyFileFailed = true;
          fileOutcomes.push({
            path: fileRef.path,
            status: "failed",
            reason: "File exceeds 20 MB limit",
          });
          continue;
        }
        if (totalDownloadedBytes + byteLen > MAX_TOTAL_ATTACHMENT_BYTES) {
          anyFileFailed = true;
          fileOutcomes.push({
            path: fileRef.path,
            status: "failed",
            reason: "Total attachments exceed 50 MB limit",
          });
          continue;
        }
        totalDownloadedBytes += byteLen;

        try {
          const fileChunkIndex = chunks.length + fileIdx;
          let weixinPreflight: WeixinPreflightAuthorizedData | null = null;
          let weixinContextToken: string | null = claim.weixin_context_token ?? null;
          let effectiveRecipientId = recipientPlatformUserId;
          if (platformStr === "weixin") {
            weixinPreflight = await this.runWeixinPreflight(
              options.deliveryKey,
              effectiveWeixinAttempt,
              fileChunkIndex,
            );
            weixinContextToken = weixinPreflight.weixin_context_token;
            effectiveRecipientId = weixinPreflight.recipient_id;
          }

          const sendRes = await adapter.sendFile({
            targetAddress: finalAddress,
            recipientPlatformUserId: effectiveRecipientId,
            path: fileRef.path,
            filename: basename(fileRef.path) || "attachment",
            content: downloaded.content,
            mimeType: downloaded.mimeType,
            threadScopeKey: effectiveThreadScopeKey,
            deliveryKey: options.deliveryKey,
            deliveryAttempt: effectiveWeixinAttempt,
            weixinPreflight,
            weixinAccountId: weixinPreflight?.account_id ?? claim.weixin_account_id ?? null,
            weixinContextToken,
            inboundMessageId: options.inboundMessageId ?? null,
            refreshWeixinPreflight:
              platformStr === "weixin"
                ? () =>
                    this.backend.preflightWeixinSend(options.deliveryKey, {
                      delivery_attempt: effectiveWeixinAttempt,
                      chunk_index: fileChunkIndex,
                    })
                : undefined,
          });
          const fileSucceeded =
            sendRes.status === "sent" || sendRes.status === "linked";
          if (fileSucceeded) {
            platformMessageIds.push(sendRes.messageId);
            anyFileSucceeded = true;
            maybeNotifyFirstCurrentDelivery();
          } else {
            anyFileFailed = true;
          }
          fileOutcomes.push({
            path: fileRef.path,
            status: sendRes.status,
            reason: sendRes.reason ?? null,
          });
          if (fileSucceeded) {
            await recordProgress({
              delivery_key: options.deliveryKey,
              sent_chunks: sentChunks,
              total_chunks: totalChunks,
              platform_message_ids: [...platformMessageIds],
              files_status: [...fileOutcomes],
            });
          }
        } catch (err) {
          if (isStaleGatewayEpochError(err)) {
            throw err;
          }
          anyFileFailed = true;
          if (err instanceof ChannelDeliveryError) {
            if (err.outcome === "unknown") {
              fileUnknownError = err.message;
            }
            if (!fileFailureCode) {
              fileFailureCode = err.code;
            }
            if (!fileFailureDenyCode && err.denyCode) {
              fileFailureDenyCode = err.denyCode;
            }
          }
          fileOutcomes.push({
            path: fileRef.path,
            status: "failed",
            reason: err instanceof Error ? err.message : String(err),
          });
        }
      }
    } else if (files.length > 0) {
      for (const fileRef of files) {
        anyFileFailed = true;
        fileOutcomes.push({
          path: fileRef.path,
          status: "failed",
          reason: "Skipped because earlier message chunk failed",
        });
      }
    }

    const somethingSucceeded = sentChunks > 0 || anyFileSucceeded;
    let finalStatus: AgentSendResponse["status"];
    let finalCode: AgentSendResponse["code"] = null;
    let finalMessage: string;

    if (chunkFailure) {
      if (somethingSucceeded) {
        finalStatus = "partial";
        finalCode = chunkFailure.code;
        finalMessage = `Delivered ${sentChunks}/${totalChunks} text chunks before error: ${chunkFailure.message}`;
      } else if (chunkFailure.outcome === "unknown") {
        finalStatus = "unknown";
        finalCode = chunkFailure.code;
        finalMessage = `Delivery outcome could not be confirmed: ${chunkFailure.message}`;
      } else {
        finalStatus = "failed";
        finalCode = chunkFailure.code;
        finalMessage = chunkFailure.message;
      }
    } else if (anyFileFailed) {
      if (somethingSucceeded) {
        finalStatus = "partial";
        finalCode = fileFailureCode;
        finalMessage = "Message delivered, but one or more attached files failed.";
      } else if (fileUnknownError) {
        finalStatus = "unknown";
        finalCode = "unavailable";
        finalMessage = `File delivery outcome could not be confirmed: ${fileUnknownError}`;
      } else {
        finalStatus = "failed";
        finalCode = fileFailureCode;
        finalMessage = "All attached files failed to deliver.";
      }
    } else {
      finalStatus = "sent";
      finalCode = null;
      finalMessage = `Delivered to ${finalAddress}.`;
    }

    const responseObj: AgentSendResponse = {
      status: finalStatus,
      code: finalCode,
      address: finalAddress,
      current: isCurrent,
      duplicate: false,
      message: finalMessage,
      files: fileOutcomes,
    };

    const recordedErrorCode =
      platformStr === "weixin"
        ? (chunkFailure?.denyCode ?? fileFailureDenyCode ?? finalCode)
        : finalCode;

    await recordProgress({
      delivery_key: options.deliveryKey,
      sent_chunks: sentChunks,
      total_chunks: totalChunks,
      platform_message_ids: platformMessageIds,
      files_status: fileOutcomes,
      first_response_json: responseObj as unknown as Record<string, unknown>,
      status: finalStatus,
      error_code: recordedErrorCode,
      error_message: finalStatus === "sent" ? null : finalMessage,
    });

    return responseObj;
  }

  private async runWeixinPreflight(
    deliveryKey: string,
    deliveryAttempt: number,
    chunkIndex: number,
  ): Promise<WeixinPreflightAuthorizedData> {
    const pf = await this.backend.preflightWeixinSend(deliveryKey, {
      delivery_attempt: deliveryAttempt,
      chunk_index: chunkIndex,
    });
    if (pf.authorized) {
      return pf;
    }
    if (pf.deny_code === "source_generation_unknown") {
      throw new ChannelDeliveryError(
        pf.message ?? "Weixin delivery source generation is unknown.",
        {
          outcome: "unknown",
          code: "unavailable",
          retryable: false,
          denyCode: pf.deny_code,
        },
      );
    }
    if (pf.decision === "defer" || pf.retryable) {
      throw new ChannelDeliveryError(
        pf.message ?? `Weixin send deferred (${pf.deny_code}); please retry.`,
        {
          outcome: "failed",
          code: "unavailable",
          retryable: true,
          denyCode: pf.deny_code,
        },
      );
    }
    throw new ChannelDeliveryError(
      pf.message ?? `Weixin send denied (${pf.deny_code}).`,
      {
        outcome: "failed",
        code: "not_allowed",
        retryable: false,
        denyCode: pf.deny_code,
      },
    );
  }

  /**
   * Store a Discord thread that create or adoption already confirmed.
   * The same progress transaction records `before_body` when no body phase
   * exists yet. A missing acknowledgment does not send. The caller writes one
   * terminal status for this claim generation. A later claim can resume only
   * when that stored phase is still `before_body`.
   */
  private async storeConfirmedDiscordThread(
    deliveryKey: string,
    address: string,
    deliveryAttempt?: number,
  ): Promise<void> {
    let stored = false;
    try {
      const ack = await this.backend.updateDeliveryProgress({
        delivery_key: deliveryKey,
        confirmed_thread_address: address,
        ...(typeof deliveryAttempt === "number"
          ? { delivery_attempt: deliveryAttempt }
          : {}),
      });
      const phase = ack.discord_body_phase;
      stored =
        ack.ok === true &&
        ack.confirmed_thread_address === address &&
        (phase === "before_body" || phase === "dispatching");
    } catch {
      stored = false;
    }
    if (stored) return;
    throw new ChannelDeliveryError("Discord confirmed thread was not stored", {
      outcome: "unknown",
      code: "unavailable",
    });
  }

  /**
   * Authorize one Discord body POST.
   * The POST runs only after this acknowledgment echoes `dispatching`.
   * A lost acknowledgment does not send and does not write a second status.
   * The caller writes one terminal status for this claim generation.
   * `unknown` plus `dispatching` is not claimed again, including after startup.
   */
  private async authorizeDiscordBodyDispatch(
    deliveryKey: string,
    deliveryAttempt?: number,
  ): Promise<void> {
    let authorized = false;
    try {
      const ack = await this.backend.updateDeliveryProgress({
        delivery_key: deliveryKey,
        discord_body_phase: "dispatching",
        ...(typeof deliveryAttempt === "number"
          ? { delivery_attempt: deliveryAttempt }
          : {}),
      });
      authorized = ack.ok === true && ack.discord_body_phase === "dispatching";
    } catch {
      authorized = false;
    }
    if (authorized) return;
    throw new ChannelDeliveryError("Discord body dispatch was not acknowledged", {
      outcome: "unknown",
      code: "unavailable",
    });
  }

  private inferPlatform(
    explicitPlatform?: string | null,
    target?: string | null,
  ): ChannelPlatform | null {
    if (explicitPlatform && isSupportedPlatform(explicitPlatform.toLowerCase())) {
      return explicitPlatform.toLowerCase() as ChannelPlatform;
    }
    if (target) {
      const [prefix] = target.split(":", 1);
      if (prefix && isSupportedPlatform(prefix.toLowerCase())) {
        return prefix.toLowerCase() as ChannelPlatform;
      }
    }
    return null;
  }
}
