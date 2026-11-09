import { chmodSync, mkdirSync, writeFileSync } from "node:fs";
import { basename, resolve } from "node:path";
import type { AgentRoutesHandler } from "./agent-routes.js";
import {
  type BackendClient,
  BackendHttpError,
  type I1RegisterAndResolveResponse,
  type RecoverAutomationRunRow,
  type RecoverBackgroundRunRow,
  type RecoverInboundEventRow,
  type RecoverThreadWatchRow,
  type SseEvent,
  type SseOrErrorResponse,
  type ThreadMessageDispatchPayload,
  type ThreadStatusInfo,
  type ThreadWakeupItem,
} from "./backend-client.js";
import type { ChannelAdapter } from "./channels/adapter.js";
import type { DeliveryService } from "./delivery-service.js";
import {
  ACTIVE_WAKEUP_STATES,
  type AskUserQuestionItem,
  buildSurfaceRules,
  type ChannelPlatform,
  type ChatContentItem,
  type InboundChannelEvent,
  INLINE_IMAGE_MIME_TYPES,
  INTERNAL_REPLAY_NEEDED_OUTCOMES,
  type InterruptActionRequest,
  MAX_ATTACHMENT_BYTES,
  MAX_ATTACHMENT_COUNT,
  MAX_INLINE_IMAGE_BYTES,
  MAX_TOTAL_ATTACHMENT_BYTES,
  type PendingInterruptState,
  PUBLIC_LIVE_STATUSES,
  PUBLIC_REPLAY_NEEDED_STATUSES,
  type UploadedAttachmentRecord,
} from "./types.js";
import {
  makeCreditSupplementSteerKey,
  makeInboundRequestKey,
  makeReturnedSteerRequestKey,
  makeRetryRequestKey,
  makeTurnAfterSteerRequestKey,
} from "./uuid.js";

const BACKOFF_DELAYS_MS = [1000, 2000, 4000];
const CAPACITY_BACKOFF_DELAYS_MS = [2000, 4000, 8000];
export const DEFAULT_BUSY_THREAD_MAX_WAIT_MS = 30 * 60 * 1000;

export interface PendingNeedsInputNotice {
  wsName: string;
  title: string;
  question: string;
  explicitToken: string | null;
  deliveryKey?: string | null;
  resolvedToken?: string | null;
}

export interface PendingReplayFallbackEntry {
  userId: string;
  threadId: string;
  runId: string | null;
  turnIndex: number | null;
  platform: ChannelPlatform;
  targetAddress: string;
  inboundMessageId?: string | null;
  scopeKey?: string | null;
  isBackgroundRun?: boolean;
}

export interface ActiveWatchHandle {
  threadId: string;
  userId: string;
  platform: ChannelPlatform;
  targetAddress: string;
  scopeKey: string;
  generation: number;
  lastSeenTurnIndex: number;
  abortController: AbortController;
  promise: Promise<void>;
  syncingReconnect?: boolean;
  needsReconnectSync?: boolean;
  needsInputSeqByThread?: Map<string, number>;
  lastNeedsInputTokenByThread?: Map<string, string>;
  pendingNeedsInputThreads?: Map<string, PendingNeedsInputNotice>;
  pendingReplayFallbacks?: Map<string, PendingReplayFallbackEntry>;
}

export class ChannelGatewayOrchestrator {
  private readonly backend: BackendClient;
  private readonly adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  private readonly deliveryService: DeliveryService;
  private readonly agentRoutes: AgentRoutesHandler;
  private readonly webBaseUrl: string;
  private readonly weixinCacheDir: string;
  private readonly backoffDelayFn: (ms: number) => Promise<void>;
  private readonly busyThreadMaxWaitMs: number;
  private readonly nowFn: () => number;
  private shuttingDown = false;

  readonly activeWatches = new Map<string, ActiveWatchHandle>();
  private readonly activeBackgroundRunIds = new Map<string, Set<string>>();
  private readonly activeInboundTurns = new Set<Promise<void>>();
  private startupRecoveryPromise: Promise<void> | null = null;

  constructor(options: {
    backend: BackendClient;
    adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
    deliveryService: DeliveryService;
    agentRoutes: AgentRoutesHandler;
    webBaseUrl: string;
    weixinCacheDir: string;
    backoffDelayFn?: (ms: number) => Promise<void>;
    busyThreadMaxWaitMs?: number;
    nowFn?: () => number;
  }) {
    this.backend = options.backend;
    this.adapters = options.adapters;
    this.deliveryService = options.deliveryService;
    this.agentRoutes = options.agentRoutes;
    this.webBaseUrl = options.webBaseUrl.replace(/\/+$/, "");
    this.weixinCacheDir = options.weixinCacheDir;
    this.backoffDelayFn =
      options.backoffDelayFn ??
      ((ms: number) => new Promise((r) => setTimeout(r, ms)));
    this.busyThreadMaxWaitMs =
      options.busyThreadMaxWaitMs ?? DEFAULT_BUSY_THREAD_MAX_WAIT_MS;
    this.nowFn = options.nowFn ?? (() => Date.now());

    for (const adapter of this.adapters.values()) {
      adapter.onInboundEvent(async (event) => {
        await this.handleInboundEvent(event);
      });
    }
  }

  // =========================================================================
  // §3.3 & §1.3.1 Inbound Event Entry Point
  // =========================================================================

  async handleInboundEvent(event: InboundChannelEvent): Promise<void> {
    // Group chats require explicit trigger (@bot or slash command)
    if (event.chatType === "group" && event.isGroupTrigger === false) {
      return;
    }

    const trimmedText = (event.text ?? "").trim();

    // Check for slash commands (§3.3): /bind, /unbind, /workspace, /new, /stop
    if (trimmedText.startsWith("/")) {
      const handled = await this.tryHandleSlashCommand(event, trimmedText);
      if (handled) {
        return;
      }
    }

    const requestKey = makeInboundRequestKey(event.platform, event.messageId);
    const resolved = await this.backend.registerAndResolveInbound({
      platform: event.platform,
      platform_user_id: event.platformUserId,
      chat_type: event.chatType,
      chat_address: event.chatAddress,
      thread_scope_key: event.threadScopeKey,
      message_id: event.messageId,
      request_key: requestKey,
      raw_payload: {
        ...(event.rawPayload ?? {}),
        platform_user_id: event.platformUserId,
        text: event.text,
        chat_id: event.chatId,
        chat_name: event.chatName ?? null,
      },
      weixin_context_token: event.weixinContextToken ?? null,
    });

    await this.launchInboundTurn(event, () =>
      this.executeRegisteredInboundTurn(event, resolved),
    );
  }

  private async recoverDetachedInboundTurn(
    event: InboundChannelEvent,
  ): Promise<void> {
    const requestKey = makeInboundRequestKey(event.platform, event.messageId);
    const reresolved = await this.backend.registerAndResolveInbound({
      platform: event.platform,
      platform_user_id: event.platformUserId,
      chat_type: event.chatType,
      chat_address: event.chatAddress,
      thread_scope_key: event.threadScopeKey,
      message_id: event.messageId,
      request_key: requestKey,
      raw_payload: {
        ...(event.rawPayload ?? {}),
        platform_user_id: event.platformUserId,
        text: event.text,
        chat_id: event.chatId,
        chat_name: event.chatName ?? null,
      },
      weixin_context_token: event.weixinContextToken ?? null,
    });
    await this.executeRegisteredInboundTurn(event, reresolved);
  }

  private async launchInboundTurn(
    event: InboundChannelEvent,
    runTurn: () => Promise<void>,
  ): Promise<void> {
    const turnPromise = (async () => {
      try {
        await runTurn();
      } catch {
        try {
          await this.recoverDetachedInboundTurn(event);
        } catch {
          // The inbound row keeps its last persisted state, so a later
          // recovery pass can query and replay with the same request key.
        }
      }
    })();

    this.activeInboundTurns.add(turnPromise);
    void turnPromise.finally(() => {
      this.activeInboundTurns.delete(turnPromise);
    });

    await new Promise<void>((resolve) => {
      const timer = setImmediate(resolve);
      void turnPromise.finally(() => {
        clearImmediate(timer);
        resolve();
      });
    });
  }

  private async executeRegisteredInboundTurn(
    event: InboundChannelEvent,
    resolved: I1RegisterAndResolveResponse,
  ): Promise<void> {
    // Step 1: Deduplication check on existing_status
    if (
      resolved.existing_status === "completed" ||
      resolved.existing_status === "failed"
    ) {
      if (
        resolved.fallback_pending &&
        resolved.thread_id &&
        resolved.dalton_user_id
      ) {
        await this.deliverReplayFallback({
          userId: resolved.dalton_user_id,
          threadId: resolved.thread_id,
          runId: resolved.run_id,
          turnIndex: resolved.turn_index,
          platform: event.platform,
          targetAddress: event.chatAddress,
          inboundMessageId: event.messageId,
          scopeKey: event.threadScopeKey,
        });
      }
      return;
    }

    if (
      resolved.existing_status === "dispatched" ||
      resolved.existing_status === "streaming" ||
      resolved.existing_status === "steered" ||
      resolved.existing_status === "pending_resubmit"
    ) {
      await this.recoverSingleInboundRow({
        platform: event.platform,
        message_id: event.messageId,
        platform_user_id: event.platformUserId,
        dalton_user_id: resolved.dalton_user_id,
        chat_type: event.chatType,
        chat_address: event.chatAddress,
        thread_scope_key: event.threadScopeKey,
        request_key: resolved.request_key,
        retry_request_key: resolved.retry_request_key,
        resubmitted_request_key: resolved.resubmitted_request_key,
        steered_into_run_id: resolved.steered_into_run_id,
        thread_id: resolved.thread_id,
        run_id: resolved.run_id,
        turn_index: resolved.turn_index,
        workspace_id: resolved.workspace_id,
        external_thread_id: resolved.external_thread_id,
        generation: resolved.generation,
        status: resolved.existing_status,
        fallback_pending: resolved.fallback_pending,
        sent_to_current_count: resolved.sent_to_current_count,
        uploaded_attachments: resolved.uploaded_attachments ?? [],
        raw_payload: resolved.raw_payload ?? {},
        pending_interrupt: resolved.pending_interrupt,
      });
      return;
    }

    // Step 2: Check link & permission status
    if (!resolved.linked || !resolved.allowed || !resolved.dalton_user_id) {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "failed",
      });
      const denyNotice = this.formatDenyNotice(
        event.platform,
        resolved.deny_reason ??
          (resolved.linked ? "workspace_write_denied" : "not_linked"),
      );
      await this.sendDirectAdapterNotice(event, denyNotice);
      return;
    }

    const userId = resolved.dalton_user_id;

    // Step 3: Upload attachments to workspace if present (§1.3.1)
    let uploadedAttachments: UploadedAttachmentRecord[] =
      resolved.uploaded_attachments ?? [];

    if (
      uploadedAttachments.length === 0 &&
      event.attachments &&
      event.attachments.length > 0
    ) {
      const uploadOutcome = await this.downloadAndUploadAttachments(
        event,
        resolved,
        userId,
      );
      if (!uploadOutcome.ok) {
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          status: "failed",
        });
        await this.sendNoticeDelivery({
          deliveryKey: `notice:${event.platform}:${event.messageId}:attachment_err`,
          userId,
          platform: event.platform,
          target: event.chatAddress,
          inboundMessageId: event.messageId,
          text: uploadOutcome.errorMessage,
          replyToMessageId: event.messageId,
          threadScopeKey: event.threadScopeKey,
        });
        return;
      }
      uploadedAttachments = uploadOutcome.uploaded;
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        uploaded_attachments: uploadedAttachments,
      });
    }

    const messageContent = this.buildMessageContent(
      event.text,
      uploadedAttachments,
    );

    // Step 4: Check HITL `pending_interrupt` (§1.3.1)
    if (resolved.pending_interrupt && resolved.pending_interrupt.thread_id) {
      const handledHitl = await this.handlePendingInterruptReply({
        event,
        userId,
        resolved: { ...resolved, uploaded_attachments: uploadedAttachments },
        messageContent,
        pendingInterrupt: resolved.pending_interrupt,
      });
      if (handledHitl) {
        return;
      }
    }

    // Step 5: Normal conversation dispatch
    const effectiveRequestKey =
      resolved.resubmitted_request_key ??
      resolved.retry_request_key ??
      resolved.request_key;

    await this.dispatchConversationTurn({
      event,
      userId,
      resolved: { ...resolved, uploaded_attachments: uploadedAttachments },
      messageContent,
      requestKey: effectiveRequestKey,
      steerOnly: false,
      retryAttempt: 0,
    });
  }

  // =========================================================================
  // §3.3 Slash Commands (`/bind`, `/unbind`, `/workspace`, `/new`, `/stop`)
  // =========================================================================

  private async tryHandleSlashCommand(
    event: InboundChannelEvent,
    trimmedText: string,
  ): Promise<boolean> {
    const match = /^\/(bind|unbind|workspace|new|stop)(?:\s+(.*))?$/i.exec(
      trimmedText,
    );
    if (!match) {
      return false;
    }
    const cmdName = match[1]!.toLowerCase();
    const arg = (match[2] ?? "").trim();

    if (cmdName === "bind") {
      if (!arg) {
        await this.sendDirectAdapterNotice(
          event,
          "Usage: `/bind <6-CHAR-CODE>` (generate a bind code in Web Settings → Channels).",
        );
        return true;
      }
      const res = await this.backend.consumeBindCode({
        action: "bind",
        platform: event.platform,
        platform_user_id: event.platformUserId,
        platform_username: event.platformUsername ?? null,
        platform_tenant_key: event.platformTenantKey ?? null,
        weixin_context_token: event.weixinContextToken ?? null,
        code: arg,
        is_group: event.chatType === "group",
      });
      if (!res.ok) {
        if (res.reason === "revoked_in_group") {
          await this.sendDirectAdapterNotice(
            event,
            "For security, `/bind` cannot be used in a group chat. This code has been revoked; please generate a new code and send `/bind <CODE>` in a direct message.",
          );
        } else if (res.reason === "platform_mismatch") {
          await this.sendDirectAdapterNotice(
            event,
            `This bind code was generated for ${res.expected_platform ?? "another platform"}, not ${event.platform}.`,
          );
        } else {
          await this.sendDirectAdapterNotice(
            event,
            "Bind code is invalid or expired. Please generate a new code in Web Settings → Channels.",
          );
        }
        return true;
      }
      await this.sendDirectAdapterNotice(
        event,
        `Successfully linked your ${event.platform} account to Dalton.`,
      );
      return true;
    }

    if (cmdName === "unbind") {
      const res = await this.backend.consumeBindCode({
        action: "unbind",
        platform: event.platform,
        platform_user_id: event.platformUserId,
        is_group: event.chatType === "group",
      });
      if (!res.ok) {
        await this.sendDirectAdapterNotice(
          event,
          `Your ${event.platform} account is not currently linked.`,
        );
        return true;
      }
      if (res.user_id) {
        this.closeWatchesForUserPlatform(res.user_id, event.platform);
      }
      await this.sendDirectAdapterNotice(
        event,
        `Unlinked your ${event.platform} account from Dalton.`,
      );
      return true;
    }

    // For `/workspace`, `/new`, `/stop`, resolve user identity via I-1
    const reqKey = makeInboundRequestKey(event.platform, event.messageId);
    const resolved = await this.backend.registerAndResolveInbound({
      platform: event.platform,
      platform_user_id: event.platformUserId,
      chat_type: event.chatType,
      chat_address: event.chatAddress,
      thread_scope_key: event.threadScopeKey,
      message_id: event.messageId,
      request_key: reqKey,
      raw_payload: { command: cmdName, arg },
      weixin_context_token: event.weixinContextToken ?? null,
    });

    if (!resolved.linked || !resolved.dalton_user_id) {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "failed",
      });
      await this.sendDirectAdapterNotice(
        event,
        this.formatDenyNotice(event.platform, "not_linked"),
      );
      return true;
    }

    const userId = resolved.dalton_user_id;

    if (cmdName === "workspace") {
      if (!arg) {
        const listRes = await this.backend.sessionCommand({
          cmd: "workspace_list",
          platform: event.platform,
          dalton_user_id: userId,
          chat_type: event.chatType,
          chat_address: event.chatAddress,
          chat_id: event.chatId,
          chat_name: event.chatName ?? null,
          scope_key: event.threadScopeKey,
        });
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          status: "completed",
        });
        if (!listRes.ok) {
          await this.sendDirectAdapterNotice(
            event,
            this.formatDenyNotice(
              event.platform,
              listRes.deny_reason ?? "workspace_write_denied",
            ),
          );
          return true;
        }
        const currentWs = listRes.current_workspace_id;
        const lines = [
          `Current workspace: ${currentWs ? `\`${currentWs}\`` : "Home (Chief of Staff)"}`,
          "Available workspaces:",
          "- `home` (Chief of Staff)",
          ...(listRes.workspaces ?? []).map(
            (w) =>
              `- \`${w.name}\` (\`${w.workspace_id}\`)${w.workspace_id === currentWs ? " [current]" : ""}`,
          ),
          "Use `/workspace <name|id|home>` to switch.",
        ];
        await this.sendDirectAdapterNotice(event, lines.join("\n"));
        return true;
      }

      const switchRes = await this.backend.sessionCommand({
        cmd: "workspace_switch",
        platform: event.platform,
        dalton_user_id: userId,
        chat_type: event.chatType,
        chat_address: event.chatAddress,
        chat_id: event.chatId,
        chat_name: event.chatName ?? null,
        scope_key: event.threadScopeKey,
        workspace_query: arg,
      });
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "completed",
      });
      if (!switchRes.ok) {
        await this.sendDirectAdapterNotice(
          event,
          this.formatDenyNotice(
            event.platform,
            switchRes.deny_reason ?? "workspace_not_found",
          ),
        );
        return true;
      }
      const targetLabel = switchRes.workspace_id
        ? `workspace \`${switchRes.workspace_id}\``
        : "Home (Chief of Staff)";
      await this.sendDirectAdapterNotice(
        event,
        `Switched this conversation to ${targetLabel} and started a new thread.`,
      );
      return true;
    }

    if (cmdName === "new") {
      const newRes = await this.backend.sessionCommand({
        cmd: "new",
        platform: event.platform,
        dalton_user_id: userId,
        chat_type: event.chatType,
        chat_address: event.chatAddress,
        chat_id: event.chatId,
        chat_name: event.chatName ?? null,
        scope_key: event.threadScopeKey,
      });
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "completed",
      });
      if (!newRes.ok) {
        await this.sendDirectAdapterNotice(
          event,
          this.formatDenyNotice(
            event.platform,
            newRes.deny_reason ?? "external_member_group_forbidden",
          ),
        );
        return true;
      }
      await this.sendDirectAdapterNotice(
        event,
        "Started a new conversation thread.",
      );
      return true;
    }

    if (cmdName === "stop") {
      const stopRes = await this.backend.sessionCommand({
        cmd: "stop",
        platform: event.platform,
        dalton_user_id: userId,
        chat_type: event.chatType,
        chat_address: event.chatAddress,
        chat_id: event.chatId,
        chat_name: event.chatName ?? null,
        scope_key: event.threadScopeKey,
        external_thread_id: resolved.external_thread_id,
      });
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "completed",
      });
      if (!stopRes.ok) {
        await this.sendDirectAdapterNotice(
          event,
          this.formatDenyNotice(
            event.platform,
            stopRes.deny_reason ?? "external_member_group_forbidden",
          ),
        );
        return true;
      }
      if (!stopRes.thread_id) {
        await this.sendDirectAdapterNotice(
          event,
          "No active conversation thread to stop.",
        );
        return true;
      }
      await this.backend.cancelThreadTurn(
        userId,
        stopRes.thread_id,
        stopRes.run_id,
      );
      await this.sendDirectAdapterNotice(event, "Stopped the current run.");
      return true;
    }

    return false;
  }

  // =========================================================================
  // §1.3.1 Workspace Readiness & Attachment Upload
  // =========================================================================

  private async downloadAndUploadAttachments(
    event: InboundChannelEvent,
    resolved: I1RegisterAndResolveResponse,
    userId: string,
  ): Promise<
    | { ok: true; uploaded: UploadedAttachmentRecord[] }
    | { ok: false; errorMessage: string }
  > {
    const descriptors = event.attachments ?? [];
    if (descriptors.length > MAX_ATTACHMENT_COUNT) {
      return {
        ok: false,
        errorMessage: `Too many attachments (${descriptors.length}); maximum is ${MAX_ATTACHMENT_COUNT} files per message.`,
      };
    }

    const adapter = this.adapters.get(event.platform);
    if (!adapter) {
      return {
        ok: false,
        errorMessage: `Channel adapter for ${event.platform} is unavailable.`,
      };
    }

    // Ensure workspace is initialized and running before uploading files (§1.3.1)
    let targetWorkspaceId = resolved.workspace_id;
    let workspaceStatus = resolved.workspace_status ?? null;
    const workspaceOwnerId = resolved.workspace_owner_id ?? userId;

    if (workspaceOwnerId !== userId) {
      // Colleague's workspace: must already be running; never start someone else's computer
      if (workspaceStatus !== "running" || !targetWorkspaceId) {
        return {
          ok: false,
          errorMessage:
            "This shared colleague workspace is currently stopped. Please ask the workspace owner to start it (or start it from the web UI) before uploading attachments.",
        };
      }
    } else {
      // Own workspace or Home workspace (`workspace_id == null` or lazily uninitialized `workspace_status == null`)
      if (!targetWorkspaceId || workspaceStatus === null) {
        const home = await this.backend.ensureHomeWorkspace(userId);
        if (!targetWorkspaceId) {
          targetWorkspaceId = home.workspace_id;
        }
        workspaceStatus = home.status;
      }
      if (workspaceStatus !== "running") {
        try {
          await this.backend.startWorkspace(userId, targetWorkspaceId);
        } catch (err) {
          if (err instanceof BackendHttpError && err.status === 404) {
            const home = await this.backend.ensureHomeWorkspace(userId);
            targetWorkspaceId = home.workspace_id;
            if (home.status !== "running") {
              await this.backend.startWorkspace(userId, targetWorkspaceId);
            }
          } else {
            throw err;
          }
        }
      }
    }

    const uploaded: UploadedAttachmentRecord[] = [];
    let totalBytes = 0;
    const safeMsgId =
      event.messageId.replace(/[^a-zA-Z0-9._-]/g, "_") || "message";

    for (const desc of descriptors) {
      if (desc.sizeBytes !== undefined && desc.sizeBytes > MAX_ATTACHMENT_BYTES) {
        return {
          ok: false,
          errorMessage: `Attachment "${desc.filename}" exceeds the 20 MB per-file limit.`,
        };
      }

      const downloaded = await adapter.downloadAttachment(desc, event);
      const byteLength = downloaded.content.byteLength;
      if (byteLength > MAX_ATTACHMENT_BYTES) {
        return {
          ok: false,
          errorMessage: `Attachment "${downloaded.filename}" exceeds the 20 MB per-file limit.`,
        };
      }
      if (totalBytes + byteLength > MAX_TOTAL_ATTACHMENT_BYTES) {
        return {
          ok: false,
          errorMessage:
            "Total attachment size exceeds the 50 MB per-message limit.",
        };
      }
      totalBytes += byteLength;

      const safeFilename = this.sanitizeFilename(
        downloaded.filename || desc.filename || "attachment",
      );
      // Upload to non-restricted `uploads/...` path (`_is_non_owner_blocked_path` blocks `.agents/` for non-owners)
      const workspacePath = `uploads/${event.platform}/${safeMsgId}/${safeFilename}`;

      await this.backend.uploadWorkspaceFile(
        userId,
        targetWorkspaceId,
        workspacePath,
        safeFilename,
        downloaded.content,
        downloaded.mimeType || desc.mimeType || "application/octet-stream",
      );

      const mimeType = (
        downloaded.mimeType ||
        desc.mimeType ||
        "application/octet-stream"
      ).toLowerCase();

      let dataUrl: string | undefined;
      if (
        INLINE_IMAGE_MIME_TYPES.has(mimeType) &&
        byteLength <= MAX_INLINE_IMAGE_BYTES
      ) {
        dataUrl = `data:${mimeType};base64,${downloaded.content.toString("base64")}`;
      }

      uploaded.push({
        filename: safeFilename,
        workspace_path: workspacePath,
        mime_type: mimeType,
        size_bytes: byteLength,
        ...(dataUrl ? { data_url: dataUrl } : {}),
      });
    }

    return { ok: true, uploaded };
  }

  private buildMessageContent(
    rawText: string,
    uploadedAttachments: UploadedAttachmentRecord[],
  ): string | ChatContentItem[] {
    if (!uploadedAttachments || uploadedAttachments.length === 0) {
      return rawText;
    }

    const imageItems: ChatContentItem[] = [];
    const pathLines: string[] = [];

    for (const att of uploadedAttachments) {
      if (att.data_url) {
        // Real backend `ContentItem` (`src/server/models/chat.py:282-314`): `{ type: "image", image_url: "<string>" }`
        imageItems.push({
          type: "image",
          image_url: att.data_url,
        });
      }
      pathLines.push(`[Attached file in workspace: ${att.workspace_path}]`);
    }

    const combinedText = [rawText.trim(), ...pathLines]
      .filter((s) => s.length > 0)
      .join("\n\n");

    return [
      {
        type: "text",
        text: combinedText,
      },
      ...imageItems,
    ];
  }

  // =========================================================================
  // §1.3.1 Three HITL `pending_interrupt` Categories
  // =========================================================================

  private async handlePendingInterruptReply(params: {
    event: InboundChannelEvent;
    userId: string;
    resolved: I1RegisterAndResolveResponse;
    messageContent: string | ChatContentItem[];
    pendingInterrupt: PendingInterruptState;
  }): Promise<boolean> {
    const { event, userId, resolved, pendingInterrupt } = params;
    const threadId = pendingInterrupt.thread_id;
    const interruptId = pendingInterrupt.interrupt_id;
    const trimmed = (event.text ?? "").trim();
    const kind =
      pendingInterrupt.kind ??
      pendingInterrupt.category ??
      "ask_user_question";

    // Before answering an interrupt, check live thread status (`GET /api/v1/threads/{id}/status`).
    // If the interrupt was already resolved on the web UI (`status !== "interrupted"`), clear
    // `pending_interrupt` and fall through to normal new-turn dispatch.
    try {
      const liveStatus = await this.backend.getThreadStatus(userId, threadId);
      if (liveStatus.status && liveStatus.status !== "interrupted") {
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          session_interrupt_update: {
            platform: event.platform,
            scope_key: event.threadScopeKey,
            dalton_user_id: userId,
            pending_interrupt: null,
          },
        });
        return false;
      }
    } catch {
      // If status check fails transiently, continue with pendingInterrupt handling
    }

    // Category 2: `web_only_approval` — never approve from chat; direct user to web UI (`web/src/Main.tsx:145-146`)
    if (kind === "web_only_approval") {
      const threadUrl = `${this.webBaseUrl}/chat/t/${threadId}`;
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "completed",
      });
      await this.sendNoticeDelivery({
        deliveryKey: `notice:${event.platform}:${event.messageId}:web_approval`,
        userId,
        threadId,
        runId: pendingInterrupt.run_id,
        platform: event.platform,
        target: event.chatAddress,
        inboundMessageId: event.messageId,
        text: `This action (${pendingInterrupt.action_type ?? "approval"}) requires confirmation in the web app: ${threadUrl}`,
        replyToMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
      });
      return true;
    }

    // Category 1: `ask_user_question` — resume via `POST /api/v1/threads/{id}/messages` with `messages: []` + `hitl_response`
    // (`src/ptc_agent/agent/middleware/ask_user.py:56-80`: `skip` sends `type: "reject"`, normal answer sends `type: "approve"`)
    if (kind === "ask_user_question") {
      const normalizedQuestions = this.extractQuestionsFromInterrupt(pendingInterrupt);
      const isSkip = /^(skip|跳过)$/i.test(trimmed);
      const decision = isSkip
        ? {
            type: "reject",
            message: "",
          }
        : {
            type: "approve",
            message: this.resolveAskUserQuestionAnswerText(
              trimmed,
              normalizedQuestions,
            ),
          };
      const hitlResponse = {
        [interruptId]: {
          decisions: [decision],
        },
      };
      await this.dispatchHitlResumeTurn({
        event,
        userId,
        resolved,
        threadId,
        requestKey: resolved.retry_request_key ?? resolved.request_key,
        hitlResponse,
        retryAttempt: 0,
      });
      return true;
    }

    // Category 3: `credit_pause`
    if (kind === "credit_pause") {
      const isBareContinue =
        /^(continue|resume|retry|ok|继续|恢复|好)$/i.test(trimmed) &&
        (resolved.uploaded_attachments ?? []).length === 0;

      const hitlApprove = {
        [interruptId]: {
          decisions: [
            {
              type: "approve",
              message: trimmed || "continue",
            },
          ],
        },
      };

      if (isBareContinue) {
        await this.dispatchHitlResumeTurn({
          event,
          userId,
          resolved,
          threadId,
          requestKey: resolved.retry_request_key ?? resolved.request_key,
          hitlResponse: hitlApprove,
          retryAttempt: 0,
        });
        return true;
      }

      // Supplementary instructions during `credit_pause`:
      // ① Send HITL resume via `POST /api/v1/threads/{id}/messages` (`messages: []` + `hitl_response`) to wake the thread.
      // ② Once `event: metadata` / `turn_start` provides the active `new_run_id`, send supplementary text via `steer_only: true` (`...:credit:{new_run_id}`).
      // ③ After the resume SSE stream finishes and settles main turn text, reconcile the supplementary steer via `I-7 steer/reconcile` (and resubmit as a new turn if unconsumed).
      const supplementText = this.extractCreditSupplementText(trimmed);
      const supplementContent = this.buildMessageContent(
        supplementText,
        resolved.uploaded_attachments ?? [],
      );

      await this.dispatchCreditPauseResumeWithSupplement({
        event,
        userId,
        resolved,
        threadId,
        interruptRunId: pendingInterrupt.run_id,
        requestKey: resolved.retry_request_key ?? resolved.request_key,
        hitlResponse: hitlApprove,
        supplementText,
        supplementContent,
        retryAttempt: 0,
      });
      return true;
    }

    return false;
  }

  private extractQuestionsFromInterrupt(pendingInterrupt: {
    questions?: AskUserQuestionItem[];
    action_requests?: InterruptActionRequest[];
  }): AskUserQuestionItem[] {
    if (
      Array.isArray(pendingInterrupt.questions) &&
      pendingInterrupt.questions.length > 0
    ) {
      return pendingInterrupt.questions.map((q, idx) => ({
        id: q.id || `q_${idx + 1}`,
        question: q.question,
        allow_multiple: Boolean(q.allow_multiple),
        options: (q.options ?? []).map((opt, oIdx) =>
          typeof opt === "string"
            ? { id: String(oIdx + 1), label: opt }
            : { id: opt.id ?? String(oIdx + 1), label: String(opt.label ?? opt) },
        ),
      }));
    }
    const fromActions: AskUserQuestionItem[] = [];
    for (const [idx, req] of (pendingInterrupt.action_requests ?? []).entries()) {
      if (typeof req.question === "string" && req.question.length > 0) {
        const rawOpts = Array.isArray(req.options) ? req.options : [];
        fromActions.push({
          id: typeof req.id === "string" ? req.id : `q_${idx + 1}`,
          question: req.question,
          allow_multiple: Boolean(req.allow_multiple),
          options: rawOpts.map((opt, oIdx) =>
            typeof opt === "string"
              ? { id: String(oIdx + 1), label: opt }
              : {
                  id: opt.id ?? String(oIdx + 1),
                  label: String(opt.label ?? opt),
                },
          ),
        });
      }
    }
    return fromActions;
  }

  private resolveAskUserQuestionAnswerText(
    trimmed: string,
    questions: AskUserQuestionItem[],
  ): string {
    if (questions.length === 1) {
      const q = questions[0]!;
      const opts = q.options ?? [];
      const num = Number.parseInt(trimmed, 10);
      if (
        Number.isInteger(num) &&
        num >= 1 &&
        num <= opts.length &&
        String(num) === trimmed
      ) {
        const chosen = opts[num - 1]!;
        return chosen.label;
      }
      if (q.allow_multiple && /^[\d,\s]+$/.test(trimmed)) {
        const nums = trimmed
          .split(/[\s,]+/)
          .filter((s) => s.length > 0)
          .map((s) => Number.parseInt(s, 10));
        if (
          nums.length > 0 &&
          nums.every((n) => Number.isInteger(n) && n >= 1 && n <= opts.length)
        ) {
          return nums.map((n) => opts[n - 1]!.label).join(", ");
        }
      }
    }
    return trimmed;
  }

  private extractCreditSupplementText(trimmed: string): string {
    const match =
      /^(?:continue|resume|retry|ok|继续|恢复|好)[,，:：;；\s]+(.+)$/is.exec(
        trimmed,
      );
    if (match && match[1]) {
      return match[1].trim();
    }
    return trimmed;
  }

  private async dispatchHitlResumeTurn(params: {
    event: InboundChannelEvent;
    userId: string;
    resolved: I1RegisterAndResolveResponse;
    threadId: string;
    requestKey: string;
    hitlResponse: Record<string, { decisions: Array<Record<string, unknown>> }>;
    retryAttempt: number;
    busyWaitElapsedMs?: number;
    busyWaitStartedAtMs?: number;
  }): Promise<void> {
    const {
      event,
      userId,
      resolved,
      threadId,
      requestKey,
      hitlResponse,
      retryAttempt,
      busyWaitElapsedMs = 0,
      busyWaitStartedAtMs,
    } = params;

    const payload: ThreadMessageDispatchPayload = {
      messages: [],
      request_key: requestKey,
      workspace_id: resolved.workspace_id,
      platform: event.platform,
      turn_platform: event.platform,
      surface_rules: buildSurfaceRules(event.platform),
      steer_only: false,
      hitl_response: hitlResponse,
    };

    const resp = await this.backend.appendThreadMessages(
      userId,
      threadId,
      payload,
    );

    if (resp.isSse && resp.events) {
      // Do NOT clear `pending_interrupt` before `consumeTurnSseStream` confirms the turn was accepted
      // without an immediate SSE `event: error` (e.g. `writer_capacity` or `stopping`).
      await this.consumeTurnSseStream({
        events: resp.events,
        event,
        userId,
        resolved,
        threadId,
        requestKey,
        retryAttempt,
        busyWaitElapsedMs,
        busyWaitStartedAtMs,
        hitlResponse,
        clearPendingInterruptOnAccept: true,
      });
      return;
    }

    await this.handleDispatchConflict({
      status: resp.status,
      errorBody: resp.errorBody ?? {},
      event,
      userId,
      resolved,
      messageContent: [],
      threadId,
      requestKey,
      steerOnly: false,
      retryAttempt,
      busyWaitElapsedMs,
      busyWaitStartedAtMs,
      isRetryEndpoint: false,
      isHitlResume: true,
      hitlResponse,
    });
  }

  private async dispatchCreditPauseResumeWithSupplement(params: {
    event: InboundChannelEvent;
    userId: string;
    resolved: I1RegisterAndResolveResponse;
    threadId: string;
    interruptRunId: string;
    requestKey: string;
    hitlResponse: Record<string, { decisions: Array<Record<string, unknown>> }>;
    supplementText: string;
    supplementContent: string | ChatContentItem[];
    retryAttempt?: number;
    busyWaitElapsedMs?: number;
    busyWaitStartedAtMs?: number;
  }): Promise<void> {
    const {
      event,
      userId,
      resolved,
      threadId,
      interruptRunId,
      requestKey,
      hitlResponse,
      supplementText,
      supplementContent,
      retryAttempt = 0,
      busyWaitElapsedMs = 0,
      busyWaitStartedAtMs,
    } = params;

    await this.backend.updateInboundProgress({
      platform: event.platform,
      message_id: event.messageId,
      raw_payload_patch: {
        credit_resume_dispatched: true,
        credit_interrupt_run_id: interruptRunId,
        credit_supplement_text: supplementText,
      },
    });

    // Step ①: Resume the credit-paused turn via `POST /api/v1/threads/{id}/messages` (`messages: []` + `hitl_response`)
    const resumePayload: ThreadMessageDispatchPayload = {
      messages: [],
      request_key: requestKey,
      workspace_id: resolved.workspace_id,
      platform: event.platform,
      turn_platform: event.platform,
      surface_rules: buildSurfaceRules(event.platform),
      steer_only: false,
      hitl_response: hitlResponse,
    };

    const resp = await this.backend.appendThreadMessages(
      userId,
      threadId,
      resumePayload,
    );

    if (!resp.isSse || !resp.events) {
      await this.handleDispatchConflict({
        status: resp.status,
        errorBody: resp.errorBody ?? {},
        event,
        userId,
        resolved,
        messageContent: supplementContent,
        threadId,
        requestKey,
        steerOnly: false,
        retryAttempt,
        busyWaitElapsedMs,
        busyWaitStartedAtMs,
        isRetryEndpoint: false,
        isHitlResume: true,
        hitlResponse,
        creditSupplementContext: {
          interruptRunId,
          supplementText,
          supplementContent,
        },
      });
      return;
    }

    let activeResumeRunId = interruptRunId;
    let steerSent = false;
    let steerHitNotRunning = false;

    const sendSupplementSteer = async (targetRunId: string): Promise<void> => {
      if (steerSent) return;
      steerSent = true;
      activeResumeRunId = targetRunId;
      const steerKey = makeCreditSupplementSteerKey(
        event.platform,
        event.messageId,
        targetRunId,
      );
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        steered_into_run_id: targetRunId,
        raw_payload_patch: {
          credit_steer_queued: true,
          credit_steer_request_key: steerKey,
          credit_steer_run_id: targetRunId,
          credit_steer_state: "pending",
          credit_turn_text_settled: false,
          credit_supplement_text: supplementText,
        },
      });

      try {
        const steerResp = await this.backend.appendThreadMessages(
          userId,
          threadId,
          {
            messages: [{ role: "user", content: supplementContent }],
            request_key: steerKey,
            workspace_id: resolved.workspace_id,
            platform: event.platform,
            turn_platform: event.platform,
            surface_rules: buildSurfaceRules(event.platform),
            steer_only: true,
          },
        );
        if (steerResp.isSse && steerResp.events) {
          for await (const _ev of steerResp.events) {
            // Drain steering_accepted SSE stream
          }
        } else if (steerResp.status === 409) {
          const detail = this.parseConflictDetail(steerResp.errorBody);
          if (detail.reason === "not_running" || detail.code === "not_running") {
            steerHitNotRunning = true;
          }
        }
      } catch {
        // Network error during steer: Step ③ will reconcile via I-7 `steer/reconcile`
      }
    };

    // Step ②: Consume the resumed turn's SSE stream, firing `sendSupplementSteer` as soon as `event: metadata` / `turn_start` arrives
    const streamAccepted = await this.consumeTurnSseStream({
      events: resp.events,
      event,
      userId,
      resolved,
      threadId,
      requestKey,
      retryAttempt,
      busyWaitElapsedMs,
      busyWaitStartedAtMs,
      hitlResponse,
      clearPendingInterruptOnAccept: true,
      creditSupplementContext: {
        interruptRunId,
        supplementText,
        supplementContent,
      },
      deferFinalCompletion: true,
      onRunStarted: async (newRunId) => {
        await sendSupplementSteer(newRunId || interruptRunId);
      },
    });

    if (!streamAccepted) {
      return;
    }

    if (!steerSent) {
      await sendSupplementSteer(activeResumeRunId);
    }

    // Mark main turn text settled before reconciling the supplementary steer
    await this.backend.updateInboundProgress({
      platform: event.platform,
      message_id: event.messageId,
      raw_payload_patch: {
        credit_turn_text_settled: true,
      },
    });

    // Step ③: Reconcile whether the supplementary steer was absorbed by the resumed turn
    await this.reconcileCreditSupplementSteer({
      event,
      userId,
      resolved,
      threadId,
      runId: activeResumeRunId,
      supplementContent,
      steerHitNotRunning,
    });
  }

  private async reconcileCreditSupplementSteer(params: {
    event: InboundChannelEvent;
    userId: string;
    resolved: I1RegisterAndResolveResponse;
    threadId: string;
    runId: string;
    supplementContent: string | ChatContentItem[];
    steerHitNotRunning?: boolean;
  }): Promise<void> {
    const {
      event,
      userId,
      resolved,
      threadId,
      runId,
      supplementContent,
      steerHitNotRunning,
    } = params;

    if (steerHitNotRunning) {
      const turnKey = makeTurnAfterSteerRequestKey(
        event.platform,
        event.messageId,
        1,
      );
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        resubmitted_request_key: turnKey,
        raw_payload_patch: {
          credit_steer_state: "resubmitted",
        },
      });
      await this.dispatchConversationTurn({
        event,
        userId,
        resolved: {
          ...resolved,
          thread_id: threadId,
          resubmitted_request_key: turnKey,
        },
        messageContent: supplementContent,
        requestKey: turnKey,
        steerOnly: false,
        retryAttempt: 1,
        explicitThreadId: threadId,
      });
      return;
    }

    const safeReconcileCreditSteer = async () => {
      try {
        return await this.backend.reconcileSteer({
          platform: event.platform,
          message_id: event.messageId,
          run_id: runId,
        });
      } catch {
        return {
          ok: true,
          reconcile_status: "still_pending" as const,
          outcome: null,
          run_status: "in_progress",
          inbound_status: "steered",
          resubmitted_request_key: null,
        };
      }
    };

    let rec = await safeReconcileCreditSteer();

    // While `reconcile_status === "still_pending"`, keep polling `reconcileSteer` with backoff
    // up to the real wall-clock `busyThreadMaxWaitMs` deadline until the host run settles the steer!
    const reconcileStartedAtMs = this.nowFn();
    let accumulatedSleepMs = 0;
    let pollIdx = 0;

    while (
      rec.reconcile_status === "still_pending" &&
      !this.shuttingDown &&
      Math.max(this.nowFn() - reconcileStartedAtMs, accumulatedSleepMs) <
        this.busyThreadMaxWaitMs
    ) {
      const delayMs =
        BACKOFF_DELAYS_MS[Math.min(pollIdx, BACKOFF_DELAYS_MS.length - 1)]!;
      pollIdx++;
      await this.backoffDelayFn(delayMs);
      accumulatedSleepMs += delayMs;
      if (this.shuttingDown) {
        return;
      }
      try {
        const liveness = await this.backend.getDispatchesLiveness(userId, [
          runId,
        ]);
        const liveState = liveness[runId]?.status;
        if (liveState && !PUBLIC_LIVE_STATUSES.has(liveState)) {
          rec = await safeReconcileCreditSteer();
          if (rec.reconcile_status !== "still_pending") {
            break;
          }
        }
      } catch {
        // Ignore transient liveness poll errors
      }
      rec = await safeReconcileCreditSteer();
    }

    if (rec.reconcile_status === "consumed") {
      let replaySettled = true;
      if (
        rec.outcome &&
        INTERNAL_REPLAY_NEEDED_OUTCOMES.has(rec.outcome)
      ) {
        replaySettled = await this.deliverReplayFallback({
          userId,
          threadId,
          runId,
          turnIndex: resolved.turn_index,
          platform: event.platform,
          targetAddress: event.chatAddress,
          inboundMessageId: event.messageId,
          scopeKey: event.threadScopeKey,
        });
      }
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "completed",
        fallback_pending: !replaySettled,
        raw_payload_patch: {
          credit_steer_state: "consumed",
        },
      });
      return;
    }

    // Only resubmit with `:returned:{run_id}` when backend `I-7` confirms `pending_resubmit`
    // (or `not_found` when steer never reached `conversation_turn_steers`); never on `still_pending`!
    if (
      rec.reconcile_status === "pending_resubmit" ||
      rec.reconcile_status === "not_found"
    ) {
      const resubmitKey =
        rec.resubmitted_request_key ??
        makeReturnedSteerRequestKey(event.platform, event.messageId, runId);
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        resubmitted_request_key: resubmitKey,
        raw_payload_patch: {
          credit_steer_state: "resubmitted",
        },
      });
      await this.dispatchConversationTurn({
        event,
        userId,
        resolved: {
          ...resolved,
          thread_id: threadId,
          resubmitted_request_key: resubmitKey,
        },
        messageContent: supplementContent,
        requestKey: resubmitKey,
        steerOnly: false,
        retryAttempt: 1,
        explicitThreadId: threadId,
      });
      return;
    }

    if (rec.reconcile_status === "still_pending") {
      if (this.shuttingDown) {
        return;
      }
      // Keep inbound row in `steered` state in the backend recovery set (`channel_gateway.py:3448-3572, 3733`)
      // so startup recovery and subsequent reconciliation continue to settle it once the host run finishes,
      // and send a one-time notice per inbound message instead of telling the user to resend (Grok r5 #4).
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "steered",
        steered_into_run_id: runId,
        fallback_pending: false,
        raw_payload_patch: {
          credit_steer_state: "steered",
        },
      });
      await this.sendNoticeDelivery({
        deliveryKey: `notice:${event.platform}:${event.messageId}:steer_queued`,
        userId,
        threadId,
        runId,
        platform: event.platform,
        target: event.chatAddress,
        inboundMessageId: event.messageId,
        text: "这条补充已排队，宿主回合结束后会处理。",
        replyToMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
      });
      return;
    }

    if (rec.reconcile_status === "returned_unresubmitted") {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "failed",
        fallback_pending: false,
        raw_payload_patch: {
          credit_steer_state: "failed",
        },
      });
      await this.sendNoticeDelivery({
        deliveryKey: `notice:${event.platform}:${event.messageId}:credit_returned_steer`,
        userId,
        threadId,
        runId,
        platform: event.platform,
        target: event.chatAddress,
        inboundMessageId: event.messageId,
        text: "The resumed run ended with an error before your supplementary instructions could be processed. Please send your message again if needed.",
        replyToMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
      });
    }
  }

  private async dispatchCancelledRunRetry(params: {
    event: InboundChannelEvent;
    userId: string;
    resolved: I1RegisterAndResolveResponse;
    threadId: string;
    failedRunId: string | null;
    requestKey: string;
    retryAttempt: number;
    busyWaitElapsedMs?: number;
    busyWaitStartedAtMs?: number;
    messageContent?: string | ChatContentItem[];
    isHitlResume?: boolean;
    hitlResponse?: Record<string, { decisions: Array<Record<string, unknown>> }>;
    creditSupplementContext?: {
      interruptRunId: string;
      supplementText: string;
      supplementContent: string | ChatContentItem[];
    };
  }): Promise<void> {
    const {
      event,
      userId,
      resolved,
      threadId,
      failedRunId,
      requestKey,
      retryAttempt,
      busyWaitElapsedMs = 0,
      busyWaitStartedAtMs,
      messageContent,
      isHitlResume,
      hitlResponse,
      creditSupplementContext,
    } = params;

    let workspaceId = resolved.workspace_id;
    if (!workspaceId) {
      const home = await this.backend.ensureHomeWorkspace(userId);
      workspaceId = home.workspace_id;
    }

    const resp = await this.backend.retryThreadTurn(userId, threadId, {
      workspace_id: workspaceId,
      run_id: failedRunId,
      request_key: requestKey,
      platform: event.platform,
      turn_platform: event.platform,
      surface_rules: buildSurfaceRules(event.platform),
    });

    if (resp.isSse && resp.events) {
      await this.consumeTurnSseStream({
        events: resp.events,
        event,
        userId,
        resolved,
        threadId,
        requestKey,
        retryAttempt,
        busyWaitElapsedMs,
        busyWaitStartedAtMs,
        isRetryEndpoint: true,
        failedRunId,
        messageContent,
        hitlResponse,
        clearPendingInterruptOnAccept: Boolean(isHitlResume || hitlResponse),
        creditSupplementContext,
      });
      return;
    }

    await this.handleDispatchConflict({
      status: resp.status,
      errorBody: resp.errorBody ?? {},
      event,
      userId,
      resolved,
      messageContent: messageContent ?? event.text,
      threadId,
      requestKey,
      steerOnly: false,
      retryAttempt,
      busyWaitElapsedMs,
      busyWaitStartedAtMs,
      isRetryEndpoint: true,
      failedRunId,
      isHitlResume,
      hitlResponse,
      creditSupplementContext,
    });
  }

  // =========================================================================
  // §1.3.1 & §1.3.2 Conversation Dispatch & Full Conflict Matrix
  // =========================================================================

  private async dispatchConversationTurn(params: {
    event: InboundChannelEvent;
    userId: string;
    resolved: I1RegisterAndResolveResponse;
    messageContent: string | ChatContentItem[];
    requestKey: string;
    steerOnly: boolean;
    retryAttempt: number;
    busyWaitElapsedMs?: number;
    busyWaitStartedAtMs?: number;
    explicitThreadId?: string | null;
  }): Promise<void> {
    const {
      event,
      userId,
      resolved,
      messageContent,
      requestKey,
      steerOnly,
      retryAttempt,
      busyWaitElapsedMs = 0,
      busyWaitStartedAtMs,
    } = params;

    const targetThreadId =
      params.explicitThreadId !== undefined
        ? params.explicitThreadId
        : resolved.thread_id;

    const payload: ThreadMessageDispatchPayload = {
      messages: Array.isArray(messageContent) && messageContent.length === 0
        ? []
        : [{ role: "user", content: messageContent }],
      request_key: requestKey,
      workspace_id: resolved.workspace_id,
      external_thread_id: targetThreadId ? undefined : resolved.external_thread_id,
      platform: event.platform,
      turn_platform: event.platform,
      surface_rules: buildSurfaceRules(event.platform),
      ...(steerOnly ? { steer_only: true } : {}),
    };

    const resp = targetThreadId
      ? await this.backend.appendThreadMessages(userId, targetThreadId, payload)
      : await this.backend.createThreadMessages(userId, payload);

    if (resp.isSse && resp.events) {
      await this.consumeTurnSseStream({
        events: resp.events,
        event,
        userId,
        resolved,
        threadId: targetThreadId,
        requestKey,
        retryAttempt,
        busyWaitElapsedMs,
        busyWaitStartedAtMs,
        steerOnly,
        messageContent,
      });
      return;
    }

    await this.handleDispatchConflict({
      status: resp.status,
      errorBody: resp.errorBody ?? {},
      event,
      userId,
      resolved,
      messageContent,
      threadId: targetThreadId,
      requestKey,
      steerOnly,
      retryAttempt,
      busyWaitElapsedMs,
      busyWaitStartedAtMs,
      isRetryEndpoint: false,
    });
  }

  private async handleDispatchConflict(params: {
    status: number;
    errorBody: Record<string, unknown>;
    event: InboundChannelEvent;
    userId: string;
    resolved: I1RegisterAndResolveResponse;
    messageContent: string | ChatContentItem[];
    threadId: string | null;
    requestKey: string;
    steerOnly: boolean;
    retryAttempt: number;
    busyWaitElapsedMs?: number;
    busyWaitStartedAtMs?: number;
    isRetryEndpoint: boolean;
    failedRunId?: string | null;
    isHitlResume?: boolean;
    hitlResponse?: Record<string, { decisions: Array<Record<string, unknown>> }>;
    creditSupplementContext?: {
      interruptRunId: string;
      supplementText: string;
      supplementContent: string | ChatContentItem[];
    };
  }): Promise<void> {
    const {
      status,
      errorBody,
      event,
      userId,
      resolved,
      messageContent,
      threadId,
      requestKey,
      steerOnly,
      retryAttempt,
      busyWaitElapsedMs = 0,
      busyWaitStartedAtMs,
      isRetryEndpoint,
      failedRunId,
      isHitlResume,
      hitlResponse,
      creditSupplementContext,
    } = params;

    const detail = this.parseConflictDetail(errorBody);
    const conflictThreadId = detail.thread_id ?? threadId;
    const conflictRunId = detail.run_id ?? failedRunId ?? resolved.run_id;

    const dispatchResendWithNewKey = async (
      nextAttempt: number,
      newKey: string,
      nextBusyWaitElapsedMs = busyWaitElapsedMs,
      nextBusyWaitStartedAtMs = busyWaitStartedAtMs,
    ): Promise<void> => {
      const updatedResolved: I1RegisterAndResolveResponse = {
        ...resolved,
        thread_id: conflictThreadId ?? resolved.thread_id,
        run_id: conflictRunId ?? resolved.run_id,
        retry_request_key: newKey,
      };
      if (creditSupplementContext && conflictThreadId && hitlResponse) {
        await this.dispatchCreditPauseResumeWithSupplement({
          event,
          userId,
          resolved: updatedResolved,
          threadId: conflictThreadId,
          interruptRunId: creditSupplementContext.interruptRunId,
          requestKey: newKey,
          hitlResponse,
          supplementText: creditSupplementContext.supplementText,
          supplementContent: creditSupplementContext.supplementContent,
          retryAttempt: nextAttempt,
          busyWaitElapsedMs: nextBusyWaitElapsedMs,
          busyWaitStartedAtMs: nextBusyWaitStartedAtMs,
        });
      } else if (isHitlResume && conflictThreadId && hitlResponse) {
        await this.dispatchHitlResumeTurn({
          event,
          userId,
          resolved: updatedResolved,
          threadId: conflictThreadId,
          requestKey: newKey,
          hitlResponse,
          retryAttempt: nextAttempt,
          busyWaitElapsedMs: nextBusyWaitElapsedMs,
          busyWaitStartedAtMs: nextBusyWaitStartedAtMs,
        });
      } else {
        await this.dispatchConversationTurn({
          event,
          userId,
          resolved: updatedResolved,
          messageContent,
          requestKey: newKey,
          steerOnly: false,
          retryAttempt: nextAttempt,
          busyWaitElapsedMs: nextBusyWaitElapsedMs,
          busyWaitStartedAtMs: nextBusyWaitStartedAtMs,
          explicitThreadId: conflictThreadId,
        });
      }
    };

    const dispatchRetryWithSameKey = async (
      nextAttempt: number,
      nextBusyWaitElapsedMs = busyWaitElapsedMs,
      nextBusyWaitStartedAtMs = busyWaitStartedAtMs,
    ): Promise<void> => {
      if (creditSupplementContext && conflictThreadId && hitlResponse) {
        await this.dispatchCreditPauseResumeWithSupplement({
          event,
          userId,
          resolved,
          threadId: conflictThreadId,
          interruptRunId: creditSupplementContext.interruptRunId,
          requestKey,
          hitlResponse,
          supplementText: creditSupplementContext.supplementText,
          supplementContent: creditSupplementContext.supplementContent,
          retryAttempt: nextAttempt,
          busyWaitElapsedMs: nextBusyWaitElapsedMs,
          busyWaitStartedAtMs: nextBusyWaitStartedAtMs,
        });
      } else if (isHitlResume && conflictThreadId && hitlResponse) {
        await this.dispatchHitlResumeTurn({
          event,
          userId,
          resolved,
          threadId: conflictThreadId,
          requestKey,
          hitlResponse,
          retryAttempt: nextAttempt,
          busyWaitElapsedMs: nextBusyWaitElapsedMs,
          busyWaitStartedAtMs: nextBusyWaitStartedAtMs,
        });
      } else if (isRetryEndpoint && conflictThreadId) {
        const preservedFailedRunId = failedRunId ?? conflictRunId;
        await this.dispatchCancelledRunRetry({
          event,
          userId,
          resolved: {
            ...resolved,
            run_id: preservedFailedRunId,
          },
          threadId: conflictThreadId,
          failedRunId: preservedFailedRunId,
          requestKey,
          retryAttempt: nextAttempt,
          busyWaitElapsedMs: nextBusyWaitElapsedMs,
          busyWaitStartedAtMs: nextBusyWaitStartedAtMs,
          messageContent,
          isHitlResume,
          hitlResponse,
          creditSupplementContext,
        });
      } else {
        await this.dispatchConversationTurn({
          event,
          userId,
          resolved,
          messageContent,
          requestKey,
          steerOnly: false,
          retryAttempt: nextAttempt,
          busyWaitElapsedMs: nextBusyWaitElapsedMs,
          busyWaitStartedAtMs: nextBusyWaitStartedAtMs,
          explicitThreadId: conflictThreadId,
        });
      }
    };

    // Handle `409 resend_required` (`src/server/services/runs/pending_input.py:72-80, 179-187, 300-301`)
    // Returned by `POST /threads/{id}/retry` when the failed turn's input (HITL `hitl_response` or multimodal attachments)
    // never reached the graph (`pending.resend === True`).
    if (
      detail.code === "resend_required" &&
      retryAttempt <= BACKOFF_DELAYS_MS.length
    ) {
      const nextAttempt = isRetryEndpoint ? retryAttempt : retryAttempt + 1;
      const retryKey = isRetryEndpoint
        ? requestKey
        : makeRetryRequestKey(
            event.platform,
            event.messageId,
            conflictRunId ?? nextAttempt,
          );
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        thread_id: conflictThreadId,
        run_id: conflictRunId,
        retry_request_key: retryKey,
      });
      await dispatchResendWithNewKey(
        nextAttempt,
        retryKey,
        busyWaitElapsedMs,
        busyWaitStartedAtMs,
      );
      return;
    }

    // -----------------------------------------------------------------------
    // Type A: `duplicate_request` (§1.3.1)
    // -----------------------------------------------------------------------
    if (
      detail.code === "duplicate_request" ||
      detail.error_type === "duplicate_request"
    ) {
      const outcome = detail.run_status ?? detail.outcome ?? "unknown";
      const sameEndpoint = detail.same_endpoint !== false;

      // Steer duplicate (`steered: true` or `outcome == "steered"`)
      if (detail.steered === true || outcome === "steered") {
        const steeredRunId = conflictRunId ?? resolved.steered_into_run_id;
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          status: "steered",
          thread_id: conflictThreadId,
          steered_into_run_id: steeredRunId,
          fallback_pending: false,
        });
        await this.waitAndReconcileSteer({
          event,
          userId,
          resolved,
          messageContent,
          threadId: conflictThreadId,
          steeredIntoRunId: steeredRunId,
          retryAttempt,
        });
        return;
      }

      // Endpoint mismatch on key collision (`same_endpoint == false`)
      if (!sameEndpoint) {
        const nextAttempt = retryAttempt + 1;
        const newRetryKey = makeRetryRequestKey(
          event.platform,
          event.messageId,
          conflictRunId ?? nextAttempt,
        );
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          retry_request_key: newRetryKey,
        });
        await dispatchResendWithNewKey(
          nextAttempt,
          newRetryKey,
          busyWaitElapsedMs,
          busyWaitStartedAtMs,
        );
        return;
      }

      // A1: `run_status` is live (`in_progress`, `running`, `starting`, `streaming`, etc.)
      if (
        (outcome === "in_progress" ||
          PUBLIC_LIVE_STATUSES.has(outcome)) &&
        conflictThreadId
      ) {
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          status: "dispatched",
          thread_id: conflictThreadId,
          run_id: conflictRunId,
          fallback_pending: true,
        });
        await this.attachToLiveStreamOrReplay({
          userId,
          threadId: conflictThreadId,
          runId: conflictRunId,
          turnIndex: detail.turn_index ?? resolved.turn_index,
          event,
          resolved,
        });
        return;
      }

      // A2: `run_status in ("completed", "success", "interrupted")`
      if (
        (outcome === "completed" ||
          outcome === "success" ||
          outcome === "interrupted") &&
        conflictThreadId
      ) {
        await this.deliverReplayFallback({
          userId,
          threadId: conflictThreadId,
          runId: conflictRunId,
          turnIndex: detail.turn_index ?? resolved.turn_index,
          platform: event.platform,
          targetAddress: event.chatAddress,
          inboundMessageId: event.messageId,
          scopeKey: event.threadScopeKey,
        });
        return;
      }

      // A3: `run_status in ("cancelled", "error")` and `retryable == true` (`messaging.py:368-379, 1755-1775`, `pending_input.py:152, 300-301`)
      // Always invoke `POST /api/v1/threads/{id}/retry` with `:retry:{failed_run_id}` first so runs that already entered
      // the graph resume from their checkpoint without re-appending user input; only if `/retry` returns `409 resend_required`
      // (`pending.resend === True` because the input never reached the graph) does `handleDispatchConflict` re-send `/messages`.
      if (
        (outcome === "cancelled" || outcome === "error") &&
        detail.retryable === true &&
        conflictThreadId &&
        retryAttempt < BACKOFF_DELAYS_MS.length
      ) {
        const nextAttempt = retryAttempt + 1;
        const retryKey = makeRetryRequestKey(
          event.platform,
          event.messageId,
          conflictRunId ?? nextAttempt,
        );
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          thread_id: conflictThreadId,
          run_id: conflictRunId,
          retry_request_key: retryKey,
        });
        await this.dispatchCancelledRunRetry({
          event,
          userId,
          resolved,
          threadId: conflictThreadId,
          failedRunId: conflictRunId,
          requestKey: retryKey,
          retryAttempt: nextAttempt,
          busyWaitElapsedMs,
          busyWaitStartedAtMs,
          messageContent,
          isHitlResume,
          hitlResponse,
          creditSupplementContext,
        });
        return;
      }

      // A4 / A5 / A6: `outcome in ("error", "failed", "cancelled", "stale_recovery")`
      if (
        outcome === "error" ||
        outcome === "failed" ||
        outcome === "cancelled" ||
        outcome === "stale_recovery"
      ) {
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          status: "failed",
          fallback_pending: false,
        });
        await this.sendNoticeDelivery({
          deliveryKey: `notice:${event.platform}:${event.messageId}:dup_${outcome}`,
          userId,
          threadId: conflictThreadId,
          runId: conflictRunId,
          platform: event.platform,
          target: event.chatAddress,
          inboundMessageId: event.messageId,
          text: `The run ended with status "${outcome}".`,
          replyToMessageId: event.messageId,
          threadScopeKey: event.threadScopeKey,
        });
        return;
      }

      // Unknown `run_status`: resolve via `resolveRunStatusWithRetry` (handles pre-START ledger window)
      if (conflictThreadId && conflictRunId) {
        const runState = await this.resolveRunStatusWithRetry(
          userId,
          conflictThreadId,
          conflictRunId,
        );
        if (PUBLIC_LIVE_STATUSES.has(runState)) {
          await this.attachToLiveStreamOrReplay({
            userId,
            threadId: conflictThreadId,
            runId: conflictRunId,
            turnIndex: detail.turn_index ?? resolved.turn_index,
            event,
            resolved,
          });
          return;
        }
        if (PUBLIC_REPLAY_NEEDED_STATUSES.has(runState)) {
          await this.deliverReplayFallback({
            userId,
            threadId: conflictThreadId,
            runId: conflictRunId,
            turnIndex: detail.turn_index ?? resolved.turn_index,
            platform: event.platform,
            targetAddress: event.chatAddress,
            inboundMessageId: event.messageId,
            scopeKey: event.threadScopeKey,
          });
          return;
        }
      }

      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "failed",
        fallback_pending: false,
      });
      return;
    }

    // -----------------------------------------------------------------------
    // Type B: `admission_conflict` (§1.3.1)
    // -----------------------------------------------------------------------
    const isAdmissionConflict =
      detail.error_type === "admission_conflict" ||
      detail.code === "admission_conflict" ||
      detail.code === "running" ||
      detail.code === "not_running" ||
      detail.code === "stopping" ||
      detail.code === "compacting" ||
      detail.code === "workspace_moving" ||
      detail.code === "turn_conflict" ||
      detail.code === "attempt_conflict";

    if (isAdmissionConflict) {
      const reason = detail.reason ?? detail.code ?? "";

      // B1: `reason == "running"`
      if (reason === "running") {
        let targetTid = conflictThreadId;
        if (!targetTid && resolved.external_thread_id) {
          const st = await this.backend.getChannelState({
            user_id: userId,
            platform: event.platform,
            external_thread_id: resolved.external_thread_id,
          });
          targetTid =
            st.resolved_thread_id ??
            st.current_conversation?.thread_id ??
            null;
        }

        if (targetTid) {
          if (isRetryEndpoint || isHitlResume) {
            // Another client already resumed the interrupted turn; attach to `/messages/stream?run_id=...`
            await this.backend.updateInboundProgress({
              platform: event.platform,
              message_id: event.messageId,
              session_interrupt_update: {
                platform: event.platform,
                scope_key: event.threadScopeKey,
                dalton_user_id: userId,
                pending_interrupt: null,
              },
            });
            await this.attachToLiveStreamOrReplay({
              userId,
              threadId: targetTid,
              runId: conflictRunId,
              turnIndex: detail.turn_index ?? resolved.turn_index,
              event,
              resolved,
            });
            return;
          }

          // Normal user message while thread is running -> switch to `steer_only: true` keeping the SAME `requestKey`!
          if (!steerOnly) {
            await this.backend.updateInboundProgress({
              platform: event.platform,
              message_id: event.messageId,
              thread_id: targetTid,
              steered_into_run_id: conflictRunId,
            });
            await this.dispatchConversationTurn({
              event,
              userId,
              resolved: {
                ...resolved,
                thread_id: targetTid,
                run_id: conflictRunId,
              },
              messageContent,
              requestKey,
              steerOnly: true,
              retryAttempt,
              busyWaitElapsedMs,
              busyWaitStartedAtMs,
              explicitThreadId: targetTid,
            });
            return;
          }
        }
      }

      // B2: `reason == "not_running"` (while `steer_only: true` raced with turn completion)
      if (reason === "not_running" && steerOnly) {
        const nextAttempt = retryAttempt + 1;
        const turnKey = makeTurnAfterSteerRequestKey(
          event.platform,
          event.messageId,
          nextAttempt,
        );
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          retry_request_key: turnKey,
          steered_into_run_id: null,
        });
        await this.dispatchConversationTurn({
          event,
          userId,
          resolved: { ...resolved, retry_request_key: turnKey },
          messageContent,
          requestKey: turnKey,
          steerOnly: false,
          retryAttempt: nextAttempt,
          busyWaitElapsedMs,
          busyWaitStartedAtMs,
          explicitThreadId: conflictThreadId,
        });
        return;
      }

      // B3: `reason in ("stopping", "compacting", "workspace_moving", "turn_conflict", "attempt_conflict")`
      if (
        reason === "stopping" ||
        reason === "compacting" ||
        reason === "workspace_moving" ||
        reason === "turn_conflict" ||
        reason === "attempt_conflict"
      ) {
        if (
          (isRetryEndpoint || isHitlResume) &&
          reason === "attempt_conflict" &&
          conflictThreadId
        ) {
          const st = await this.backend.getThreadStatus(
            userId,
            conflictThreadId,
          );
          if (PUBLIC_LIVE_STATUSES.has(String(st.status ?? ""))) {
            await this.attachToLiveStreamOrReplay({
              userId,
              threadId: conflictThreadId,
              runId: st.run_id ?? conflictRunId,
              turnIndex: st.latest_turn_index ?? st.turn_index ?? resolved.turn_index,
              event,
              resolved,
            });
            return;
          }
          if (PUBLIC_REPLAY_NEEDED_STATUSES.has(String(st.status ?? ""))) {
            await this.deliverReplayFallback({
              userId,
              threadId: conflictThreadId,
              runId: st.run_id ?? conflictRunId,
              turnIndex: st.latest_turn_index ?? st.turn_index ?? resolved.turn_index,
              platform: event.platform,
              targetAddress: event.chatAddress,
              inboundMessageId: event.messageId,
              scopeKey: event.threadScopeKey,
            });
            return;
          }
        }

        const isLongBusyReason =
          reason === "stopping" ||
          reason === "compacting" ||
          reason === "workspace_moving";

        if (isLongBusyReason) {
          // Long `stopping` / `compacting` / `workspace_moving`: keep inbound row pending and poll
          // while the thread/run remains in `PUBLIC_LIVE_STATUSES` (including `"running"` during compaction,
          // `thread_status.py:134`, `lifecycle.py:109`) up to real wall-clock `this.busyThreadMaxWaitMs` (default 30 minutes).
          const rawBusyStarted =
            typeof resolved.raw_payload?.busy_wait_started_at_ms === "number"
              ? resolved.raw_payload.busy_wait_started_at_ms
              : undefined;
          const startedAtMs =
            busyWaitStartedAtMs ??
            rawBusyStarted ??
            this.nowFn() - busyWaitElapsedMs;
          if (busyWaitStartedAtMs === undefined && rawBusyStarted === undefined) {
            await this.backend.updateInboundProgress({
              platform: event.platform,
              message_id: event.messageId,
              raw_payload_patch: {
                busy_wait_started_at_ms: startedAtMs,
              },
            });
          }

          let accumulatedSleepMs = busyWaitElapsedMs;
          const getElapsedMs = (): number =>
            Math.max(this.nowFn() - startedAtMs, accumulatedSleepMs);
          let busyPollStep = 0;

          while (getElapsedMs() < this.busyThreadMaxWaitMs) {
            const delayMs =
              BACKOFF_DELAYS_MS[
                Math.min(busyPollStep, BACKOFF_DELAYS_MS.length - 1)
              ]!;
            busyPollStep++;
            await this.backoffDelayFn(delayMs);
            accumulatedSleepMs += delayMs;
            if (getElapsedMs() >= this.busyThreadMaxWaitMs) {
              break;
            }

            let stillLive: boolean | null = null;
            if (conflictThreadId) {
              try {
                const st = await this.backend.getThreadStatus(
                  userId,
                  conflictThreadId,
                );
                if (typeof st.status === "string" && st.status.length > 0) {
                  stillLive = PUBLIC_LIVE_STATUSES.has(st.status);
                }
              } catch {
                // Ignore transient status lookup error
              }
            }
            if (stillLive === null && conflictRunId) {
              try {
                const liveness = await this.backend.getDispatchesLiveness(
                  userId,
                  [conflictRunId],
                );
                const runState = liveness[conflictRunId]?.status;
                if (typeof runState === "string" && runState.length > 0) {
                  stillLive = PUBLIC_LIVE_STATUSES.has(runState);
                }
              } catch {
                // Ignore transient liveness lookup error
              }
            }

            if (stillLive === false) {
              break;
            }
            if (stillLive === null) {
              // No status oracle available (e.g. threadId & runId not yet known): re-probe dispatch after one backoff step
              break;
            }
          }

          const finalElapsedMs = getElapsedMs();
          if (finalElapsedMs < this.busyThreadMaxWaitMs) {
            // Keep long-busy status polling separate from post-dispatch conflict/capacity retries:
            // once the thread is no longer busy, the first re-dispatch starts at retry attempt 0 (Grok r5 #2).
            await dispatchRetryWithSameKey(
              0,
              finalElapsedMs,
              startedAtMs,
            );
            return;
          }
        } else if (retryAttempt < BACKOFF_DELAYS_MS.length) {
          await this.backoffDelayFn(BACKOFF_DELAYS_MS[retryAttempt]!);
          await dispatchRetryWithSameKey(
            retryAttempt + 1,
            busyWaitElapsedMs,
            busyWaitStartedAtMs,
          );
          return;
        }

        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          status: "failed",
          fallback_pending: false,
        });
        await this.sendNoticeDelivery({
          deliveryKey: `notice:${event.platform}:${event.messageId}:admission_${reason}`,
          userId,
          threadId: conflictThreadId,
          platform: event.platform,
          target: event.chatAddress,
          inboundMessageId: event.messageId,
          text: `The conversation is currently busy (${reason}). Please try again in a moment.`,
          replyToMessageId: event.messageId,
          threadScopeKey: event.threadScopeKey,
        });
        return;
      }
    }

    // -----------------------------------------------------------------------
    // Type C: `writer_capacity` / `capacity_limit` (§1.3.1)
    // -----------------------------------------------------------------------
    if (
      detail.code === "writer_capacity" ||
      detail.code === "capacity_limit" ||
      detail.error_type === "capacity_limit" ||
      status === 429 ||
      status === 503
    ) {
      const shouldRetryCapacity =
        (detail.code === "writer_capacity" || detail.retryable === true) &&
        retryAttempt < CAPACITY_BACKOFF_DELAYS_MS.length;

      if (shouldRetryCapacity) {
        const delayMs = CAPACITY_BACKOFF_DELAYS_MS[retryAttempt]!;
        await this.backoffDelayFn(delayMs);
        await dispatchRetryWithSameKey(retryAttempt + 1);
        return;
      }

      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "failed",
        fallback_pending: false,
      });
      await this.sendNoticeDelivery({
        deliveryKey: `notice:${event.platform}:${event.messageId}:capacity_limit`,
        userId,
        threadId: conflictThreadId,
        platform: event.platform,
        target: event.chatAddress,
        inboundMessageId: event.messageId,
        text:
          detail.message ||
          "You have reached the maximum number of concurrent runs. Please wait for an active run to finish.",
        replyToMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
      });
      return;
    }

    // -----------------------------------------------------------------------
    // Type D: `external_id_conflict` (§1.3.1)
    // -----------------------------------------------------------------------
    if (
      detail.code === "external_id_conflict" ||
      detail.error_type === "external_id_conflict"
    ) {
      let resolvedTid = detail.thread_id ?? null;
      if (!resolvedTid && resolved.external_thread_id) {
        const st = await this.backend.getChannelState({
          user_id: userId,
          platform: event.platform,
          external_thread_id: resolved.external_thread_id,
        });
        resolvedTid =
          st.resolved_thread_id ??
          st.current_conversation?.thread_id ??
          null;
      }
      if (resolvedTid && retryAttempt < 3) {
        const nextAttempt = retryAttempt + 1;
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          thread_id: resolvedTid,
        });
        await this.dispatchConversationTurn({
          event,
          userId,
          resolved: { ...resolved, thread_id: resolvedTid },
          messageContent,
          requestKey,
          steerOnly: false,
          retryAttempt: nextAttempt,
          busyWaitElapsedMs,
          explicitThreadId: resolvedTid,
        });
        return;
      }
    }

    // Generic error fallback
    await this.backend.updateInboundProgress({
      platform: event.platform,
      message_id: event.messageId,
      status: "failed",
      fallback_pending: false,
    });
    await this.sendNoticeDelivery({
      deliveryKey: `notice:${event.platform}:${event.messageId}:err_${status}`,
      userId,
      threadId: conflictThreadId,
      platform: event.platform,
      target: event.chatAddress,
      inboundMessageId: event.messageId,
      text: detail.message || `Request failed with status ${status}.`,
      replyToMessageId: event.messageId,
      threadScopeKey: event.threadScopeKey,
    });
  }

  // =========================================================================
  // §1.3.2 & §1.3.3 SSE Stream Consumption, Steer Reconcile & Replay Fallback
  // =========================================================================

  private async consumeTurnSseStream(params: {
    events: AsyncIterable<SseEvent>;
    event: InboundChannelEvent;
    userId: string;
    resolved: I1RegisterAndResolveResponse;
    threadId: string | null;
    requestKey: string;
    retryAttempt: number;
    busyWaitElapsedMs?: number;
    busyWaitStartedAtMs?: number;
    steerOnly?: boolean;
    isRetryEndpoint?: boolean;
    failedRunId?: string | null;
    messageContent?: string | ChatContentItem[];
    hitlResponse?: Record<string, { decisions: Array<Record<string, unknown>> }>;
    clearPendingInterruptOnAccept?: boolean;
    creditSupplementContext?: {
      interruptRunId: string;
      supplementText: string;
      supplementContent: string | ChatContentItem[];
    };
    deferFinalCompletion?: boolean;
    onRunStarted?: (runId: string, turnIndex: number | null) => Promise<void>;
    isAttachedStream?: boolean;
    attachedWithCursor?: boolean;
  }): Promise<boolean> {
    const {
      event,
      userId,
      resolved,
      requestKey,
      retryAttempt,
      busyWaitElapsedMs = 0,
      busyWaitStartedAtMs,
    } = params;
    let currentThreadId = params.threadId;
    let currentRunId = resolved.run_id;
    let currentTurnIndex = resolved.turn_index;
    let currentSegmentText = "";
    let lastCompletedSegmentText = "";
    let steeringAccepted = false;
    let steeredIntoRunId: string | null =
      resolved.steered_into_run_id ?? resolved.run_id;
    let streamErrorDetail: Record<string, unknown> | null = null;
    let retryEventPayload: Record<string, unknown> | null = null;
    let sawStreamGap = false;
    let sawRunEnd = false;
    let sawInterrupt = false;
    let interruptDeliveryFailed = false;
    let sawTurnAccepted = false;
    let runEndOutcome: string | null = null;
    let progressRecorded = false;
    let interruptCleared = false;
    let runStartedCallbackInvoked = false;

    for await (const sse of params.events) {
      if (sse.event === ":comment") {
        continue;
      }

      const payload = this.safeParseJson(sse.data);

      if (sse.event === "steering_accepted") {
        steeringAccepted = true;
        if (typeof payload.thread_id === "string") {
          currentThreadId = payload.thread_id;
        }
        if (typeof payload.run_id === "string") {
          steeredIntoRunId = payload.run_id;
        }
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          status: "steered",
          thread_id: currentThreadId,
          steered_into_run_id: steeredIntoRunId,
          fallback_pending: false,
        });
        continue;
      }

      if (sse.event === "stream_gap") {
        sawStreamGap = true;
        if (typeof payload.thread_id === "string") {
          currentThreadId = payload.thread_id;
        }
        if (typeof payload.run_id === "string") {
          currentRunId = payload.run_id;
        }
        continue;
      }

      if (
        sse.event === "thread_created" ||
        sse.event === "turn_start" ||
        sse.event === "run_started" ||
        sse.event === "metadata"
      ) {
        sawTurnAccepted = true;
        if (typeof payload.thread_id === "string") {
          currentThreadId = payload.thread_id;
        }
        if (typeof payload.run_id === "string") {
          currentRunId = payload.run_id;
        }
        if (typeof payload.turn_index === "number") {
          currentTurnIndex = payload.turn_index;
        }
        if (currentThreadId && !progressRecorded) {
          progressRecorded = true;
          interruptCleared = Boolean(params.clearPendingInterruptOnAccept);
          await this.backend.updateInboundProgress({
            platform: event.platform,
            message_id: event.messageId,
            status: "dispatched",
            thread_id: currentThreadId,
            run_id: currentRunId,
            turn_index: currentTurnIndex,
            fallback_pending: true,
            raw_payload_patch: {
              platform_user_id: event.platformUserId,
              generation: resolved.generation,
            },
            ...(params.clearPendingInterruptOnAccept
              ? {
                  session_interrupt_update: {
                    platform: event.platform,
                    scope_key: event.threadScopeKey,
                    dalton_user_id: userId,
                    pending_interrupt: null,
                  },
                }
              : {}),
            thread_watch: {
              thread_id: currentThreadId,
              dalton_user_id: userId,
              platform: event.platform,
              target_address: event.chatAddress,
              scope_key: event.threadScopeKey,
              generation: resolved.generation,
              last_seen_turn_index: currentTurnIndex ?? 0,
              watch_active: true,
            },
          });
          this.ensureThreadWatch({
            threadId: currentThreadId,
            userId,
            platform: event.platform,
            targetAddress: event.chatAddress,
            scopeKey: event.threadScopeKey,
            generation: resolved.generation,
            lastSeenTurnIndex: currentTurnIndex ?? 0,
          });
        }
        if (params.onRunStarted && !runStartedCallbackInvoked && currentRunId) {
          runStartedCallbackInvoked = true;
          await params.onRunStarted(currentRunId, currentTurnIndex);
        }
        continue;
      }

      if (sse.event === "tool_calls") {
        sawTurnAccepted = true;
        if (this.isMainAgentLane(payload.agent)) {
          // Reset pre-tool preamble so only post-tool final response text is sent as fallback
          if (currentSegmentText.trim().length > 0) {
            lastCompletedSegmentText = currentSegmentText;
            currentSegmentText = "";
          }
        }
        continue;
      }

      if (
        sse.event === "message_chunk" ||
        sse.event === "text" ||
        sse.event === "content_block_delta"
      ) {
        sawTurnAccepted = true;
        // Ignore subagent chunks (`agent: "task:<id>"` or `"subagent:<id>"`, `src/server/services/history/replay/lanes.py:11`)
        if (!this.isMainAgentLane(payload.agent)) {
          continue;
        }
        // Skip reasoning chunks (`content_type !== "text"` when `content_type` is present)
        if (
          typeof payload.content_type === "string" &&
          payload.content_type !== "text"
        ) {
          continue;
        }
        const delta =
          typeof payload.content === "string"
            ? payload.content
            : typeof payload.text === "string"
              ? payload.text
              : typeof payload.delta === "string"
                ? payload.delta
                : "";
        if (delta && payload.role !== "user") {
          currentSegmentText += delta;
        }
        if (payload.finish_reason === "tool_calls") {
          if (currentSegmentText.trim().length > 0) {
            lastCompletedSegmentText = currentSegmentText;
            currentSegmentText = "";
          }
        }
        continue;
      }

      if (sse.event === "message") {
        sawTurnAccepted = true;
        if (
          this.isMainAgentLane(payload.agent) &&
          payload.role === "assistant" &&
          typeof payload.content === "string"
        ) {
          currentSegmentText = payload.content;
        }
        continue;
      }

      if (sse.event === "interrupt") {
        sawTurnAccepted = true;
        sawInterrupt = true;
        if (typeof payload.thread_id === "string") {
          currentThreadId = payload.thread_id;
        }
        if (typeof payload.run_id === "string") {
          currentRunId = payload.run_id;
        }
        if (currentThreadId && currentRunId) {
          const intDelivered = await this.handleInterruptEvent({
            payload,
            userId,
            threadId: currentThreadId,
            runId: currentRunId,
            turnIndex: currentTurnIndex,
            platform: event.platform,
            targetAddress: event.chatAddress,
            scopeKey: event.threadScopeKey,
            inboundMessageId: event.messageId,
          });
          if (!intDelivered) {
            interruptDeliveryFailed = true;
          }
        }
        continue;
      }

      if (sse.event === "retry") {
        if (typeof payload.thread_id === "string") {
          currentThreadId = payload.thread_id;
        }
        if (typeof payload.run_id === "string") {
          currentRunId = payload.run_id;
        }
        retryEventPayload = payload;
        continue;
      }

      if (sse.event === "run_end") {
        sawTurnAccepted = true;
        sawRunEnd = true;
        if (typeof payload.thread_id === "string") {
          currentThreadId = payload.thread_id;
        }
        if (typeof payload.run_id === "string") {
          currentRunId = payload.run_id;
        }
        runEndOutcome =
          typeof payload.outcome === "string"
            ? payload.outcome
            : typeof payload.status === "string"
              ? payload.status
              : "completed";
        continue;
      }

      if (sse.event === "error") {
        streamErrorDetail = payload;
        continue;
      }
    }

    const assistantText =
      currentSegmentText.trim().length > 0
        ? currentSegmentText
        : lastCompletedSegmentText;

    // If `steering_accepted` was received, attach to the running turn and reconcile when it finishes (§1.3.2)
    if (steeringAccepted) {
      await this.waitAndReconcileSteer({
        event,
        userId,
        resolved,
        messageContent: params.messageContent ?? event.text,
        threadId: currentThreadId,
        steeredIntoRunId,
        retryAttempt,
      });
      return true;
    }

    // Handle `event: retry` (`src/server/handlers/chat/error_handling.py:293-297, 669-680`, `pending_input.py:63-70`):
    // - `recovery: "resend"` -> re-send the original request via `/messages` (including `hitl_response` for HITL answers)
    // - `recovery: "retry"` -> call `/retry` with `:retry:{failed_run_id}`
    if (
      retryEventPayload &&
      !sawStreamGap &&
      assistantText.trim().length === 0 &&
      (!sawRunEnd || runEndOutcome === "error" || runEndOutcome === "cancelled") &&
      retryAttempt < BACKOFF_DELAYS_MS.length
    ) {
      const recObj =
        retryEventPayload.recovery &&
        typeof retryEventPayload.recovery === "object"
          ? (retryEventPayload.recovery as Record<string, unknown>)
          : null;
      const recoveryStrategy =
        typeof retryEventPayload.recovery === "string"
          ? retryEventPayload.recovery
          : typeof recObj?.strategy === "string"
            ? recObj.strategy
            : retryEventPayload.auto_retry === true
              ? "retry"
              : null;

      if (recoveryStrategy === "resend") {
        const nextAttempt = retryAttempt + 1;
        // When `event: retry` (`recovery: "resend"`) arrives AFTER a failed run was already created in `agent_runs`
        // (`src/server/handlers/chat/error_handling.py:293-297, 335, 658-680`), reusing the original `request_key`
        // on `/messages` would hit `409 duplicate_request` (`admission.py:286, 369`). Mint `:retry:{failed_run_id}`!
        const failedRunOnResend =
          typeof retryEventPayload.run_id === "string" &&
          retryEventPayload.run_id.length > 0
            ? retryEventPayload.run_id
            : sawTurnAccepted && currentRunId
              ? currentRunId
              : null;
        let effectiveResendKey = requestKey;
        let effectiveResendResolved: I1RegisterAndResolveResponse = {
          ...resolved,
          thread_id: currentThreadId,
        };
        if (failedRunOnResend) {
          effectiveResendKey = makeRetryRequestKey(
            event.platform,
            event.messageId,
            failedRunOnResend,
          );
          await this.backend.updateInboundProgress({
            platform: event.platform,
            message_id: event.messageId,
            thread_id: currentThreadId,
            run_id: failedRunOnResend,
            retry_request_key: effectiveResendKey,
          });
          effectiveResendResolved = {
            ...effectiveResendResolved,
            run_id: failedRunOnResend,
            retry_request_key: effectiveResendKey,
          };
        }
        if (params.creditSupplementContext && currentThreadId && params.hitlResponse) {
          await this.dispatchCreditPauseResumeWithSupplement({
            event,
            userId,
            resolved: effectiveResendResolved,
            threadId: currentThreadId,
            interruptRunId: params.creditSupplementContext.interruptRunId,
            requestKey: effectiveResendKey,
            hitlResponse: params.hitlResponse,
            supplementText: params.creditSupplementContext.supplementText,
            supplementContent: params.creditSupplementContext.supplementContent,
            retryAttempt: nextAttempt,
            busyWaitElapsedMs,
            busyWaitStartedAtMs,
          });
        } else if (params.hitlResponse && currentThreadId) {
          await this.dispatchHitlResumeTurn({
            event,
            userId,
            resolved: effectiveResendResolved,
            threadId: currentThreadId,
            requestKey: effectiveResendKey,
            hitlResponse: params.hitlResponse,
            retryAttempt: nextAttempt,
            busyWaitElapsedMs,
            busyWaitStartedAtMs,
          });
        } else {
          await this.dispatchConversationTurn({
            event,
            userId,
            resolved: effectiveResendResolved,
            messageContent: params.messageContent ?? event.text,
            requestKey: effectiveResendKey,
            steerOnly: Boolean(params.steerOnly),
            retryAttempt: nextAttempt,
            busyWaitElapsedMs,
            busyWaitStartedAtMs,
            explicitThreadId: currentThreadId,
          });
        }
        return false;
      }

      if (recoveryStrategy === "retry" && currentThreadId) {
        const nextAttempt = retryAttempt + 1;
        const targetFailedRunId =
          currentRunId ?? params.failedRunId ?? resolved.run_id;
        const retryKey = makeRetryRequestKey(
          event.platform,
          event.messageId,
          targetFailedRunId ?? nextAttempt,
        );
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          thread_id: currentThreadId,
          run_id: targetFailedRunId,
          retry_request_key: retryKey,
        });
        await this.dispatchCancelledRunRetry({
          event,
          userId,
          resolved: {
            ...resolved,
            thread_id: currentThreadId,
            run_id: targetFailedRunId,
            retry_request_key: retryKey,
          },
          threadId: currentThreadId,
          failedRunId: targetFailedRunId,
          requestKey: retryKey,
          retryAttempt: nextAttempt,
          busyWaitElapsedMs,
          busyWaitStartedAtMs,
          messageContent: params.messageContent ?? event.text,
          isHitlResume: Boolean(params.hitlResponse),
          hitlResponse: params.hitlResponse,
          creditSupplementContext: params.creditSupplementContext,
        });
        return false;
      }
    }

    // If the SSE stream yielded an `event: error` (e.g. admission conflict or runtime failure)
    if (streamErrorDetail) {
      const parsedErr = this.parseConflictDetail(streamErrorDetail);
      if (
        parsedErr.code === "duplicate_request" ||
        parsedErr.error_type === "duplicate_request" ||
        parsedErr.code === "resend_required" ||
        parsedErr.error_type === "admission_conflict" ||
        parsedErr.code === "admission_conflict" ||
        parsedErr.code === "running" ||
        parsedErr.code === "not_running" ||
        parsedErr.code === "stopping" ||
        parsedErr.code === "compacting" ||
        parsedErr.code === "workspace_moving" ||
        parsedErr.code === "turn_conflict" ||
        parsedErr.code === "attempt_conflict" ||
        parsedErr.code === "writer_capacity" ||
        parsedErr.code === "capacity_limit" ||
        parsedErr.error_type === "capacity_limit" ||
        parsedErr.code === "external_id_conflict" ||
        parsedErr.error_type === "external_id_conflict"
      ) {
        await this.handleDispatchConflict({
          status: 409,
          errorBody: streamErrorDetail,
          event,
          userId,
          resolved,
          messageContent: params.messageContent ?? event.text,
          threadId: currentThreadId,
          requestKey,
          steerOnly: Boolean(params.steerOnly),
          retryAttempt,
          busyWaitElapsedMs,
          busyWaitStartedAtMs,
          isRetryEndpoint: Boolean(params.isRetryEndpoint),
          failedRunId: params.failedRunId ?? currentRunId,
          isHitlResume: Boolean(params.hitlResponse),
          hitlResponse: params.hitlResponse,
          creditSupplementContext: params.creditSupplementContext,
        });
        return false;
      }

      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "failed",
        fallback_pending: false,
      });
      const errMsg =
        typeof streamErrorDetail.message === "string"
          ? streamErrorDetail.message
          : typeof streamErrorDetail.error === "string"
            ? streamErrorDetail.error
            : "An error occurred while processing your message.";
      await this.sendNoticeDelivery({
        deliveryKey: `notice:${event.platform}:${event.messageId}:stream_err`,
        userId,
        threadId: currentThreadId,
        runId: currentRunId,
        platform: event.platform,
        target: event.chatAddress,
        inboundMessageId: event.messageId,
        text: errMsg,
        replyToMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
      });
      return false;
    }

    // If `event: run_end` reported a terminal failure/cancellation (`outcome in ("error", "failed", "cancelled")`)
    if (
      sawRunEnd &&
      (runEndOutcome === "error" ||
        runEndOutcome === "failed" ||
        runEndOutcome === "cancelled")
    ) {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "failed",
        fallback_pending: false,
      });
      await this.sendNoticeDelivery({
        deliveryKey: `notice:${event.platform}:${event.messageId}:run_end_${runEndOutcome}`,
        userId,
        threadId: currentThreadId,
        runId: currentRunId,
        platform: event.platform,
        target: event.chatAddress,
        inboundMessageId: event.messageId,
        text: `The run ended with status "${runEndOutcome}".`,
        replyToMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
      });
      return false;
    }

    // Bare SSE EOF without `run_end` or `interrupt` is a mid-stream disconnect, NOT normal completion!
    // Never deliver partial `assistantText` and never mark `completed` unless `/messages/replay` settles the turn.
    if (!sawRunEnd && !sawInterrupt) {
      if (currentThreadId && (!params.clearPendingInterruptOnAccept || sawTurnAccepted)) {
        if (
          params.clearPendingInterruptOnAccept &&
          !interruptCleared &&
          sawTurnAccepted
        ) {
          await this.backend.updateInboundProgress({
            platform: event.platform,
            message_id: event.messageId,
            session_interrupt_update: {
              platform: event.platform,
              scope_key: event.threadScopeKey,
              dalton_user_id: userId,
              pending_interrupt: null,
            },
          });
          interruptCleared = true;
        }
        const replaySettled = await this.deliverReplayFallback({
          userId,
          threadId: currentThreadId,
          runId: currentRunId,
          turnIndex: currentTurnIndex,
          platform: event.platform,
          targetAddress: event.chatAddress,
          inboundMessageId: event.messageId,
          scopeKey: event.threadScopeKey,
          deferFinalCompletion: params.deferFinalCompletion,
        });
        if (replaySettled) {
          await this.evaluateWatchCloseConditions(userId, currentThreadId);
          return true;
        }
      }
      // Keep inbound row in the recovery set (`dispatched` / `received` with `fallback_pending: true`)
      if (!progressRecorded) {
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          status: sawTurnAccepted ? "dispatched" : "received",
          thread_id: currentThreadId,
          run_id: currentRunId,
          turn_index: currentTurnIndex,
          fallback_pending: true,
        });
      }
      return false;
    }

    // Only clear `pending_interrupt` after turn acceptance is confirmed and no new interrupt occurred
    if (
      params.clearPendingInterruptOnAccept &&
      !interruptCleared &&
      sawTurnAccepted &&
      !sawInterrupt
    ) {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        session_interrupt_update: {
          platform: event.platform,
          scope_key: event.threadScopeKey,
          dalton_user_id: userId,
          pending_interrupt: null,
        },
      });
    }

    // If `event: stream_gap` occurred on an attached stream (`src/server/handlers/chat/run_stream_reader.py:199-206`),
    // or the stream was attached with a non-zero cursor (`run_stream_reader.py:183` only yields events strictly AFTER `last_event_id`),
    // earlier chunks are not in this stream — recover full turn output via `/messages/replay` instead of partial chunks!
    if (
      (sawStreamGap || (params.attachedWithCursor && !sawInterrupt)) &&
      currentThreadId
    ) {
      const settled = await this.deliverReplayFallback({
        userId,
        threadId: currentThreadId,
        runId: currentRunId,
        turnIndex: currentTurnIndex,
        platform: event.platform,
        targetAddress: event.chatAddress,
        inboundMessageId: event.messageId,
        scopeKey: event.threadScopeKey,
        deferFinalCompletion: params.deferFinalCompletion,
      });
      return settled;
    }

    // Normal SSE stream completion (`sawRunEnd` or `sawInterrupt`): re-query `I-7` for fresh `sent_to_current_count` before fallback (§1.3.3)
    const prog = await this.backend.updateInboundProgress({
      platform: event.platform,
      message_id: event.messageId,
      thread_id: currentThreadId,
      run_id: currentRunId,
      turn_index: currentTurnIndex,
    });

    let fallbackDeliveryFailed = false;
    const trimmedAssistant = assistantText.trim();
    if (
      !sawInterrupt &&
      trimmedAssistant.length > 0 &&
      !prog.has_in_flight_or_unknown_current_text &&
      currentRunId
    ) {
      const settledOk = await this.settleTurnFallbackAndPartialNotice({
        fallbackDeliveryKey: `fallback:${currentRunId}`,
        partialNoticeKeyId: currentRunId,
        userId,
        threadId: currentThreadId,
        runId: currentRunId,
        platform: event.platform,
        targetAddress: event.chatAddress,
        inboundMessageId: event.messageId,
        replyToMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
        trimmedText: trimmedAssistant,
        sentToCurrentCount: prog.sent_to_current_count,
      });
      if (!settledOk) {
        fallbackDeliveryFailed = true;
      }
    } else if (
      !sawInterrupt &&
      params.isAttachedStream &&
      trimmedAssistant.length === 0 &&
      !prog.has_in_flight_or_unknown_current_text &&
      currentThreadId
    ) {
      // Attached stream closed without emitting chunks (e.g. finished right before attach): recover via replay
      const settled = await this.deliverReplayFallback({
        userId,
        threadId: currentThreadId,
        runId: currentRunId,
        turnIndex: currentTurnIndex,
        platform: event.platform,
        targetAddress: event.chatAddress,
        inboundMessageId: event.messageId,
        scopeKey: event.threadScopeKey,
        deferFinalCompletion: params.deferFinalCompletion,
      });
      return settled;
    }

    // Preserve `fallback_pending: true` if an `/agent/send` to current conversation is still `in_flight` or `unknown`,
    // or if `executeDelivery` returned an explicit failure status!
    const keepFallbackPending =
      interruptDeliveryFailed ||
      (!sawInterrupt &&
        (fallbackDeliveryFailed ||
          (prog.sent_to_current_count === 0 &&
            prog.has_in_flight_or_unknown_current_text)));

    if (!params.deferFinalCompletion) {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "completed",
        fallback_pending: keepFallbackPending,
        ...(currentThreadId &&
        typeof currentTurnIndex === "number" &&
        !keepFallbackPending
          ? {
              thread_watch: {
                thread_id: currentThreadId,
                dalton_user_id: userId,
                platform: event.platform,
                target_address: event.chatAddress,
                scope_key: event.threadScopeKey,
                generation: resolved.generation,
                last_seen_turn_index: currentTurnIndex,
              },
            }
          : {}),
      });
    } else if (keepFallbackPending) {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        fallback_pending: true,
      });
    }

    if (currentThreadId) {
      if (keepFallbackPending) {
        const watchHandle = this.activeWatches.get(currentThreadId);
        if (watchHandle) {
          if (!watchHandle.pendingReplayFallbacks) {
            watchHandle.pendingReplayFallbacks = new Map<
              string,
              PendingReplayFallbackEntry
            >();
          }
          watchHandle.pendingReplayFallbacks.set(event.messageId, {
            userId,
            threadId: currentThreadId,
            runId: currentRunId,
            turnIndex: currentTurnIndex,
            platform: event.platform,
            targetAddress: event.chatAddress,
            inboundMessageId: event.messageId,
            scopeKey: event.threadScopeKey,
          });
          watchHandle.needsReconnectSync = true;
        }
      } else {
        await this.evaluateWatchCloseConditions(userId, currentThreadId);
      }
    }
    return !keepFallbackPending;
  }

  private async waitAndReconcileSteer(params: {
    event: InboundChannelEvent;
    userId: string;
    resolved: I1RegisterAndResolveResponse;
    messageContent: string | ChatContentItem[];
    threadId: string | null;
    steeredIntoRunId: string | null;
    retryAttempt: number;
  }): Promise<void> {
    const {
      event,
      userId,
      resolved,
      messageContent,
      threadId,
      steeredIntoRunId,
      retryAttempt,
    } = params;

    // Attach to live stream of the host turn (`?run_id={steeredIntoRunId}`) if threadId is known
    let attachedInterruptDeliveryFailed = false;
    if (threadId) {
      try {
        const streamResp = await this.backend.streamThreadMessages(
          userId,
          threadId,
          { runId: steeredIntoRunId },
        );
        if (streamResp.isSse && streamResp.events) {
          for await (const ev of streamResp.events) {
            if (ev.event === "interrupt" && steeredIntoRunId) {
              const payload = this.safeParseJson(ev.data);
              const intOk = await this.handleInterruptEvent({
                payload,
                userId,
                threadId,
                runId:
                  typeof payload.run_id === "string"
                    ? payload.run_id
                    : steeredIntoRunId,
                turnIndex:
                  typeof payload.turn_index === "number"
                    ? payload.turn_index
                    : null,
                platform: event.platform,
                targetAddress: event.chatAddress,
                scopeKey: event.threadScopeKey,
                inboundMessageId: event.messageId,
              });
              if (!intOk) {
                attachedInterruptDeliveryFailed = true;
              }
            }
          }
        }
      } catch {
        // Proceed to reconcileSteer even if stream attachment fails
      }
    }

    const safeReconcileSteer = async () => {
      try {
        return await this.backend.reconcileSteer({
          platform: event.platform,
          message_id: event.messageId,
          run_id: steeredIntoRunId,
        });
      } catch {
        return {
          ok: true,
          reconcile_status: "still_pending" as const,
          outcome: null,
          run_status: "in_progress",
          inbound_status: "steered",
          resubmitted_request_key: null,
        };
      }
    };

    let rec = await safeReconcileSteer();

    // When `reconcile_status === "still_pending"` (`channel_gateway.py:3490, 3538-3572`), the host run is still
    // running (`run_status in ("running", "streaming", "queued")`) or `interrupted` with `outcome == "taken"`
    // awaiting resume completion. Keep polling up to `busyThreadMaxWaitMs` until it settles!
    const reconcileStartedAtMs = this.nowFn();
    let accumulatedSleepMs = 0;
    let pollIdx = 0;
    while (
      rec.reconcile_status === "still_pending" &&
      !this.shuttingDown &&
      Math.max(this.nowFn() - reconcileStartedAtMs, accumulatedSleepMs) <
        this.busyThreadMaxWaitMs
    ) {
      const delayMs =
        BACKOFF_DELAYS_MS[Math.min(pollIdx, BACKOFF_DELAYS_MS.length - 1)]!;
      pollIdx++;
      await this.backoffDelayFn(delayMs);
      accumulatedSleepMs += delayMs;
      if (this.shuttingDown) {
        return;
      }
      if (steeredIntoRunId) {
        try {
          const liveness = await this.backend.getDispatchesLiveness(userId, [
            steeredIntoRunId,
          ]);
          const liveState = liveness[steeredIntoRunId]?.status;
          if (liveState && !PUBLIC_LIVE_STATUSES.has(liveState)) {
            rec = await safeReconcileSteer();
            if (rec.reconcile_status !== "still_pending") {
              break;
            }
          }
        } catch {
          // Ignore transient liveness poll errors
        }
      }
      rec = await safeReconcileSteer();
    }

    if (rec.reconcile_status === "consumed") {
      let replaySettled = !attachedInterruptDeliveryFailed;
      if (
        ((rec.outcome && INTERNAL_REPLAY_NEEDED_OUTCOMES.has(rec.outcome)) ||
          attachedInterruptDeliveryFailed) &&
        threadId &&
        steeredIntoRunId
      ) {
        replaySettled = await this.deliverReplayFallback({
          userId,
          threadId,
          runId: steeredIntoRunId,
          turnIndex: resolved.turn_index,
          platform: event.platform,
          targetAddress: event.chatAddress,
          inboundMessageId: event.messageId,
          scopeKey: event.threadScopeKey,
        });
      }
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "completed",
        fallback_pending: !replaySettled,
      });
      return;
    }

    // Only resubmit with `:returned:{run_id}` when `I-7` confirms `reconcile_status === "pending_resubmit"` (or `"not_found"`)!
    if (
      rec.reconcile_status === "pending_resubmit" ||
      rec.reconcile_status === "not_found"
    ) {
      const resubmitKey =
        rec.resubmitted_request_key ??
        makeReturnedSteerRequestKey(
          event.platform,
          event.messageId,
          steeredIntoRunId ?? "unknown",
        );
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        resubmitted_request_key: resubmitKey,
      });
      await this.dispatchConversationTurn({
        event,
        userId,
        resolved: {
          ...resolved,
          thread_id: threadId ?? resolved.thread_id,
          resubmitted_request_key: resubmitKey,
        },
        messageContent,
        requestKey: resubmitKey,
        steerOnly: false,
        retryAttempt: retryAttempt + 1,
        explicitThreadId: threadId ?? resolved.thread_id,
      });
      return;
    }

    if (rec.reconcile_status === "still_pending") {
      if (this.shuttingDown) {
        return;
      }
      // Keep inbound row in `steered` state in the backend recovery set (`channel_gateway.py:3448-3572, 3733`)
      // so startup recovery and subsequent reconciliation continue to settle it once the host run finishes,
      // and send a one-time notice per inbound message instead of telling the user to resend (Grok r5 #4).
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "steered",
        steered_into_run_id: steeredIntoRunId,
        fallback_pending: false,
      });
      await this.sendNoticeDelivery({
        deliveryKey: `notice:${event.platform}:${event.messageId}:steer_queued`,
        userId,
        threadId,
        runId: steeredIntoRunId,
        platform: event.platform,
        target: event.chatAddress,
        inboundMessageId: event.messageId,
        text: "这条补充已排队，宿主回合结束后会处理。",
        replyToMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
      });
      return;
    }

    if (rec.reconcile_status === "returned_unresubmitted") {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "failed",
        fallback_pending: false,
      });
      await this.sendNoticeDelivery({
        deliveryKey: `notice:${event.platform}:${event.messageId}:returned_steer`,
        userId,
        threadId,
        runId: steeredIntoRunId,
        platform: event.platform,
        target: event.chatAddress,
        inboundMessageId: event.messageId,
        text: "The previous run ended with an error before your follow-up message could be processed. Please send your message again if needed.",
        replyToMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
      });
      return;
    }
  }

  /**
   * Unbounded pagination over `GET /api/v1/threads/{thread_id}/messages/replay?source=auto&limit=5` (§1.3.3).
   * Real event sequence (`src/server/app/threads/messaging.py:1244-1312`, `src/server/services/history/replay/checkpoint.py:216-439`,
   * `src/server/services/history/replay/stubs.py:72-97`, `src/server/services/history/replay/items.py:49-60`):
   * `snapshot` (first page when active runs exist) -> `history_page` (`first_turn_index`, `has_more`, `next_before_turn`)
   * -> `turn_Separator` (with `turn_index`) -> `user_message` -> `tool_calls` / `tool_call_result`
   * -> `message_chunk` (only main-lane `content_type === "text"` after the last `tool_calls`)
   * -> `interrupt` (only on tip turn when `before_turn === undefined`) -> `replay_done` (required end-of-page marker).
   */
  async deliverReplayFallback(params: {
    userId: string;
    threadId: string;
    runId: string | null;
    turnIndex: number | null;
    platform: ChannelPlatform;
    targetAddress: string;
    inboundMessageId?: string | null;
    scopeKey?: string | null;
    isBackgroundRun?: boolean;
    deferFinalCompletion?: boolean;
  }): Promise<boolean> {
    const {
      userId,
      threadId,
      runId,
      turnIndex,
      platform,
      targetAddress,
      inboundMessageId,
      scopeKey,
      isBackgroundRun,
      deferFinalCompletion,
    } = params;

    let foundText = "";
    let foundTargetTurn = false;
    let resolvedRunId = runId;
    let anyPageSucceeded = false;
    let tipInterruptDelivered = false;
    let tipInterruptDeliveryFailed = false;
    let terminalEmptyOutcome:
      | "interrupted"
      | "cancelled"
      | "error"
      | "completed"
      | null = null;
    let terminalEmptyErrorText: string | null = null;
    const replayWaitStartedAtMs = this.nowFn();
    let accumulatedReplaySleepMs = 0;
    const getReplayElapsedMs = (): number =>
      Math.max(this.nowFn() - replayWaitStartedAtMs, accumulatedReplaySleepMs);
    let unsettledPass = 0;

    while (!this.shuttingDown) {
      let beforeTurn: number | undefined = undefined;
      let sawUnsettledTargetTurn = false;
      let immediateReplayAfterLiveRunEnd = false;
      anyPageSucceeded = false;
      tipInterruptDelivered = false;
      tipInterruptDeliveryFailed = false;
      terminalEmptyOutcome = null;
      terminalEmptyErrorText = null;

      while (true) {
        const isFirstPage = beforeTurn === undefined;
        let pageSucceeded = false;
        let currentEventTurnIndex: number | null = null;
        let currentEventRunId: string | null = null;
        let earliestTurnInBatch: number | null = null;
        let latestTurnInBatch: number | null = null;
        const textsByTurn = new Map<number, string>();
        const textsByRun = new Map<string, string>();
        const turnToRunId = new Map<number, string>();
        const userMsgRunIdByTurn = new Map<number, string | null>();
        const userMsgCompletedAtByTurn = new Map<number, string | null>();
        const stoppedTurnsInBatch = new Set<number>();
        const stoppedRunsInBatch = new Set<string>();
        const erroredTurnsInBatch = new Map<number, string>();
        const erroredRunsInBatch = new Map<string, string>();
        const mismatchedAttemptTurnsInBatch = new Set<number>();
        const seenTurnsInBatch = new Set<number>();
        const settledTurnsInBatch = new Set<number>();
        const seenRunsInBatch = new Set<string>();
        const settledRunsInBatch = new Set<string>();
        const pendingResetTurns = new Set<number>();
        const pendingResetRuns = new Set<string>();
        let pendingResetLastTurn = false;
        let lastTurnText = "";
        let pendingTipInterrupt: Record<string, unknown> | null = null;
        let activeMainRunFromSnapshot: {
          runId: string;
          lastEventId: string | null;
        } | null = null;
        let replayDoneMeta: {
          has_more?: boolean;
          oldest_turn_index?: number | null;
          latest_run_id?: string | null;
          latest_turn_index?: number | null;
        } = {};

        for (
          let pageAttempt = 0;
          pageAttempt <= BACKOFF_DELAYS_MS.length;
          pageAttempt++
        ) {
          if (pageAttempt > 0) {
            const pageDelayMs = BACKOFF_DELAYS_MS[pageAttempt - 1]!;
            await this.backoffDelayFn(pageDelayMs);
            accumulatedReplaySleepMs += pageDelayMs;
          }

          let resp: SseOrErrorResponse;
          try {
            resp = await this.backend.replayThreadMessages(
              userId,
              threadId,
              beforeTurn,
            );
          } catch {
            continue;
          }
          if (!resp.isSse || !resp.events) {
            continue;
          }

          currentEventTurnIndex = null;
          currentEventRunId = null;
          earliestTurnInBatch = null;
          latestTurnInBatch = null;
          textsByTurn.clear();
          textsByRun.clear();
          turnToRunId.clear();
          userMsgRunIdByTurn.clear();
          userMsgCompletedAtByTurn.clear();
          stoppedTurnsInBatch.clear();
          stoppedRunsInBatch.clear();
          erroredTurnsInBatch.clear();
          erroredRunsInBatch.clear();
          mismatchedAttemptTurnsInBatch.clear();
          seenTurnsInBatch.clear();
          settledTurnsInBatch.clear();
          seenRunsInBatch.clear();
          settledRunsInBatch.clear();
          pendingResetTurns.clear();
          pendingResetRuns.clear();
          pendingResetLastTurn = false;
          lastTurnText = "";
          pendingTipInterrupt = null;
          activeMainRunFromSnapshot = null;
          replayDoneMeta = {};
          let pageReplayUnavailable = false;
          let sawReplayDone = false;

          for await (const sse of resp.events) {
            if (sse.event === ":comment") continue;
            const data = this.safeParseJson(sse.data);

            if (
              sse.event === "replay_unavailable" ||
              (sse.event === "error" && data.type === "replay_unavailable")
            ) {
              pageReplayUnavailable = true;
              break;
            }

            if (sse.event === "error") {
              if (this.isMainAgentLane(data.agent)) {
                const tIdx =
                  typeof data.turn_index === "number"
                    ? data.turn_index
                    : currentEventTurnIndex;
                const rId =
                  typeof data.response_id === "string"
                    ? data.response_id
                    : typeof data.run_id === "string"
                      ? data.run_id
                      : currentEventRunId;
                if (tIdx === null || !mismatchedAttemptTurnsInBatch.has(tIdx)) {
                  const errText =
                    typeof data.error === "string" ? data.error.trim() : "";
                  if (tIdx !== null) {
                    settledTurnsInBatch.add(tIdx);
                    erroredTurnsInBatch.set(tIdx, errText);
                  }
                  if (rId) {
                    settledRunsInBatch.add(rId);
                    erroredRunsInBatch.set(rId, errText);
                  }
                }
              }
              continue;
            }

            if (sse.event === "snapshot" && isFirstPage) {
              if (Array.isArray(data.active_runs)) {
                for (const item of data.active_runs) {
                  if (item && typeof item === "object") {
                    const ar = item as Record<string, unknown>;
                    const lane =
                      typeof ar.lane === "string" ? ar.lane : "main";
                    if (
                      lane === "main" &&
                      typeof ar.run_id === "string" &&
                      ar.run_id.length > 0
                    ) {
                      const cursorObj =
                        ar.cursor && typeof ar.cursor === "object"
                          ? (ar.cursor as Record<string, unknown>)
                          : null;
                      const rawCursorId = cursorObj?.last_event_id;
                      const parsedLastEventId =
                        typeof rawCursorId === "number" &&
                        Number.isFinite(rawCursorId) &&
                        rawCursorId > 0
                          ? String(rawCursorId)
                          : typeof rawCursorId === "string" &&
                              rawCursorId.length > 0 &&
                              rawCursorId !== "0"
                            ? rawCursorId
                            : null;
                      activeMainRunFromSnapshot = {
                        runId: ar.run_id,
                        lastEventId: parsedLastEventId,
                      };
                    }
                  }
                }
              }
              continue;
            }

            if (
              sse.event === "turn_Separator" ||
              sse.event === "turn_start" ||
              sse.event === "user_message"
            ) {
              if (typeof data.turn_index === "number") {
                currentEventTurnIndex = data.turn_index;
                seenTurnsInBatch.add(data.turn_index);
                if (
                  earliestTurnInBatch === null ||
                  data.turn_index < earliestTurnInBatch
                ) {
                  earliestTurnInBatch = data.turn_index;
                }
                if (
                  latestTurnInBatch === null ||
                  data.turn_index > latestTurnInBatch
                ) {
                  latestTurnInBatch = data.turn_index;
                }
              }
              const terminalUserMsgRunId =
                typeof data.response_id === "string"
                  ? data.response_id
                  : typeof data.run_id === "string"
                    ? data.run_id
                    : null;
              const reportRunId =
                typeof data.report_run_id === "string"
                  ? data.report_run_id
                  : null;
              const evtRunId = terminalUserMsgRunId ?? reportRunId;
              const userMsgCompletedAt =
                typeof data.run_completed_at === "string" &&
                data.run_completed_at.length > 0
                  ? data.run_completed_at
                  : null;
              if (
                currentEventTurnIndex !== null &&
                sse.event === "user_message"
              ) {
                userMsgRunIdByTurn.set(
                  currentEventTurnIndex,
                  terminalUserMsgRunId,
                );
                userMsgCompletedAtByTurn.set(
                  currentEventTurnIndex,
                  userMsgCompletedAt,
                );
              }
              if (evtRunId) {
                currentEventRunId = evtRunId;
                seenRunsInBatch.add(evtRunId);
                if (currentEventTurnIndex !== null) {
                  turnToRunId.set(currentEventTurnIndex, evtRunId);
                }
              }
              // In `src/server/services/history/replay/items.py:40-60` and `stubs.py:85-97`:
              // 1. An in-flight `user_message` stub has NO `run_id` and NO `run_completed_at`
              //    (whereas a running `system` turn still carries `report_run_id` from `open_run` at `items.py:46-48`!).
              // 2. During an in-progress `/retry` (`open_run = new_run_2`) of a turn that had an earlier terminal
              //    failed attempt (`failed_run_1` in `responses_by_turn`), `stubs.py:88-90` sets `archived = []` while
              //    `items.py:56-60` stamps `failed_run_1` as `run_id` and `run_completed_at` on `user_message`.
              // When `runId` (the expected current attempt's `run_id`) is specified, `user_message.run_id` MUST equal `runId`;
              // if `terminalUserMsgRunId !== runId`, this turn's replay stub still reflects an earlier attempt (or is in-flight)
              // and must NOT be treated as settled!
              const isTargetTurnOrUnspecified =
                turnIndex === null ||
                turnIndex === undefined ||
                currentEventTurnIndex === turnIndex;
              const hasEarlierAttemptMismatch =
                Boolean(runId) &&
                isTargetTurnOrUnspecified &&
                terminalUserMsgRunId !== runId;
              const hasOpenReportRunMismatch =
                reportRunId !== null && reportRunId !== terminalUserMsgRunId;
              if (
                currentEventTurnIndex !== null &&
                (hasEarlierAttemptMismatch || hasOpenReportRunMismatch)
              ) {
                mismatchedAttemptTurnsInBatch.add(currentEventTurnIndex);
              } else if (
                sse.event === "user_message" &&
                (terminalUserMsgRunId !== null ||
                  typeof data.run_completed_at === "string")
              ) {
                if (currentEventTurnIndex !== null) {
                  settledTurnsInBatch.add(currentEventTurnIndex);
                }
                if (evtRunId) {
                  settledRunsInBatch.add(evtRunId);
                }
              }
              continue;
            }

            if (
              sse.event === "tool_call_result" ||
              sse.event === "credit_usage" ||
              sse.event === "turn_end"
            ) {
              const tIdx =
                typeof data.turn_index === "number"
                  ? data.turn_index
                  : currentEventTurnIndex;
              const rId =
                typeof data.response_id === "string"
                  ? data.response_id
                  : typeof data.run_id === "string"
                    ? data.run_id
                    : currentEventRunId;
              if (tIdx !== null && mismatchedAttemptTurnsInBatch.has(tIdx)) {
                continue;
              }
              if (tIdx !== null) {
                settledTurnsInBatch.add(tIdx);
              }
              if (rId) {
                settledRunsInBatch.add(rId);
              }
              continue;
            }

            // When main agent makes tool calls, mark pre-tool preamble for reset on the next post-tool text chunk
            // so that if no post-tool text follows (e.g. a failed `send_message` as the final step), pre-tool text is preserved.
            if (sse.event === "tool_calls") {
              if (this.isMainAgentLane(data.agent)) {
                const tIdx =
                  typeof data.turn_index === "number"
                    ? data.turn_index
                    : currentEventTurnIndex;
                const rId =
                  typeof data.response_id === "string"
                    ? data.response_id
                    : typeof data.run_id === "string"
                      ? data.run_id
                      : currentEventRunId;
                if (tIdx !== null && mismatchedAttemptTurnsInBatch.has(tIdx)) {
                  continue;
                }
                if (tIdx !== null) {
                  settledTurnsInBatch.add(tIdx);
                  pendingResetTurns.add(tIdx);
                }
                if (rId) {
                  settledRunsInBatch.add(rId);
                  pendingResetRuns.add(rId);
                }
                pendingResetLastTurn = true;
              }
              continue;
            }

            if (
              sse.event === "message_chunk" ||
              sse.event === "text" ||
              sse.event === "message"
            ) {
              if (!this.isMainAgentLane(data.agent)) {
                continue;
              }
              const role =
                typeof data.role === "string" ? data.role : "assistant";
              if (role === "user") continue;
              // Only concatenate `content_type == "text"` (skip `"reasoning"` / `"reasoning_signal"`)
              if (
                typeof data.content_type === "string" &&
                data.content_type !== "text"
              ) {
                continue;
              }
              const tIdx =
                typeof data.turn_index === "number"
                  ? data.turn_index
                  : currentEventTurnIndex;
              const rId =
                typeof data.response_id === "string"
                  ? data.response_id
                  : typeof data.run_id === "string"
                    ? data.run_id
                    : currentEventRunId;
              // `src/server/services/history/replay/stopped.py:45-54` emits `event: message_chunk` with `finish_reason: "stopped"`
              // and no `content` for user-stopped (`cancelled`) turns!
              if (data.finish_reason === "stopped") {
                if (tIdx === null || !mismatchedAttemptTurnsInBatch.has(tIdx)) {
                  if (tIdx !== null) {
                    settledTurnsInBatch.add(tIdx);
                    stoppedTurnsInBatch.add(tIdx);
                  }
                  if (rId) {
                    settledRunsInBatch.add(rId);
                    stoppedRunsInBatch.add(rId);
                  }
                }
              }
              const chunk =
                typeof data.content === "string"
                  ? data.content
                  : typeof data.text === "string"
                    ? data.text
                    : "";
              if (!chunk) continue;

              if (tIdx !== null && mismatchedAttemptTurnsInBatch.has(tIdx)) {
                continue;
              }

              if (tIdx !== null) {
                seenTurnsInBatch.add(tIdx);
                settledTurnsInBatch.add(tIdx);
                if (pendingResetTurns.has(tIdx)) {
                  textsByTurn.set(tIdx, "");
                  pendingResetTurns.delete(tIdx);
                }
                textsByTurn.set(tIdx, (textsByTurn.get(tIdx) ?? "") + chunk);
                if (earliestTurnInBatch === null || tIdx < earliestTurnInBatch) {
                  earliestTurnInBatch = tIdx;
                }
                if (latestTurnInBatch === null || tIdx > latestTurnInBatch) {
                  latestTurnInBatch = tIdx;
                }
                if (rId && !turnToRunId.has(tIdx)) {
                  turnToRunId.set(tIdx, rId);
                }
              }
              if (rId) {
                seenRunsInBatch.add(rId);
                settledRunsInBatch.add(rId);
                if (pendingResetRuns.has(rId)) {
                  textsByRun.set(rId, "");
                  pendingResetRuns.delete(rId);
                }
                textsByRun.set(rId, (textsByRun.get(rId) ?? "") + chunk);
              }
              if (pendingResetLastTurn) {
                lastTurnText = "";
                pendingResetLastTurn = false;
              }
              lastTurnText += chunk;
              continue;
            }

            if (sse.event === "interrupt" && isFirstPage) {
              pendingTipInterrupt = data;
              if (currentEventTurnIndex !== null) {
                settledTurnsInBatch.add(currentEventTurnIndex);
              }
              if (currentEventRunId) {
                settledRunsInBatch.add(currentEventRunId);
              }
              continue;
            }

            if (sse.event === "replay_done" || sse.event === "history_page") {
              if (sse.event === "replay_done") {
                sawReplayDone = true;
              }
              const oldestOrFirst =
                typeof data.oldest_turn_index === "number"
                  ? data.oldest_turn_index
                  : typeof data.first_turn_index === "number"
                    ? data.first_turn_index
                    : replayDoneMeta.oldest_turn_index ?? null;
              replayDoneMeta = {
                has_more:
                  typeof data.has_more === "boolean"
                    ? data.has_more
                    : Boolean(replayDoneMeta.has_more),
                oldest_turn_index: oldestOrFirst,
                latest_run_id:
                  typeof data.latest_run_id === "string"
                    ? data.latest_run_id
                    : replayDoneMeta.latest_run_id ?? null,
                latest_turn_index:
                  typeof data.latest_turn_index === "number"
                    ? data.latest_turn_index
                    : replayDoneMeta.latest_turn_index ?? null,
              };
              continue;
            }
          }

          // Every page in `/messages/replay` ends with `event: replay_done` (`messaging.py:1305-1312`).
          // A stream that ends at EOF without `replay_done` is a truncated page and must be retried.
          if (!pageReplayUnavailable && sawReplayDone) {
            pageSucceeded = true;
            break;
          }
        }

        if (!pageSucceeded) {
          anyPageSucceeded = false;
          break;
        }
        anyPageSucceeded = true;

        // If `event: snapshot` on the first page shows an active main-lane run (`activeMainRunFromSnapshot`),
        // that run is still in-flight (not settled in replay).
        if (isFirstPage && activeMainRunFromSnapshot) {
          settledRunsInBatch.delete(activeMainRunFromSnapshot.runId);
          if (latestTurnInBatch !== null) {
            settledTurnsInBatch.delete(latestTurnInBatch);
          }
          const matchesTargetTurn =
            (turnIndex === null ||
              turnIndex === undefined ||
              latestTurnInBatch === turnIndex) &&
            (!runId || activeMainRunFromSnapshot.runId === runId);
          if (matchesTargetTurn) {
            const preCursorText =
              (latestTurnInBatch !== null
                ? textsByTurn.get(latestTurnInBatch)
                : undefined) ??
              textsByRun.get(activeMainRunFromSnapshot.runId) ??
              "";
            const liveSettled = await this.consumeInFlightReplayRunStream({
              userId,
              threadId,
              runId: activeMainRunFromSnapshot.runId,
              lastEventId: activeMainRunFromSnapshot.lastEventId,
              preCursorText,
              turnIndex: latestTurnInBatch ?? turnIndex,
              platform,
              targetAddress,
              inboundMessageId,
              scopeKey,
            });
            if (!liveSettled.settled) {
              // The attached live stream broke before `run_end` / `interrupt`, or attached with cursor `N > 0` without
              // pre-cursor text in the replay stub (`stubs.py:88`, `run_stream_reader.py:183`) and now saw `run_end`:
              // mark `sawUnsettledTargetTurn = true` so the outer pass loop re-queries `/messages/replay` for the full turn!
              sawUnsettledTargetTurn = true;
              if (liveSettled.runEnded) {
                immediateReplayAfterLiveRunEnd = true;
              }
              break;
            }
            foundText = liveSettled.assistantText;
            foundTargetTurn = true;
            resolvedRunId = activeMainRunFromSnapshot.runId;
            break;
          }
        }

        let pageLiveStatus: ThreadStatusInfo | null = null;
        let pageLiveStatusFetched = false;
        const getPageLiveStatus = async (): Promise<ThreadStatusInfo | null> => {
          if (!pageLiveStatusFetched) {
            pageLiveStatusFetched = true;
            try {
              pageLiveStatus = await this.backend.getThreadStatus(
                userId,
                threadId,
              );
            } catch {
              pageLiveStatus = null;
            }
          }
          return pageLiveStatus;
        };

        // Tip interrupt check on first page (`beforeTurn === undefined`, §1.3.3)
        if (isFirstPage && pendingTipInterrupt && scopeKey) {
          const liveStatus = await getPageLiveStatus();
          if (!liveStatus) {
            tipInterruptDeliveryFailed = true;
          } else {
            const interruptRunId =
              typeof pendingTipInterrupt.run_id === "string"
                ? pendingTipInterrupt.run_id
                : liveStatus.run_id ??
                  replayDoneMeta.latest_run_id ??
                  resolvedRunId ??
                  "unknown";
            if (
              liveStatus.status === "interrupted" &&
              (!liveStatus.run_id || liveStatus.run_id === interruptRunId)
            ) {
              try {
                const intOk = await this.handleInterruptEvent({
                  payload: pendingTipInterrupt,
                  userId,
                  threadId,
                  runId: interruptRunId,
                  turnIndex:
                    liveStatus.latest_turn_index ??
                    liveStatus.turn_index ??
                    replayDoneMeta.latest_turn_index ??
                    latestTurnInBatch ??
                    turnIndex,
                  platform,
                  targetAddress,
                  scopeKey,
                  inboundMessageId: inboundMessageId ?? undefined,
                });
                if (intOk) {
                  tipInterruptDelivered = true;
                } else {
                  tipInterruptDeliveryFailed = true;
                }
              } catch {
                tipInterruptDeliveryFailed = true;
              }
            }
          }
        }

        const resolveTerminalEmptyTurn = async (
          targetTurnIdx: number | null,
          targetRunId: string | null,
        ): Promise<boolean> => {
          if (
            targetTurnIdx !== null &&
            mismatchedAttemptTurnsInBatch.has(targetTurnIdx)
          ) {
            return false;
          }
          const turnUserMsgRunId =
            targetTurnIdx !== null
              ? (userMsgRunIdByTurn.get(targetTurnIdx) ?? null)
              : null;
          const turnUserMsgCompletedAt =
            targetTurnIdx !== null
              ? (userMsgCompletedAtByTurn.get(targetTurnIdx) ?? null)
              : null;
          if (runId && turnUserMsgRunId !== null && turnUserMsgRunId !== runId) {
            return false;
          }
          if (
            isFirstPage &&
            targetTurnIdx !== null &&
            latestTurnInBatch === targetTurnIdx &&
            runId &&
            replayDoneMeta.latest_run_id &&
            replayDoneMeta.latest_run_id !== runId
          ) {
            return false;
          }
          if (
            isFirstPage &&
            (targetTurnIdx === null || latestTurnInBatch === targetTurnIdx) &&
            (tipInterruptDelivered || tipInterruptDeliveryFailed)
          ) {
            terminalEmptyOutcome = "interrupted";
            return true;
          }
          if (
            (targetTurnIdx !== null && stoppedTurnsInBatch.has(targetTurnIdx)) ||
            (targetRunId !== null && stoppedRunsInBatch.has(targetRunId))
          ) {
            terminalEmptyOutcome = "cancelled";
            return true;
          }
          if (
            (targetTurnIdx !== null && erroredTurnsInBatch.has(targetTurnIdx)) ||
            (targetRunId !== null && erroredRunsInBatch.has(targetRunId))
          ) {
            terminalEmptyOutcome = "error";
            terminalEmptyErrorText =
              (targetTurnIdx !== null
                ? erroredTurnsInBatch.get(targetTurnIdx)
                : null) ||
              (targetRunId !== null
                ? erroredRunsInBatch.get(targetRunId)
                : null) ||
              null;
            return true;
          }
          const hasTerminalUserMsg =
            turnUserMsgRunId !== null &&
            turnUserMsgCompletedAt !== null &&
            (!runId || turnUserMsgRunId === runId);
          if (!hasTerminalUserMsg && !isFirstPage) {
            return false;
          }
          const liveStatus = isFirstPage ? await getPageLiveStatus() : null;
          if (liveStatus) {
            const statusStr = String(liveStatus.status ?? "");
            if (PUBLIC_LIVE_STATUSES.has(statusStr)) {
              return false;
            }
            const liveRunIdMatches =
              !runId || !liveStatus.run_id || liveStatus.run_id === runId;
            if (statusStr === "interrupted" && liveRunIdMatches) {
              if (
                scopeKey &&
                !tipInterruptDelivered &&
                !tipInterruptDeliveryFailed
              ) {
                const interruptRunId =
                  targetRunId ??
                  liveStatus.run_id ??
                  turnUserMsgRunId ??
                  replayDoneMeta.latest_run_id ??
                  "unknown";
                const syntheticPayload: Record<string, unknown> =
                  pendingTipInterrupt ?? {
                    interrupt_id:
                      liveStatus.progress?.checkpoint_id ??
                      interruptRunId ??
                      "interrupt",
                    run_id: interruptRunId,
                    thread_id: threadId,
                  };
                try {
                  const intOk = await this.handleInterruptEvent({
                    payload: syntheticPayload,
                    userId,
                    threadId,
                    runId: interruptRunId,
                    turnIndex:
                      targetTurnIdx ??
                      liveStatus.latest_turn_index ??
                      liveStatus.turn_index ??
                      replayDoneMeta.latest_turn_index ??
                      latestTurnInBatch ??
                      turnIndex,
                    platform,
                    targetAddress,
                    scopeKey,
                    inboundMessageId: inboundMessageId ?? undefined,
                  });
                  if (intOk) {
                    tipInterruptDelivered = true;
                  } else {
                    tipInterruptDeliveryFailed = true;
                  }
                } catch {
                  tipInterruptDeliveryFailed = true;
                }
              }
              terminalEmptyOutcome = "interrupted";
              return true;
            }
            if (statusStr === "cancelled" && liveRunIdMatches) {
              terminalEmptyOutcome = "cancelled";
              return true;
            }
            if (
              (statusStr === "error" || statusStr === "failed") &&
              liveRunIdMatches
            ) {
              terminalEmptyOutcome = "error";
              return true;
            }
          }
          if (hasTerminalUserMsg) {
            terminalEmptyOutcome = "completed";
            return true;
          }
          return false;
        };

        // Target turn matching:
        // 1. If `turnIndex` is explicitly specified, ONLY match that `turnIndex` — and ONLY if:
        //    (a) it has settled in this page,
        //    (b) when `runId` is specified, `user_message.run_id` (or the turn's `run_id`) matches the expected `runId`
        //        (never settling on an earlier failed attempt's `run_id` during an in-progress `/retry`!), and
        //    (c) non-empty assistant text was produced for the turn (or a tip `interrupt` / terminal empty outcome was resolved for it)!
        if (turnIndex !== null && turnIndex !== undefined) {
          const turnUserMsgRunId = userMsgRunIdByTurn.get(turnIndex) ?? null;
          const turnRunIdMatches =
            !runId ||
            (turnUserMsgRunId !== null
              ? turnUserMsgRunId === runId
              : turnToRunId.get(turnIndex) === runId);
          const candidateText = textsByTurn.get(turnIndex) ?? "";
          const isTipTurnWithHandledInterrupt =
            isFirstPage &&
            latestTurnInBatch === turnIndex &&
            (tipInterruptDelivered || tipInterruptDeliveryFailed);
          if (
            settledTurnsInBatch.has(turnIndex) &&
            turnRunIdMatches &&
            (candidateText.trim().length > 0 || isTipTurnWithHandledInterrupt)
          ) {
            foundText = candidateText;
            foundTargetTurn = true;
            if (isTipTurnWithHandledInterrupt && candidateText.trim().length === 0) {
              terminalEmptyOutcome = "interrupted";
            }
            if (!resolvedRunId) {
              if (turnToRunId.has(turnIndex)) {
                resolvedRunId = turnToRunId.get(turnIndex) ?? null;
              } else if (
                isFirstPage &&
                latestTurnInBatch === turnIndex &&
                replayDoneMeta.latest_run_id
              ) {
                resolvedRunId = replayDoneMeta.latest_run_id;
              }
            }
            break;
          }
          // Before marking `sawUnsettledTargetTurn = true`, check whether the backend has already given
          // a terminal outcome for this attempt (`items.py:56-60` `TERMINAL_STATUSES`, `stopped.py:45-54`, or `/status`).
          if (seenTurnsInBatch.has(turnIndex)) {
            const candidateRunId =
              resolvedRunId ??
              turnToRunId.get(turnIndex) ??
              (isFirstPage && latestTurnInBatch === turnIndex
                ? (replayDoneMeta.latest_run_id ?? null)
                : null);
            if (
              turnRunIdMatches &&
              candidateText.trim().length === 0 &&
              (await resolveTerminalEmptyTurn(turnIndex, candidateRunId))
            ) {
              foundText = "";
              foundTargetTurn = true;
              if (!resolvedRunId && candidateRunId) {
                resolvedRunId = candidateRunId;
              }
              break;
            }
            sawUnsettledTargetTurn = true;
            break;
          }
        } else if (runId) {
          const runCandidateText = textsByRun.get(runId) ?? "";
          if (
            settledRunsInBatch.has(runId) &&
            runCandidateText.trim().length > 0
          ) {
            foundText = runCandidateText;
            foundTargetTurn = true;
            break;
          }
          // When `turnIndex` was null and `replay` chunks don't carry `run_id`/`response_id`, match the single latest settled turn on the first page
          // ONLY if its `user_message.run_id` (and `replay_done.latest_run_id`) matches `runId` and its text is non-empty!
          if (
            isFirstPage &&
            latestTurnInBatch !== null &&
            settledTurnsInBatch.has(latestTurnInBatch) &&
            (userMsgRunIdByTurn.get(latestTurnInBatch) ?? null) === runId &&
            (!replayDoneMeta.latest_run_id ||
              replayDoneMeta.latest_run_id === runId) &&
            (textsByTurn.get(latestTurnInBatch) ?? "").trim().length > 0
          ) {
            foundText = textsByTurn.get(latestTurnInBatch) ?? "";
            foundTargetTurn = true;
            break;
          }
          const runCandidateTurnIdx =
            isFirstPage &&
            latestTurnInBatch !== null &&
            ((userMsgRunIdByTurn.get(latestTurnInBatch) ?? null) === runId ||
              turnToRunId.get(latestTurnInBatch) === runId)
              ? latestTurnInBatch
              : null;
          if (
            seenRunsInBatch.has(runId) ||
            (isFirstPage && replayDoneMeta.latest_run_id === runId) ||
            (isFirstPage &&
              latestTurnInBatch !== null &&
              !settledTurnsInBatch.has(latestTurnInBatch))
          ) {
            if (await resolveTerminalEmptyTurn(runCandidateTurnIdx, runId)) {
              foundText = "";
              foundTargetTurn = true;
              resolvedRunId = runId;
              break;
            }
            sawUnsettledTargetTurn = true;
            break;
          }
        } else {
          // Neither `turnIndex` nor `runId` specified: take latest settled turn on first page
          if (
            latestTurnInBatch !== null &&
            settledTurnsInBatch.has(latestTurnInBatch) &&
            (textsByTurn.get(latestTurnInBatch) ?? "").trim().length > 0
          ) {
            foundText = textsByTurn.get(latestTurnInBatch) ?? "";
            foundTargetTurn = true;
            if (!resolvedRunId) {
              if (turnToRunId.has(latestTurnInBatch)) {
                resolvedRunId = turnToRunId.get(latestTurnInBatch) ?? null;
              } else if (replayDoneMeta.latest_run_id) {
                resolvedRunId = replayDoneMeta.latest_run_id;
              }
            }
            break;
          }
          if (lastTurnText.trim().length > 0 && textsByTurn.size === 0) {
            foundText = lastTurnText;
            foundTargetTurn = true;
            if (!resolvedRunId && replayDoneMeta.latest_run_id) {
              resolvedRunId = replayDoneMeta.latest_run_id;
            }
            break;
          }
          if (
            latestTurnInBatch !== null &&
            (!settledTurnsInBatch.has(latestTurnInBatch) ||
              (textsByTurn.get(latestTurnInBatch) ?? "").trim().length === 0)
          ) {
            const latestCandidateRunId =
              turnToRunId.get(latestTurnInBatch) ??
              replayDoneMeta.latest_run_id ??
              null;
            if (
              await resolveTerminalEmptyTurn(
                latestTurnInBatch,
                latestCandidateRunId,
              )
            ) {
              foundText = "";
              foundTargetTurn = true;
              if (!resolvedRunId && latestCandidateRunId) {
                resolvedRunId = latestCandidateRunId;
              }
              break;
            }
            sawUnsettledTargetTurn = true;
            break;
          }
        }

        const nextCursor =
          replayDoneMeta.oldest_turn_index ?? earliestTurnInBatch;
        if (
          !replayDoneMeta.has_more ||
          nextCursor === null ||
          nextCursor <= 0 ||
          (beforeTurn !== undefined && nextCursor >= beforeTurn)
        ) {
          break;
        }

        beforeTurn = nextCursor;
      }

      if (foundTargetTurn || !anyPageSucceeded) {
        break;
      }

      // If the target turn was seen as an in-flight stub (e.g. `messaging.py:1256 -> 1289` race where the turn completed
      // between replay page generation and `build_thread_snapshot`, or attached stream broke before `run_end`),
      // keep polling `/messages/replay` with backoff up to `busyThreadMaxWaitMs` until it settles!
      const canContinueWaiting =
        !this.shuttingDown &&
        getReplayElapsedMs() < this.busyThreadMaxWaitMs &&
        (sawUnsettledTargetTurn || unsettledPass < BACKOFF_DELAYS_MS.length);
      if (!canContinueWaiting) {
        break;
      }

      if (!immediateReplayAfterLiveRunEnd) {
        const passDelayMs =
          BACKOFF_DELAYS_MS[
            Math.min(unsettledPass, BACKOFF_DELAYS_MS.length - 1)
          ]!;
        unsettledPass++;
        await this.backoffDelayFn(passDelayMs);
        accumulatedReplaySleepMs += passDelayMs;
      }
    }

    // If `/messages/replay` failed to open an SSE stream, emitted `replay_unavailable`,
    // ended without `replay_done`, or did not contain a settled target turn yet, do NOT clear `fallback_pending`.
    if (!anyPageSucceeded || !foundTargetTurn) {
      return false;
    }

    // Check `sent_to_current_count` and `in_flight` / `unknown` guard via `I-7 inbound/progress`
    const prog = await this.backend.updateInboundProgress({
      ...(inboundMessageId ? { platform, message_id: inboundMessageId } : {}),
      run_id: resolvedRunId,
      thread_id: threadId,
    });

    if (
      prog.sent_to_current_count === 0 &&
      prog.has_in_flight_or_unknown_current_text
    ) {
      return false;
    }

    const fallbackEntryKey =
      inboundMessageId ??
      resolvedRunId ??
      `${threadId}:turn:${turnIndex ?? "latest"}`;
    const registerWatchPendingFallback = () => {
      const watchHandle = this.activeWatches.get(threadId);
      if (watchHandle) {
        if (!watchHandle.pendingReplayFallbacks) {
          watchHandle.pendingReplayFallbacks = new Map<
            string,
            PendingReplayFallbackEntry
          >();
        }
        watchHandle.pendingReplayFallbacks.set(fallbackEntryKey, {
          userId,
          threadId,
          runId: resolvedRunId,
          turnIndex,
          platform,
          targetAddress,
          inboundMessageId,
          scopeKey,
          isBackgroundRun,
        });
        watchHandle.needsReconnectSync = true;
      }
    };
    const clearWatchPendingFallback = () => {
      const watchHandle = this.activeWatches.get(threadId);
      watchHandle?.pendingReplayFallbacks?.delete(fallbackEntryKey);
    };

    const trimmed = foundText.trim();
    if (trimmed.length === 0) {
      if (
        terminalEmptyOutcome === "interrupted" ||
        tipInterruptDelivered ||
        tipInterruptDeliveryFailed
      ) {
        if (tipInterruptDeliveryFailed || !tipInterruptDelivered) {
          if (inboundMessageId) {
            await this.backend.updateInboundProgress({
              platform,
              message_id: inboundMessageId,
              status: "completed",
              fallback_pending: true,
            });
          }
          if (isBackgroundRun && resolvedRunId) {
            await this.backend.updateInboundProgress({
              background_run: {
                run_id: resolvedRunId,
                thread_id: threadId,
                dalton_user_id: userId,
                platform,
                target_address: targetAddress,
                turn_index: turnIndex,
                status: "completed",
                fallback_pending: true,
              },
            });
          }
          registerWatchPendingFallback();
          return false;
        }
      } else if (
        terminalEmptyOutcome === "cancelled" ||
        terminalEmptyOutcome === "error"
      ) {
        if (prog.sent_to_current_count === 0) {
          const noticeText =
            terminalEmptyOutcome === "cancelled"
              ? 'The run ended with status "cancelled".'
              : terminalEmptyErrorText ||
                "An error occurred while processing your request.";
          const noticeKeyId =
            resolvedRunId ?? `${threadId}:turn:${turnIndex ?? "latest"}`;
          const noticeOk = await this.sendNoticeDelivery({
            deliveryKey: `notice:terminal_empty:${noticeKeyId}:${terminalEmptyOutcome}`,
            userId,
            threadId,
            runId: resolvedRunId,
            platform,
            target: targetAddress,
            inboundMessageId: inboundMessageId ?? null,
            text: noticeText,
            threadScopeKey: scopeKey ?? null,
          });
          if (!noticeOk) {
            if (inboundMessageId) {
              await this.backend.updateInboundProgress({
                platform,
                message_id: inboundMessageId,
                status: "failed",
                fallback_pending: true,
              });
            }
            if (isBackgroundRun && resolvedRunId) {
              await this.backend.updateInboundProgress({
                background_run: {
                  run_id: resolvedRunId,
                  thread_id: threadId,
                  dalton_user_id: userId,
                  platform,
                  target_address: targetAddress,
                  turn_index: turnIndex,
                  status: "failed",
                  fallback_pending: true,
                },
              });
            }
            registerWatchPendingFallback();
            return false;
          }
        }
        clearWatchPendingFallback();
        if (deferFinalCompletion) {
          return true;
        }
        if (inboundMessageId) {
          await this.backend.updateInboundProgress({
            platform,
            message_id: inboundMessageId,
            status: "failed",
            fallback_pending: false,
          });
        }
        if (isBackgroundRun && resolvedRunId) {
          await this.backend.updateInboundProgress({
            background_run: {
              run_id: resolvedRunId,
              thread_id: threadId,
              dalton_user_id: userId,
              platform,
              target_address: targetAddress,
              turn_index: turnIndex,
              status: "failed",
              fallback_pending: false,
            },
          });
        }
        return true;
      } else if (
        terminalEmptyOutcome === "completed" &&
        !prog.has_in_flight_or_unknown_current_text
      ) {
        // Turn completed in terminal state (`user_message.run_id` + `run_completed_at`, `items.py:56-60`)
        // with no assistant text in replay — per §1.3.3, there is no fallback text to deliver, so settle
        // as `completed, fallback_pending: false` instead of leaving `fallback_pending: true` across restarts.
      } else {
        return false;
      }
    }

    const fallbackDeliveryKey = resolvedRunId
      ? `fallback:${resolvedRunId}`
      : `fallback:${threadId}:turn:${turnIndex}`;
    const partialNoticeKeyId =
      resolvedRunId ?? `${threadId}:turn:${turnIndex ?? "latest"}`;
    const canSettleFallback =
      foundTargetTurn &&
      trimmed.length > 0 &&
      (resolvedRunId !== null ||
        (turnIndex !== null && turnIndex !== undefined)) &&
      !prog.has_in_flight_or_unknown_current_text;

    let fallbackTextDeliveryFailed = false;
    if (canSettleFallback) {
      const settledOk = await this.settleTurnFallbackAndPartialNotice({
        fallbackDeliveryKey,
        partialNoticeKeyId,
        userId,
        threadId,
        runId: resolvedRunId,
        platform,
        targetAddress,
        inboundMessageId: inboundMessageId ?? null,
        threadScopeKey: scopeKey ?? null,
        trimmedText: trimmed,
        sentToCurrentCount: prog.sent_to_current_count,
      });
      if (!settledOk) {
        fallbackTextDeliveryFailed = true;
      }
    }

    if (fallbackTextDeliveryFailed || tipInterruptDeliveryFailed) {
      if (inboundMessageId) {
        await this.backend.updateInboundProgress({
          platform,
          message_id: inboundMessageId,
          status: "completed",
          fallback_pending: true,
        });
      }
      if ((isBackgroundRun || !inboundMessageId) && resolvedRunId) {
        await this.backend.updateInboundProgress({
          background_run: {
            run_id: resolvedRunId,
            thread_id: threadId,
            dalton_user_id: userId,
            platform,
            target_address: targetAddress,
            turn_index: turnIndex,
            status: "completed",
            fallback_pending: true,
          },
        });
      }
      registerWatchPendingFallback();
      return false;
    }

    clearWatchPendingFallback();
    if (deferFinalCompletion) {
      return true;
    }

    if (inboundMessageId) {
      await this.backend.updateInboundProgress({
        platform,
        message_id: inboundMessageId,
        status: "completed",
        fallback_pending: false,
      });
    }
    if ((isBackgroundRun || !inboundMessageId) && resolvedRunId) {
      await this.backend.updateInboundProgress({
        background_run: {
          run_id: resolvedRunId,
          thread_id: threadId,
          dalton_user_id: userId,
          platform,
          target_address: targetAddress,
          turn_index: turnIndex,
          status: "completed",
          fallback_pending: false,
        },
      });
    }
    return true;
  }

  private async consumeInFlightReplayRunStream(params: {
    userId: string;
    threadId: string;
    runId: string;
    lastEventId: string | null;
    preCursorText?: string;
    turnIndex: number | null;
    platform: ChannelPlatform;
    targetAddress: string;
    inboundMessageId?: string | null;
    scopeKey?: string | null;
  }): Promise<{ settled: boolean; runEnded: boolean; assistantText: string }> {
    try {
      const streamResp = await this.backend.streamThreadMessages(
        params.userId,
        params.threadId,
        { runId: params.runId, lastEventId: params.lastEventId },
      );
      if (!streamResp.isSse || !streamResp.events) {
        return { settled: false, runEnded: false, assistantText: "" };
      }

      const preCursorText = params.preCursorText ?? "";
      let currentSegmentText = "";
      let lastCompletedSegmentText = "";
      let sawPostCursorToolCalls = false;
      let sawStreamGap = false;
      let sawRunEnd = false;
      let sawInterrupt = false;
      let interruptDelivered = true;
      let runEndOutcome = "completed";

      for await (const sse of streamResp.events) {
        if (sse.event === ":comment") continue;
        const payload = this.safeParseJson(sse.data);

        if (sse.event === "stream_gap" || sse.event === "error") {
          sawStreamGap = true;
          break;
        }

        if (sse.event === "tool_calls") {
          if (this.isMainAgentLane(payload.agent)) {
            sawPostCursorToolCalls = true;
            if (currentSegmentText.trim().length > 0) {
              lastCompletedSegmentText = currentSegmentText;
              currentSegmentText = "";
            }
          }
          continue;
        }

        if (
          sse.event === "message_chunk" ||
          sse.event === "text" ||
          sse.event === "content_block_delta"
        ) {
          if (!this.isMainAgentLane(payload.agent)) {
            continue;
          }
          if (
            typeof payload.content_type === "string" &&
            payload.content_type !== "text"
          ) {
            continue;
          }
          const delta =
            typeof payload.content === "string"
              ? payload.content
              : typeof payload.text === "string"
                ? payload.text
                : typeof payload.delta === "string"
                  ? payload.delta
                  : "";
          if (delta && payload.role !== "user") {
            currentSegmentText += delta;
          }
          if (payload.finish_reason === "tool_calls") {
            sawPostCursorToolCalls = true;
            if (currentSegmentText.trim().length > 0) {
              lastCompletedSegmentText = currentSegmentText;
              currentSegmentText = "";
            }
          }
          continue;
        }

        if (sse.event === "message") {
          if (
            this.isMainAgentLane(payload.agent) &&
            payload.role === "assistant" &&
            typeof payload.content === "string"
          ) {
            currentSegmentText = payload.content;
          }
          continue;
        }

        if (sse.event === "interrupt") {
          sawInterrupt = true;
          if (params.scopeKey) {
            interruptDelivered = await this.handleInterruptEvent({
              payload,
              userId: params.userId,
              threadId: params.threadId,
              runId: params.runId,
              turnIndex: params.turnIndex,
              platform: params.platform,
              targetAddress: params.targetAddress,
              scopeKey: params.scopeKey,
              inboundMessageId: params.inboundMessageId ?? undefined,
            });
          }
          continue;
        }

        if (sse.event === "run_end") {
          sawRunEnd = true;
          runEndOutcome =
            typeof payload.outcome === "string"
              ? payload.outcome
              : typeof payload.status === "string"
                ? payload.status
                : "completed";
          continue;
        }
      }

      if (sawInterrupt) {
        return {
          settled: interruptDelivered,
          runEnded: true,
          assistantText: "",
        };
      }
      if (sawRunEnd && runEndOutcome === "completed") {
        const postCursorText =
          currentSegmentText.trim().length > 0
            ? currentSegmentText
            : lastCompletedSegmentText;
        // Backend `src/server/handlers/chat/run_stream_reader.py:183` only yields events strictly AFTER `last_event_id`
        // (`cursor = f"{last_event_id}-0"`), and `src/server/services/history/replay/stubs.py:88` emits `archived = []`
        // for an in-flight turn (`open_run`). If we attached with a non-null cursor `lastEventId` and `preCursorText` was
        // not present in the replay page (unless a post-cursor `tool_calls` started a brand-new final segment with non-empty
        // post-tool text), or if `sawStreamGap` occurred, or if the combined text is empty, we must NOT deliver only the
        // tail segment or treat empty text as settled! Return `settled: false, runEnded: true` so `deliverReplayFallback`
        // immediately re-polls `/messages/replay` now that the turn has completed and committed its full checkpoint.
        const missingPreCursorPrefix =
          params.lastEventId !== null &&
          preCursorText.length === 0 &&
          !(sawPostCursorToolCalls && currentSegmentText.trim().length > 0);
        const combinedText = sawPostCursorToolCalls
          ? postCursorText
          : `${preCursorText}${postCursorText}`;
        if (
          sawStreamGap ||
          missingPreCursorPrefix ||
          combinedText.trim().length === 0
        ) {
          return { settled: false, runEnded: true, assistantText: "" };
        }
        return { settled: true, runEnded: true, assistantText: combinedText };
      }
      return { settled: false, runEnded: sawRunEnd, assistantText: "" };
    } catch {
      return { settled: false, runEnded: false, assistantText: "" };
    }
  }

  private async handleInterruptEvent(params: {
    payload: Record<string, unknown>;
    userId: string;
    threadId: string;
    runId: string;
    turnIndex: number | null;
    platform: ChannelPlatform;
    targetAddress: string;
    scopeKey: string;
    inboundMessageId?: string;
  }): Promise<boolean> {
    const {
      payload,
      userId,
      threadId,
      runId,
      turnIndex,
      platform,
      targetAddress,
      scopeKey,
      inboundMessageId,
    } = params;

    const interruptId =
      typeof payload.interrupt_id === "string"
        ? payload.interrupt_id
        : typeof payload.id === "string"
          ? payload.id
          : "default";

    const actionRequests = Array.isArray(payload.action_requests)
      ? (payload.action_requests as InterruptActionRequest[])
      : [];
    const firstAction = actionRequests[0] ?? {};
    const firstActionArgs =
      firstAction.args && typeof firstAction.args === "object"
        ? firstAction.args
        : {};

    const rawKind =
      typeof payload.kind === "string"
        ? payload.kind
        : typeof payload.action_type === "string"
          ? payload.action_type
          : typeof payload.type === "string"
            ? payload.type
            : typeof firstAction.type === "string"
              ? firstAction.type
              : typeof firstAction.action === "string"
                ? firstAction.action
                : typeof firstAction.name === "string"
                  ? firstAction.name
                  : typeof firstAction.tool === "string"
                    ? firstAction.tool
                    : "ask_user_question";

    const normKind = rawKind.toLowerCase();
    const rawQuestions = Array.isArray(payload.questions)
      ? (payload.questions as AskUserQuestionItem[])
      : Array.isArray(firstAction.questions)
        ? (firstAction.questions as AskUserQuestionItem[])
        : Array.isArray(firstActionArgs.questions)
          ? (firstActionArgs.questions as AskUserQuestionItem[])
          : undefined;
    const extractedQuestions = this.extractQuestionsFromInterrupt({
      questions: rawQuestions,
      action_requests: actionRequests,
    });

    let kind: PendingInterruptState["kind"] = "ask_user_question";
    if (normKind.includes("credit") || normKind === "insufficient_credits") {
      kind = "credit_pause";
    } else if (
      normKind === "ask_user_question" ||
      normKind === "askuserquestion" ||
      normKind.includes("ask_user") ||
      normKind === "question"
    ) {
      kind = "ask_user_question";
    } else if (
      normKind === "web_only_approval" ||
      normKind.includes("plan") ||
      normKind.includes("oauth") ||
      normKind.includes("dangerous") ||
      normKind.includes("approval") ||
      normKind.includes("order") ||
      normKind.includes("trade")
    ) {
      kind = "web_only_approval";
    } else if (extractedQuestions && extractedQuestions.length > 0) {
      kind = "ask_user_question";
    } else if (
      actionRequests.length > 0 &&
      (firstAction.name || firstAction.tool || firstAction.type || firstAction.action)
    ) {
      kind = "web_only_approval";
    }

    const promptTextFromPayload =
      typeof payload.prompt === "string"
        ? payload.prompt
        : typeof firstAction.message === "string"
          ? firstAction.message
          : typeof firstAction.prompt === "string"
            ? firstAction.prompt
            : typeof firstAction.description === "string"
              ? firstAction.description
              : typeof firstActionArgs.prompt === "string"
                ? firstActionArgs.prompt
                : typeof firstActionArgs.question === "string"
                  ? firstActionArgs.question
                  : typeof firstAction.question === "string"
                    ? firstAction.question
                    : undefined;

    const pendingState: PendingInterruptState = {
      interrupt_id: interruptId,
      kind,
      category: kind,
      thread_id: threadId,
      run_id: runId,
      turn_index: turnIndex,
      action_type:
        typeof payload.action_type === "string" ? payload.action_type : rawKind,
      ...(actionRequests.length > 0 ? { action_requests: actionRequests } : {}),
      questions: extractedQuestions,
      prompt: promptTextFromPayload,
    };

    await this.backend.updateInboundProgress({
      session_interrupt_update: {
        platform,
        scope_key: scopeKey,
        dalton_user_id: userId,
        pending_interrupt: pendingState,
      },
    });

    const promptText = this.formatInterruptPrompt(pendingState);
    const deliveryRes = await this.deliveryService.executeDelivery({
      deliveryKey: `interrupt:${runId}:${interruptId}`,
      kind: "interrupt",
      userId,
      threadId,
      runId,
      platform,
      target: targetAddress,
      inboundMessageId: inboundMessageId ?? null,
      text: promptText,
      threadScopeKey: scopeKey,
    });
    return deliveryRes.status === "sent" || deliveryRes.status === "partial";
  }

  private formatInterruptPrompt(state: PendingInterruptState): string {
    if (state.kind === "web_only_approval") {
      return `Action requires confirmation in the web UI (${state.action_type ?? "approval"}): ${this.webBaseUrl}/chat/t/${state.thread_id}`;
    }
    if (state.kind === "credit_pause") {
      return (
        state.prompt ||
        "This run is paused due to credit limits. Reply `continue` (or send additional instructions) after topping up credits to resume."
      );
    }
    if (state.questions && state.questions.length > 0) {
      const lines: string[] = [];
      for (const q of state.questions) {
        lines.push(q.question);
        (q.options ?? []).forEach((opt, idx) => {
          lines.push(`${idx + 1}. ${opt.label}`);
        });
      }
      lines.push("Reply with an option number, your answer, or `skip`.");
      return lines.join("\n");
    }
    return state.prompt || "Please reply to continue, or reply `skip`.";
  }

  private async attachToLiveStreamOrReplay(params: {
    userId: string;
    threadId: string;
    runId: string | null;
    turnIndex: number | null;
    event: InboundChannelEvent;
    resolved: I1RegisterAndResolveResponse;
    lastEventId?: string | null;
    deferFinalCompletion?: boolean;
  }): Promise<boolean> {
    const {
      userId,
      threadId,
      runId,
      turnIndex,
      event,
      resolved,
      lastEventId,
      deferFinalCompletion,
    } = params;
    const streamResp = await this.backend.streamThreadMessages(
      userId,
      threadId,
      { runId, lastEventId },
    );
    if (streamResp.isSse && streamResp.events) {
      return await this.consumeTurnSseStream({
        events: streamResp.events,
        event,
        userId,
        resolved: {
          ...resolved,
          thread_id: threadId,
          run_id: runId,
          turn_index: turnIndex,
        },
        threadId,
        requestKey: resolved.request_key,
        retryAttempt: 0,
        isAttachedStream: true,
        attachedWithCursor: Boolean(lastEventId && lastEventId !== "0"),
        deferFinalCompletion,
      });
    }

    return await this.deliverReplayFallback({
      userId,
      threadId,
      runId,
      turnIndex,
      platform: event.platform,
      targetAddress: event.chatAddress,
      inboundMessageId: event.messageId,
      scopeKey: event.threadScopeKey,
      deferFinalCompletion,
    });
  }

  // =========================================================================
  // §1.3.4 `/watch` Lifecycle, 4 `workflow_started` Shapes & 7-Condition Close
  // =========================================================================

  ensureThreadWatch(params: {
    threadId: string;
    userId: string;
    platform: ChannelPlatform;
    targetAddress: string;
    scopeKey: string;
    generation: number;
    lastSeenTurnIndex: number;
  }): ActiveWatchHandle {
    const existing = this.activeWatches.get(params.threadId);
    if (existing) {
      existing.targetAddress = params.targetAddress;
      existing.scopeKey = params.scopeKey;
      existing.generation = params.generation;
      if (params.lastSeenTurnIndex > existing.lastSeenTurnIndex) {
        existing.lastSeenTurnIndex = params.lastSeenTurnIndex;
      }
      return existing;
    }

    const abortController = new AbortController();
    const handle: ActiveWatchHandle = {
      ...params,
      abortController,
      promise: Promise.resolve(),
      needsInputSeqByThread: new Map<string, number>(),
      lastNeedsInputTokenByThread: new Map<string, string>(),
      pendingNeedsInputThreads: new Map<string, PendingNeedsInputNotice>(),
    };
    handle.promise = this.runWatchLoop(handle);
    this.activeWatches.set(params.threadId, handle);
    return handle;
  }

  private async terminateDeletedThreadWatch(
    threadId: string,
    explicitHandle?: ActiveWatchHandle,
  ): Promise<void> {
    const handle = explicitHandle ?? this.activeWatches.get(threadId);
    if (handle) {
      handle.abortController.abort();
      this.activeWatches.delete(threadId);
      try {
        await this.backend.updateInboundProgress({
          thread_watch: {
            thread_id: threadId,
            dalton_user_id: handle.userId,
            platform: handle.platform,
            target_address: handle.targetAddress,
            scope_key: handle.scopeKey,
            generation: handle.generation,
            last_seen_turn_index: handle.lastSeenTurnIndex,
            watch_active: false,
          },
        });
      } catch {
        // Ignore progress update failure if thread row was deleted
      }
    }
  }

  private async runWatchLoop(handle: ActiveWatchHandle): Promise<void> {
    let consecutiveFailures = 0;
    try {
      while (!handle.abortController.signal.aborted) {
        let sawNonErrorEvent = false;
        try {
          const resp = await this.backend.watchThread(
            handle.userId,
            handle.threadId,
            handle.abortController.signal,
          );
          if (handle.abortController.signal.aborted) {
            break;
          }
          // If `/watch` returns HTTP 404 (`src/server/app/threads/messaging.py:1357`), the thread was deleted — stop watching permanently!
          if (resp.status === 404) {
            await this.terminateDeletedThreadWatch(handle.threadId, handle);
            break;
          }
          if (resp.isSse && resp.events) {
            for await (const sse of resp.events) {
              if (handle.abortController.signal.aborted) break;
              if (sse.event === ":comment") continue;
              if (sse.event === "error") {
                break;
              }
              sawNonErrorEvent = true;
              consecutiveFailures = 0;
              const data = this.safeParseJson(sse.data);

              if (sse.event === "watch_snapshot") {
                const snapTurnIdx =
                  typeof data.latest_turn_index === "number"
                    ? data.latest_turn_index
                    : typeof data.turn_index === "number"
                      ? data.turn_index
                      : null;
                if (Array.isArray(data.recent_report_back_run_ids)) {
                  for (const item of data.recent_report_back_run_ids) {
                    if (
                      typeof item === "string" &&
                      item.length > 0 &&
                      item !== data.report_back_run_id
                    ) {
                      const recentSettled = await this.deliverReplayFallback({
                        userId: handle.userId,
                        threadId: handle.threadId,
                        runId: item,
                        turnIndex: null,
                        platform: handle.platform,
                        targetAddress: handle.targetAddress,
                        scopeKey: handle.scopeKey,
                        isBackgroundRun: true,
                      });
                      if (!recentSettled) {
                        handle.needsReconnectSync = true;
                      }
                    }
                  }
                }
                if (
                  Array.isArray(data.owed_threads) &&
                  data.owed_threads.length > 0
                ) {
                  const owedSettled =
                    await this.reconcileOwedThreadsNeedsInput(
                      handle,
                      data.owed_threads,
                    );
                  if (!owedSettled) {
                    handle.needsReconnectSync = true;
                  }
                }
                if (
                  typeof data.report_back_run_id === "string" &&
                  data.report_back_run_id.length > 0
                ) {
                  if (
                    snapTurnIdx !== null &&
                    snapTurnIdx > handle.lastSeenTurnIndex
                  ) {
                    handle.lastSeenTurnIndex = snapTurnIdx;
                  }
                  await this.handleWorkflowStartedEvent(handle, {
                    thread_id: handle.threadId,
                    run_id: data.report_back_run_id,
                    status: "running",
                  });
                } else if (data.pending_report_back === false) {
                  let snapReconnectOk = true;
                  if (
                    (snapTurnIdx !== null &&
                      snapTurnIdx > handle.lastSeenTurnIndex) ||
                    handle.needsReconnectSync
                  ) {
                    try {
                      snapReconnectOk =
                        await this.syncWatchReconnectState(handle);
                    } catch (err) {
                      if (
                        err instanceof BackendHttpError &&
                        err.status === 404
                      ) {
                        await this.terminateDeletedThreadWatch(
                          handle.threadId,
                          handle,
                        );
                        break;
                      }
                      handle.needsReconnectSync = true;
                      snapReconnectOk = false;
                    }
                    if (snapReconnectOk && snapTurnIdx !== null) {
                      handle.lastSeenTurnIndex = Math.max(
                        handle.lastSeenTurnIndex,
                        snapTurnIdx,
                      );
                    }
                  }
                  if (snapReconnectOk) {
                    await this.evaluateWatchCloseConditions(
                      handle.userId,
                      handle.threadId,
                    );
                  }
                } else if (
                  snapTurnIdx !== null &&
                  snapTurnIdx > handle.lastSeenTurnIndex
                ) {
                  handle.lastSeenTurnIndex = snapTurnIdx;
                }
                continue;
              }

              if (sse.event === "workflow_started") {
                await this.handleWorkflowStartedEvent(handle, data);
                continue;
              }

              if (sse.event === "timeout") {
                break;
              }
            }
          }
        } catch (err) {
          if (err instanceof BackendHttpError && err.status === 404) {
            await this.terminateDeletedThreadWatch(handle.threadId, handle);
            break;
          }
          if (handle.abortController.signal.aborted) {
            break;
          }
        }

        if (handle.abortController.signal.aborted) {
          break;
        }

        // On empty EOF (`wake.py:128` 5s listener timeout), `event: error`, non-SSE response, or network error:
        // backoff before calling `recover?mode=watch_reconnect` and checking close conditions.
        if (!sawNonErrorEvent) {
          const delayMs =
            BACKOFF_DELAYS_MS[
              Math.min(consecutiveFailures, BACKOFF_DELAYS_MS.length - 1)
            ]!;
          consecutiveFailures++;
          await this.backoffDelayFn(delayMs);
          if (handle.abortController.signal.aborted) {
            break;
          }
        }

        // IMPORTANT (`src/dalton/database/channel_gateway.py:3575-3671`):
        // Always run `syncWatchReconnectState` (`recover?mode=watch_reconnect`) BEFORE `evaluateWatchCloseConditions`
        // so that background turns completed during the disconnect window are backfilled into `dalton_channel_background_runs`
        // and replayed/delivered before `evaluateWatchCloseConditions` sees an idle thread and sets `watch_active: false`!
        let reconnectSyncOk = false;
        try {
          reconnectSyncOk = await this.syncWatchReconnectState(handle);
        } catch (err) {
          if (err instanceof BackendHttpError && err.status === 404) {
            await this.terminateDeletedThreadWatch(handle.threadId, handle);
            break;
          }
          handle.needsReconnectSync = true;
          reconnectSyncOk = false;
        }

        if (handle.abortController.signal.aborted) {
          break;
        }

        // Never evaluate close conditions or write `watch_active: false` when `syncWatchReconnectState` failed!
        if (!reconnectSyncOk) {
          if (sawNonErrorEvent) {
            const delayMs =
              BACKOFF_DELAYS_MS[
                Math.min(consecutiveFailures, BACKOFF_DELAYS_MS.length - 1)
              ]!;
            consecutiveFailures++;
            await this.backoffDelayFn(delayMs);
          }
          continue;
        }

        // Check whether all 7 close conditions are now satisfied
        try {
          const closed = await this.evaluateWatchCloseConditions(
            handle.userId,
            handle.threadId,
          );
          if (closed || handle.abortController.signal.aborted) {
            break;
          }
        } catch {
          // Ignore transient status/wakeup error; continue watching
        }
      }
    } finally {
      if (this.activeWatches.get(handle.threadId) === handle) {
        this.activeWatches.delete(handle.threadId);
      }
    }
  }

  private extractSubStatusPauseToken(subSt: ThreadStatusInfo): string | null {
    const subRunId =
      typeof subSt.run_id === "string" && subSt.run_id.length > 0
        ? subSt.run_id
        : null;
    const subTurnIdx =
      typeof subSt.latest_turn_index === "number"
        ? subSt.latest_turn_index
        : typeof subSt.turn_index === "number"
          ? subSt.turn_index
          : null;
    const subCheckpointId =
      subSt.progress &&
      typeof subSt.progress === "object" &&
      typeof subSt.progress.checkpoint_id === "string" &&
      subSt.progress.checkpoint_id.length > 0
        ? subSt.progress.checkpoint_id
        : null;
    // When a sub-thread is `interrupted`, `/status` does not carry `run_id` (`thread_status.py:212-226`),
    // and if checkpoint reading fails, `progress` is `null` (`thread_status.py:176-192`), leaving only `latest_turn_index`.
    // Never construct a degraded turn-only token (`run:<turnIdx>`); require `subRunId` or `subCheckpointId`!
    if (subRunId || subCheckpointId) {
      return `${subRunId ?? "run"}:${subTurnIdx ?? 0}${
        subCheckpointId ? `:${subCheckpointId}` : ""
      }`;
    }
    return null;
  }

  private async computeNeedsInputDeliveryKey(
    handle: ActiveWatchHandle,
    analystThreadId: string,
    _explicitInterruptToken?: string | null,
    options?: {
      preloadedSubStatus?: ThreadStatusInfo | null;
      skipStatusLookup?: boolean;
    },
  ): Promise<string | null> {
    if (!handle.needsInputSeqByThread) {
      handle.needsInputSeqByThread = new Map<string, number>();
    }
    if (!handle.lastNeedsInputTokenByThread) {
      handle.lastNeedsInputTokenByThread = new Map<string, string>();
    }

    // Unify pause identity to a single canonical source: sub-thread `/status` (`extractSubStatusPauseToken`).
    // Never switch to `/messages/replay` (`interrupt:<id>`) or another token format when `/status` fails or
    // `progress.checkpoint_id` is temporarily missing — instead return `null` so the notice is deferred in
    // `pendingNeedsInputThreads` until `/status` provides the canonical identity.
    let subStatusToken: string | null = options?.preloadedSubStatus
      ? this.extractSubStatusPauseToken(options.preloadedSubStatus)
      : null;

    if (!subStatusToken && !options?.skipStatusLookup) {
      for (let attempt = 0; attempt <= BACKOFF_DELAYS_MS.length; attempt++) {
        if (attempt > 0) {
          await this.backoffDelayFn(BACKOFF_DELAYS_MS[attempt - 1]!);
        }
        try {
          const subSt = await this.backend.getThreadStatus(
            handle.userId,
            analystThreadId,
          );
          const extracted = this.extractSubStatusPauseToken(subSt);
          if (extracted) {
            subStatusToken = extracted;
            break;
          }
        } catch {
          // Retry with backoff when sub-thread status lookup fails
        }
      }
    }

    // Never fall back to process-local `:seq:1` or a secondary identity source when `/status` pause identity is unavailable!
    if (!subStatusToken) {
      return null;
    }

    const baseKey = `notice:needs_input:${handle.threadId}:${analystThreadId}`;
    return `${baseKey}:${subStatusToken}`;
  }

  private async deliverOrDeferNeedsInputNotice(
    handle: ActiveWatchHandle,
    analystThreadId: string,
    notice: PendingNeedsInputNotice,
    options?: {
      preloadedSubStatus?: ThreadStatusInfo | null;
      skipStatusLookup?: boolean;
    },
  ): Promise<boolean> {
    if (!handle.pendingNeedsInputThreads) {
      handle.pendingNeedsInputThreads = new Map<
        string,
        PendingNeedsInputNotice
      >();
    }
    if (!handle.lastNeedsInputTokenByThread) {
      handle.lastNeedsInputTokenByThread = new Map<string, string>();
    }
    const existingPending =
      handle.pendingNeedsInputThreads.get(analystThreadId) ?? null;
    const preservedDeliveryKey =
      typeof notice.deliveryKey === "string" && notice.deliveryKey.length > 0
        ? notice.deliveryKey
        : typeof existingPending?.deliveryKey === "string" &&
            existingPending.deliveryKey.length > 0 &&
            existingPending.explicitToken === notice.explicitToken
          ? existingPending.deliveryKey
          : null;
    const deliveryKey =
      preservedDeliveryKey ??
      (await this.computeNeedsInputDeliveryKey(
        handle,
        analystThreadId,
        notice.explicitToken,
        options,
      ));
    if (!deliveryKey) {
      handle.pendingNeedsInputThreads.set(analystThreadId, notice);
      handle.needsReconnectSync = true;
      return false;
    }
    const prefix = `notice:needs_input:${handle.threadId}:${analystThreadId}:`;
    const resolvedToken = deliveryKey.startsWith(prefix)
      ? deliveryKey.slice(prefix.length)
      : notice.resolvedToken ?? notice.explicitToken ?? null;
    const noticeWithKey: PendingNeedsInputNotice = {
      ...notice,
      deliveryKey,
      resolvedToken,
    };
    const sentOk = await this.sendNoticeDelivery({
      deliveryKey,
      userId: handle.userId,
      threadId: handle.threadId,
      platform: handle.platform,
      target: handle.targetAddress,
      text: `Analyst in ${notice.wsName}${notice.title} needs your input${notice.question} Open ${this.webBaseUrl}/chat/t/${analystThreadId}`,
      threadScopeKey: handle.scopeKey,
    });
    if (!sentOk) {
      handle.pendingNeedsInputThreads.set(analystThreadId, noticeWithKey);
      handle.needsReconnectSync = true;
      return false;
    }
    if (resolvedToken) {
      handle.lastNeedsInputTokenByThread.set(analystThreadId, resolvedToken);
    }
    handle.pendingNeedsInputThreads.delete(analystThreadId);
    return true;
  }

  private async reconcileOwedThreadsNeedsInput(
    handle: ActiveWatchHandle,
    owedThreads: unknown,
  ): Promise<boolean> {
    if (!Array.isArray(owedThreads) || owedThreads.length === 0) {
      return true;
    }
    if (!handle.pendingNeedsInputThreads) {
      handle.pendingNeedsInputThreads = new Map<
        string,
        PendingNeedsInputNotice
      >();
    }
    let allSettled = true;
    for (const rawItem of owedThreads) {
      if (!rawItem || typeof rawItem !== "object") {
        continue;
      }
      const item = rawItem as Record<string, unknown>;
      const analystThreadId =
        typeof item.thread_id === "string" && item.thread_id.length > 0
          ? item.thread_id
          : null;
      if (!analystThreadId) {
        continue;
      }
      if (handle.pendingNeedsInputThreads.has(analystThreadId)) {
        continue;
      }
      const wsName =
        typeof item.workspace_name === "string" &&
        item.workspace_name.length > 0
          ? item.workspace_name
          : "workspace";
      const title =
        typeof item.title === "string" && item.title.length > 0
          ? ` (${item.title})`
          : "";
      const candidateNotice: PendingNeedsInputNotice = {
        wsName,
        title,
        question: ".",
        explicitToken: null,
      };

      let subSt: ThreadStatusInfo | null = null;
      let statusFetchFailed = false;
      for (let attempt = 0; attempt <= BACKOFF_DELAYS_MS.length; attempt++) {
        if (attempt > 0) {
          await this.backoffDelayFn(BACKOFF_DELAYS_MS[attempt - 1]!);
        }
        try {
          subSt = await this.backend.getThreadStatus(
            handle.userId,
            analystThreadId,
          );
          statusFetchFailed = false;
          if (
            subSt.status !== "interrupted" ||
            this.extractSubStatusPauseToken(subSt) !== null
          ) {
            break;
          }
        } catch {
          statusFetchFailed = true;
        }
      }

      // Do NOT enqueue `analystThreadId` into `pendingNeedsInputThreads` when `/status` fails on all attempts:
      // we must confirm the sub-thread is actually `interrupted` (waiting for input) before treating it as `needs_input`!
      if (statusFetchFailed) {
        handle.needsReconnectSync = true;
        allSettled = false;
        continue;
      }

      if (subSt && subSt.status === "interrupted") {
        const currentPauseToken = this.extractSubStatusPauseToken(subSt);
        const lastNotifiedToken =
          handle.lastNeedsInputTokenByThread?.get(analystThreadId) ?? null;
        if (
          currentPauseToken !== null &&
          lastNotifiedToken === currentPauseToken
        ) {
          continue;
        }
        const delivered = await this.deliverOrDeferNeedsInputNotice(
          handle,
          analystThreadId,
          candidateNotice,
          { preloadedSubStatus: subSt, skipStatusLookup: true },
        );
        if (!delivered) {
          allSettled = false;
        }
      }
    }
    return allSettled;
  }

  /**
   * Handle all shapes of `event: workflow_started` (`src/server/services/report_back/handoff/wake.py:80-87` & §1.3.4):
   * 1. Active background turn (`{ thread_id, run_id }`, optional `status === "running"`).
   * 2. Directly settled turn (`settled_Directly === true`, `run_id === null`).
   * 3. Failed to start (`{ error: "background_workflow_failed" }` or `status === "error"` with `run_id === null`).
   * 4. Quiet wakeup (`quiet === true`) or consumption clear (`{ thread_id, cleared: true }`).
   * 5. Dispatched analyst/PTC thread needing HITL input (`{ needs_input: "<ptc_thread_id>" }` from `wake.py:82`, or object).
   */
  private async handleWorkflowStartedEvent(
    handle: ActiveWatchHandle,
    data: Record<string, unknown>,
  ): Promise<void> {
    const runId = typeof data.run_id === "string" ? data.run_id : null;
    const status = typeof data.status === "string" ? data.status : "running";
    const turnIndex =
      typeof data.turn_index === "number"
        ? data.turn_index
        : handle.lastSeenTurnIndex + 1;
    const settledDirectly = Boolean(
      data.settled_Directly ?? data.settled_directly,
    );
    const isQuiet = Boolean(data.quiet);
    const isCleared = Boolean(data.cleared);

    // Shape 4: Quiet wakeup turn or consumption clear (`{ thread_id, cleared: true }`)
    if (isQuiet || isCleared) {
      await this.evaluateWatchCloseConditions(handle.userId, handle.threadId);
      return;
    }

    // Shape 5: Dispatched analyst/PTC thread needing HITL input (`needs_input: "<ptc_thread_id>"` from `wake.py:82` or object)
    if (data.needs_input && !runId) {
      let noticeDelivered = false;
      if (typeof data.needs_input === "object" && data.needs_input !== null) {
        const ni = data.needs_input as Record<string, unknown>;
        const analystThreadId =
          typeof ni.thread_id === "string" && ni.thread_id.length > 0
            ? ni.thread_id
            : handle.threadId;
        const wsName =
          typeof ni.workspace_name === "string" && ni.workspace_name.length > 0
            ? ni.workspace_name
            : "workspace";
        const title =
          typeof ni.title === "string" && ni.title.length > 0
            ? ` (${ni.title})`
            : "";
        const question =
          typeof ni.question === "string" && ni.question.length > 0
            ? `: ${ni.question}`
            : ".";
        const explicitToken =
          typeof ni.interrupt_id === "string" && ni.interrupt_id.length > 0
            ? ni.interrupt_id
            : typeof ni.run_id === "string" && ni.run_id.length > 0
              ? ni.run_id
              : null;
        noticeDelivered = await this.deliverOrDeferNeedsInputNotice(
          handle,
          analystThreadId,
          {
            wsName,
            title,
            question,
            explicitToken,
          },
        );
      } else if (
        typeof data.needs_input === "string" &&
        data.needs_input.length > 0
      ) {
        const analystThreadId = data.needs_input;
        let wsName = "workspace";
        let title = "";
        try {
          const st = await this.backend.getThreadStatus(
            handle.userId,
            handle.threadId,
          );
          if (Array.isArray(st.owed_threads)) {
            const matched = st.owed_threads.find(
              (item) => item.thread_id === analystThreadId,
            );
            if (matched) {
              if (
                typeof matched.workspace_name === "string" &&
                matched.workspace_name.length > 0
              ) {
                wsName = matched.workspace_name;
              }
              if (
                typeof matched.title === "string" &&
                matched.title.length > 0
              ) {
                title = ` (${matched.title})`;
              }
            }
          }
        } catch {
          // Fall back to generic label if status lookup fails
        }
        noticeDelivered = await this.deliverOrDeferNeedsInputNotice(
          handle,
          analystThreadId,
          {
            wsName,
            title,
            question: ".",
            explicitToken: null,
          },
        );
      }
      if (noticeDelivered) {
        await this.evaluateWatchCloseConditions(handle.userId, handle.threadId);
      }
      return;
    }

    // Shape 2: Settled directly without a live run_id (`settled_Directly == true`)
    if (settledDirectly && !runId) {
      const settled = await this.deliverReplayFallback({
        userId: handle.userId,
        threadId: handle.threadId,
        runId: null,
        turnIndex,
        platform: handle.platform,
        targetAddress: handle.targetAddress,
        scopeKey: handle.scopeKey,
      });
      if (settled) {
        handle.lastSeenTurnIndex = Math.max(
          handle.lastSeenTurnIndex,
          turnIndex,
        );
        await this.evaluateWatchCloseConditions(handle.userId, handle.threadId);
      } else {
        handle.needsReconnectSync = true;
      }
      return;
    }

    // Shape 3: Error starting workflow (`{ error: "..." }` or `status === "error"` with `run_id === null`)
    if ((status === "error" || typeof data.error === "string") && !runId) {
      const errMsg =
        typeof data.error === "string"
          ? data.error
          : "A background report-back turn failed to start.";
      const noticeOk = await this.sendNoticeDelivery({
        deliveryKey: `notice:watch_err:${handle.threadId}:${turnIndex}`,
        userId: handle.userId,
        threadId: handle.threadId,
        platform: handle.platform,
        target: handle.targetAddress,
        text: errMsg,
        threadScopeKey: handle.scopeKey,
      });
      if (noticeOk) {
        await this.evaluateWatchCloseConditions(handle.userId, handle.threadId);
      } else {
        handle.needsReconnectSync = true;
      }
      return;
    }

    // Shape 1: Live background run (`run_id` present)
    if (runId) {
      this.trackBackgroundRunId(handle.threadId, runId, true);
      await this.backend.updateInboundProgress({
        background_run: {
          run_id: runId,
          thread_id: handle.threadId,
          dalton_user_id: handle.userId,
          platform: handle.platform,
          target_address: handle.targetAddress,
          turn_index: turnIndex,
          status: "running",
          fallback_pending: true,
        },
      });

      await this.consumeBackgroundRunStream({
        handle,
        runId,
        turnIndex,
      });
    }
  }

  private async consumeBackgroundRunStream(params: {
    handle: ActiveWatchHandle;
    runId: string;
    turnIndex: number;
  }): Promise<boolean> {
    const { handle, runId, turnIndex } = params;
    let currentSegmentText = "";
    let lastCompletedSegmentText = "";
    let isQuiet = false;
    let sawStreamErrorOrGap = false;
    let sawRunEnd = false;
    let sawInterrupt = false;
    let interruptDelivered = true;
    let runEndOutcome: string | null = null;
    let settledOk = false;

    try {
      const streamResp = await this.backend.streamThreadMessages(
        handle.userId,
        handle.threadId,
        { runId },
      );
      if (streamResp.isSse && streamResp.events) {
        for await (const sse of streamResp.events) {
          if (sse.event === ":comment") continue;
          const payload = this.safeParseJson(sse.data);

          if (payload.quiet === true) {
            isQuiet = true;
          }

          if (sse.event === "tool_calls") {
            if (this.isMainAgentLane(payload.agent)) {
              if (currentSegmentText.trim().length > 0) {
                lastCompletedSegmentText = currentSegmentText;
                currentSegmentText = "";
              }
            }
            continue;
          }

          if (sse.event === "stream_gap" || sse.event === "error") {
            sawStreamErrorOrGap = true;
            break;
          }

          if (sse.event === "run_end") {
            sawRunEnd = true;
            runEndOutcome =
              typeof payload.outcome === "string"
                ? payload.outcome
                : typeof payload.status === "string"
                  ? payload.status
                  : "completed";
            continue;
          }

          if (
            sse.event === "message_chunk" ||
            sse.event === "text" ||
            sse.event === "content_block_delta"
          ) {
            if (!this.isMainAgentLane(payload.agent)) {
              continue;
            }
            if (
              typeof payload.content_type === "string" &&
              payload.content_type !== "text"
            ) {
              continue;
            }
            const delta =
              typeof payload.content === "string"
                ? payload.content
                : typeof payload.text === "string"
                  ? payload.text
                  : "";
            if (delta && payload.role !== "user") {
              currentSegmentText += delta;
            }
            if (payload.finish_reason === "tool_calls") {
              if (currentSegmentText.trim().length > 0) {
                lastCompletedSegmentText = currentSegmentText;
                currentSegmentText = "";
              }
            }
          } else if (sse.event === "message") {
            if (
              this.isMainAgentLane(payload.agent) &&
              payload.role === "assistant" &&
              typeof payload.content === "string"
            ) {
              currentSegmentText = payload.content;
            }
          } else if (sse.event === "interrupt") {
            sawInterrupt = true;
            interruptDelivered = await this.handleInterruptEvent({
              payload,
              userId: handle.userId,
              threadId: handle.threadId,
              runId,
              turnIndex,
              platform: handle.platform,
              targetAddress: handle.targetAddress,
              scopeKey: handle.scopeKey,
            });
          }
        }
      }

      if (
        sawRunEnd &&
        (runEndOutcome === "error" ||
          runEndOutcome === "failed" ||
          runEndOutcome === "cancelled")
      ) {
        await this.backend.updateInboundProgress({
          background_run: {
            run_id: runId,
            thread_id: handle.threadId,
            dalton_user_id: handle.userId,
            platform: handle.platform,
            target_address: handle.targetAddress,
            turn_index: turnIndex,
            status: "failed",
            fallback_pending: false,
          },
        });
        settledOk = true;
        return true;
      }

      // Bare EOF or mid-stream error/gap without `run_end` / `interrupt`: must settle via `/messages/replay`!
      if (!sawRunEnd && !sawInterrupt) {
        if (!isQuiet) {
          const settled = await this.deliverReplayFallback({
            userId: handle.userId,
            threadId: handle.threadId,
            runId,
            turnIndex,
            platform: handle.platform,
            targetAddress: handle.targetAddress,
            scopeKey: handle.scopeKey,
            isBackgroundRun: true,
          });
          if (settled) {
            handle.lastSeenTurnIndex = Math.max(
              handle.lastSeenTurnIndex,
              turnIndex,
            );
            await this.backend.updateInboundProgress({
              thread_watch: {
                thread_id: handle.threadId,
                dalton_user_id: handle.userId,
                platform: handle.platform,
                target_address: handle.targetAddress,
                scope_key: handle.scopeKey,
                generation: handle.generation,
                last_seen_turn_index: handle.lastSeenTurnIndex,
              },
            });
            settledOk = true;
            return true;
          }
          handle.needsReconnectSync = true;
          return false;
        }
        settledOk = true;
        return true;
      }

      const assistantText =
        currentSegmentText.trim().length > 0
          ? currentSegmentText
          : lastCompletedSegmentText;

      let keepFallbackPending = sawInterrupt && !interruptDelivered;
      if (!isQuiet && !sawInterrupt) {
        const prog = await this.backend.updateInboundProgress({
          run_id: runId,
          thread_id: handle.threadId,
        });
        if (
          prog.sent_to_current_count === 0 &&
          prog.has_in_flight_or_unknown_current_text
        ) {
          keepFallbackPending = true;
        } else {
          const trimmed = assistantText.trim();
          const bgFallbackDeliveryKey = `fallback:${runId}`;
          if (
            !sawStreamErrorOrGap &&
            trimmed.length > 0 &&
            !prog.has_in_flight_or_unknown_current_text
          ) {
            const settledOk = await this.settleTurnFallbackAndPartialNotice({
              fallbackDeliveryKey: bgFallbackDeliveryKey,
              partialNoticeKeyId: runId,
              userId: handle.userId,
              threadId: handle.threadId,
              runId,
              platform: handle.platform,
              targetAddress: handle.targetAddress,
              threadScopeKey: handle.scopeKey,
              trimmedText: trimmed,
              sentToCurrentCount: prog.sent_to_current_count,
            });
            if (settledOk) {
              handle.pendingReplayFallbacks?.delete(runId);
            } else {
              keepFallbackPending = true;
              if (!handle.pendingReplayFallbacks) {
                handle.pendingReplayFallbacks = new Map<
                  string,
                  PendingReplayFallbackEntry
                >();
              }
              handle.pendingReplayFallbacks.set(runId, {
                userId: handle.userId,
                threadId: handle.threadId,
                runId,
                turnIndex,
                platform: handle.platform,
                targetAddress: handle.targetAddress,
                scopeKey: handle.scopeKey,
                isBackgroundRun: true,
              });
            }
          } else if (
            (trimmed.length === 0 || sawStreamErrorOrGap) &&
            !prog.has_in_flight_or_unknown_current_text
          ) {
            const settled = await this.deliverReplayFallback({
              userId: handle.userId,
              threadId: handle.threadId,
              runId,
              turnIndex,
              platform: handle.platform,
              targetAddress: handle.targetAddress,
              scopeKey: handle.scopeKey,
              isBackgroundRun: true,
            });
            if (!settled) {
              keepFallbackPending = true;
            }
          }
        }
      }

      if (keepFallbackPending) {
        handle.needsReconnectSync = true;
      } else {
        handle.lastSeenTurnIndex = Math.max(
          handle.lastSeenTurnIndex,
          turnIndex,
        );
      }
      await this.backend.updateInboundProgress({
        background_run: {
          run_id: runId,
          thread_id: handle.threadId,
          dalton_user_id: handle.userId,
          platform: handle.platform,
          target_address: handle.targetAddress,
          turn_index: turnIndex,
          status: "completed",
          fallback_pending: keepFallbackPending,
        },
        ...(!keepFallbackPending
          ? {
              thread_watch: {
                thread_id: handle.threadId,
                dalton_user_id: handle.userId,
                platform: handle.platform,
                target_address: handle.targetAddress,
                scope_key: handle.scopeKey,
                generation: handle.generation,
                last_seen_turn_index: handle.lastSeenTurnIndex,
              },
            }
          : {}),
      });
      settledOk = !keepFallbackPending;
      return settledOk;
    } catch (err) {
      handle.needsReconnectSync = true;
      throw err;
    } finally {
      this.trackBackgroundRunId(handle.threadId, runId, false);
      if (
        settledOk &&
        !handle.syncingReconnect &&
        !handle.needsReconnectSync
      ) {
        await this.evaluateWatchCloseConditions(handle.userId, handle.threadId);
      }
    }
  }

  /**
   * 7-Condition Close Check for `/watch` (§1.3.4 & `src/tools/thread_status/status_slice.py`):
   * 1. `status` is not in `PUBLIC_LIVE_STATUSES`, `pending_report_back !== true && pending_report_back !== null`, `pending_steers` empty
   * 2. `subagents.active_count == 0` (and `active_subagents` is empty)
   * 3. `commands.running_count == 0` (and `running_commands` is empty)
   * 4. `analysts.active_count == 0` (and `delegated_tasks` is empty)
   * 5. `automations.running_count == 0` (and `running_automations` is empty)
   * 6. `pending_settlements == 0` / `pending_settlements.count == 0`
   * 7. `GET /api/v1/threads/{thread_id}/wakeups` has no active (`armed` / `held` / `held_usage`) items
   * Plus no active `dalton_channel_background_runs` for `thread_id`.
   */
  async evaluateWatchCloseConditions(
    userId: string,
    threadId: string,
  ): Promise<boolean> {
    const activeBg = this.activeBackgroundRunIds.get(threadId);
    if (activeBg && activeBg.size > 0) {
      return false;
    }

    let st: ThreadStatusInfo;
    try {
      st = await this.backend.getThreadStatus(userId, threadId);
    } catch (err) {
      if (err instanceof BackendHttpError && err.status === 404) {
        await this.terminateDeletedThreadWatch(threadId);
        return true;
      }
      throw err;
    }

    if (st.status === "not_found") {
      await this.terminateDeletedThreadWatch(threadId);
      return true;
    }
    if (PUBLIC_LIVE_STATUSES.has(String(st.status ?? ""))) {
      return false;
    }
    if (st.pending_report_back === true || st.pending_report_back === null) {
      return false;
    }
    if (
      typeof st.report_back_run_id === "string" &&
      st.report_back_run_id.length > 0
    ) {
      return false;
    }
    if (Array.isArray(st.pending_steers) && st.pending_steers.length > 0) {
      return false;
    }
    if (
      (Array.isArray(st.active_tasks) && st.active_tasks.length > 0) ||
      (Array.isArray(st.active_commands) && st.active_commands.length > 0) ||
      (Array.isArray(st.owed_threads) && st.owed_threads.length > 0) ||
      (Array.isArray(st.owed_report_kinds) && st.owed_report_kinds.length > 0)
    ) {
      return false;
    }
    if (
      (st.subagents?.count ?? st.subagents?.active_count ?? 0) > 0 ||
      (Array.isArray(st.subagents?.items) && st.subagents.items.length > 0) ||
      (Array.isArray(st.active_subagents) && st.active_subagents.length > 0)
    ) {
      return false;
    }
    if (
      (st.commands?.count ?? st.commands?.running_count ?? 0) > 0 ||
      (Array.isArray(st.commands?.items) && st.commands.items.length > 0) ||
      (Array.isArray(st.running_commands) && st.running_commands.length > 0)
    ) {
      return false;
    }
    if (
      (st.analysts?.count ?? st.analysts?.active_count ?? 0) > 0 ||
      (Array.isArray(st.analysts?.items) && st.analysts.items.length > 0) ||
      (Array.isArray(st.delegated_tasks) && st.delegated_tasks.length > 0)
    ) {
      return false;
    }
    if (
      (st.automations?.count ?? st.automations?.running_count ?? 0) > 0 ||
      (Array.isArray(st.automations?.items) &&
        st.automations.items.length > 0) ||
      (Array.isArray(st.automations?.running) &&
        st.automations.running.length > 0) ||
      (Array.isArray(st.running_automations) &&
        st.running_automations.length > 0)
    ) {
      return false;
    }
    if (typeof st.pending_settlements === "number") {
      if (st.pending_settlements > 0) {
        return false;
      }
    } else if (
      typeof st.pending_settlements === "object" &&
      st.pending_settlements !== null
    ) {
      if (
        (st.pending_settlements.count ?? 0) > 0 ||
        (Array.isArray(st.pending_settlements.items) &&
          st.pending_settlements.items.length > 0)
      ) {
        return false;
      }
    }
    if ((st.wakeups?.armed_count ?? 0) > 0) {
      return false;
    }

    let wakeups: ThreadWakeupItem[];
    try {
      wakeups = await this.backend.getThreadWakeups(userId, threadId);
    } catch (err) {
      if (err instanceof BackendHttpError && err.status === 404) {
        await this.terminateDeletedThreadWatch(threadId);
        return true;
      }
      throw err;
    }
    if (
      wakeups.some((w) =>
        ACTIVE_WAKEUP_STATES.has(String(w.state ?? w.status ?? "")),
      )
    ) {
      return false;
    }

    const handle = this.activeWatches.get(threadId);
    if (handle) {
      // Never close the watch while `syncWatchReconnectState` is actively running!
      if (handle.syncingReconnect) {
        return false;
      }
      const stTurnIdx = st.latest_turn_index ?? st.turn_index;
      // Before closing the watch, if `latest_turn_index` advanced beyond `handle.lastSeenTurnIndex`,
      // or a prior reconnect backfill failed (`handle.needsReconnectSync`), or deferred `needs_input`
      // notices are still pending, run `syncWatchReconnectState` (`recover?mode=watch_reconnect`) first.
      // Never close the watch if backfill fails!
      if (
        handle.needsReconnectSync ||
        (handle.pendingNeedsInputThreads &&
          handle.pendingNeedsInputThreads.size > 0) ||
        (typeof stTurnIdx === "number" &&
          stTurnIdx > handle.lastSeenTurnIndex)
      ) {
        try {
          const syncOk = await this.syncWatchReconnectState(handle);
          if (!syncOk) {
            handle.needsReconnectSync = true;
            return false;
          }
        } catch (err) {
          if (err instanceof BackendHttpError && err.status === 404) {
            await this.terminateDeletedThreadWatch(threadId, handle);
            return true;
          }
          handle.needsReconnectSync = true;
          return false;
        }
      }
      if (
        handle.needsReconnectSync ||
        (handle.pendingNeedsInputThreads &&
          handle.pendingNeedsInputThreads.size > 0)
      ) {
        return false;
      }
      handle.abortController.abort();
      this.activeWatches.delete(threadId);
      await this.backend.updateInboundProgress({
        thread_watch: {
          thread_id: threadId,
          dalton_user_id: handle.userId,
          platform: handle.platform,
          target_address: handle.targetAddress,
          scope_key: handle.scopeKey,
          generation: handle.generation,
          last_seen_turn_index: handle.lastSeenTurnIndex,
          watch_active: false,
        },
      });
    }
    return true;
  }

  private async syncWatchReconnectState(
    handle: ActiveWatchHandle,
  ): Promise<boolean> {
    if (handle.syncingReconnect) {
      return !handle.needsReconnectSync;
    }
    handle.syncingReconnect = true;
    try {
      const rec = await this.backend.recoverState({
        mode: "watch_reconnect",
        thread_id: handle.threadId,
      });

      let allSettled = true;
      if (
        handle.pendingReplayFallbacks &&
        handle.pendingReplayFallbacks.size > 0
      ) {
        for (const pendingFb of [...handle.pendingReplayFallbacks.values()]) {
          const fbOk = await this.deliverReplayFallback(pendingFb);
          if (!fbOk) {
            allSettled = false;
          }
        }
      }
      if (
        handle.pendingNeedsInputThreads &&
        handle.pendingNeedsInputThreads.size > 0
      ) {
        for (const [analystThreadId, pendingNotice] of [
          ...handle.pendingNeedsInputThreads.entries(),
        ]) {
          const noticeOk = await this.deliverOrDeferNeedsInputNotice(
            handle,
            analystThreadId,
            pendingNotice,
          );
          if (!noticeOk) {
            allSettled = false;
          }
        }
      }

      for (const bg of rec.background_runs ?? []) {
        const bgOk = await this.recoverSingleBackgroundRun(bg);
        if (!bgOk) {
          allSettled = false;
        }
      }

      for (const w of rec.thread_watches ?? []) {
        if (w.watch_active && w.thread_id === handle.threadId) {
          let st: ThreadStatusInfo;
          try {
            st = await this.backend.getThreadStatus(
              w.dalton_user_id,
              w.thread_id,
            );
          } catch (err) {
            if (err instanceof BackendHttpError && err.status === 404) {
              await this.terminateDeletedThreadWatch(handle.threadId, handle);
              return true;
            }
            throw err;
          }
          if (st.status === "not_found") {
            await this.terminateDeletedThreadWatch(handle.threadId, handle);
            return true;
          }
          if (Array.isArray(st.owed_threads) && st.owed_threads.length > 0) {
            const owedOk = await this.reconcileOwedThreadsNeedsInput(
              handle,
              st.owed_threads,
            );
            if (!owedOk) {
              allSettled = false;
            }
          }
          const stTurnIdx = st.latest_turn_index ?? st.turn_index;
          let highestSettledTurn = Math.max(
            handle.lastSeenTurnIndex,
            w.last_seen_turn_index,
          );
          if (
            typeof stTurnIdx === "number" &&
            stTurnIdx > w.last_seen_turn_index &&
            PUBLIC_REPLAY_NEEDED_STATUSES.has(String(st.status ?? ""))
          ) {
            for (
              let tIdx = w.last_seen_turn_index + 1;
              tIdx <= stTurnIdx;
              tIdx++
            ) {
              const turnOk = await this.deliverReplayFallback({
                userId: w.dalton_user_id,
                threadId: w.thread_id,
                runId: tIdx === stTurnIdx ? (st.run_id ?? null) : null,
                turnIndex: tIdx,
                platform: w.platform,
                targetAddress: w.target_address,
                scopeKey: w.scope_key,
              });
              if (!turnOk) {
                allSettled = false;
                break;
              }
              highestSettledTurn = Math.max(highestSettledTurn, tIdx);
            }
          } else if (typeof stTurnIdx === "number") {
            highestSettledTurn = Math.max(highestSettledTurn, stTurnIdx);
          }
          handle.lastSeenTurnIndex = highestSettledTurn;
        }
      }

      handle.needsReconnectSync = !allSettled;
      return allSettled;
    } finally {
      handle.syncingReconnect = false;
    }
  }

  /**
   * Reconnect `/watch` after 45s keepalive timeout or 30m periodic rotation (§1.3.4).
   */
  async reconnectThreadWatch(threadId: string): Promise<void> {
    const rec = await this.backend.recoverState({
      mode: "watch_reconnect",
      thread_id: threadId,
    });

    for (const bg of rec.background_runs ?? []) {
      await this.recoverSingleBackgroundRun(bg);
    }

    for (const w of rec.thread_watches ?? []) {
      if (w.watch_active) {
        const st = await this.backend.getThreadStatus(
          w.dalton_user_id,
          w.thread_id,
        );
        const stTurnIdx = st.latest_turn_index ?? st.turn_index;
        if (
          typeof stTurnIdx === "number" &&
          stTurnIdx > w.last_seen_turn_index &&
          PUBLIC_REPLAY_NEEDED_STATUSES.has(String(st.status ?? ""))
        ) {
          for (
            let tIdx = w.last_seen_turn_index + 1;
            tIdx <= stTurnIdx;
            tIdx++
          ) {
            await this.deliverReplayFallback({
              userId: w.dalton_user_id,
              threadId: w.thread_id,
              runId: tIdx === stTurnIdx ? (st.run_id ?? null) : null,
              turnIndex: tIdx,
              platform: w.platform,
              targetAddress: w.target_address,
              scopeKey: w.scope_key,
            });
          }
        }
        const handle = this.ensureThreadWatch({
          threadId: w.thread_id,
          userId: w.dalton_user_id,
          platform: w.platform,
          targetAddress: w.target_address,
          scopeKey: w.scope_key,
          generation: w.generation,
          lastSeenTurnIndex:
            typeof stTurnIdx === "number"
              ? Math.max(w.last_seen_turn_index, stTurnIdx)
              : w.last_seen_turn_index,
        });
        if (Array.isArray(st.owed_threads) && st.owed_threads.length > 0) {
          await this.reconcileOwedThreadsNeedsInput(handle, st.owed_threads);
        }
        await this.evaluateWatchCloseConditions(w.dalton_user_id, w.thread_id);
      }
    }
  }

  // =========================================================================
  // §5.2 Full Startup Recovery
  // =========================================================================

  async syncWeixinStateCache(): Promise<void> {
    try {
      const wxState = await this.backend.getWeixinState(true);
      if (wxState.configured) {
        mkdirSync(this.weixinCacheDir, { recursive: true, mode: 0o700 });
        try {
          chmodSync(this.weixinCacheDir, 0o700);
        } catch {
          // Ignore chmod errors on non-POSIX filesystems
        }
        const stateFilePath = resolve(this.weixinCacheDir, "state.json");
        writeFileSync(
          stateFilePath,
          JSON.stringify(
            {
              ilink_bot_id: wxState.ilink_bot_id,
              owner_user_id: wxState.owner_user_id,
              bot_token: wxState.bot_token ?? null,
              base_url: wxState.base_url,
              get_updates_buf: wxState.get_updates_buf,
              status: wxState.status,
              updated_at: wxState.updated_at,
            },
            null,
            2,
          ),
          { mode: 0o600 },
        );
        try {
          chmodSync(stateFilePath, 0o600);
        } catch {
          // Ignore chmod errors on non-POSIX filesystems
        }
      }
    } catch {
      // Do not fail startup if Weixin is unconfigured or encryption key is absent
    }
  }

  startStartupRecoveryInBackground(): Promise<void> {
    if (!this.startupRecoveryPromise) {
      this.startupRecoveryPromise = this.runStartupRecovery().catch(() => {
        // Prevent unhandled rejection during background startup recovery
      });
    }
    return this.startupRecoveryPromise;
  }

  async waitForStartupRecovery(): Promise<void> {
    if (this.startupRecoveryPromise) {
      await this.startupRecoveryPromise;
    }
  }

  async runStartupRecovery(): Promise<void> {
    // Step 1: Sync Weixin credentials & get_updates_buf from `I-9` to local 0700/0600 cache
    await this.syncWeixinStateCache();

    // Step 2: Call `I-7` `GET /api/v1/dalton/channels/internal/recover?mode=startup`
    const recovery = await this.backend.recoverState({ mode: "startup" });

    // Run recovery tasks concurrently so one long-lived SSE stream does not block other rows
    await Promise.allSettled([
      ...(recovery.inbound_events ?? []).map((row) =>
        this.recoverSingleInboundRow(row),
      ),
      ...(recovery.background_runs ?? []).map((bg) =>
        this.recoverSingleBackgroundRun(bg),
      ),
      ...(recovery.thread_watches ?? []).map((w) =>
        this.recoverSingleThreadWatch(w),
      ),
      ...(recovery.automation_runs ?? []).map((autoRun) =>
        this.recoverSingleAutomationRun(autoRun),
      ),
    ]);
  }

  private async recoverSingleInboundRow(
    row: RecoverInboundEventRow,
  ): Promise<void> {
    const userId = row.dalton_user_id;
    if (!userId) {
      await this.backend.updateInboundProgress({
        platform: row.platform,
        message_id: row.message_id,
        status: "failed",
        fallback_pending: false,
      });
      return;
    }

    const platformUserId =
      (typeof row.platform_user_id === "string" &&
      row.platform_user_id.length > 0
        ? row.platform_user_id
        : typeof row.raw_payload?.platform_user_id === "string"
          ? row.raw_payload.platform_user_id
          : typeof row.raw_payload?.sender_id === "string"
            ? row.raw_payload.sender_id
            : "") || "recovered_user";
    const generation =
      typeof row.generation === "number"
        ? row.generation
        : typeof row.raw_payload?.generation === "number"
          ? row.raw_payload.generation
          : 0;

    const syntheticEvent: InboundChannelEvent = {
      platform: row.platform,
      platformUserId,
      chatType: row.chat_type,
      chatAddress: row.chat_address,
      chatId: String(row.raw_payload?.chat_id ?? row.chat_address),
      threadScopeKey: row.thread_scope_key,
      messageId: row.message_id,
      text: String(row.raw_payload?.text ?? ""),
    };

    const pendingFromRow =
      row.pending_interrupt ??
      (row.raw_payload?.pending_interrupt &&
      typeof row.raw_payload.pending_interrupt === "object"
        ? (row.raw_payload.pending_interrupt as PendingInterruptState)
        : null);

    const syntheticResolved: I1RegisterAndResolveResponse = {
      existing_status: row.status,
      linked: true,
      allowed: true,
      dalton_user_id: userId,
      request_key: row.request_key,
      retry_request_key: row.retry_request_key,
      resubmitted_request_key: row.resubmitted_request_key,
      steered_into_run_id: row.steered_into_run_id,
      thread_id: row.thread_id,
      run_id: row.run_id,
      turn_index: row.turn_index,
      workspace_id: row.workspace_id,
      external_thread_id: row.external_thread_id,
      generation,
      fallback_pending: row.fallback_pending,
      sent_to_current_count: row.sent_to_current_count,
      uploaded_attachments: row.uploaded_attachments ?? [],
      raw_payload: row.raw_payload ?? {},
      pending_interrupt: pendingFromRow,
    };

    // Case 1: `(status == 'completed' || status == 'failed')` and `fallback_pending == true`
    if (
      (row.status === "completed" || row.status === "failed") &&
      row.fallback_pending &&
      row.thread_id
    ) {
      await this.deliverReplayFallback({
        userId,
        threadId: row.thread_id,
        runId: row.run_id,
        turnIndex: row.turn_index,
        platform: row.platform,
        targetAddress: row.chat_address,
        inboundMessageId: row.message_id,
        scopeKey: row.thread_scope_key,
      });
      return;
    }

    // Check if this row is mid-way through a `credit_pause` supplementary steer recovery
    if (
      (row.raw_payload?.credit_steer_queued === true ||
        row.raw_payload?.credit_resume_dispatched === true ||
        typeof row.raw_payload?.credit_steer_request_key === "string") &&
      row.thread_id
    ) {
      let creditRunId =
        typeof row.raw_payload.credit_steer_run_id === "string"
          ? row.raw_payload.credit_steer_run_id
          : row.steered_into_run_id ?? row.run_id ?? "";
      const supplementText =
        typeof row.raw_payload.credit_supplement_text === "string"
          ? row.raw_payload.credit_supplement_text
          : this.extractCreditSupplementText(syntheticEvent.text);
      const supplementContent = this.buildMessageContent(
        supplementText,
        row.uploaded_attachments ?? [],
      );
      const supplementRequestKey =
        typeof row.raw_payload.credit_steer_request_key === "string"
          ? row.raw_payload.credit_steer_request_key
          : makeCreditSupplementSteerKey(
              row.platform,
              row.message_id,
              creditRunId || "unknown",
            );

      // If `credit_turn_text_settled === true`, skip re-delivering the main turn text and only reconcile the supplement steer!
      if (row.raw_payload.credit_turn_text_settled === true && creditRunId) {
        await this.reconcileCreditSupplementSteer({
          event: syntheticEvent,
          userId,
          resolved: syntheticResolved,
          threadId: row.thread_id,
          runId: creditRunId,
          supplementContent,
        });
        return;
      }

      const st = await this.backend.getThreadStatus(userId, row.thread_id);
      const effectiveRunId = st.run_id ?? (creditRunId || null);
      if (!creditRunId && effectiveRunId) {
        creditRunId = effectiveRunId;
      }

      // If crashed after dispatching resume turn (`credit_resume_dispatched: true`) but before sending supplement steer
      if (
        row.raw_payload.credit_resume_dispatched === true &&
        row.raw_payload.credit_steer_queued !== true &&
        creditRunId
      ) {
        await this.backend.updateInboundProgress({
          platform: row.platform,
          message_id: row.message_id,
          steered_into_run_id: creditRunId,
          raw_payload_patch: {
            credit_steer_queued: true,
            credit_steer_request_key: supplementRequestKey,
            credit_steer_run_id: creditRunId,
            credit_steer_state: "pending",
            credit_turn_text_settled: false,
            credit_supplement_text: supplementText,
          },
        });
        try {
          const steerResp = await this.backend.appendThreadMessages(
            userId,
            row.thread_id,
            {
              messages: [{ role: "user", content: supplementContent }],
              request_key: supplementRequestKey,
              workspace_id: row.workspace_id,
              platform: row.platform,
              turn_platform: row.platform,
              surface_rules: buildSurfaceRules(row.platform),
              steer_only: true,
            },
          );
          if (steerResp.isSse && steerResp.events) {
            for await (const _ev of steerResp.events) {
              // Drain steering_accepted SSE stream
            }
          }
        } catch {
          // Reconcile step will handle any transient failure
        }
      }

      let turnTextSettled = false;
      if (PUBLIC_LIVE_STATUSES.has(String(st.status ?? ""))) {
        turnTextSettled = await this.attachToLiveStreamOrReplay({
          userId,
          threadId: row.thread_id,
          runId: effectiveRunId,
          turnIndex: st.latest_turn_index ?? st.turn_index ?? row.turn_index,
          event: syntheticEvent,
          resolved: syntheticResolved,
          deferFinalCompletion: true,
        });
      } else if (PUBLIC_REPLAY_NEEDED_STATUSES.has(String(st.status ?? ""))) {
        turnTextSettled = await this.deliverReplayFallback({
          userId,
          threadId: row.thread_id,
          runId: effectiveRunId,
          turnIndex: st.latest_turn_index ?? st.turn_index ?? row.turn_index,
          platform: row.platform,
          targetAddress: row.chat_address,
          inboundMessageId: row.message_id,
          scopeKey: row.thread_scope_key,
          deferFinalCompletion: true,
        });
      }
      if (turnTextSettled && creditRunId) {
        await this.backend.updateInboundProgress({
          platform: row.platform,
          message_id: row.message_id,
          raw_payload_patch: {
            credit_turn_text_settled: true,
          },
        });
        await this.reconcileCreditSupplementSteer({
          event: syntheticEvent,
          userId,
          resolved: syntheticResolved,
          threadId: row.thread_id,
          runId: creditRunId,
          supplementContent,
        });
      }
      return;
    }

    const recoveredBusyWaitStartedAtMs =
      typeof row.raw_payload?.busy_wait_started_at_ms === "number"
        ? row.raw_payload.busy_wait_started_at_ms
        : undefined;

    // Case 2: `status == 'dispatched'` or `'streaming'` (`src/dalton/database/channel_gateway.py:3733`)
    if (row.status === "dispatched" || row.status === "streaming") {
      // Subcase 2a: Missing `thread_id` or `run_id` (crashed before receiving `event: metadata`) -> re-dispatch idempotently with same `request_key`
      if (!row.thread_id || !row.run_id) {
        const effectiveKey =
          row.resubmitted_request_key ??
          row.retry_request_key ??
          row.request_key;
        const messageContent = this.buildMessageContent(
          syntheticEvent.text,
          row.uploaded_attachments ?? [],
        );
        await this.dispatchConversationTurn({
          event: syntheticEvent,
          userId,
          resolved: syntheticResolved,
          messageContent,
          requestKey: effectiveKey,
          steerOnly: false,
          retryAttempt: 0,
          busyWaitStartedAtMs: recoveredBusyWaitStartedAtMs,
          explicitThreadId: row.thread_id,
        });
        return;
      }

      const runStatus = await this.resolveRunStatusWithRetry(
        userId,
        row.thread_id,
        row.run_id,
      );

      if (PUBLIC_LIVE_STATUSES.has(runStatus)) {
        await this.attachToLiveStreamOrReplay({
          userId,
          threadId: row.thread_id,
          runId: row.run_id,
          turnIndex: row.turn_index,
          event: syntheticEvent,
          resolved: syntheticResolved,
        });
        return;
      }

      if (PUBLIC_REPLAY_NEEDED_STATUSES.has(runStatus)) {
        await this.deliverReplayFallback({
          userId,
          threadId: row.thread_id,
          runId: row.run_id,
          turnIndex: row.turn_index,
          platform: row.platform,
          targetAddress: row.chat_address,
          inboundMessageId: row.message_id,
          scopeKey: row.thread_scope_key,
        });
        return;
      }

      await this.backend.updateInboundProgress({
        platform: row.platform,
        message_id: row.message_id,
        status: "failed",
        fallback_pending: false,
      });
      await this.sendNoticeDelivery({
        deliveryKey: `notice:${row.platform}:${row.message_id}:rec_${runStatus}`,
        userId,
        threadId: row.thread_id,
        runId: row.run_id,
        platform: row.platform,
        target: row.chat_address,
        inboundMessageId: row.message_id,
        text: `The interrupted run ended with status "${runStatus}".`,
        threadScopeKey: row.thread_scope_key,
      });
      return;
    }

    // Case 2b: `status == 'pending_resubmit'` (`src/dalton/database/channel_gateway.py:3733`)
    if (row.status === "pending_resubmit") {
      const resubmitKey =
        row.resubmitted_request_key ??
        makeReturnedSteerRequestKey(
          row.platform,
          row.message_id,
          row.steered_into_run_id ?? row.run_id ?? "unknown",
        );
      const messageContent = this.buildMessageContent(
        syntheticEvent.text,
        row.uploaded_attachments ?? [],
      );
      await this.dispatchConversationTurn({
        event: syntheticEvent,
        userId,
        resolved: syntheticResolved,
        messageContent,
        requestKey: resubmitKey,
        steerOnly: false,
        retryAttempt: 0,
        busyWaitStartedAtMs: recoveredBusyWaitStartedAtMs,
        explicitThreadId: row.thread_id,
      });
      return;
    }

    // Case 3: `status == 'steered'`
    if (row.status === "steered") {
      const messageContent = this.buildMessageContent(
        syntheticEvent.text,
        row.uploaded_attachments ?? [],
      );
      await this.waitAndReconcileSteer({
        event: syntheticEvent,
        userId,
        resolved: syntheticResolved,
        messageContent,
        threadId: row.thread_id,
        steeredIntoRunId: row.steered_into_run_id ?? row.run_id,
        retryAttempt: 0,
      });
      return;
    }

    // Case 4: `status == 'received'` (or `'pending'` / `'accepted'`)
    if (
      row.status === "received" ||
      row.status === "pending" ||
      row.status === "accepted"
    ) {
      const messageContent = this.buildMessageContent(
        syntheticEvent.text,
        row.uploaded_attachments ?? [],
      );

      // Check if the session has an active `pending_interrupt` (either on `row` or via idempotent `I-1`)
      let activeInterrupt = syntheticResolved.pending_interrupt;
      if (!activeInterrupt) {
        try {
          const reresolved = await this.backend.registerAndResolveInbound({
            platform: row.platform,
            platform_user_id: platformUserId,
            chat_type: row.chat_type,
            chat_address: row.chat_address,
            thread_scope_key: row.thread_scope_key,
            message_id: row.message_id,
            request_key: row.request_key,
            raw_payload: row.raw_payload ?? {},
          });
          activeInterrupt = reresolved.pending_interrupt ?? null;
          syntheticResolved.pending_interrupt = activeInterrupt;
        } catch {
          // Proceed if registerAndResolveInbound is not mocked in unit test
        }
      }

      if (activeInterrupt && activeInterrupt.thread_id) {
        const handledHitl = await this.handlePendingInterruptReply({
          event: syntheticEvent,
          userId,
          resolved: syntheticResolved,
          messageContent,
          pendingInterrupt: activeInterrupt,
        });
        if (handledHitl) {
          return;
        }
      }

      const effectiveKey =
        row.resubmitted_request_key ?? row.retry_request_key ?? row.request_key;
      await this.dispatchConversationTurn({
        event: syntheticEvent,
        userId,
        resolved: syntheticResolved,
        messageContent,
        requestKey: effectiveKey,
        steerOnly: false,
        retryAttempt: 0,
        busyWaitStartedAtMs: recoveredBusyWaitStartedAtMs,
        explicitThreadId: row.thread_id,
      });
    }
  }

  private async recoverSingleBackgroundRun(
    bg: RecoverBackgroundRunRow,
  ): Promise<boolean> {
    if (
      (bg.status === "completed" || bg.status === "failed") &&
      bg.fallback_pending
    ) {
      return await this.deliverReplayFallback({
        userId: bg.dalton_user_id,
        threadId: bg.thread_id,
        runId: bg.run_id,
        turnIndex: bg.turn_index,
        platform: bg.platform,
        targetAddress: bg.target_address,
        isBackgroundRun: true,
      });
    }

    if (bg.status === "running" || bg.status === "streaming") {
      const runState = await this.resolveRunStatusWithRetry(
        bg.dalton_user_id,
        bg.thread_id,
        bg.run_id,
      );

      if (PUBLIC_LIVE_STATUSES.has(runState)) {
        const handle = this.ensureThreadWatch({
          threadId: bg.thread_id,
          userId: bg.dalton_user_id,
          platform: bg.platform,
          targetAddress: bg.target_address,
          scopeKey: bg.target_address,
          generation: 0,
          lastSeenTurnIndex: bg.turn_index ?? 0,
        });
        return await this.consumeBackgroundRunStream({
          handle,
          runId: bg.run_id,
          turnIndex: bg.turn_index ?? 0,
        });
      }

      if (
        PUBLIC_REPLAY_NEEDED_STATUSES.has(runState) ||
        runState === "cancelled" ||
        runState === "failed" ||
        runState === "error"
      ) {
        return await this.deliverReplayFallback({
          userId: bg.dalton_user_id,
          threadId: bg.thread_id,
          runId: bg.run_id,
          turnIndex: bg.turn_index,
          platform: bg.platform,
          targetAddress: bg.target_address,
          isBackgroundRun: true,
        });
      }

      await this.backend.updateInboundProgress({
        background_run: {
          run_id: bg.run_id,
          thread_id: bg.thread_id,
          dalton_user_id: bg.dalton_user_id,
          platform: bg.platform,
          target_address: bg.target_address,
          turn_index: bg.turn_index,
          status: "failed",
          fallback_pending: false,
        },
      });
    }
    return true;
  }

  private async recoverSingleThreadWatch(
    w: RecoverThreadWatchRow,
  ): Promise<void> {
    if (!w.watch_active) return;

    const st = await this.backend.getThreadStatus(
      w.dalton_user_id,
      w.thread_id,
    );
    const stTurnIdx = st.latest_turn_index ?? st.turn_index;
    if (
      typeof stTurnIdx === "number" &&
      stTurnIdx > w.last_seen_turn_index &&
      PUBLIC_REPLAY_NEEDED_STATUSES.has(String(st.status ?? ""))
    ) {
      for (
        let tIdx = w.last_seen_turn_index + 1;
        tIdx <= stTurnIdx;
        tIdx++
      ) {
        await this.deliverReplayFallback({
          userId: w.dalton_user_id,
          threadId: w.thread_id,
          runId: tIdx === stTurnIdx ? (st.run_id ?? null) : null,
          turnIndex: tIdx,
          platform: w.platform,
          targetAddress: w.target_address,
          scopeKey: w.scope_key,
        });
      }
    }

    const handle = this.ensureThreadWatch({
      threadId: w.thread_id,
      userId: w.dalton_user_id,
      platform: w.platform,
      targetAddress: w.target_address,
      scopeKey: w.scope_key,
      generation: w.generation,
      lastSeenTurnIndex:
        typeof stTurnIdx === "number"
          ? Math.max(w.last_seen_turn_index, stTurnIdx)
          : w.last_seen_turn_index,
    });
    if (Array.isArray(st.owed_threads) && st.owed_threads.length > 0) {
      await this.reconcileOwedThreadsNeedsInput(handle, st.owed_threads);
    }
    await this.evaluateWatchCloseConditions(w.dalton_user_id, w.thread_id);
  }

  private async recoverSingleAutomationRun(
    autoRun: RecoverAutomationRunRow,
  ): Promise<void> {
    if (autoRun.status !== "finishing") return;
    await this.agentRoutes.executeAutomationRunFinish({
      executionId: autoRun.execution_id,
      userId: autoRun.user_id,
      threadId: autoRun.thread_id,
      automationName: autoRun.automation_name,
      finishStatus: autoRun.finish_status ?? "completed",
      finalText: autoRun.final_text,
      targets: autoRun.targets ?? [],
    });
  }

  // =========================================================================
  // Internal Utilities
  // =========================================================================

  /**
   * Returns true if the SSE event belongs to the main agent lane (`agent` is omitted, `"main"`, or not a `subagent:*` / `task:*` lane).
   * Matches `src/server/services/history/replay/lanes.py:11`: `not (agent.startswith("subagent:") or agent.startswith("task:"))`.
   */
  private isMainAgentLane(agent: unknown): boolean {
    if (typeof agent !== "string" || agent.length === 0) {
      return true;
    }
    return !agent.startsWith("subagent:") && !agent.startsWith("task:");
  }

  /**
   * Resolve run status via `getDispatchesLiveness` with backoff retry on missing `run_id`
   * (covers the window between `create_conversation_thread` and `register_run_in_ledger`, `messaging.py:1507-1508`).
   */
  private async resolveRunStatusWithRetry(
    userId: string,
    threadId: string,
    runId: string,
  ): Promise<string> {
    for (let attempt = 0; attempt <= BACKOFF_DELAYS_MS.length; attempt++) {
      if (attempt > 0) {
        await this.backoffDelayFn(BACKOFF_DELAYS_MS[attempt - 1]!);
      }
      const liveness = await this.backend.getDispatchesLiveness(userId, [
        runId,
      ]);
      const status = liveness[runId]?.status;
      if (typeof status === "string" && status.length > 0) {
        return status;
      }
      try {
        const st = await this.backend.getThreadStatus(userId, threadId);
        if (
          st.run_id === runId &&
          typeof st.status === "string" &&
          st.status.length > 0 &&
          st.status !== "idle"
        ) {
          return st.status;
        }
      } catch {
        // Ignore status lookup error during retry
      }
    }
    return "not_found";
  }

  private trackBackgroundRunId(
    threadId: string,
    runId: string,
    active: boolean,
  ): void {
    let set = this.activeBackgroundRunIds.get(threadId);
    if (active) {
      if (!set) {
        set = new Set<string>();
        this.activeBackgroundRunIds.set(threadId, set);
      }
      set.add(runId);
    } else if (set) {
      set.delete(runId);
      if (set.size === 0) {
        this.activeBackgroundRunIds.delete(threadId);
      }
    }
  }

  private closeWatchesForUserPlatform(
    userId: string,
    platform: ChannelPlatform,
  ): void {
    for (const [tid, handle] of this.activeWatches.entries()) {
      if (handle.userId === userId && handle.platform === platform) {
        handle.abortController.abort();
        this.activeWatches.delete(tid);
      }
    }
  }

  async stopAllWatches(): Promise<void> {
    this.shuttingDown = true;
    for (const handle of this.activeWatches.values()) {
      handle.abortController.abort();
    }
    this.activeWatches.clear();
  }

  private async settleTurnFallbackAndPartialNotice(params: {
    fallbackDeliveryKey: string;
    partialNoticeKeyId: string;
    userId: string;
    threadId: string | null;
    runId: string | null;
    platform: ChannelPlatform;
    targetAddress: string;
    inboundMessageId?: string | null;
    replyToMessageId?: string | null;
    threadScopeKey?: string | null;
    trimmedText: string;
    sentToCurrentCount: number;
  }): Promise<boolean> {
    let needsPartialNotice = false;
    if (params.sentToCurrentCount === 0) {
      const deliveryRes = await this.deliveryService.executeDelivery({
        deliveryKey: params.fallbackDeliveryKey,
        kind: "fallback",
        userId: params.userId,
        threadId: params.threadId,
        runId: params.runId,
        platform: params.platform,
        target: params.targetAddress,
        inboundMessageId: params.inboundMessageId ?? null,
        text: params.trimmedText,
        replyToMessageId: params.replyToMessageId ?? null,
        threadScopeKey: params.threadScopeKey ?? null,
      });
      if (
        deliveryRes.status !== "sent" &&
        deliveryRes.status !== "partial"
      ) {
        return false;
      }
      if (deliveryRes.status === "partial") {
        needsPartialNotice = true;
      }
    } else {
      // `sent_to_current_count > 0`: query the `I-6` ledger for `fallback:{run_id}` (`channel_gateway.py:2465-2473`).
      // - `already_completed` with `status === "partial"` -> send `notice:partial:{run_id}`.
      // - `already_completed` with `status === "sent"` -> do not send notice.
      // - `claimed` (meaning `sent_to_current_count > 0` came from `/agent/send` and `fallback:{run_id}` was never used) ->
      //   release the unused claim as `failed` without sending duplicate fallback text or notice.
      const claim = await this.backend.authorizeAndClaimDelivery({
        delivery_key: params.fallbackDeliveryKey,
        kind: "fallback",
        user_id: params.userId,
        thread_id: params.threadId,
        run_id: params.runId,
        platform: params.platform,
        target: params.targetAddress,
        inbound_message_id: params.inboundMessageId ?? null,
        has_non_empty_text: true,
        total_chunks: 1,
      });
      if (claim.claim_status === "already_completed") {
        const prev = (claim.first_response_json ?? {}) as { status?: string };
        const ledgerStatus = prev.status ?? claim.status ?? "sent";
        if (ledgerStatus === "partial") {
          needsPartialNotice = true;
        }
      } else if (claim.claim_status === "in_flight_or_unknown") {
        return false;
      } else if (claim.claim_status === "claimed") {
        await this.backend.updateDeliveryProgress({
          delivery_key: params.fallbackDeliveryKey,
          status: "failed",
          sent_chunks: 0,
          total_chunks: 1,
          error_code: "already_sent_to_current",
          error_message:
            "Text already sent to current conversation via /agent/send.",
        });
      }
    }

    if (needsPartialNotice && params.threadId) {
      const partialNoticeOk = await this.sendNoticeDelivery({
        deliveryKey: `notice:partial:${params.partialNoticeKeyId}`,
        userId: params.userId,
        threadId: params.threadId,
        runId: params.runId,
        platform: params.platform,
        target: params.targetAddress,
        inboundMessageId: params.inboundMessageId ?? null,
        text: `后续内容未能发送，请在网页查看：${this.webBaseUrl}/chat/t/${params.threadId}`,
        threadScopeKey: params.threadScopeKey ?? null,
      });
      if (!partialNoticeOk) {
        return false;
      }
    }

    return true;
  }

  private async sendNoticeDelivery(options: {
    deliveryKey: string;
    userId: string;
    threadId?: string | null;
    runId?: string | null;
    platform: ChannelPlatform;
    target: string;
    inboundMessageId?: string | null;
    text: string;
    replyToMessageId?: string | null;
    threadScopeKey?: string | null;
  }): Promise<boolean> {
    const res = await this.deliveryService.executeDelivery({
      deliveryKey: options.deliveryKey,
      kind: "notice",
      userId: options.userId,
      threadId: options.threadId ?? null,
      runId: options.runId ?? null,
      platform: options.platform,
      target: options.target,
      inboundMessageId: options.inboundMessageId ?? null,
      text: options.text,
      replyToMessageId: options.replyToMessageId ?? null,
      threadScopeKey: options.threadScopeKey ?? null,
    });
    return res.status === "sent" || res.status === "partial";
  }

  private async sendDirectAdapterNotice(
    event: InboundChannelEvent,
    text: string,
  ): Promise<void> {
    const adapter = this.adapters.get(event.platform);
    if (!adapter || !adapter.isConfigured() || !adapter.isConnected()) return;
    const chunks = adapter.splitTextChunks(text);
    for (let i = 0; i < chunks.length; i++) {
      await adapter.sendTextChunk({
        targetAddress:
          event.chatType === "dm" ? `${event.platform}:@me` : event.chatAddress,
        recipientPlatformUserId: event.platformUserId,
        text: chunks[i]!,
        chunkIndex: i,
        totalChunks: chunks.length,
        replyToMessageId: i === 0 ? event.messageId : null,
        threadScopeKey: event.threadScopeKey,
        weixinContextToken: event.weixinContextToken ?? null,
      });
    }
  }

  private formatDenyNotice(
    platform: ChannelPlatform,
    denyReason: string,
  ): string {
    switch (denyReason) {
      case "not_linked":
        return `Your ${platform} account is not linked to Dalton yet. Open ${this.webBaseUrl}/settings/channels to generate a bind code, then send \`/bind <CODE>\` in a direct message.`;
      case "external_member_group_forbidden":
        return "External accounts can use Dalton in direct messages only, not in group chats.";
      case "workspace_write_denied":
        return "You do not have write access to the workspace bound to this chat.";
      case "colleague_dispatch_disabled":
        return "Colleague workspace dispatch is currently disabled.";
      case "workspace_not_found":
        return "The workspace bound to this conversation was not found. Use `/workspace` to switch to another workspace or Home.";
      default:
        return `Request denied (${denyReason}).`;
    }
  }

  private sanitizeFilename(filename: string): string {
    const base = basename(filename).replace(/[^a-zA-Z0-9._-]/g, "_");
    return base.length > 0 ? base : "attachment";
  }

  private parseConflictDetail(
    errorBody: Record<string, unknown> | undefined,
  ): {
    code?: string;
    error_type?: string;
    reason?: string;
    outcome?: string;
    run_status?: string;
    retryable?: boolean;
    steered?: boolean;
    same_endpoint?: boolean;
    thread_id?: string;
    run_id?: string;
    turn_index?: number;
    message?: string;
  } {
    if (!errorBody) return {};
    const inner =
      errorBody.detail && typeof errorBody.detail === "object"
        ? (errorBody.detail as Record<string, unknown>)
        : errorBody;
    const rawDetailStr =
      typeof errorBody.detail === "string" ? errorBody.detail : undefined;

    let code = typeof inner.code === "string" ? inner.code : undefined;
    let errorType =
      typeof inner.error_type === "string" ? inner.error_type : undefined;
    let reason = typeof inner.reason === "string" ? inner.reason : undefined;

    if (!code && rawDetailStr) {
      if (rawDetailStr.includes("external_thread_id")) {
        code = "external_id_conflict";
        errorType = "external_id_conflict";
      } else if (
        rawDetailStr.includes("Thread state changed during retry admission")
      ) {
        code = "attempt_conflict";
        errorType = "admission_conflict";
      } else if (
        rawDetailStr.includes("Concurrent turn creation in progress")
      ) {
        code = "turn_conflict";
        errorType = "admission_conflict";
      }
    }

    if (
      !reason &&
      code &&
      (code === "running" ||
        code === "not_running" ||
        code === "stopping" ||
        code === "compacting" ||
        code === "workspace_moving" ||
        code === "turn_conflict" ||
        code === "attempt_conflict")
    ) {
      reason = code;
    }

    return {
      code,
      error_type: errorType,
      reason,
      outcome: typeof inner.outcome === "string" ? inner.outcome : undefined,
      run_status:
        typeof inner.run_status === "string" ? inner.run_status : undefined,
      retryable:
        typeof inner.retryable === "boolean" ? inner.retryable : undefined,
      steered: typeof inner.steered === "boolean" ? inner.steered : undefined,
      same_endpoint:
        typeof inner.same_endpoint === "boolean"
          ? inner.same_endpoint
          : undefined,
      thread_id:
        typeof inner.run_thread_id === "string"
          ? inner.run_thread_id
          : typeof inner.thread_id === "string"
            ? inner.thread_id
            : undefined,
      run_id: typeof inner.run_id === "string" ? inner.run_id : undefined,
      turn_index:
        typeof inner.turn_index === "number" ? inner.turn_index : undefined,
      message:
        typeof inner.message === "string"
          ? inner.message
          : typeof inner.error === "string"
            ? inner.error
            : rawDetailStr,
    };
  }

  private safeParseJson(raw: string): Record<string, unknown> {
    if (!raw) return {};
    try {
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object"
        ? (parsed as Record<string, unknown>)
        : {};
    } catch {
      return {};
    }
  }
}
