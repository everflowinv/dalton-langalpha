import { basename } from "node:path";
import type { AgentRoutesHandler } from "./agent-routes.js";
import {
  buildImageCorrectionText,
  containsUnresolvedSandboxImagePath,
  DeliverableTextAccumulator,
  mergeImagesByMessage,
  resolveChannelFallbackText,
  resolveDeliveryImageUrls,
} from "./shared/deliverable-text.js";
import {
  type BackendClient,
  BackendHttpError,
  type ChartSnapshotItem,
  type DeliveryActivityOrigin,
  type I1RegisterAndResolveResponse,
  type I3SessionCommandRequest,
  type I3SessionCommandResponse,
  type I7InboundProgressResponse,
  type I7RecoverResponse,
  type RecoverAutomationRunRow,
  type RecoverBackgroundRunRow,
  type RecoverInboundEventRow,
  type RecoverThreadWatchRow,
  type SessionCommandTargetKind,
  type SseEvent,
  type SseOrErrorResponse,
  type ThreadMessageDispatchPayload,
  type ThreadStatusInfo,
  type ThreadWakeupItem,
  type UnresolvedCurrentAgentSend,
  type WorkspaceUploadWeixinContext,
} from "./backend-client.js";
import type { ChannelAdapter } from "./channels/adapter.js";
import type { WeixinPersistedAttachmentMeta } from "./channels/weixin/types.js";
import type { StatusFeedbackConfig } from "./config.js";
import {
  expiredBindCodeUsage,
  missingBindCodeUsage,
  notLinkedBindHelp,
  parseSlashCommand,
  type ParsedSlashCommand,
} from "./commands.js";
import {
  type DeliveryService,
  WEIXIN_ROTATION_DENY_CODES,
} from "./delivery-service.js";
import {
  type CollectedChartLayerRef,
  type CollectedHtmlWidgetRef,
  type PendingChartDeliveryReceipt,
  chartLayerCompositeKey,
  collectPublishedChartLayers,
  collectReportSourceChartLayers,
  deliverTurnChartsAndWidgets,
  isDmTargetAddress,
  parseChartSnapshotsList,
} from "./chart-snapshots.js";
import { isStaleGatewayEpochError } from "./gateway-lease.js";

type WeixinAttachmentAbortReason = "shutdown" | "superseded";

/**
 * Internal signal that a Weixin inbound attachment task was intentionally aborted
 * (e.g. account rotation / superseded generation / shutdown) and MUST NOT be
 * re-launched via `recoverDetachedInboundTurn`.
 */
class WeixinAttachmentAbortedError extends Error {
  readonly reason: WeixinAttachmentAbortReason;

  constructor(
    message: string,
    reason: WeixinAttachmentAbortReason,
    options?: { cause?: unknown },
  ) {
    super(message, options);
    this.name = "WeixinAttachmentAbortedError";
    this.reason = reason;
  }
}
import { ProcessingFeedbackCoordinator } from "./processing-feedback.js";
import {
  createSubsystemLogger,
  extractSafeLogErrorFields,
  sanitizeLogErrorCode,
} from "./shared/logger.js";
import {
  ACTIVE_WAKEUP_STATES,
  type AgentSendResponse,
  type AskUserQuestionItem,
  buildSurfaceRules,
  type ChannelPlatform,
  type ChatContentItem,
  type InboundAttachmentDescriptor,
  type InboundChannelEvent,
  INLINE_IMAGE_MIME_TYPES,
  INTERNAL_REPLAY_NEEDED_OUTCOMES,
  type InterruptActionRequest,
  isSupportedPlatform,
  MAX_ATTACHMENT_BYTES,
  MAX_ATTACHMENT_COUNT,
  MAX_INLINE_IMAGE_BYTES,
  MAX_TOTAL_ATTACHMENT_BYTES,
  type NativeAutocompleteChoice,
  type NativeAutocompleteInteractionEvent,
  type NativeCommandInteractionEvent,
  type NativeCommandResponder,
  type NativeInteractionEvent,
  type OneTimeWeixinReplyEnvelope,
  type PendingInterruptState,
  PUBLIC_LIVE_STATUSES,
  PUBLIC_REPLAY_NEEDED_STATUSES,
  type TurnDeliverableItem,
  type UploadedAttachmentRecord,
} from "./types.js";
import { SafeFetchError } from "./shared/safe-fetch.js";
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
  generation?: number | null;
  activityOrigin?: DeliveryActivityOrigin | null;
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

function storedBindingAction(
  request: unknown,
): "bind" | "unbind" | null {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    return null;
  }
  const action = (request as Record<string, unknown>).action;
  const cleaned = typeof action === "string" ? action.trim().toLowerCase() : "";
  return cleaned === "bind" || cleaned === "unbind" ? cleaned : null;
}

function recordField(value: object, key: string): unknown {
  if (!Object.prototype.hasOwnProperty.call(value, key)) return undefined;
  return Reflect.get(value, key);
}

/** Fields shared by a stored bind result and an I-2 consume response. */
function confirmationFields(value: object): {
  ok: boolean;
  reason: string | null;
  expectedPlatform: string | null;
  userId: string | null;
  deliveryKey: string | null;
  duplicate: boolean;
  action: "bind" | "unbind" | null;
} {
  const reason = recordField(value, "reason");
  const expected = recordField(value, "expected_platform");
  const userId = recordField(value, "user_id");
  const deliveryKey = recordField(value, "delivery_key");
  const action = recordField(value, "action");
  return {
    ok: recordField(value, "ok") === true,
    reason: typeof reason === "string" ? reason : null,
    expectedPlatform: typeof expected === "string" ? expected : null,
    userId:
      typeof userId === "string" && userId.trim().length > 0
        ? userId.trim()
        : null,
    deliveryKey:
      typeof deliveryKey === "string" && deliveryKey.length > 0
        ? deliveryKey
        : null,
    duplicate: recordField(value, "duplicate") === true,
    action: action === "bind" || action === "unbind" ? action : null,
  };
}


/** Chinese names for the actions that only the web app can confirm. */
const WEB_ONLY_ACTION_LABELS: Record<string, string> = {
  create_workspace: "新建工作区",
  archive_workspace: "归档工作区",
  delete_workspace: "删除工作区",
  delete_thread: "删除对话",
  ptc_agent: "把任务交给工作区分析师",
  dalton_notify_set_route: "设置通知去向",
  dalton_notify_add_personal_target: "添加个人通知目标",
  dalton_notify_test: "发送测试通知",
  // Order governance & brokerage trading tools (`order_governance.py`, `brokerage_capabilities.py`)
  order_approval: "订单操作",
  place_order: "下单",
  submit_order: "下单",
  trading_order_place: "下单",
  place_equity_order: "下单",
  place_option_order: "下单",
  place_crypto_order: "下单",
  place_advanced_order: "下单",
  crypto_account_create_order: "下单",
  sim_trade_input_order: "下单",
  create_order_instruction: "下单",
  modify_order: "改单",
  replace_order: "改单",
  trading_order_replace: "改单",
  crypto_account_modify_order: "改单",
  sim_trade_modify_order: "改单",
  cancel_order: "撤单",
  trading_order_cancel: "撤单",
  cancel_equity_order: "撤单",
  cancel_option_order: "撤单",
  cancel_crypto_order: "撤单",
  cancel_advanced_order: "撤单",
  cancel_option_exercise: "撤单",
  crypto_account_cancel_order: "撤单",
  sim_trade_cancel_order: "撤单",
  delete_order_instruction: "撤单",
  decline_trade_approval: "撤单",
  exercise_option: "期权行权",
  trading_order_confirm: "确认订单",
  answer_order_question: "确认订单",
  approval: "一项操作",
};

const GENERIC_WEB_ONLY_WRAPPER_KINDS = new Set<string>([
  "web_only_approval",
  "order_approval",
  "approval",
]);

function lookupWebOnlyActionLabel(rawAction: string | undefined): string | null {
  const cleaned = rawAction?.trim();
  if (!cleaned) return null;
  if (WEB_ONLY_ACTION_LABELS[cleaned]) {
    return WEB_ONLY_ACTION_LABELS[cleaned]!;
  }
  const lower = cleaned.toLowerCase();
  if (WEB_ONLY_ACTION_LABELS[lower]) {
    return WEB_ONLY_ACTION_LABELS[lower]!;
  }
  // Handle MCP-namespaced tool names like `ibkr__submit_order` or `broker.cancel_order`.
  const suffixMatch = /(?:__|[./:])([a-z0-9_]+)$/i.exec(lower);
  if (suffixMatch?.[1] && WEB_ONLY_ACTION_LABELS[suffixMatch[1]]) {
    return WEB_ONLY_ACTION_LABELS[suffixMatch[1]]!;
  }
  return null;
}

function extractActionNameFromRequest(req: InterruptActionRequest | undefined): string | undefined {
  if (!req || typeof req !== "object") return undefined;
  for (const candidate of [req.name, req.tool, req.action, req.type]) {
    if (typeof candidate === "string" && candidate.trim().length > 0) {
      return candidate.trim();
    }
  }
  return undefined;
}

function resolveWebOnlyApprovalActionType(params: {
  payloadActionType?: string;
  rawKind?: string;
  actionRequests?: InterruptActionRequest[];
  reviewConfigs?: unknown[];
}): string | undefined {
  const payloadActionType = params.payloadActionType?.trim() || undefined;
  const rawKind = params.rawKind?.trim() || undefined;
  const requests = params.actionRequests ?? [];

  const requestNames: string[] = [];
  for (const req of requests) {
    const name = extractActionNameFromRequest(req);
    if (name) requestNames.push(name);
  }
  if (requestNames.length === 0 && Array.isArray(params.reviewConfigs)) {
    for (const cfg of params.reviewConfigs) {
      if (
        cfg &&
        typeof cfg === "object" &&
        typeof (cfg as { action_name?: unknown }).action_name === "string"
      ) {
        const name = (cfg as { action_name: string }).action_name.trim();
        if (name) requestNames.push(name);
      }
    }
  }

  // Prefer explicit non-wrapper `payload.action_type` if provided.
  if (
    payloadActionType &&
    !GENERIC_WEB_ONLY_WRAPPER_KINDS.has(payloadActionType.toLowerCase())
  ) {
    return payloadActionType;
  }

  if (requestNames.length > 0) {
    const resolvedLabels = requestNames.map((n) => lookupWebOnlyActionLabel(n));
    const nonNullLabels = resolvedLabels.filter((l): l is string => l !== null);
    if (nonNullLabels.length === requestNames.length) {
      const uniqueLabels = new Set(nonNullLabels);
      if (uniqueLabels.size === 1) {
        return requestNames[0];
      }
      // Multiple distinct actions inside one order_approval batch -> fall back to `order_approval`.
      if (
        rawKind?.toLowerCase() === "order_approval" ||
        payloadActionType?.toLowerCase() === "order_approval"
      ) {
        return "order_approval";
      }
    }
    if (resolvedLabels[0] !== null) {
      return requestNames[0];
    }
    // Unknown specific tool inside an `order_approval` interrupt -> show "订单操作".
    if (
      rawKind?.toLowerCase() === "order_approval" ||
      payloadActionType?.toLowerCase() === "order_approval"
    ) {
      return "order_approval";
    }
    return requestNames[0];
  }

  return payloadActionType ?? rawKind;
}

/** The confirmation-only prompt a chat shows: which action, and where to confirm it. */
export function webOnlyApprovalText(
  actionType: string | undefined,
  link: string,
  actionRequests?: InterruptActionRequest[],
): string {
  const effectiveActionType = resolveWebOnlyApprovalActionType({
    payloadActionType: actionType,
    actionRequests,
  });
  const label =
    lookupWebOnlyActionLabel(effectiveActionType) ??
    lookupWebOnlyActionLabel(actionType) ??
    "一项操作";
  return `「${label}」需要在网页上确认，请打开：${link}`;
}

export class ChannelGatewayOrchestrator {
  private readonly backend: BackendClient;
  private readonly adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  private readonly deliveryService: DeliveryService;
  private readonly agentRoutes: AgentRoutesHandler;
  private readonly webBaseUrl: string;
  private readonly backoffDelayFn: (ms: number) => Promise<void>;
  private readonly busyThreadMaxWaitMs: number;
  private readonly nowFn: () => number;
  private readonly processingFeedback: ProcessingFeedbackCoordinator;
  private shuttingDown = false;

  readonly activeWatches = new Map<string, ActiveWatchHandle>();
  private readonly activeBackgroundRunIds = new Map<string, Set<string>>();
  private readonly activeInboundTurns = new Set<Promise<void>>();
  private readonly settledDeliverableKeys = new Set<string>();
  private readonly inFlightDeliverableSettlements = new Map<
    string,
    Promise<boolean>
  >();
  private readonly settledChartKeys = new Set<string>();
  private readonly inFlightChartSettlements = new Map<
    string,
    Promise<boolean>
  >();
  private readonly pendingChartReceipts = new Map<
    string,
    PendingChartDeliveryReceipt
  >();
  private static readonly MAX_SETTLED_DELIVERABLE_KEYS = 2_000;
  private readonly deliveredPersonalFirstTouchThreads = new Set<string>();
  private readonly logger = createSubsystemLogger("orchestrator");
  private readonly activeInboundUploadControllers = new Map<
    AbortController,
    {
      accountId?: string;
      botGeneration?: number;
      abortReason?: WeixinAttachmentAbortReason;
    }
  >();
  private startupRecoveryPromise: Promise<void> | null = null;

  constructor(options: {
    backend: BackendClient;
    adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
    deliveryService: DeliveryService;
    agentRoutes: AgentRoutesHandler;
    webBaseUrl: string;
    statusFeedback?: StatusFeedbackConfig;
    backoffDelayFn?: (ms: number) => Promise<void>;
    busyThreadMaxWaitMs?: number;
    nowFn?: () => number;
  }) {
    this.backend = options.backend;
    this.adapters = options.adapters;
    this.deliveryService = options.deliveryService;
    this.agentRoutes = options.agentRoutes;
    this.webBaseUrl = options.webBaseUrl.replace(/\/+$/, "");
    this.backoffDelayFn =
      options.backoffDelayFn ??
      ((ms: number) => new Promise((r) => setTimeout(r, ms)));
    this.busyThreadMaxWaitMs =
      options.busyThreadMaxWaitMs ?? DEFAULT_BUSY_THREAD_MAX_WAIT_MS;
    this.nowFn = options.nowFn ?? (() => Date.now());
    this.processingFeedback = new ProcessingFeedbackCoordinator({
      adapters: options.adapters,
      config: options.statusFeedback,
    });
    this.deliveryService.addFirstCurrentDeliveryListener((info) => {
      this.processingFeedback.onFirstCurrentDelivery(info);
    });

    for (const adapter of this.adapters.values()) {
      adapter.onInboundEvent(async (event) => {
        await this.handleInboundEvent(event);
      });
      adapter.onNativeInteraction?.(async (event) => {
        await this.handleNativeInteraction(event);
      });
      adapter.onWeixinAccountInvalidated?.(
        ({ accountId, activeBotGeneration, includeSameGeneration }) => {
          this.cancelInboundUploadsForAccount(accountId, activeBotGeneration, {
            includeSameGeneration,
          });
        },
      );
    }
  }

  /**
   * R6 #2, R7 #1 & R9 #2: Cancels in-flight Weixin inbound attachment tasks for an account.
   * Only driven by authoritative snapshot application:
   * - When `activeBotGeneration` is provided for a generation bump (`includeSameGeneration !== true`),
   *   only strictly older generations (`botGeneration < activeBotGeneration`) are aborted.
   * - When `includeSameGeneration === true` (same-generation credential rotation), all tasks started
   *   prior to the switch with `botGeneration <= activeBotGeneration` are aborted so a task whose
   *   final I-7 returns after new credentials are installed still throws `superseded`.
   * - When `activeBotGeneration` is omitted (account removed/disabled), all tasks for the account are aborted.
   */
  cancelInboundUploadsForAccount(
    accountId: string | null | undefined,
    activeBotGeneration?: number,
    options?: { includeSameGeneration?: boolean },
  ): void {
    const cleanAccountId = accountId?.trim();
    if (!cleanAccountId) return;
    const includeSameGen = options?.includeSameGeneration === true;
    for (const [ctrl, meta] of [...this.activeInboundUploadControllers.entries()]) {
      if (meta.accountId !== cleanAccountId) continue;
      if (
        typeof activeBotGeneration === "number" &&
        typeof meta.botGeneration === "number" &&
        (includeSameGen
          ? meta.botGeneration > activeBotGeneration
          : meta.botGeneration >= activeBotGeneration)
      ) {
        continue;
      }
      meta.abortReason = "superseded";
      ctrl.abort();
      this.activeInboundUploadControllers.delete(ctrl);
    }
  }

  private notifyWeixinAccountRotation(
    _accountId: string | null | undefined,
    reason: string,
  ): void {
    // R7 #1: Rotation hints without an authoritative snapshot generation only trigger
    // accounts sync; snapshot application performs generation-aware cancellation.
    void this.adapters.get("weixin")?.triggerAccountsSync?.(undefined, reason);
  }

  /**
   * R7 #3 & R8 #4: Marks a superseded Weixin inbound message as terminal `failed`
   * and persists the failure reason via the contract §6.4 `raw_payload_patch` field
   * (no invented top-level field on I-7 inbound progress). Failures are logged with
   * whitelisted error fields only.
   */
  private async markWeixinInboundSuperseded(
    platform: ChannelPlatform,
    messageId: string,
  ): Promise<void> {
    try {
      await this.backend.updateInboundProgress({
        platform,
        message_id: messageId,
        status: "failed",
        fallback_pending: false,
        raw_payload_patch: {
          error_code: "weixin_bot_generation_superseded",
        },
      });
    } catch (err) {
      if (isStaleGatewayEpochError(err)) {
        return;
      }
      this.logger.warn(
        "Failed to mark superseded Weixin inbound event as terminal failed",
        {
          platform,
          message_id: messageId,
          ...extractSafeLogErrorFields(err),
        },
      );
    }
  }

  /**
   * Synchronously aborts all in-flight Weixin inbound attachment tasks
   * (Decision #1 & R5 #1).
   */
  abortAllActiveInboundUploads(): void {
    this.shuttingDown = true;
    for (const [ctrl, meta] of [...this.activeInboundUploadControllers.entries()]) {
      meta.abortReason = "shutdown";
      ctrl.abort();
    }
    this.activeInboundUploadControllers.clear();
  }

  private async dispatchOneTimeWeixinReply(
    envelope: OneTimeWeixinReplyEnvelope | null | undefined,
    dedupeKey: string,
    syncRevision?: number | null,
  ): Promise<void> {
    if (!envelope) return;
    const wxAdapter = this.adapters.get("weixin");
    try {
      await wxAdapter?.sendOneTimeReply?.(envelope, dedupeKey);
    } catch {
      // Best-effort D16 one-time reply: never retry or block unbind settlement.
    }
    if (wxAdapter?.triggerAccountsSync) {
      void wxAdapter.triggerAccountsSync(syncRevision ?? undefined, "one_time_reply");
    }
  }

  // =========================================================================
  // §3.3 & §1.3.1 Inbound Event Entry Point
  // =========================================================================

  async handleInboundEvent(event: InboundChannelEvent): Promise<void> {
    if (
      event.platform === "weixin" &&
      (event.chatType === "group" ||
        Boolean(
          typeof event.rawPayload?.group_id === "string" &&
            event.rawPayload.group_id.trim(),
        ))
    ) {
      const requestKey = makeInboundRequestKey(event.platform, event.messageId);
      await this.backend.registerAndResolveInbound({
        platform: event.platform,
        platform_user_id: event.platformUserId,
        chat_type: "group",
        chat_address: event.chatAddress,
        thread_scope_key: event.threadScopeKey,
        message_id: event.messageId,
        request_key: requestKey,
        raw_payload: this.buildInboundRawPayload(event),
        weixin_account_id: event.weixinAccountId ?? null,
        weixin_context_token: null,
        weixin_bot_id: event.weixinBotId ?? null,
        weixin_bot_generation: event.weixinBotGeneration ?? null,
        weixin_context_seq: event.weixinContextSeq ?? null,
      });
      return;
    }

    if (event.rawPayload?.invalid_inbound_event === true) {
      const requestKey = makeInboundRequestKey(event.platform, event.messageId);
      await this.backend.registerAndResolveInbound({
        platform: event.platform,
        platform_user_id: event.platformUserId,
        chat_type: event.chatType,
        chat_address: event.chatAddress,
        thread_scope_key: event.threadScopeKey,
        message_id: event.messageId,
        request_key: requestKey,
        raw_payload: this.buildInboundRawPayload(event),
        weixin_account_id: event.weixinAccountId ?? null,
        weixin_context_token: null,
        weixin_bot_id: event.weixinBotId ?? null,
        weixin_bot_generation: event.weixinBotGeneration ?? null,
        weixin_context_seq: event.weixinContextSeq ?? null,
      });
      return;
    }

    // Group chats require an explicit structured mention trigger (`isGroupTrigger === true`).
    if (event.chatType === "group" && event.isGroupTrigger !== true) {
      return;
    }

    // Check for slash commands (§3.3 & §4.4): Weixin-only collect/done/cancel/help stay isolated from Feishu/Discord
    const parsedCmd = parseSlashCommand(
      event.commandText ?? event.text ?? "",
      event.chatType,
      event.platform,
    );
    if (parsedCmd) {
      await this.tryHandleSlashCommand(event, parsedCmd);
      return;
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
      raw_payload: this.buildInboundRawPayload(event),
      external_topic_id: event.externalTopicId ?? null,
      root_message_id: event.rootMessageId ?? null,
      weixin_account_id: event.weixinAccountId ?? null,
      weixin_context_token: event.weixinContextToken ?? null,
      weixin_bot_id: event.weixinBotId ?? null,
      weixin_bot_generation: event.weixinBotGeneration ?? null,
      weixin_context_seq: event.weixinContextSeq ?? null,
      ...(event.weixinMediaMeta && event.weixinMediaMeta.length > 0
        ? { weixin_media_meta: event.weixinMediaMeta }
        : {}),
      ...(event.unsupportedItemTypes && event.unsupportedItemTypes.length > 0
        ? { unsupported_item_types: event.unsupportedItemTypes }
        : {}),
    });

    if (
      typeof resolved.resolved_scope_key === "string" &&
      resolved.resolved_scope_key.trim().length > 0
    ) {
      event.threadScopeKey = resolved.resolved_scope_key.trim();
    }
    if (
      resolved.topic_newly_registered &&
      resolved.external_topic_id &&
      resolved.resolved_scope_key &&
      resolved.topic_initiator_platform_user_id
    ) {
      this.adapters.get(event.platform)?.recordBotTopic?.({
        chatAddress: event.chatAddress,
        externalTopicId: resolved.external_topic_id,
        canonicalScopeKey: resolved.resolved_scope_key,
        initiatorPlatformUserId: resolved.topic_initiator_platform_user_id,
        rootMessageId: event.rootMessageId ?? null,
      });
    }

    // D16 one-time reply (e.g. conflict auto-unbind) — send directly with inbound context_token without preflight.
    // R8 #1: Do not blindly cancel attachments here; `dispatchOneTimeWeixinReply` triggers snapshot sync.
    if (event.platform === "weixin" && resolved.one_time_weixin_reply) {
      await this.dispatchOneTimeWeixinReply(
        resolved.one_time_weixin_reply,
        `onetime:${event.platform}:${event.messageId}`,
      );
      return;
    }

    // §4.4 pure unsupported / merged-forward Weixin message: conditional hint when usable token exists.
    if (
      event.platform === "weixin" &&
      resolved.deny_reason === "unsupported_merged_forward"
    ) {
      if (resolved.has_usable_weixin_context_token === true) {
        await this.sendInboundNotice(
          event,
          resolved.dalton_user_id,
          `notice:${event.platform}:${event.messageId}:merged_forward_unsupported`,
          "暂无法直接解析微信『合并转发』的聊天记录，请改用『逐条转发』或发送聊天长截图。",
        );
      }
      return;
    }

    await this.launchInboundTurn(event, () =>
      this.executeRegisteredInboundTurn(event, resolved),
    );
  }

  private buildInboundRawPayload(
    event: InboundChannelEvent,
  ): Record<string, unknown> {
    return {
      ...(event.rawPayload ?? {}),
      platform_user_id: event.platformUserId,
      text: event.text,
      ...(event.commandText !== undefined
        ? { command_text: event.commandText }
        : {}),
      ...(event.voiceWithoutTranscription
        ? { voice_without_transcription: true }
        : {}),
      chat_id: event.chatId,
      chat_name: event.chatName ?? null,
      attachments: (event.attachments ?? []).map((attachment) => {
        const extra = attachment as InboundAttachmentDescriptor &
          Record<string, unknown>;
        return {
          id: attachment.id,
          filename: attachment.filename,
          mimeType: attachment.mimeType,
          ...(attachment.sizeBytes !== undefined
            ? { sizeBytes: attachment.sizeBytes }
            : {}),
          ...(typeof extra.kind === "string" ? { kind: extra.kind } : {}),
          ...(typeof extra.encryptQueryParam === "string"
            ? { encryptQueryParam: extra.encryptQueryParam }
            : {}),
          ...(typeof extra.fullUrl === "string"
            ? { fullUrl: extra.fullUrl }
            : {}),
          ...(typeof extra.aesKeyHex === "string"
            ? { aesKeyHex: extra.aesKeyHex }
            : {}),
          ...(typeof extra.aesKeyBase64 === "string"
            ? { aesKeyBase64: extra.aesKeyBase64 }
            : {}),
        };
      }),
    };
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
      raw_payload: this.buildInboundRawPayload(event),
      weixin_account_id: event.weixinAccountId ?? null,
      weixin_context_token: event.weixinContextToken ?? null,
      weixin_bot_id: event.weixinBotId ?? null,
      weixin_bot_generation: event.weixinBotGeneration ?? null,
      weixin_context_seq: event.weixinContextSeq ?? null,
      ...(event.weixinMediaMeta && event.weixinMediaMeta.length > 0
        ? { weixin_media_meta: event.weixinMediaMeta }
        : {}),
      ...(event.unsupportedItemTypes && event.unsupportedItemTypes.length > 0
        ? { unsupported_item_types: event.unsupportedItemTypes }
        : {}),
    });
    if (event.platform === "weixin" && reresolved.one_time_weixin_reply) {
      await this.dispatchOneTimeWeixinReply(
        reresolved.one_time_weixin_reply,
        `onetime:${event.platform}:${event.messageId}`,
      );
      return;
    }
    await this.executeRegisteredInboundTurn(event, reresolved);
  }

  private async launchInboundTurn(
    event: InboundChannelEvent,
    runTurn: () => Promise<void>,
  ): Promise<void> {
    const turnPromise = (async () => {
      let recoveryFailed = false;
      try {
        await runTurn();
      } catch (err) {
        if (isStaleGatewayEpochError(err)) {
          return;
        }
        if (err instanceof WeixinAttachmentAbortedError) {
          if (err.reason === "superseded") {
            recoveryFailed = true;
            await this.markWeixinInboundSuperseded(
              event.platform,
              event.messageId,
            );
          }
          return;
        }
        try {
          await this.recoverDetachedInboundTurn(event);
        } catch (retryErr) {
          if (isStaleGatewayEpochError(retryErr)) {
            return;
          }
          if (retryErr instanceof WeixinAttachmentAbortedError) {
            if (retryErr.reason === "superseded") {
              recoveryFailed = true;
              await this.markWeixinInboundSuperseded(
                event.platform,
                event.messageId,
              );
            }
            return;
          }
          // The inbound row keeps its last persisted state, so a later
          // recovery pass can query and replay with the same request key.
          // Do not stamp a permanent failure reaction while startup recovery may still deliver.
          recoveryFailed = true;
        }
      } finally {
        this.processingFeedback.settleMessage(
          event,
          recoveryFailed ? "failed" : "done",
        );
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
    // Group and invalid rows stay silent. Any other persisted denial, such as
    // `not_linked`, still sends its notice on a later register of the same
    // message. The delivery key does not change, so a notice that already
    // completed is not sent again.
    if (resolved.existing_status === "ignored") {
      const persistedReason = resolved.deny_reason ?? "";
      if (
        persistedReason === "weixin_group_unsupported" ||
        persistedReason === "invalid_inbound_event" ||
        persistedReason.length === 0
      ) {
        return;
      }
    }

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
          generation: resolved.generation,
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
      const denyReason =
        resolved.deny_reason ??
        (resolved.linked ? "workspace_write_denied" : "not_linked");
      if (
        event.platform === "weixin" &&
        WEIXIN_ROTATION_DENY_CODES.has(denyReason)
      ) {
        this.notifyWeixinAccountRotation(
          event.weixinAccountId ?? resolved.weixin_account_id,
          denyReason,
        );
      }
      if (
        denyReason === "weixin_group_unsupported" ||
        denyReason === "invalid_inbound_event"
      ) {
        return;
      }
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "failed",
      });
      const denyNotice = this.formatDenyNotice(event.platform, denyReason);
      await this.sendInboundNotice(
        event,
        resolved.dalton_user_id,
        `notice:${event.platform}:${event.messageId}:deny_${denyReason}`,
        denyNotice,
      );
      return;
    }

    const userId = resolved.dalton_user_id;

    // Step 2b: Voice message without transcription and without attachments
    if (
      (event.voiceWithoutTranscription === true ||
        event.rawPayload?.voice_without_transcription === true) &&
      event.text.trim().length === 0 &&
      (event.attachments ?? []).length === 0
    ) {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "completed",
      });
      await this.sendInboundNotice(
        event,
        userId,
        `notice:${event.platform}:${event.messageId}:voice_no_text`,
        "无法读取这条语音消息的文字内容（当前未包含语音转写）。请在微信中开启语音转文字后重试，或直接发送文字消息。",
      );
      return;
    }

    // Step 2c: Start best-effort processing status feedback (non-blocking, in-memory only).
    // When `recoverDetachedInboundTurn` re-enters this method for the same message,
    // `begin()` reuses the still-active handle without adding a duplicate reaction.
    this.processingFeedback.begin(event);

    // Step 3: Upload attachments to workspace if present (§1.3.1)
    let uploadedAttachments: UploadedAttachmentRecord[] =
      resolved.uploaded_attachments ?? [];

    const attachmentDescriptors = event.attachments ?? [];
    if (
      attachmentDescriptors.length > 0 &&
      uploadedAttachments.length < attachmentDescriptors.length
    ) {
      const uploadOutcome = await this.downloadAndUploadAttachments(
        event,
        resolved,
        userId,
        uploadedAttachments,
      );
      if (!uploadOutcome.ok) {
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          status: "failed",
        });
        const noticeOutcome = await this.sendNoticeDeliveryOutcome({
          deliveryKey: `notice:${event.platform}:${event.messageId}:attachment_err`,
          userId,
          platform: event.platform,
          target: event.chatAddress,
          inboundMessageId: event.messageId,
          text: uploadOutcome.errorMessage,
          replyToMessageId: event.messageId,
          threadScopeKey: event.threadScopeKey,
        });
        this.processingFeedback.settleMessage(
          event,
          noticeOutcome.definitiveFailure ? "delivery_failed" : "failed",
        );
        return;
      }
      uploadedAttachments = uploadOutcome.uploaded;
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
  // §3.3 Slash Commands (`/bind`, `/unbind`, `/workspace`, `/main`, `/new`, `/stop`, `/<workspace_name>`)
  // =========================================================================

  private bindConfirmationText(
    event: InboundChannelEvent,
    action: "bind" | "unbind",
    result: object,
  ): string {
    const fields = confirmationFields(result);
    const reason = fields.reason ?? "";
    if (action === "bind" && !fields.ok) {
      if (
        reason === "missing_bind_code" ||
        reason === "missing_argument"
      ) {
        return missingBindCodeUsage(this.webBaseUrl);
      }
      if (reason === "revoked_in_group") {
        return "For security, `/bind` cannot be used in a group chat. This code has been revoked; please generate a new code and send `/bind <CODE>` in a direct message.";
      }
      if (reason === "platform_mismatch") {
        const expected = fields.expectedPlatform ?? "another platform";
        return `This bind code was generated for ${expected}, not ${event.platform}.`;
      }
      return expiredBindCodeUsage(this.webBaseUrl);
    }
    if (action === "bind") {
      return `Successfully linked your ${event.platform} account to Dalton.`;
    }
    if (!fields.ok) {
      return `Your ${event.platform} account is not currently linked.`;
    }
    return `Unlinked your ${event.platform} account from Dalton.`;
  }

  private storedBindingConfirmation(
    row: {
      consumed_bind_result?: Record<string, unknown> | null;
      command_result?: object | null;
      command_request?: object | null;
    },
  ): {
    action: "bind" | "unbind";
    result: object;
    legacyCommandRecord: boolean;
  } | null {
    const consumed = row.consumed_bind_result;
    if (consumed && typeof consumed === "object" && !Array.isArray(consumed)) {
      const action = confirmationFields(consumed).action ?? "bind";
      return { action, result: consumed, legacyCommandRecord: false };
    }
    const commandResult = row.command_result;
    if (
      !commandResult ||
      typeof commandResult !== "object" ||
      Array.isArray(commandResult)
    ) {
      return null;
    }
    const fields = confirmationFields(commandResult);
    const requestAction = storedBindingAction(row.command_request);
    let action: "bind" | "unbind" | null = fields.action;
    if (!action && fields.reason === "missing_bind_code") action = "bind";
    if (!action && requestAction && recordField(commandResult, "ok") !== undefined) {
      const okValue = recordField(commandResult, "ok");
      if (okValue === true || okValue === false) action = requestAction;
    }
    if (!action) return null;
    return {
      action,
      result: commandResult,
      legacyCommandRecord: fields.deliveryKey == null,
    };
  }

  /**
   * One confirmation entry for a live bind/unbind and for startup recovery.
   * `replyPending === false` skips the send, the inbound update, and the
   * watch close. A saved `delivery_key` is reused. A record without that key
   * keeps `notice:<platform>:<message>:command` on every attempt, so a later
   * duplicate does not become a second message. `closeWatchesOnSuccessfulUnbind`
   * is set by a fresh live unbind and by a consumed or keyed recovery; old
   * production recovery leaves watches open. A close uses `generations` from
   * the unbind result: only a watch for that user, platform, and scope whose
   * generation is lower than the stored post-unbind generation is aborted.
   * A missing or invalid map does not close any watch.
   */
  private async deliverStoredCommandConfirmation(params: {
    event: InboundChannelEvent;
    userId: string | null;
    action: "bind" | "unbind";
    result: object;
    replyPending?: boolean;
    closeWatchesOnSuccessfulUnbind?: boolean;
  }): Promise<boolean> {
    if (params.replyPending === false) return false;
    const fields = confirmationFields(params.result);
    const legacy = fields.deliveryKey == null;
    const deliveryKey =
      fields.deliveryKey ??
      `notice:${params.event.platform}:${params.event.messageId}:command`;
    const linkedUser =
      params.userId && params.userId.trim().length > 0
        ? params.userId.trim()
        : null;
    if (
      params.closeWatchesOnSuccessfulUnbind &&
      params.action === "unbind" &&
      fields.ok &&
      linkedUser
    ) {
      this.closeWatchesOlderThanStoredUnbind(
        linkedUser,
        params.event.platform,
        params.result,
      );
    }
    let delivered = false;
    if (legacy && !linkedUser) {
      const adapter = this.adapters.get(params.event.platform);
      if (!adapter || !adapter.isConfigured() || !adapter.isConnected()) {
        return false;
      }
      try {
        await this.sendDirectAdapterNotice(
          params.event,
          this.bindConfirmationText(params.event, params.action, params.result),
        );
        delivered = true;
      } catch {
        delivered = false;
      }
    } else {
      delivered = await this.sendInboundNotice(
        params.event,
        linkedUser,
        deliveryKey,
        this.bindConfirmationText(params.event, params.action, params.result),
      );
    }
    if (!delivered) return false;
    await this.backend.updateInboundProgress({
      platform: params.event.platform,
      message_id: params.event.messageId,
      status: legacy ? (fields.ok ? "completed" : "failed") : "completed",
      command_reply_pending: false,
      command_effect_status: "done",
      fallback_pending: false,
    });
    return true;
  }

  private async recoverStoredBindingConfirmation(
    row: RecoverInboundEventRow,
    event: InboundChannelEvent,
  ): Promise<boolean> {
    const stored = this.storedBindingConfirmation({
      consumed_bind_result: row.consumed_bind_result ?? null,
      command_result:
        row.command_result && typeof row.command_result === "object"
          ? row.command_result
          : null,
      command_request:
        row.command_request && typeof row.command_request === "object"
          ? row.command_request
          : null,
    });
    if (!stored) return false;
    if (row.command_reply_pending === false) return true;
    const storedUser = confirmationFields(stored.result).userId;
    const rowUser =
      typeof row.dalton_user_id === "string" && row.dalton_user_id.trim()
        ? row.dalton_user_id.trim()
        : null;
    await this.deliverStoredCommandConfirmation({
      event,
      userId: storedUser ?? rowUser,
      action: stored.action,
      result: stored.result,
      replyPending: row.command_reply_pending,
      closeWatchesOnSuccessfulUnbind:
        !stored.legacyCommandRecord && stored.action === "unbind",
    });
    return true;
  }

  /** Repeat a bind or unbind that I-1 already stored, without consuming again. */
  private async replayRegisteredBinding(
    event: InboundChannelEvent,
    resolved: I1RegisterAndResolveResponse,
    fallbackAction: "bind" | "unbind",
  ): Promise<boolean> {
    const consumed = resolved.consumed_bind_result;
    if (consumed && typeof consumed === "object" && !Array.isArray(consumed)) {
      const fields = confirmationFields(consumed);
      const action = fields.action ?? fallbackAction;
      await this.deliverStoredCommandConfirmation({
        event,
        userId: fields.userId ?? resolved.dalton_user_id,
        action,
        result: consumed,
        replyPending: resolved.command_reply_pending,
        closeWatchesOnSuccessfulUnbind: action === "unbind",
      });
      return true;
    }
    const requestAction = storedBindingAction(resolved.command_request);
    const commandResult = resolved.command_result;
    if (
      !requestAction ||
      !commandResult ||
      typeof commandResult !== "object" ||
      Array.isArray(commandResult)
    ) {
      return false;
    }
    const okValue = recordField(commandResult, "ok");
    if (okValue !== true && okValue !== false) return false;
    const fields = confirmationFields(commandResult);
    await this.deliverStoredCommandConfirmation({
      event,
      userId: fields.userId ?? resolved.dalton_user_id,
      action: requestAction,
      result: commandResult,
      replyPending: resolved.command_reply_pending,
      closeWatchesOnSuccessfulUnbind:
        requestAction === "unbind" && fields.deliveryKey != null,
    });
    return true;
  }

  /**
   * R6 #1 & R7 #7: Weixin `/bind` usage and invalid-command hints register the inbound via I-1
   * (`register-and-resolve`, NEVER I-3 `cmd: "help"`) and deliver fixed gateway-generated text
   * through I-6 + `preflight-send`.
   * Retryable I-1 registration failures (5xx, 503 `roster_lookup_error`, 408/429, network/timeout)
   * propagate to `WeixinMonitor` so the cursor does NOT advance; explicit 4xx rejections are logged
   * with whitelisted error fields and allow the cursor to advance.
   */
  private async sendWeixinCommandHintViaPreflight(
    event: InboundChannelEvent,
    noticeKey: string,
    text: string,
  ): Promise<void> {
    let resolved: I1RegisterAndResolveResponse;
    try {
      resolved = await this.backend.registerAndResolveInbound({
        platform: "weixin",
        platform_user_id: event.platformUserId,
        chat_type: event.chatType,
        chat_address: event.chatAddress,
        thread_scope_key: event.threadScopeKey,
        message_id: event.messageId,
        request_key: makeInboundRequestKey(event.platform, event.messageId),
        raw_payload: {
          ...this.buildInboundRawPayload(event),
          hint_delivery: true,
        },
        weixin_account_id: event.weixinAccountId ?? null,
        weixin_context_token: event.weixinContextToken ?? null,
        weixin_bot_id: event.weixinBotId ?? null,
        weixin_bot_generation: event.weixinBotGeneration ?? null,
        weixin_context_seq: event.weixinContextSeq ?? null,
      });
    } catch (err) {
      if (isStaleGatewayEpochError(err)) {
        throw err;
      }
      const isExplicitNonRetryable4xx =
        err instanceof BackendHttpError &&
        err.status >= 400 &&
        err.status < 500 &&
        err.status !== 408 &&
        err.status !== 429;
      if (!isExplicitNonRetryable4xx) {
        // Retryable registration failure (5xx, 503 roster_lookup_error, timeout, network):
        // bubble up to WeixinMonitor so cursor is NOT committed.
        throw err;
      }
      this.logger.warn(
        "Weixin command hint registration rejected with 4xx; advancing cursor",
        {
          account_id: event.weixinAccountId ?? null,
          message_id: event.messageId,
          ...extractSafeLogErrorFields(err),
        },
      );
      return;
    }

    try {
      if (resolved.one_time_weixin_reply) {
        await this.dispatchOneTimeWeixinReply(
          resolved.one_time_weixin_reply,
          `onetime:${event.platform}:${event.messageId}:hint`,
        );
        return;
      }
      if (
        resolved.existing_status === "completed" ||
        resolved.existing_status === "ignored"
      ) {
        return;
      }
      if (!resolved.linked || !resolved.allowed || !resolved.dalton_user_id) {
        if (
          resolved.deny_reason &&
          WEIXIN_ROTATION_DENY_CODES.has(resolved.deny_reason)
        ) {
          this.notifyWeixinAccountRotation(
            event.weixinAccountId ?? resolved.weixin_account_id,
            resolved.deny_reason,
          );
        }
        this.logger.warn(
          "Skipping Weixin command hint because inbound account is not eligible",
          {
            account_id: event.weixinAccountId ?? null,
            message_id: event.messageId,
            deny_reason: sanitizeLogErrorCode(resolved.deny_reason),
          },
        );
        return;
      }
      const delivered = await this.sendInboundNotice(
        event,
        resolved.dalton_user_id,
        noticeKey,
        text,
      );
      if (delivered) {
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          status: "completed",
          command_reply_pending: false,
          fallback_pending: false,
        });
      }
    } catch (err) {
      if (isStaleGatewayEpochError(err)) {
        throw err;
      }
      this.logger.warn("Failed to deliver Weixin command hint; advancing cursor", {
        account_id: event.weixinAccountId ?? null,
        message_id: event.messageId,
        ...extractSafeLogErrorFields(err),
      });
    }
  }

  private async tryHandleSlashCommand(
    event: InboundChannelEvent,
    parsedCmd: ParsedSlashCommand,
  ): Promise<boolean> {
    if (
      event.platform === "weixin" &&
      (parsedCmd.kind === "bind" ||
        (parsedCmd.kind === "invalid" &&
          parsedCmd.reason === "missing_bind_code"))
    ) {
      await this.sendWeixinCommandHintViaPreflight(
        event,
        `notice:weixin:${event.messageId}:bind_qr_hint`,
        "个人微信请通过 Dalton 网页端「设置 → 消息渠道」扫码绑定，无需发送 `/bind` 指令。",
      );
      return true;
    }

    if (parsedCmd.kind === "invalid") {
      if (event.platform === "weixin") {
        await this.sendWeixinCommandHintViaPreflight(
          event,
          `notice:weixin:${event.messageId}:invalid_cmd`,
          parsedCmd.message,
        );
        return true;
      }
      await this.sendDirectAdapterNotice(event, parsedCmd.message);
      return true;
    }

    // Weixin `/unbind` invokes I-2 directly with all 4 required identity fields (Batch A contract)
    // and delivers D16 `one_time_weixin_reply` directly with inbound `context_token`.
    // R8 #1: Do NOT cancel in-flight attachments before I-2 succeeds; let snapshot sync drive cancellation.
    if (event.platform === "weixin" && parsedCmd.kind === "unbind") {
      const res = await this.backend.consumeBindCode({
        action: "unbind",
        platform: "weixin",
        platform_user_id: event.platformUserId,
        weixin_account_id: event.weixinAccountId ?? null,
        weixin_context_token: event.weixinContextToken ?? null,
        weixin_bot_id: event.weixinBotId ?? null,
        weixin_bot_generation: event.weixinBotGeneration ?? null,
        weixin_context_seq: event.weixinContextSeq ?? null,
        inbound_message_id: event.messageId,
        is_group: event.chatType === "group",
        message_id: event.messageId,
        chat_type: event.chatType,
        chat_address: event.chatAddress,
        scope_key: event.threadScopeKey,
        request_key: makeInboundRequestKey(event.platform, event.messageId),
      });
      if (res.ok && res.user_id?.trim()) {
        this.closeWatchesOlderThanStoredUnbind(
          res.user_id.trim(),
          "weixin",
          res,
        );
      }
      if (res.one_time_weixin_reply) {
        await this.dispatchOneTimeWeixinReply(
          res.one_time_weixin_reply,
          `onetime:weixin:${event.messageId}:unbind`,
          res.accounts_revision ?? undefined,
        );
      } else {
        const wxAdapter = this.adapters.get("weixin");
        if (wxAdapter?.triggerAccountsSync) {
          void wxAdapter.triggerAccountsSync(
            res.accounts_revision ?? undefined,
            "weixin_unbind",
          );
        }
      }
      return true;
    }

    let resolved: I1RegisterAndResolveResponse | undefined;
    if (parsedCmd.kind === "bind" || parsedCmd.kind === "unbind") {
      const reqKey = makeInboundRequestKey(event.platform, event.messageId);
      resolved = await this.backend.registerAndResolveInbound({
        platform: event.platform,
        platform_user_id: event.platformUserId,
        chat_type: event.chatType,
        chat_address: event.chatAddress,
        thread_scope_key: event.threadScopeKey,
        message_id: event.messageId,
        request_key: reqKey,
        raw_payload: {
          ...this.buildInboundRawPayload(event),
          command: parsedCmd.kind,
        },
        weixin_account_id: event.weixinAccountId ?? null,
        weixin_context_token: event.weixinContextToken ?? null,
        weixin_bot_id: event.weixinBotId ?? null,
        weixin_bot_generation: event.weixinBotGeneration ?? null,
        weixin_context_seq: event.weixinContextSeq ?? null,
      });
      if (
        resolved.deny_reason === "weixin_group_unsupported" ||
        resolved.deny_reason === "invalid_inbound_event"
      ) {
        return true;
      }
      if (resolved.existing_status === "ignored" && parsedCmd.kind !== "bind") {
        const reason = resolved.deny_reason ?? "";
        if (
          reason === "weixin_group_unsupported" ||
          reason === "invalid_inbound_event" ||
          reason.length === 0
        ) {
          return true;
        }
      }
    }

    if (parsedCmd.kind === "bind") {
      if (!resolved) {
        return true;
      }
      if (await this.replayRegisteredBinding(event, resolved, "bind")) {
        return true;
      }
      const arg = parsedCmd.code;
      if (!arg) {
        const deliveryKey = `notice:${event.platform}:${event.messageId}:bind_usage`;
        const usageResult: Record<string, unknown> = {
          ok: false,
          action: "bind",
          reason: "missing_bind_code",
          delivery_key: deliveryKey,
        };
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          command_effect_status: "done",
          command_reply_pending: true,
          command_result: usageResult as unknown as I3SessionCommandResponse,
        });
        await this.deliverStoredCommandConfirmation({
          event,
          userId: resolved.dalton_user_id,
          action: "bind",
          result: usageResult,
        });
        return true;
      }
      const res = await this.backend.consumeBindCode({
        action: "bind",
        platform: event.platform,
        platform_user_id: event.platformUserId,
        platform_username: event.platformUsername ?? null,
        platform_tenant_key: event.platformTenantKey ?? null,
        weixin_context_token: event.weixinContextToken ?? null,
        weixin_bot_id: event.weixinBotId ?? null,
        weixin_bot_generation: event.weixinBotGeneration ?? null,
        inbound_message_id: event.messageId,
        code: arg,
        is_group: event.chatType === "group",
        message_id: event.messageId,
        chat_type: event.chatType,
        chat_address: event.chatAddress,
        scope_key: event.threadScopeKey,
        request_key: makeInboundRequestKey(event.platform, event.messageId),
      });
      await this.deliverStoredCommandConfirmation({
        event,
        userId: res.user_id?.trim() || resolved.dalton_user_id,
        action: "bind",
        result: res,
        replyPending: res.command_reply_pending,
        closeWatchesOnSuccessfulUnbind: false,
      });
      return true;
    }

    if (parsedCmd.kind === "unbind") {
      if (!resolved) {
        return true;
      }
      if (await this.replayRegisteredBinding(event, resolved, "unbind")) {
        return true;
      }
      const res = await this.backend.consumeBindCode({
        action: "unbind",
        platform: event.platform,
        platform_user_id: event.platformUserId,
        weixin_context_token: event.weixinContextToken ?? null,
        weixin_bot_id: event.weixinBotId ?? null,
        weixin_bot_generation: event.weixinBotGeneration ?? null,
        inbound_message_id: event.messageId,
        is_group: event.chatType === "group",
        message_id: event.messageId,
        chat_type: event.chatType,
        chat_address: event.chatAddress,
        scope_key: event.threadScopeKey,
        request_key: makeInboundRequestKey(event.platform, event.messageId),
      });
      const legacyReplay =
        res.duplicate === true &&
        !(typeof res.delivery_key === "string" && res.delivery_key.length > 0);
      await this.deliverStoredCommandConfirmation({
        event,
        userId: res.user_id?.trim() || resolved.dalton_user_id,
        action: "unbind",
        result: res,
        replyPending: res.command_reply_pending,
        closeWatchesOnSuccessfulUnbind: !legacyReplay,
      });
      return true;
    }

    const reqKey = makeInboundRequestKey(event.platform, event.messageId);
    const sessionCommandPayload = {
      cmd: parsedCmd.cmd,
      platform: event.platform,
      platform_user_id: event.platformUserId,
      chat_type: event.chatType,
      chat_address: event.chatAddress,
      chat_id: event.chatId,
      chat_name: event.chatName ?? null,
      scope_key: event.threadScopeKey,
      message_id: event.messageId,
      request_key: reqKey,
      raw_payload: {
        ...this.buildInboundRawPayload(event),
        command: parsedCmd.canonicalCommand,
        cmd: parsedCmd.cmd,
      },
      weixin_account_id: event.weixinAccountId ?? null,
      weixin_context_token: event.weixinContextToken ?? null,
      weixin_bot_id: event.weixinBotId ?? null,
      weixin_bot_generation: event.weixinBotGeneration ?? null,
      weixin_context_seq: event.weixinContextSeq ?? null,
      weixin_context_source:
        event.weixinContextSeq != null ? "protocol" : null,
      target_kind: parsedCmd.targetKind ?? null,
      workspace_id: parsedCmd.workspaceId ?? null,
      workspace_query: parsedCmd.workspaceQuery ?? null,
      ...(parsedCmd.commandArg !== undefined
        ? { command_arg: parsedCmd.commandArg }
        : {}),
    };

    const cmdRes = await this.backend.sessionCommand(sessionCommandPayload);

    await this.completeSessionCommandExecution({
      event,
      cmd: parsedCmd.cmd,
      targetKind: parsedCmd.targetKind ?? null,
      workspaceQuery: parsedCmd.workspaceQuery ?? null,
      cmdRes,
    });
    return true;
  }

  // =========================================================================
  // Discord Native Interactions (`INTERACTION_CREATE`)
  // =========================================================================

  async handleNativeInteraction(event: NativeInteractionEvent): Promise<void> {
    if (event.kind === "autocomplete") {
      await this.handleNativeAutocompleteInteraction(event);
      return;
    }
    await this.handleNativeCommandInteraction(event);
  }

  private async handleNativeAutocompleteInteraction(
    event: NativeAutocompleteInteractionEvent,
  ): Promise<void> {
    const deadlineMs = event.deadlineMs ?? event.responder.receivedAtMs + 1_800;
    const remainingMs = deadlineMs - this.nowFn();
    if (remainingMs <= 0) {
      await event.responder.respondChoices([]);
      return;
    }
    const budgetMs = Math.min(1_800, remainingMs);
    let timeoutHandle: ReturnType<typeof setTimeout> | undefined;
    try {
      const queryPromise = this.backend.sessionCommand({
        cmd: "workspace_list",
        platform: event.platform,
        platform_user_id: event.platformUserId,
        chat_type: event.chatType,
        chat_address: event.chatAddress,
        chat_id: event.chatId,
        chat_name: event.chatName ?? null,
        scope_key: event.threadScopeKey,
        is_internal_query: true,
      });
      const timeoutPromise = new Promise<null>((resolve) => {
        timeoutHandle = setTimeout(() => resolve(null), budgetMs);
        (timeoutHandle as { unref?: () => void }).unref?.();
      });
      const res = await Promise.race([queryPromise, timeoutPromise]);
      if (
        !res ||
        !res.ok ||
        res.allowed === false ||
        res.linked === false ||
        !Array.isArray(res.workspaces)
      ) {
        await event.responder.respondChoices([]);
        return;
      }

      const rawQuery = (event.query ?? "").trim().toLowerCase();
      const prefixMatches: NativeAutocompleteChoice[] = [];
      const substringMatches: NativeAutocompleteChoice[] = [];
      for (const ws of res.workspaces) {
        const wsId = typeof ws.workspace_id === "string" ? ws.workspace_id.trim() : "";
        const wsName = typeof ws.name === "string" ? ws.name.trim() : "";
        if (!wsId || !wsName) continue;
        if (!rawQuery) {
          prefixMatches.push({ name: wsName, value: wsId });
          continue;
        }
        const lowerName = wsName.toLowerCase();
        const lowerId = wsId.toLowerCase();
        if (lowerName.startsWith(rawQuery) || lowerId.startsWith(rawQuery)) {
          prefixMatches.push({ name: wsName, value: wsId });
        } else if (lowerName.includes(rawQuery) || lowerId.includes(rawQuery)) {
          substringMatches.push({ name: wsName, value: wsId });
        }
      }
      const choices = [...prefixMatches, ...substringMatches].slice(0, 25);
      await event.responder.respondChoices(choices);
    } catch {
      await event.responder.respondChoices([]);
    } finally {
      if (timeoutHandle) clearTimeout(timeoutHandle);
    }
  }

  private async handleNativeCommandInteraction(
    event: NativeCommandInteractionEvent,
  ): Promise<void> {
    const ackOutcome = event.responder.ackResult.outcome;
    const rawPayload: Record<string, unknown> = {
      ...event.rawPayload,
      platform_user_id: event.platformUserId,
      ...(event.platformUsername
        ? { platform_username: event.platformUsername }
        : {}),
      chat_id: event.chatId,
      ...(event.chatName ? { chat_name: event.chatName } : {}),
      command: `/${event.commandName}`,
      cmd: event.cmd,
      native_interaction: true,
      native_ack_status: ackOutcome,
      native_reply_status:
        ackOutcome === "acknowledged" ? "pending" : "not_applicable",
    };

    if (ackOutcome !== "acknowledged") {
      // Initial callback failed or had an unknown outcome: record the unexecuted
      // interaction in the ledger without executing the command or touching session state.
      await this.backend.sessionCommand({
        cmd: event.cmd,
        platform: event.platform,
        platform_user_id: event.platformUserId,
        chat_type: event.chatType,
        chat_address: event.chatAddress,
        chat_id: event.chatId,
        chat_name: event.chatName ?? null,
        scope_key: event.threadScopeKey,
        message_id: event.interactionId,
        request_key: event.requestKey,
        raw_payload: rawPayload,
        target_kind: event.targetKind ?? null,
        workspace_id: event.workspaceId ?? null,
        workspace_query: event.workspaceQuery ?? null,
        native_interaction: true,
        native_ack_status: ackOutcome,
        native_reply_status: "not_applicable",
      });
      return;
    }

    const cmdRes = await this.backend.sessionCommand({
      cmd: event.cmd,
      platform: event.platform,
      platform_user_id: event.platformUserId,
      chat_type: event.chatType,
      chat_address: event.chatAddress,
      chat_id: event.chatId,
      chat_name: event.chatName ?? null,
      scope_key: event.threadScopeKey,
      message_id: event.interactionId,
      request_key: event.requestKey,
      raw_payload: rawPayload,
      target_kind: event.targetKind ?? null,
      workspace_id: event.workspaceId ?? null,
      workspace_query: event.workspaceQuery ?? null,
      native_interaction: true,
      native_ack_status: "acknowledged",
      native_reply_status: "pending",
    });

    if (cmdRes.not_executed === true) {
      return;
    }

    const syntheticEvent: InboundChannelEvent = {
      platform: event.platform,
      platformUserId: event.platformUserId,
      ...(event.platformUsername
        ? { platformUsername: event.platformUsername }
        : {}),
      chatType: event.chatType,
      chatAddress: event.chatAddress,
      chatId: event.chatId,
      ...(event.chatName ? { chatName: event.chatName } : {}),
      threadScopeKey: event.threadScopeKey,
      messageId: event.interactionId,
      text: "",
      rawPayload,
    };

    await this.completeSessionCommandExecution({
      event: syntheticEvent,
      cmd: event.cmd,
      targetKind: event.targetKind ?? null,
      workspaceQuery: event.workspaceQuery ?? null,
      cmdRes,
      nativeResponder: event.responder,
    });
  }

  private isNativeInteractionEvent(
    rawPayload?: Record<string, unknown> | null,
    cmdRes?: I3SessionCommandResponse | null,
  ): boolean {
    if (cmdRes?.native_interaction === true) return true;
    if (!rawPayload || typeof rawPayload !== "object") return false;
    return (
      rawPayload.native_interaction === true ||
      Boolean(
        rawPayload.discord_interaction &&
          typeof rawPayload.discord_interaction === "object" &&
          !Array.isArray(rawPayload.discord_interaction),
      )
    );
  }

  private async deliverNativeCommandReply(params: {
    event: InboundChannelEvent;
    cmd: "workspace_list" | "workspace_switch" | "new" | "stop";
    cmdRes: I3SessionCommandResponse;
    replyText: string;
    nativeResponder?: NativeCommandResponder;
  }): Promise<void> {
    const { event, cmd, cmdRes, replyText, nativeResponder } = params;
    const terminalStatus = cmdRes.ok ? "completed" : "failed";
    const effectDonePatch =
      cmd === "stop" && cmdRes.ok ? { command_effect_status: "done" } : {};

    if (!nativeResponder) {
      // Process restarted and the short-lived interaction token is no longer available:
      // preserve the command result, close out the reply attempt, and never fall back to public channel send.
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: terminalStatus,
        command_reply_pending: false,
        fallback_pending: false,
        ...effectDonePatch,
        raw_payload_patch: {
          native_interaction: true,
          native_reply_status: "abandoned_on_restart",
        },
      });
      return;
    }

    if (nativeResponder.isExpired()) {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: terminalStatus,
        command_reply_pending: false,
        fallback_pending: false,
        ...effectDonePatch,
        raw_payload_patch: {
          native_interaction: true,
          native_reply_status: "expired",
        },
      });
      return;
    }

    const editOutcome = await nativeResponder.sendPrivateReply(replyText);
    if (editOutcome.outcome === "sent") {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: terminalStatus,
        command_reply_pending: false,
        fallback_pending: false,
        ...effectDonePatch,
        raw_payload_patch: {
          native_interaction: true,
          native_reply_status: "sent",
        },
      });
      return;
    }

    if (editOutcome.outcome === "expired" || editOutcome.outcome === "failed") {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: terminalStatus,
        command_reply_pending: false,
        fallback_pending: false,
        ...effectDonePatch,
        raw_payload_patch: {
          native_interaction: true,
          native_reply_status:
            editOutcome.outcome === "expired" ? "expired" : "failed",
        },
      });
      return;
    }

    // Transient / unknown webhook PATCH outcome within the 15-minute token window:
    // keep `command_reply_pending: true` so a retry with the valid token can still update `@original`
    // without re-executing the command.
    await this.backend.updateInboundProgress({
      platform: event.platform,
      message_id: event.messageId,
      ...effectDonePatch,
      raw_payload_patch: {
        native_interaction: true,
        native_reply_status: "failed",
      },
    });
  }

  private async completeSessionCommandExecution(params: {
    event: InboundChannelEvent;
    cmd: "workspace_list" | "workspace_switch" | "new" | "stop";
    targetKind?: SessionCommandTargetKind | null;
    workspaceQuery?: string | null;
    cmdRes: I3SessionCommandResponse;
    nativeResponder?: NativeCommandResponder;
  }): Promise<void> {
    const {
      event,
      cmd,
      targetKind,
      workspaceQuery,
      cmdRes,
      nativeResponder,
    } = params;
    const isNative =
      Boolean(nativeResponder) ||
      this.isNativeInteractionEvent(event.rawPayload, cmdRes);

    // D16 one-time reply on session command (e.g. conflict auto-unbind).
    // R8 #1: Let snapshot sync (triggered inside `dispatchOneTimeWeixinReply`) drive cancellation.
    if (event.platform === "weixin" && cmdRes.one_time_weixin_reply) {
      await this.dispatchOneTimeWeixinReply(
        cmdRes.one_time_weixin_reply,
        `onetime:${event.platform}:${event.messageId}:cmd_${cmd}`,
      );
      return;
    }

    if (
      event.platform === "weixin" &&
      cmdRes.deny_reason &&
      WEIXIN_ROTATION_DENY_CODES.has(cmdRes.deny_reason)
    ) {
      this.notifyWeixinAccountRotation(
        event.weixinAccountId,
        cmdRes.deny_reason,
      );
    }

    if (cmdRes.not_executed === true) {
      return;
    }

    if (
      cmdRes.command_reply_pending === false &&
      (cmd !== "stop" || cmdRes.command_effect_status === "done")
    ) {
      return;
    }

    if (
      !cmdRes.ok &&
      (cmdRes.linked === false || cmdRes.deny_reason === "not_linked")
    ) {
      const denyText = this.formatDenyNotice(
        event.platform,
        cmdRes.deny_reason ?? "not_linked",
      );
      if (isNative) {
        if (cmdRes.command_reply_pending !== false) {
          await this.deliverNativeCommandReply({
            event,
            cmd:
              cmd === "workspace_switch" || cmd === "new" || cmd === "stop"
                ? cmd
                : "workspace_list",
            cmdRes,
            replyText: denyText,
            nativeResponder,
          });
        }
        return;
      }
      const delivered = await this.sendInboundNotice(
        event,
        null,
        `notice:${event.platform}:${event.messageId}:deny_not_linked`,
        denyText,
      );
      if (delivered) {
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          command_reply_pending: false,
        });
      }
      return;
    }

    const userId = cmdRes.dalton_user_id ?? null;

    if (cmd === "stop" && this.isStopCancelDenied(cmdRes)) {
      await this.deliverPersistedStopCancelDenial({
        event,
        cmdRes,
        nativeResponder,
      });
      return;
    }

    if (cmd === "stop" && cmdRes.ok) {
      if (cmdRes.command_effect_status !== "done") {
        if (!cmdRes.thread_id || !cmdRes.run_id) {
          await this.backend.updateInboundProgress({
            platform: event.platform,
            message_id: event.messageId,
            command_effect_status: "done",
          });
        } else {
          let cancelStatus: number | null = null;
          try {
            const cancelRes = await this.backend.cancelThreadTurn(
              userId ?? "",
              cmdRes.thread_id,
              cmdRes.run_id,
            );
            cancelStatus = cancelRes.status;
          } catch (err) {
            if (err instanceof BackendHttpError) {
              cancelStatus = err.status;
            } else {
              cancelStatus = null;
            }
          }

          const isConfirmedOrAlreadyEnded =
            cancelStatus === 200 ||
            cancelStatus === 202 ||
            cancelStatus === 204 ||
            cancelStatus === 404 ||
            cancelStatus === 409;
          const isPermanentDenial =
            cancelStatus !== null &&
            cancelStatus >= 400 &&
            cancelStatus < 500 &&
            cancelStatus !== 404 &&
            cancelStatus !== 409 &&
            cancelStatus !== 429;

          if (isConfirmedOrAlreadyEnded) {
            await this.backend.updateInboundProgress({
              platform: event.platform,
              message_id: event.messageId,
              command_effect_status: "done",
            });
          } else if (isPermanentDenial && cancelStatus !== null) {
            const replyStillPending = cmdRes.command_reply_pending !== false;
            const failureResult = this.stopCancelFailureResult(
              cmdRes,
              cancelStatus,
              replyStillPending,
            );
            await this.backend.updateInboundProgress({
              platform: event.platform,
              message_id: event.messageId,
              status: "failed",
              command_effect_status: "done",
              command_reply_pending: replyStillPending,
              fallback_pending: false,
              command_result: {
                ...failureResult,
                dalton_user_id: userId ?? failureResult.dalton_user_id ?? null,
              },
            });
            if (replyStillPending) {
              await this.deliverPersistedStopCancelDenial({
                event,
                cmdRes: {
                  ...failureResult,
                  dalton_user_id: userId ?? failureResult.dalton_user_id ?? null,
                },
                nativeResponder,
              });
            }
            return;
          } else {
            // Transient failure (5xx, 429, or network error): keep `pending_cancel` persisted for recovery
            return;
          }
        }
      }
    }

    if (cmdRes.command_reply_pending === false) {
      return;
    }

    const replyText = this.formatSessionCommandReply({
      platform: event.platform,
      cmd,
      targetKind: targetKind ?? null,
      workspaceQuery: workspaceQuery ?? null,
      cmdRes,
    });

    if (isNative) {
      await this.deliverNativeCommandReply({
        event,
        cmd:
          cmd === "workspace_switch" || cmd === "new" || cmd === "stop"
            ? cmd
            : "workspace_list",
        cmdRes,
        replyText,
        nativeResponder,
      });
      return;
    }

    if (userId) {
      const delivered = await this.sendNoticeDelivery({
        deliveryKey: `notice:${event.platform}:${event.messageId}:command`,
        userId,
        threadId: cmd === "stop" ? (cmdRes.thread_id ?? null) : null,
        runId: cmd === "stop" ? (cmdRes.run_id ?? null) : null,
        platform: event.platform,
        target: event.chatAddress,
        inboundMessageId: event.messageId,
        text: replyText,
        replyToMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
        sourceScopeKey: event.threadScopeKey,
        sourceGeneration:
          typeof cmdRes.generation === "number" ? cmdRes.generation : null,
        activityOrigin: "command",
      });
      if (delivered) {
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          status: cmdRes.ok ? "completed" : "failed",
          command_reply_pending: false,
          fallback_pending: false,
          ...(cmd === "stop" && cmdRes.ok
            ? { command_effect_status: "done" }
            : {}),
        });
      }
    } else {
      const delivered = await this.sendInboundNotice(
        event,
        null,
        `notice:${event.platform}:${event.messageId}:command`,
        replyText,
      );
      if (!delivered) return;
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: cmdRes.ok ? "completed" : "failed",
        command_reply_pending: false,
        fallback_pending: false,
        ...(cmd === "stop" && cmdRes.ok
          ? { command_effect_status: "done" }
          : {}),
      });
    }
  }

  private isStopCancelDenied(cmdRes: {
    deny_reason?: string | null;
    stop_cancel_status?: number | null;
  }): boolean {
    return (
      cmdRes.deny_reason === "stop_cancel_denied" ||
      (typeof cmdRes.stop_cancel_status === "number" &&
        Number.isFinite(cmdRes.stop_cancel_status))
    );
  }

  private stopCancelFailureResult(
    cmdRes: I3SessionCommandResponse,
    cancelStatus: number,
    replyStillPending: boolean,
  ): I3SessionCommandResponse {
    return {
      ...cmdRes,
      ok: false,
      allowed: false,
      deny_reason: "stop_cancel_denied",
      stop_cancel_status: cancelStatus,
      command_effect_status: "done",
      command_reply_pending: replyStillPending,
    };
  }

  private async deliverPersistedStopCancelDenial(params: {
    event: InboundChannelEvent;
    cmdRes: I3SessionCommandResponse;
    nativeResponder?: NativeCommandResponder;
  }): Promise<void> {
    const { event, cmdRes, nativeResponder } = params;
    if (cmdRes.command_reply_pending === false) {
      return;
    }
    const cancelStatus =
      typeof cmdRes.stop_cancel_status === "number"
        ? cmdRes.stop_cancel_status
        : null;
    const failText = `Failed to stop the current run (status ${cancelStatus}).`;
    const isNative =
      Boolean(nativeResponder) ||
      this.isNativeInteractionEvent(event.rawPayload, cmdRes);
    if (isNative) {
      await this.deliverNativeCommandReply({
        event,
        cmd: "stop",
        cmdRes,
        replyText: failText,
        nativeResponder,
      });
      return;
    }
    const userId = cmdRes.dalton_user_id ?? null;
    let delivered = false;
    if (userId) {
      delivered = await this.sendNoticeDelivery({
        deliveryKey: `notice:${event.platform}:${event.messageId}:command_err`,
        userId,
        threadId: cmdRes.thread_id ?? null,
        runId: cmdRes.run_id ?? null,
        platform: event.platform,
        target: event.chatAddress,
        inboundMessageId: event.messageId,
        text: failText,
        replyToMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
        sourceScopeKey: event.threadScopeKey,
        sourceGeneration:
          typeof cmdRes.generation === "number" ? cmdRes.generation : null,
        activityOrigin: "none",
      });
    } else {
      delivered = await this.sendInboundNotice(
        event,
        null,
        `notice:${event.platform}:${event.messageId}:command_err`,
        failText,
      );
    }
    if (!delivered) {
      return;
    }
    await this.backend.updateInboundProgress({
      platform: event.platform,
      message_id: event.messageId,
      command_reply_pending: false,
    });
  }

  private formatSessionCommandReply(params: {
    platform: ChannelPlatform;
    cmd: "workspace_list" | "workspace_switch" | "new" | "stop";
    targetKind: SessionCommandTargetKind | null;
    workspaceQuery: string | null;
    cmdRes: I3SessionCommandResponse;
  }): string {
    const { platform, cmd, targetKind, workspaceQuery, cmdRes } = params;

    if (cmd === "workspace_list") {
      if (!cmdRes.ok) {
        return this.formatDenyNotice(
          platform,
          cmdRes.deny_reason ?? "workspace_write_denied",
        );
      }
      const currentWs = cmdRes.current_workspace_id ?? null;
      const isCurrentHome =
        cmdRes.is_home === true ||
        currentWs === null ||
        (cmdRes.home_workspace_id !== undefined &&
          cmdRes.home_workspace_id !== null &&
          currentWs === cmdRes.home_workspace_id);
      const currentWsName =
        cmdRes.current_workspace_name ??
        (cmdRes.workspaces ?? []).find((w) => w.workspace_id === currentWs)
          ?.name ??
        null;
      const currentLabel = !isCurrentHome && currentWs
        ? currentWsName
          ? `\`${currentWsName}\` (\`${currentWs}\`)`
          : `\`${currentWs}\``
        : "Home (Chief of Staff)";
      const lines = [
        `Current workspace: ${currentLabel}`,
        "Available workspaces:",
        `- \`home\` / \`/main\` (Chief of Staff)${isCurrentHome ? " [current]" : ""}`,
        ...(cmdRes.workspaces ?? []).map(
          (w) =>
            `- \`${w.name}\` (\`${w.workspace_id}\`)${!isCurrentHome && w.workspace_id === currentWs ? " [current]" : ""}`,
        ),
        "Use `/main` for Chief of Staff, `/<workspace name>`, or `/workspace <name|id|home>` to switch.",
      ];
      return lines.join("\n");
    }

    if (cmd === "workspace_switch") {
      if (!cmdRes.ok) {
        if (cmdRes.deny_reason === "workspace_ambiguous") {
          const candidates = cmdRes.candidates ?? [];
          if (candidates.length === 0) {
            return this.formatDenyNotice(platform, "workspace_write_denied");
          }
          const candidateLines = candidates.map(
            (c) => `- \`${c.name}\` (\`${c.workspace_id}\`)`,
          );
          return [
            `Multiple workspaces match "${workspaceQuery ?? ""}":`,
            ...candidateLines,
            "Current session was not changed. Use `/workspace <workspace_id>` to select a specific workspace.",
          ].join("\n");
        }
        if (cmdRes.deny_reason === "workspace_not_found") {
          if (targetKind === "shortcut_name") {
            return `Unknown command or workspace "${workspaceQuery ?? ""}". Current session unchanged. Use \`/workspace\` to list workspaces, \`/main\` for Chief of Staff, or \`/new\` to start a new thread.`;
          }
          return `Workspace "${workspaceQuery ?? ""}" was not found or you do not have write access. Current session unchanged. Use \`/workspace\` to list available workspaces.`;
        }
        return this.formatDenyNotice(
          platform,
          cmdRes.deny_reason ?? "workspace_not_found",
        );
      }
      const isHomeTarget =
        cmdRes.is_home === true ||
        targetKind === "home" ||
        !cmdRes.workspace_id ||
        (cmdRes.override_workspace_id === null &&
          (cmdRes.workspace_name === "Chief of Staff" ||
            cmdRes.workspace_id === cmdRes.home_workspace_id));
      const targetLabel = !isHomeTarget && cmdRes.workspace_id
        ? cmdRes.workspace_name && cmdRes.workspace_name !== cmdRes.workspace_id
          ? `workspace \`${cmdRes.workspace_name}\` (\`${cmdRes.workspace_id}\`)`
          : `workspace \`${cmdRes.workspace_id}\``
        : "Home (Chief of Staff)";
      if (cmdRes.switched === false) {
        return `Already in ${targetLabel}. Keeping current conversation.`;
      }
      return `Switched this conversation to ${targetLabel} and started a new thread.`;
    }

    if (cmd === "new") {
      if (!cmdRes.ok) {
        return this.formatDenyNotice(
          platform,
          cmdRes.deny_reason ?? "external_member_group_forbidden",
        );
      }
      const isHomeNew =
        cmdRes.is_home === true ||
        !cmdRes.workspace_id ||
        (cmdRes.override_workspace_id === null &&
          (cmdRes.workspace_name === "Chief of Staff" ||
            cmdRes.workspace_id === cmdRes.home_workspace_id));
      const wsLabel = !isHomeNew && cmdRes.workspace_id
        ? `workspace \`${cmdRes.workspace_name ?? cmdRes.workspace_id}\``
        : "Home (Chief of Staff)";
      return `Started a new conversation thread in ${wsLabel}.`;
    }

    if (!cmdRes.ok) {
      return this.formatDenyNotice(
        platform,
        cmdRes.deny_reason ?? "external_member_group_forbidden",
      );
    }
    if (!cmdRes.thread_id) {
      return "No active conversation thread to stop.";
    }
    if (!cmdRes.run_id) {
      return "No active run to stop in the current session.";
    }
    return "Stopped the current run.";
  }

  // =========================================================================
  // §1.3.1 Workspace Readiness & Attachment Upload
  // =========================================================================

  private async downloadAndUploadAttachments(
    event: InboundChannelEvent,
    resolved: I1RegisterAndResolveResponse,
    userId: string,
    alreadyUploaded: UploadedAttachmentRecord[] = [],
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

    // R5 #1 (Codex High #2): For Weixin, register one task-level AbortController at the VERY START
    // (before workspace readiness / startWorkspace) keyed by account + generation, and verify the
    // account & generation are still active before workspace readiness, every download, and every upload.
    // Feishu / Discord paths remain completely untouched.
    const weixinTaskController =
      event.platform === "weixin" ? new AbortController() : null;
    const weixinTaskMeta: {
      accountId?: string;
      botGeneration?: number;
      abortReason?: WeixinAttachmentAbortReason;
    } | null = weixinTaskController
      ? {
          accountId: event.weixinAccountId,
          botGeneration: event.weixinBotGeneration,
        }
      : null;
    if (weixinTaskController && weixinTaskMeta) {
      if (this.shuttingDown) {
        weixinTaskMeta.abortReason = "shutdown";
        weixinTaskController.abort();
      }
      this.activeInboundUploadControllers.set(
        weixinTaskController,
        weixinTaskMeta,
      );
    }

    const resolveAbortReason = (): WeixinAttachmentAbortReason =>
      weixinTaskMeta?.abortReason ?? (this.shuttingDown ? "shutdown" : "superseded");

    const assertWeixinAttachmentTaskActive = async (
      stage: string,
    ): Promise<void> => {
      if (!weixinTaskController) {
        return;
      }
      if (this.shuttingDown || weixinTaskController.signal.aborted) {
        throw new WeixinAttachmentAbortedError(
          `Weixin attachment task aborted before ${stage}`,
          resolveAbortReason(),
        );
      }
      // R11 #1 (Codex R10 #1): While a backend switch or startup reconcile has not yet
      // applied an authoritative snapshot, suspend attachment processing instead of
      // prematurely marking the task as `superseded` or acting on stale cached accounts.
      while (
        typeof adapter.hasAppliedStartupSnapshot === "function" &&
        !adapter.hasAppliedStartupSnapshot()
      ) {
        if (typeof adapter.waitForStartupSnapshot !== "function") {
          break;
        }
        const applied = await adapter.waitForStartupSnapshot(
          weixinTaskController.signal,
        );
        if (this.shuttingDown || weixinTaskController.signal.aborted) {
          throw new WeixinAttachmentAbortedError(
            `Weixin attachment task aborted while waiting for snapshot before ${stage}`,
            resolveAbortReason(),
          );
        }
        if (!applied) {
          if (weixinTaskMeta) {
            weixinTaskMeta.abortReason = "shutdown";
          }
          weixinTaskController.abort();
          throw new WeixinAttachmentAbortedError(
            `Weixin startup snapshot wait interrupted before ${stage}`,
            "shutdown",
          );
        }
      }
      if (this.shuttingDown || weixinTaskController.signal.aborted) {
        throw new WeixinAttachmentAbortedError(
          `Weixin attachment task aborted before ${stage}`,
          resolveAbortReason(),
        );
      }
      if (
        typeof adapter.isWeixinAccountGenerationActive === "function" &&
        !adapter.isWeixinAccountGenerationActive(event)
      ) {
        if (weixinTaskMeta) {
          weixinTaskMeta.abortReason = "superseded";
        }
        weixinTaskController.abort();
        throw new WeixinAttachmentAbortedError(
          `Weixin account rotated or inactive before ${stage}`,
          "superseded",
        );
      }
    };

    try {
      await assertWeixinAttachmentTaskActive("workspace_ready");

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
        await assertWeixinAttachmentTaskActive("workspace_start");
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

      const uploaded: UploadedAttachmentRecord[] = [...alreadyUploaded];
      let totalBytes = uploaded.reduce(
        (sum, record) => sum + record.size_bytes,
        0,
      );
      const safeMsgId =
        event.messageId.replace(/[^a-zA-Z0-9._-]/g, "_") || "message";
      const remainingDescriptors = descriptors.slice(uploaded.length);

      for (let remainingIndex = 0; remainingIndex < remainingDescriptors.length; remainingIndex += 1) {
        await assertWeixinAttachmentTaskActive("download");
        const desc = remainingDescriptors[remainingIndex]!;
        const descriptorIndex = alreadyUploaded.length + remainingIndex;
        if (desc.sizeBytes !== undefined && desc.sizeBytes > MAX_ATTACHMENT_BYTES) {
          return {
            ok: false,
            errorMessage: `Attachment "${desc.filename}" exceeds the 20 MB per-file limit.`,
          };
        }

        let downloaded: Awaited<ReturnType<typeof adapter.downloadAttachment>>;
        try {
          downloaded = weixinTaskController
            ? await adapter.downloadAttachment(desc, event, {
                signal: weixinTaskController.signal,
              })
            : await adapter.downloadAttachment(desc, event);
        } catch (err) {
          if (weixinTaskController?.signal.aborted) {
            throw new WeixinAttachmentAbortedError(
              "Weixin attachment download aborted due to account rotation or shutdown",
              resolveAbortReason(),
              { cause: err },
            );
          }
          if (err instanceof SafeFetchError && err.code === "size_exceeded") {
            return {
              ok: false,
              errorMessage: `Attachment "${desc.filename}" exceeds the 20 MB per-file limit.`,
            };
          }
          throw err;
        }
        await assertWeixinAttachmentTaskActive("upload");

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
        let workspacePath = `uploads/${event.platform}/${safeMsgId}/${safeFilename}`;
        if (uploaded.some((record) => record.workspace_path === workspacePath)) {
          let suffix = descriptorIndex + 1;
          workspacePath = `uploads/${event.platform}/${safeMsgId}/${suffix}-${safeFilename}`;
          while (
            uploaded.some((record) => record.workspace_path === workspacePath)
          ) {
            suffix += 1;
            workspacePath = `uploads/${event.platform}/${safeMsgId}/${suffix}-${safeFilename}`;
          }
        }

        if (weixinTaskController) {
          await assertWeixinAttachmentTaskActive("workspace_write");
          const weixinUploadContext: WorkspaceUploadWeixinContext = {
            weixinAccountId: event.weixinAccountId ?? null,
            weixinBotId: event.weixinBotId ?? null,
            weixinBotGeneration: event.weixinBotGeneration ?? null,
          };
          try {
            await this.backend.uploadWorkspaceFile(
              userId,
              targetWorkspaceId,
              workspacePath,
              safeFilename,
              downloaded.content,
              downloaded.mimeType || desc.mimeType || "application/octet-stream",
              weixinTaskController.signal,
              weixinUploadContext,
            );
          } catch (uploadErr) {
            if (uploadErr instanceof BackendHttpError && uploadErr.status === 409) {
              const bodyObj = uploadErr.body as { code?: unknown } | null;
              const errCode =
                typeof bodyObj?.code === "string" ? bodyObj.code : "";
              if (WEIXIN_ROTATION_DENY_CODES.has(errCode)) {
                this.notifyWeixinAccountRotation(
                  event.weixinAccountId,
                  errCode,
                );
                throw new WeixinAttachmentAbortedError(
                  `Weixin attachment upload rejected (${errCode})`,
                  "superseded",
                  { cause: uploadErr },
                );
              }
            }
            if (weixinTaskController.signal.aborted) {
              throw new WeixinAttachmentAbortedError(
                "Weixin attachment upload aborted due to account rotation or shutdown",
                resolveAbortReason(),
                { cause: uploadErr },
              );
            }
            throw uploadErr;
          }
          await assertWeixinAttachmentTaskActive("post_upload");
        } else {
          await this.backend.uploadWorkspaceFile(
            userId,
            targetWorkspaceId,
            workspacePath,
            safeFilename,
            downloaded.content,
            downloaded.mimeType || desc.mimeType || "application/octet-stream",
          );
        }

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
        await this.backend.updateInboundProgress({
          platform: event.platform,
          message_id: event.messageId,
          uploaded_attachments: [...uploaded],
        });
        // R8 #3 (Codex High #3): Re-check cancellation after I-7 progress save returns
        // so a rotation arriving during the final progress write never returns `ok: true`.
        await assertWeixinAttachmentTaskActive("post_progress_save");
      }

      await assertWeixinAttachmentTaskActive("attachment_complete");
      return { ok: true, uploaded };
    } catch (err) {
      if (
        err instanceof WeixinAttachmentAbortedError ||
        isStaleGatewayEpochError(err)
      ) {
        throw err;
      }
      // R7 #4 (Codex Medium #3): If this task was cancelled while awaiting workspace readiness
      // (`ensureHomeWorkspace` / `startWorkspace`) or any step, honor the cancellation even when
      // the underlying workspace request rejects, so `launchInboundTurn` does NOT enter recovery.
      if (weixinTaskController?.signal.aborted) {
        throw new WeixinAttachmentAbortedError(
          "Weixin attachment task aborted before completion",
          resolveAbortReason(),
          { cause: err },
        );
      }
      throw err;
    } finally {
      if (weixinTaskController) {
        this.activeInboundUploadControllers.delete(weixinTaskController);
      }
    }
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
            generation: resolved.generation,
            thread_id: threadId,
            run_id: pendingInterrupt.run_id,
            interrupt_id: interruptId,
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
      const noticeOutcome = await this.sendNoticeDeliveryOutcome({
        deliveryKey: `notice:${event.platform}:${event.messageId}:web_approval`,
        userId,
        threadId,
        runId: pendingInterrupt.run_id,
        platform: event.platform,
        target: event.chatAddress,
        inboundMessageId: event.messageId,
        text: webOnlyApprovalText(
          pendingInterrupt.action_type,
          threadUrl,
          pendingInterrupt.action_requests,
        ),
        replyToMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
      });
      this.processingFeedback.settleMessage(
        event,
        noticeOutcome.definitiveFailure ? "delivery_failed" : "interrupted",
      );
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
      chat_type: event.chatType, // DALTON-HOOK(personal-integrations)
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
      chat_type: event.chatType, // DALTON-HOOK(personal-integrations)
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
      this.processingFeedback.reopenForResubmit(event);
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
          generation: resolved.generation,
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
      // When `!replaySettled`, `fallback_pending: true` remains for watch/startup recovery,
      // so clear processing indicator cleanly without stamping a permanent failure emoji.
      this.processingFeedback.settleMessage(
        event,
        replaySettled ? "done" : "failed",
      );
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
      this.processingFeedback.reopenForResubmit(event);
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
      const noticeOutcome = await this.sendNoticeDeliveryOutcome({
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
      // Row stays in recovery set (`status: "steered"`), so never stamp permanent failure emoji.
      this.processingFeedback.settleMessage(
        event,
        noticeOutcome.ok ? "done" : "failed",
      );
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
      const noticeOutcome = await this.sendNoticeDeliveryOutcome({
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
      this.processingFeedback.settleMessage(
        event,
        noticeOutcome.definitiveFailure ? "delivery_failed" : "failed",
      );
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
      chat_type: event.chatType, // DALTON-HOOK(personal-integrations)
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
      chat_type: event.chatType, // DALTON-HOOK(personal-integrations)
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
        this.processingFeedback.bindRun(event, steeredRunId, "steered");
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
        this.processingFeedback.bindRun(event, conflictRunId, "primary");
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
        const replayOk = await this.deliverReplayFallback({
          userId,
          threadId: conflictThreadId,
          runId: conflictRunId,
          turnIndex: detail.turn_index ?? resolved.turn_index,
          platform: event.platform,
          targetAddress: event.chatAddress,
          inboundMessageId: event.messageId,
          scopeKey: event.threadScopeKey,
          generation: resolved.generation,
        });
        this.processingFeedback.settleMessage(
          event,
          replayOk ? "done" : "failed",
        );
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
        const noticeOutcome = await this.sendNoticeDeliveryOutcome({
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
        this.processingFeedback.settleMessage(
          event,
          noticeOutcome.definitiveFailure ? "delivery_failed" : "failed",
        );
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
          this.processingFeedback.bindRun(event, conflictRunId, "primary");
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
          const replayOk = await this.deliverReplayFallback({
            userId,
            threadId: conflictThreadId,
            runId: conflictRunId,
            turnIndex: detail.turn_index ?? resolved.turn_index,
            platform: event.platform,
            targetAddress: event.chatAddress,
            inboundMessageId: event.messageId,
            scopeKey: event.threadScopeKey,
            generation: resolved.generation,
          });
          this.processingFeedback.settleMessage(
            event,
            replayOk ? "done" : "failed",
          );
          return;
        }
      }

      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "failed",
        fallback_pending: false,
      });
      // `duplicate_request` arrived with no run metadata / unresolved run state and
      // no error notice was attempted: outcome is uncertain, so clear processing
      // feedback without stamping a permanent failure emoji.
      this.processingFeedback.settleMessage(event, "failed");
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
                generation: resolved.generation,
                thread_id: targetTid,
                run_id:
                  resolved.pending_interrupt?.run_id ?? conflictRunId ?? null,
                interrupt_id:
                  resolved.pending_interrupt?.interrupt_id ?? null,
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
              generation: resolved.generation,
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
        const noticeOutcome = await this.sendNoticeDeliveryOutcome({
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
        this.processingFeedback.settleMessage(
          event,
          noticeOutcome.definitiveFailure ? "delivery_failed" : "failed",
        );
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
      const noticeOutcome = await this.sendNoticeDeliveryOutcome({
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
      this.processingFeedback.settleMessage(
        event,
        noticeOutcome.definitiveFailure ? "delivery_failed" : "failed",
      );
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
    const noticeOutcome = await this.sendNoticeDeliveryOutcome({
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
    this.processingFeedback.settleMessage(
      event,
      noticeOutcome.definitiveFailure ? "delivery_failed" : "failed",
    );
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
    const deliverableAcc = new DeliverableTextAccumulator();
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
    let runEndDeliverables: TurnDeliverableItem[] = [];
    let runEndChartSnapshots: ChartSnapshotItem[] = [];
    let runEndOutboundBlocked: boolean | undefined;
    const collectedCharts = new Map<string, CollectedChartLayerRef>();
    const collectedWidgets = new Map<string, CollectedHtmlWidgetRef>();
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
        this.processingFeedback.bindRun(event, steeredIntoRunId, "steered");
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
        if (currentRunId) {
          this.processingFeedback.bindRun(event, currentRunId, "primary");
        }
        if (typeof payload.turn_index === "number") {
          currentTurnIndex = payload.turn_index;
        }
        if (sse.event === "metadata") {
          collectReportSourceChartLayers(payload, collectedCharts);
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
                    generation: resolved.generation,
                    thread_id: currentThreadId,
                    run_id:
                      resolved.pending_interrupt?.run_id ?? currentRunId,
                    interrupt_id:
                      resolved.pending_interrupt?.interrupt_id ?? null,
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

      if (sse.event === "personal_integration_notice") {
        sawTurnAccepted = true;
        if (typeof payload.thread_id === "string") {
          currentThreadId = payload.thread_id;
        }
        const noticeMsg =
          typeof payload.message === "string" ? payload.message.trim() : "";
        if (noticeMsg && currentThreadId) {
          await this.deliverPersonalFirstTouchNotice({
            userId,
            threadId: currentThreadId,
            runId: currentRunId,
            platform: event.platform,
            chatType: event.chatType,
            targetAddress: event.chatAddress,
            inboundMessageId: event.messageId,
            threadScopeKey: event.threadScopeKey,
            sourceGeneration: resolved.generation,
            message: noticeMsg,
          });
        }
        continue;
      }

      if (sse.event === "tool_calls") {
        sawTurnAccepted = true;
        if (this.isMainAgentLane(payload.agent)) {
          deliverableAcc.pushEvent("tool_calls", payload);
        }
        continue;
      }

      if (sse.event === "tool_call_start" || sse.event === "tool_call_result") {
        sawTurnAccepted = true;
        if (this.isMainAgentLane(payload.agent)) {
          deliverableAcc.pushEvent(sse.event, payload);
        }
        continue;
      }

      if (sse.event === "message_discarded") {
        sawTurnAccepted = true;
        if (this.isMainAgentLane(payload.agent)) {
          deliverableAcc.pushEvent("message_discarded", payload);
        }
        continue;
      }

      if (
        sse.event === "message_chunk" ||
        sse.event === "text" ||
        sse.event === "content_block_delta"
      ) {
        sawTurnAccepted = true;
        if (this.isMainAgentLane(payload.agent)) {
          deliverableAcc.pushEvent(sse.event, payload);
        }
        continue;
      }

      if (sse.event === "message") {
        sawTurnAccepted = true;
        if (this.isMainAgentLane(payload.agent)) {
          deliverableAcc.pushEvent("message", payload);
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
        if (currentRunId) {
          this.processingFeedback.bindRun(event, currentRunId, "primary");
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
            generation: resolved.generation,
            inboundMessageId: event.messageId,
          });
          if (intDelivered) {
            this.processingFeedback.settleMessage(event, "interrupted");
            this.processingFeedback.settleRun(currentRunId, "interrupted");
          } else {
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

      if (sse.event === "artifact") {
        this.collectArtifactChartOrWidget(
          payload,
          collectedCharts,
          collectedWidgets,
        );
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
        runEndDeliverables = this.parseTurnDeliverables(
          payload.turn_deliverables,
        );
        runEndChartSnapshots = parseChartSnapshotsList(payload.chart_snapshots);
        collectPublishedChartLayers(payload.chart_layers, collectedCharts);
        deliverableAcc.pushEvent("run_end", payload);
        if (typeof payload.outbound_blocked === "boolean") {
          runEndOutboundBlocked = payload.outbound_blocked;
        }
        continue;
      }

      if (sse.event === "error") {
        streamErrorDetail = payload;
        continue;
      }
    }

    const extractedDeliverable = deliverableAcc.result();
    const assistantText = extractedDeliverable.text;

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

      const inFlightWaitPromise =
        this.deliveryService.hasInFlightCurrentDelivery(currentRunId)
          ? this.deliveryService.waitForInFlightCurrentDeliveries(currentRunId)
          : null;
      const errProg = await this.backend.updateInboundProgress({
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
      const noticeOutcome = await this.sendNoticeDeliveryOutcome({
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
      await this.settleTerminalTurnErrorFeedback({
        event,
        currentRunId,
        progress: errProg,
        noticeOutcome,
        inFlightWaitPromise,
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
      const inFlightWaitPromise =
        this.deliveryService.hasInFlightCurrentDelivery(currentRunId)
          ? this.deliveryService.waitForInFlightCurrentDeliveries(currentRunId)
          : null;
      const endProg = await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "failed",
        fallback_pending: false,
      });
      const noticeOutcome = await this.sendNoticeDeliveryOutcome({
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
      await this.settleTerminalTurnErrorFeedback({
        event,
        currentRunId,
        progress: endProg,
        noticeOutcome,
        inFlightWaitPromise,
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
              generation: resolved.generation,
              thread_id: currentThreadId,
              run_id: resolved.pending_interrupt?.run_id ?? currentRunId,
              interrupt_id: resolved.pending_interrupt?.interrupt_id ?? null,
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
          generation: resolved.generation,
          activityOrigin: params.isAttachedStream ? "replay" : "conversation",
          deferFinalCompletion: params.deferFinalCompletion,
        });
        if (replaySettled) {
          this.processingFeedback.settleMessage(event, "done");
          this.processingFeedback.settleRun(currentRunId, "done");
          try {
            await this.evaluateWatchCloseConditions(userId, currentThreadId);
          } catch (err) {
            if (isStaleGatewayEpochError(err)) {
              throw err;
            }
            // Turn is already settled cleanly; ignore transient status/wakeup errors and let watch loop retry.
          }
          return true;
        }
      }
      // Keep inbound row in the recovery set (`dispatched` / `received` with `fallback_pending: true`).
      // Delivery may still be `in_flight`/`unknown` or recovered later, so clear cleanly without a failure emoji.
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
      this.processingFeedback.settleMessage(event, "failed");
      this.processingFeedback.settleRunPrimary(currentRunId, "failed");
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
          generation: resolved.generation,
          thread_id: currentThreadId,
          run_id: resolved.pending_interrupt?.run_id ?? currentRunId,
          interrupt_id: resolved.pending_interrupt?.interrupt_id ?? null,
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
        generation: resolved.generation,
        activityOrigin: params.isAttachedStream ? "replay" : "conversation",
        deferFinalCompletion: params.deferFinalCompletion,
      });
      const outcome = settled ? "done" : "failed";
      this.processingFeedback.settleMessage(event, outcome);
      if (settled) {
        this.processingFeedback.settleRun(currentRunId, outcome);
      } else {
        this.processingFeedback.settleRunPrimary(currentRunId, outcome);
      }
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
    const strictDeliverable =
      prog.strict_deliverable_text_enabled ??
      resolved.strict_deliverable_text_enabled ??
      true;
    const trimmedAssistant = resolveChannelFallbackText({
      extractedText: assistantText,
      sawBusinessToolCall: extractedDeliverable.sawBusinessToolCall,
      sentToCurrentCount: prog.sent_to_current_count,
      hasDeliveredCurrentAgentSend: prog.has_delivered_current_agent_send,
      hasFailedCurrentAgentSend: prog.has_failed_current_agent_send,
      strictDeliverableTextEnabled: strictDeliverable,
      legacyText: deliverableAcc.legacyResult(),
    });
    const hasUnresolvedAgentSendImages =
      prog.has_unresolved_current_agent_send_images === true;
    const liveResolvedImageUrls: Record<string, string> = {
      ...(prog.resolved_run_images ?? {}),
      ...deliverableAcc.getGlobalImageUrlMap(),
    };
    const liveResolvedImagesByMessage = mergeImagesByMessage(
      prog.resolved_run_images_by_message,
      deliverableAcc.getImagesByMessage(),
    );
    const liveToolCallMessageIds = deliverableAcc.getToolCallMessageIds();
    if (
      !sawInterrupt &&
      (trimmedAssistant.length > 0 || hasUnresolvedAgentSendImages) &&
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
        sourceScopeKey: event.threadScopeKey,
        sourceGeneration: resolved.generation,
        activityOrigin: params.isAttachedStream ? "replay" : "conversation",
        trimmedText: trimmedAssistant,
        sentToCurrentCount: prog.sent_to_current_count,
        hasUnresolvedCurrentAgentSendImages: hasUnresolvedAgentSendImages,
        unresolvedCurrentAgentSends: prog.unresolved_current_agent_sends,
        resolvedImageUrls: liveResolvedImageUrls,
        resolvedImagesByMessage: liveResolvedImagesByMessage,
        toolCallMessageIds: liveToolCallMessageIds,
      });
      if (!settledOk) {
        fallbackDeliveryFailed = true;
      }
    } else if (
      !sawInterrupt &&
      params.isAttachedStream &&
      trimmedAssistant.length === 0 &&
      !extractedDeliverable.sawBusinessToolCall &&
      !prog.has_delivered_current_agent_send &&
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
        generation: resolved.generation,
        activityOrigin: "replay",
        deferFinalCompletion: params.deferFinalCompletion,
      });
      const outcome = settled ? "done" : "failed";
      this.processingFeedback.settleMessage(event, outcome);
      if (settled) {
        this.processingFeedback.settleRun(currentRunId, outcome);
      } else {
        this.processingFeedback.settleRunPrimary(currentRunId, outcome);
      }
      return settled;
    }

    // Lead Decision #7: DM deliverable auto-attach settles independently of whether
    // text was already sent via `/agent/send` or `fallback:${runId}`, deduplicated
    // against `files_status` across the run.
    if (
      !sawInterrupt &&
      !fallbackDeliveryFailed &&
      !prog.has_in_flight_or_unknown_current_text &&
      currentRunId &&
      runEndDeliverables.length > 0 &&
      event.chatAddress.endsWith(":@me")
    ) {
      const delivOk = await this.settleDmTurnDeliverables({
        userId,
        threadId: currentThreadId,
        runId: currentRunId,
        platform: event.platform,
        targetAddress: event.chatAddress,
        inboundMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
        sourceScopeKey: event.threadScopeKey,
        sourceGeneration: resolved.generation,
        activityOrigin: params.isAttachedStream ? "replay" : "conversation",
        assistantText: trimmedAssistant,
        deliverables: runEndDeliverables,
      });
      if (!delivOk) {
        fallbackDeliveryFailed = true;
      }
    }

    if (
      !sawInterrupt &&
      !fallbackDeliveryFailed &&
      !prog.has_in_flight_or_unknown_current_text &&
      currentRunId &&
      (collectedCharts.size > 0 ||
        runEndChartSnapshots.length > 0 ||
        collectedWidgets.size > 0)
    ) {
      await this.settleTurnChartsAndWidgets({
        userId,
        threadId: currentThreadId,
        runId: currentRunId,
        platform: event.platform,
        targetAddress: event.chatAddress,
        inboundMessageId: event.messageId,
        threadScopeKey: event.threadScopeKey,
        sourceScopeKey: event.threadScopeKey,
        sourceGeneration: resolved.generation,
        activityOrigin: params.isAttachedStream ? "replay" : "conversation",
        charts: [...collectedCharts.values()],
        initialSnapshots: runEndChartSnapshots,
        widgets: [...collectedWidgets.values()],
        outboundBlocked: runEndOutboundBlocked,
      });
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
            generation: resolved.generation,
            activityOrigin: params.isAttachedStream ? "replay" : "conversation",
          });
          watchHandle.needsReconnectSync = true;
        }
      } else {
        try {
          await this.evaluateWatchCloseConditions(userId, currentThreadId);
        } catch (err) {
          if (isStaleGatewayEpochError(err)) {
            throw err;
          }
          // Turn is already settled cleanly; ignore transient status/wakeup errors and let watch loop retry.
        }
      }
    }
    // When `fallbackDeliveryFailed` or `interruptDeliveryFailed` occurs here,
    // `fallback_pending: true` is persisted and `ActiveWatchHandle` / startup recovery
    // will retry delivery; never stamp a permanent failure emoji on a recoverable path.
    const finalFeedbackOutcome =
      fallbackDeliveryFailed || interruptDeliveryFailed
        ? "failed"
        : sawInterrupt
          ? "interrupted"
          : "done";
    if (!params.deferFinalCompletion || keepFallbackPending) {
      this.processingFeedback.settleMessage(event, finalFeedbackOutcome);
      if (keepFallbackPending) {
        this.processingFeedback.settleRunPrimary(currentRunId, finalFeedbackOutcome);
      } else {
        this.processingFeedback.settleRun(currentRunId, finalFeedbackOutcome);
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
                generation: resolved.generation,
                inboundMessageId: event.messageId,
              });
              if (intOk) {
                this.processingFeedback.settleMessage(event, "interrupted");
              } else {
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
          generation: resolved.generation,
        });
      }
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "completed",
        fallback_pending: !replaySettled,
      });
      this.processingFeedback.settleMessage(
        event,
        replaySettled ? "done" : "failed",
      );
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
      this.processingFeedback.reopenForResubmit(event);
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
      const noticeOutcome = await this.sendNoticeDeliveryOutcome({
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
      this.processingFeedback.settleMessage(
        event,
        noticeOutcome.ok ? "done" : "failed",
      );
      return;
    }

    if (rec.reconcile_status === "returned_unresubmitted") {
      await this.backend.updateInboundProgress({
        platform: event.platform,
        message_id: event.messageId,
        status: "failed",
        fallback_pending: false,
      });
      const noticeOutcome = await this.sendNoticeDeliveryOutcome({
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
      this.processingFeedback.settleMessage(
        event,
        noticeOutcome.definitiveFailure ? "delivery_failed" : "failed",
      );
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
    generation?: number | null;
    activityOrigin?: DeliveryActivityOrigin | null;
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
      generation,
      activityOrigin,
      isBackgroundRun,
      deferFinalCompletion,
    } = params;

    let foundText = "";
    let foundLegacyText = "";
    let foundSawBusinessToolCall = false;
    let foundImageUrls: Record<string, string> = {};
    let foundImagesByMessage: Record<string, Record<string, string>> = {};
    let foundToolCallMsgIds: Record<string, string> = {};
    let foundTargetTurn = false;
    let resolvedRunId = runId;
    let anyPageSucceeded = false;
    const replayDeliverablesByRun = new Map<string, TurnDeliverableItem[]>();
    const replayChartSnapshotsByRun = new Map<string, ChartSnapshotItem[]>();
    const replayOutboundBlockedByRun = new Map<string, boolean>();
    const replayChartsByRun = new Map<string, Map<string, CollectedChartLayerRef>>();
    const replayWidgetsByRun = new Map<string, Map<string, CollectedHtmlWidgetRef>>();
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
    let usedPostRunEndOvertimeReplay = false;

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
        const accByTurn = new Map<number, DeliverableTextAccumulator>();
        const accByRun = new Map<string, DeliverableTextAccumulator>();
        const getTurnAcc = (tIdx: number): DeliverableTextAccumulator => {
          let acc = accByTurn.get(tIdx);
          if (!acc) {
            acc = new DeliverableTextAccumulator();
            accByTurn.set(tIdx, acc);
          }
          return acc;
        };
        const getRunAcc = (rId: string): DeliverableTextAccumulator => {
          let acc = accByRun.get(rId);
          if (!acc) {
            acc = new DeliverableTextAccumulator();
            accByRun.set(rId, acc);
          }
          return acc;
        };
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
        let lastTurnAcc = new DeliverableTextAccumulator();
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
          accByTurn.clear();
          accByRun.clear();
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
          lastTurnAcc = new DeliverableTextAccumulator();
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
                  let cMap = replayChartsByRun.get(evtRunId);
                  if (!cMap) {
                    cMap = new Map<string, CollectedChartLayerRef>();
                    replayChartsByRun.set(evtRunId, cMap);
                  }
                  collectReportSourceChartLayers(data.metadata, cMap);
                }
              }
              continue;
            }

            if (
              sse.event === "tool_call_result" ||
              sse.event === "tool_call_start" ||
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
                if (
                  sse.event === "tool_call_result" &&
                  data.artifact &&
                  typeof data.artifact === "object" &&
                  !Array.isArray(data.artifact)
                ) {
                  let cMap = replayChartsByRun.get(rId);
                  if (!cMap) {
                    cMap = new Map<string, CollectedChartLayerRef>();
                    replayChartsByRun.set(rId, cMap);
                  }
                  collectPublishedChartLayers(
                    (data.artifact as Record<string, unknown>).chart_layers,
                    cMap,
                  );
                }
              }
              if (
                (sse.event === "tool_call_result" ||
                  sse.event === "tool_call_start") &&
                this.isMainAgentLane(data.agent)
              ) {
                if (tIdx !== null) {
                  getTurnAcc(tIdx).pushEvent(sse.event, data);
                }
                if (rId) {
                  getRunAcc(rId).pushEvent(sse.event, data);
                }
                lastTurnAcc.pushEvent(sse.event, data);
              }
              continue;
            }

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
                  getTurnAcc(tIdx).pushEvent("tool_calls", data);
                }
                if (rId) {
                  settledRunsInBatch.add(rId);
                  getRunAcc(rId).pushEvent("tool_calls", data);
                }
                lastTurnAcc.pushEvent("tool_calls", data);
              }
              continue;
            }

            if (sse.event === "message_discarded") {
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
                  getTurnAcc(tIdx).pushEvent("message_discarded", data);
                }
                if (rId) {
                  getRunAcc(rId).pushEvent("message_discarded", data);
                }
                lastTurnAcc.pushEvent("message_discarded", data);
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
              // Only accumulate `content_type == "text"` (skip `"reasoning"` / `"reasoning_signal"`)
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
              if (!chunk && data.finish_reason !== "tool_calls") continue;

              if (tIdx !== null && mismatchedAttemptTurnsInBatch.has(tIdx)) {
                continue;
              }

              if (tIdx !== null) {
                if (chunk) {
                  seenTurnsInBatch.add(tIdx);
                  settledTurnsInBatch.add(tIdx);
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
                getTurnAcc(tIdx).pushEvent(sse.event, data);
              }
              if (rId) {
                if (chunk) {
                  seenRunsInBatch.add(rId);
                  settledRunsInBatch.add(rId);
                }
                getRunAcc(rId).pushEvent(sse.event, data);
              }
              lastTurnAcc.pushEvent(sse.event, data);
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

            if (sse.event === "artifact") {
              const rId =
                typeof data.response_id === "string"
                  ? data.response_id
                  : typeof data.run_id === "string"
                    ? data.run_id
                    : currentEventRunId;
              if (rId) {
                let cMap = replayChartsByRun.get(rId);
                if (!cMap) {
                  cMap = new Map<string, CollectedChartLayerRef>();
                  replayChartsByRun.set(rId, cMap);
                }
                let wMap = replayWidgetsByRun.get(rId);
                if (!wMap) {
                  wMap = new Map<string, CollectedHtmlWidgetRef>();
                  replayWidgetsByRun.set(rId, wMap);
                }
                this.collectArtifactChartOrWidget(data, cMap, wMap);
              }
              continue;
            }

            if (sse.event === "replay_done" || sse.event === "history_page") {
              if (sse.event === "replay_done") {
                sawReplayDone = true;
                const rawByRun = data.turn_deliverables_by_run;
                if (
                  rawByRun &&
                  typeof rawByRun === "object" &&
                  !Array.isArray(rawByRun)
                ) {
                  for (const [rid, rawItems] of Object.entries(
                    rawByRun as Record<string, unknown>,
                  )) {
                    const parsedItems = this.parseTurnDeliverables(rawItems);
                    if (parsedItems.length > 0) {
                      replayDeliverablesByRun.set(rid, parsedItems);
                    }
                  }
                }
                const rawSnapsByRun = data.chart_snapshots_by_run;
                if (
                  rawSnapsByRun &&
                  typeof rawSnapsByRun === "object" &&
                  !Array.isArray(rawSnapsByRun)
                ) {
                  for (const [rid, rawSnaps] of Object.entries(
                    rawSnapsByRun as Record<string, unknown>,
                  )) {
                    const parsedSnaps = parseChartSnapshotsList(rawSnaps);
                    if (parsedSnaps.length > 0) {
                      replayChartSnapshotsByRun.set(rid, parsedSnaps);
                    }
                  }
                }
                const rawBlockedByRun = data.outbound_blocked_by_run;
                if (
                  rawBlockedByRun &&
                  typeof rawBlockedByRun === "object" &&
                  !Array.isArray(rawBlockedByRun)
                ) {
                  for (const [rid, rawBlocked] of Object.entries(
                    rawBlockedByRun as Record<string, unknown>,
                  )) {
                    if (typeof rawBlocked === "boolean") {
                      replayOutboundBlockedByRun.set(rid, rawBlocked);
                    }
                  }
                }
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
                ? accByTurn.get(latestTurnInBatch)?.result().text
                : undefined) ??
              accByRun.get(activeMainRunFromSnapshot.runId)?.result().text ??
              "";
            const preCursorLegacyText =
              (latestTurnInBatch !== null
                ? accByTurn.get(latestTurnInBatch)?.legacyResult()
                : undefined) ??
              accByRun.get(activeMainRunFromSnapshot.runId)?.legacyResult() ??
              "";
            const liveSettled = await this.consumeInFlightReplayRunStream({
              userId,
              threadId,
              runId: activeMainRunFromSnapshot.runId,
              lastEventId: activeMainRunFromSnapshot.lastEventId,
              preCursorText,
              preCursorLegacyText,
              turnIndex: latestTurnInBatch ?? turnIndex,
              platform,
              targetAddress,
              inboundMessageId,
              scopeKey,
              generation,
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
            foundLegacyText = liveSettled.legacyText;
            foundSawBusinessToolCall =
              foundSawBusinessToolCall || liveSettled.sawBusinessToolCall;
            foundImageUrls = {
              ...foundImageUrls,
              ...(liveSettled.resolvedImageUrls ?? {}),
            };
            foundImagesByMessage = mergeImagesByMessage(
              foundImagesByMessage,
              liveSettled.resolvedImagesByMessage,
            );
            foundToolCallMsgIds = {
              ...foundToolCallMsgIds,
              ...(liveSettled.toolCallMessageIds ?? {}),
            };
            foundTargetTurn = true;
            resolvedRunId = activeMainRunFromSnapshot.runId;
            if (liveSettled.charts.length > 0) {
              let cMap = replayChartsByRun.get(resolvedRunId);
              if (!cMap) {
                cMap = new Map<string, CollectedChartLayerRef>();
                replayChartsByRun.set(resolvedRunId, cMap);
              }
              for (const chart of liveSettled.charts) {
                cMap.set(
                  chartLayerCompositeKey(chart.chartId, chart.layerId),
                  chart,
                );
              }
            }
            if (liveSettled.widgets.length > 0) {
              let wMap = replayWidgetsByRun.get(resolvedRunId);
              if (!wMap) {
                wMap = new Map<string, CollectedHtmlWidgetRef>();
                replayWidgetsByRun.set(resolvedRunId, wMap);
              }
              for (const widget of liveSettled.widgets) {
                wMap.set(widget.artifactId, widget);
              }
            }
            if (liveSettled.chartSnapshots.length > 0) {
              const mergedSnaps = new Map<string, ChartSnapshotItem>();
              for (const s of replayChartSnapshotsByRun.get(resolvedRunId) ?? []) {
                mergedSnaps.set(chartLayerCompositeKey(s.chart_id, s.layer_id), s);
              }
              for (const s of liveSettled.chartSnapshots) {
                mergedSnaps.set(chartLayerCompositeKey(s.chart_id, s.layer_id), s);
              }
              replayChartSnapshotsByRun.set(resolvedRunId, [
                ...mergedSnaps.values(),
              ]);
            }
            if (typeof liveSettled.outboundBlocked === "boolean") {
              replayOutboundBlockedByRun.set(
                resolvedRunId,
                liveSettled.outboundBlocked,
              );
            }
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
                  generation,
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
                    generation,
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

        const collectAccImages = (acc: DeliverableTextAccumulator | undefined): void => {
          if (!acc) return;
          foundImageUrls = { ...foundImageUrls, ...acc.getGlobalImageUrlMap() };
          foundImagesByMessage = mergeImagesByMessage(
            foundImagesByMessage,
            acc.getImagesByMessage(),
          );
          foundToolCallMsgIds = {
            ...foundToolCallMsgIds,
            ...acc.getToolCallMessageIds(),
          };
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
          const turnAccRes = accByTurn.get(turnIndex)?.result();
          const candidateText = turnAccRes?.text ?? "";
          const candidateLegacyText = accByTurn.get(turnIndex)?.legacyResult() ?? "";
          const candidateSawTools = Boolean(turnAccRes?.sawBusinessToolCall);
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
            foundLegacyText = candidateLegacyText;
            foundSawBusinessToolCall = candidateSawTools;
            collectAccImages(accByTurn.get(turnIndex));
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
              foundLegacyText = candidateLegacyText;
              foundSawBusinessToolCall = candidateSawTools;
              collectAccImages(accByTurn.get(turnIndex));
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
          const runAccRes = accByRun.get(runId)?.result();
          const runCandidateText = runAccRes?.text ?? "";
          const runCandidateLegacyText = accByRun.get(runId)?.legacyResult() ?? "";
          const runCandidateSawTools = Boolean(runAccRes?.sawBusinessToolCall);
          if (
            settledRunsInBatch.has(runId) &&
            runCandidateText.trim().length > 0
          ) {
            foundText = runCandidateText;
            foundLegacyText = runCandidateLegacyText;
            foundSawBusinessToolCall = runCandidateSawTools;
            collectAccImages(accByRun.get(runId));
            foundTargetTurn = true;
            break;
          }
          // When `turnIndex` was null and `replay` chunks don't carry `run_id`/`response_id`, match the single latest settled turn on the first page
          // ONLY if its `user_message.run_id` (and `replay_done.latest_run_id`) matches `runId` and its text is non-empty!
          const latestTurnRes =
            latestTurnInBatch !== null
              ? accByTurn.get(latestTurnInBatch)?.result()
              : undefined;
          const latestTurnLegacy =
            latestTurnInBatch !== null
              ? (accByTurn.get(latestTurnInBatch)?.legacyResult() ?? "")
              : "";
          if (
            isFirstPage &&
            latestTurnInBatch !== null &&
            settledTurnsInBatch.has(latestTurnInBatch) &&
            (userMsgRunIdByTurn.get(latestTurnInBatch) ?? null) === runId &&
            (!replayDoneMeta.latest_run_id ||
              replayDoneMeta.latest_run_id === runId) &&
            (latestTurnRes?.text ?? "").trim().length > 0
          ) {
            foundText = latestTurnRes?.text ?? "";
            foundLegacyText = latestTurnLegacy;
            foundSawBusinessToolCall = Boolean(latestTurnRes?.sawBusinessToolCall);
            collectAccImages(accByTurn.get(latestTurnInBatch));
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
              foundLegacyText = runCandidateLegacyText || latestTurnLegacy;
              foundSawBusinessToolCall =
                runCandidateSawTools ||
                Boolean(
                  runCandidateTurnIdx !== null
                    ? accByTurn.get(runCandidateTurnIdx)?.sawBusinessToolCall
                    : false,
                );
              collectAccImages(accByRun.get(runId));
              if (runCandidateTurnIdx !== null) {
                collectAccImages(accByTurn.get(runCandidateTurnIdx));
              }
              foundTargetTurn = true;
              resolvedRunId = runId;
              break;
            }
            sawUnsettledTargetTurn = true;
            break;
          }
        } else {
          // Neither `turnIndex` nor `runId` specified: take latest settled turn on first page
          const latestTurnRes =
            latestTurnInBatch !== null
              ? accByTurn.get(latestTurnInBatch)?.result()
              : undefined;
          const latestTurnLegacy =
            latestTurnInBatch !== null
              ? (accByTurn.get(latestTurnInBatch)?.legacyResult() ?? "")
              : "";
          const lastTurnRes = lastTurnAcc.result();
          if (
            latestTurnInBatch !== null &&
            settledTurnsInBatch.has(latestTurnInBatch) &&
            (latestTurnRes?.text ?? "").trim().length > 0
          ) {
            foundText = latestTurnRes?.text ?? "";
            foundLegacyText = latestTurnLegacy;
            foundSawBusinessToolCall = Boolean(latestTurnRes?.sawBusinessToolCall);
            collectAccImages(accByTurn.get(latestTurnInBatch));
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
          if (lastTurnRes.text.trim().length > 0 && accByTurn.size === 0) {
            foundText = lastTurnRes.text;
            foundLegacyText = lastTurnAcc.legacyResult();
            foundSawBusinessToolCall = lastTurnRes.sawBusinessToolCall;
            collectAccImages(lastTurnAcc);
            foundTargetTurn = true;
            if (!resolvedRunId && replayDoneMeta.latest_run_id) {
              resolvedRunId = replayDoneMeta.latest_run_id;
            }
            break;
          }
          if (
            latestTurnInBatch !== null &&
            (!settledTurnsInBatch.has(latestTurnInBatch) ||
              (latestTurnRes?.text ?? "").trim().length === 0)
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
              foundSawBusinessToolCall = Boolean(latestTurnRes?.sawBusinessToolCall);
              collectAccImages(accByTurn.get(latestTurnInBatch));
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
      // keep polling `/messages/replay` with backoff up to `busyThreadMaxWaitMs` until it settles.
      // When attaching via a non-zero cursor and `run_end` was already observed (`immediateReplayAfterLiveRunEnd`),
      // always allow one immediate full `/messages/replay` read even if `busyThreadMaxWaitMs` has elapsed so long
      // runs that finished after >30m still deliver their text, charts, and widgets; the wait cap still bounds
      // runs that have not finished yet.
      const withinWaitBudget = getReplayElapsedMs() < this.busyThreadMaxWaitMs;
      const allowPostRunEndOvertimeReplay =
        immediateReplayAfterLiveRunEnd && !usedPostRunEndOvertimeReplay;
      const canContinueWaiting =
        !this.shuttingDown &&
        ((withinWaitBudget &&
          (sawUnsettledTargetTurn || unsettledPass < BACKOFF_DELAYS_MS.length)) ||
          allowPostRunEndOvertimeReplay);
      if (!canContinueWaiting) {
        break;
      }

      if (!withinWaitBudget && allowPostRunEndOvertimeReplay) {
        usedPostRunEndOvertimeReplay = true;
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
          generation,
          activityOrigin,
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

    const effectiveFallbackText = resolveChannelFallbackText({
      extractedText: foundText,
      sawBusinessToolCall: foundSawBusinessToolCall,
      sentToCurrentCount: prog.sent_to_current_count,
      hasDeliveredCurrentAgentSend: prog.has_delivered_current_agent_send,
      hasFailedCurrentAgentSend: prog.has_failed_current_agent_send,
      strictDeliverableTextEnabled: prog.strict_deliverable_text_enabled !== false,
      legacyText: foundLegacyText,
    });
    const fallbackDeliveryKey = resolvedRunId
      ? `fallback:${resolvedRunId}`
      : `fallback:${threadId}:turn:${turnIndex}`;
    const partialNoticeKeyId =
      resolvedRunId ?? `${threadId}:turn:${turnIndex ?? "latest"}`;
    const hasUnresolvedAgentSendImages =
      prog.has_unresolved_current_agent_send_images === true;
    const replayResolvedImageUrls: Record<string, string> = {
      ...(prog.resolved_run_images ?? {}),
      ...foundImageUrls,
    };
    const replayResolvedImagesByMessage = mergeImagesByMessage(
      prog.resolved_run_images_by_message,
      foundImagesByMessage,
    );
    const canSettleFallback =
      foundTargetTurn &&
      (effectiveFallbackText.length > 0 || hasUnresolvedAgentSendImages) &&
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
        sourceScopeKey: scopeKey ?? null,
        sourceGeneration: generation ?? null,
        activityOrigin:
          activityOrigin ?? (isBackgroundRun ? "notification" : "replay"),
        trimmedText: effectiveFallbackText,
        sentToCurrentCount: prog.sent_to_current_count,
        hasUnresolvedCurrentAgentSendImages: hasUnresolvedAgentSendImages,
        unresolvedCurrentAgentSends: prog.unresolved_current_agent_sends,
        resolvedImageUrls: replayResolvedImageUrls,
        resolvedImagesByMessage: replayResolvedImagesByMessage,
        toolCallMessageIds: foundToolCallMsgIds,
      });
      if (!settledOk) {
        fallbackTextDeliveryFailed = true;
      }
    }

    if (
      !fallbackTextDeliveryFailed &&
      !tipInterruptDeliveryFailed &&
      !prog.has_in_flight_or_unknown_current_text &&
      resolvedRunId &&
      targetAddress.endsWith(":@me")
    ) {
      const replayDeliverables = replayDeliverablesByRun.get(resolvedRunId) ?? [];
      if (replayDeliverables.length > 0) {
        const delivOk = await this.settleDmTurnDeliverables({
          userId,
          threadId,
          runId: resolvedRunId,
          platform,
          targetAddress,
          inboundMessageId: inboundMessageId ?? null,
          threadScopeKey: scopeKey ?? null,
          sourceScopeKey: scopeKey ?? null,
          sourceGeneration: generation ?? null,
          activityOrigin:
            activityOrigin ?? (isBackgroundRun ? "notification" : "replay"),
          assistantText: trimmed,
          deliverables: replayDeliverables,
        });
        if (!delivOk) {
          fallbackTextDeliveryFailed = true;
        }
      }
    }

    if (
      !fallbackTextDeliveryFailed &&
      !tipInterruptDeliveryFailed &&
      !prog.has_in_flight_or_unknown_current_text &&
      resolvedRunId
    ) {
      const replaySnaps = replayChartSnapshotsByRun.get(resolvedRunId) ?? [];
      const replayCharts = [
        ...(replayChartsByRun.get(resolvedRunId)?.values() ?? []),
      ];
      const replayWidgets = [
        ...(replayWidgetsByRun.get(resolvedRunId)?.values() ?? []),
      ];
      if (
        replaySnaps.length > 0 ||
        replayCharts.length > 0 ||
        replayWidgets.length > 0
      ) {
        await this.settleTurnChartsAndWidgets({
          userId,
          threadId,
          runId: resolvedRunId,
          platform,
          targetAddress,
          inboundMessageId: inboundMessageId ?? null,
          threadScopeKey: scopeKey ?? null,
          sourceScopeKey: scopeKey ?? null,
          sourceGeneration: generation ?? null,
          activityOrigin:
            activityOrigin ?? (isBackgroundRun ? "notification" : "replay"),
          charts: replayCharts,
          initialSnapshots: replaySnaps,
          widgets: replayWidgets,
          outboundBlocked: replayOutboundBlockedByRun.get(resolvedRunId),
        });
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
    preCursorLegacyText?: string;
    turnIndex: number | null;
    platform: ChannelPlatform;
    targetAddress: string;
    inboundMessageId?: string | null;
    scopeKey?: string | null;
    generation?: number | null;
  }): Promise<{
    settled: boolean;
    runEnded: boolean;
    assistantText: string;
    legacyText: string;
    sawBusinessToolCall: boolean;
    charts: CollectedChartLayerRef[];
    widgets: CollectedHtmlWidgetRef[];
    chartSnapshots: ChartSnapshotItem[];
    outboundBlocked?: boolean;
    resolvedImageUrls?: Record<string, string>;
    resolvedImagesByMessage?: Record<string, Record<string, string>>;
    toolCallMessageIds?: Record<string, string>;
  }> {
    const emptyArtifacts = {
      legacyText: "",
      sawBusinessToolCall: false,
      charts: [] as CollectedChartLayerRef[],
      widgets: [] as CollectedHtmlWidgetRef[],
      chartSnapshots: [] as ChartSnapshotItem[],
      outboundBlocked: undefined as boolean | undefined,
      resolvedImageUrls: {} as Record<string, string>,
      resolvedImagesByMessage: {} as Record<string, Record<string, string>>,
      toolCallMessageIds: {} as Record<string, string>,
    };
    try {
      const streamResp = await this.backend.streamThreadMessages(
        params.userId,
        params.threadId,
        { runId: params.runId, lastEventId: params.lastEventId },
      );
      if (!streamResp.isSse || !streamResp.events) {
        return { settled: false, runEnded: false, assistantText: "", ...emptyArtifacts };
      }

      const preCursorText = params.preCursorText ?? "";
      const preCursorLegacyText = params.preCursorLegacyText ?? preCursorText;
      const deliverableAcc = new DeliverableTextAccumulator();
      let sawPostCursorToolCalls = false;
      let sawStreamGap = false;
      let sawRunEnd = false;
      let sawInterrupt = false;
      let interruptDelivered = true;
      let runEndOutcome = "completed";
      const collectedCharts = new Map<string, CollectedChartLayerRef>();
      const collectedWidgets = new Map<string, CollectedHtmlWidgetRef>();
      let runEndChartSnapshots: ChartSnapshotItem[] = [];
      let runEndOutboundBlocked: boolean | undefined;

      for await (const sse of streamResp.events) {
        if (sse.event === ":comment") continue;
        const payload = this.safeParseJson(sse.data);

        if (sse.event === "stream_gap" || sse.event === "error") {
          sawStreamGap = true;
          break;
        }

        if (sse.event === "metadata") {
          collectReportSourceChartLayers(payload, collectedCharts);
          continue;
        }

        if (sse.event === "user_message") {
          collectReportSourceChartLayers(payload.metadata, collectedCharts);
          continue;
        }

        if (sse.event === "personal_integration_notice") {
          const noticeMsg =
            typeof payload.message === "string" ? payload.message.trim() : "";
          const noticeTid =
            typeof payload.thread_id === "string" && payload.thread_id
              ? payload.thread_id
              : params.threadId;
          if (noticeMsg && noticeTid) {
            await this.deliverPersonalFirstTouchNotice({
              userId: params.userId,
              threadId: noticeTid,
              runId: params.runId,
              platform: params.platform,
              chatType: isDmTargetAddress(params.targetAddress) ? "dm" : "group",
              targetAddress: params.targetAddress,
              inboundMessageId: params.inboundMessageId ?? null,
              threadScopeKey: params.scopeKey ?? null,
              sourceGeneration: params.generation ?? null,
              message: noticeMsg,
            });
          }
          continue;
        }

        if (sse.event === "artifact") {
          this.collectArtifactChartOrWidget(
            payload,
            collectedCharts,
            collectedWidgets,
          );
          continue;
        }

        if (sse.event === "tool_call_result") {
          if (
            payload.artifact &&
            typeof payload.artifact === "object" &&
            !Array.isArray(payload.artifact)
          ) {
            collectPublishedChartLayers(
              (payload.artifact as Record<string, unknown>).chart_layers,
              collectedCharts,
            );
          }
          if (this.isMainAgentLane(payload.agent)) {
            deliverableAcc.pushEvent("tool_call_result", payload);
            if (deliverableAcc.sawBusinessToolCall) {
              sawPostCursorToolCalls = true;
            }
          }
          continue;
        }

        if (sse.event === "tool_call_start") {
          if (this.isMainAgentLane(payload.agent)) {
            deliverableAcc.pushEvent("tool_call_start", payload);
            if (deliverableAcc.sawBusinessToolCall) {
              sawPostCursorToolCalls = true;
            }
          }
          continue;
        }

        if (sse.event === "tool_calls") {
          if (this.isMainAgentLane(payload.agent)) {
            deliverableAcc.pushEvent("tool_calls", payload);
            if (deliverableAcc.sawBusinessToolCall) {
              sawPostCursorToolCalls = true;
            }
          }
          continue;
        }

        if (sse.event === "message_discarded") {
          if (this.isMainAgentLane(payload.agent)) {
            deliverableAcc.pushEvent("message_discarded", payload);
          }
          continue;
        }

        if (
          sse.event === "message_chunk" ||
          sse.event === "text" ||
          sse.event === "content_block_delta"
        ) {
          if (this.isMainAgentLane(payload.agent)) {
            deliverableAcc.pushEvent(sse.event, payload);
          }
          continue;
        }

        if (sse.event === "message") {
          if (this.isMainAgentLane(payload.agent)) {
            deliverableAcc.pushEvent("message", payload);
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
              generation: params.generation ?? null,
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
          runEndChartSnapshots = parseChartSnapshotsList(payload.chart_snapshots);
          collectPublishedChartLayers(payload.chart_layers, collectedCharts);
          deliverableAcc.pushEvent("run_end", payload);
          if (typeof payload.outbound_blocked === "boolean") {
            runEndOutboundBlocked = payload.outbound_blocked;
          }
          continue;
        }
      }

      if (sawInterrupt) {
        return {
          settled: interruptDelivered,
          runEnded: true,
          assistantText: "",
          ...emptyArtifacts,
        };
      }
      if (sawRunEnd && runEndOutcome === "completed") {
        const postCursorRes = deliverableAcc.result();
        const postCursorText = postCursorRes.text;
        const postCursorLegacyText = deliverableAcc.legacyResult();
        // Backend `src/server/handlers/chat/run_stream_reader.py:183` only yields events strictly AFTER `last_event_id`
        // (`cursor = f"{last_event_id}-0"`), and `src/server/services/history/replay/stubs.py:88` emits `archived = []`
        // (only `user_message`) for an in-flight turn (`open_run`). Even if a post-cursor `tool_calls` started a new
        // text segment with non-empty post-tool text, any pre-cursor `html_widget` artifacts or un-snapshotted
        // `chart_layer` events emitted before `last_event_id` are absent from both the stub replay page and the resumed
        // tail stream. Always return `settled: false, runEnded: true` when attaching with a non-null cursor without
        // pre-cursor replay content (or on `sawStreamGap` / empty text) so `deliverReplayFallback` immediately re-reads
        // the full committed `/messages/replay` before settling.
        const missingPreCursorPrefix =
          params.lastEventId !== null &&
          preCursorText.length === 0 &&
          preCursorLegacyText.length === 0;
        const combinedText = sawPostCursorToolCalls
          ? postCursorText
          : `${preCursorText}${postCursorText}`;
        const combinedLegacyText = sawPostCursorToolCalls
          ? postCursorLegacyText.trim().length > 0
            ? postCursorLegacyText
            : preCursorLegacyText
          : `${preCursorLegacyText}${postCursorLegacyText}`;
        const combinedSawTool =
          sawPostCursorToolCalls || postCursorRes.sawBusinessToolCall;
        if (
          sawStreamGap ||
          missingPreCursorPrefix ||
          (combinedText.trim().length === 0 &&
            combinedLegacyText.trim().length === 0 &&
            !combinedSawTool)
        ) {
          return { settled: false, runEnded: true, assistantText: "", ...emptyArtifacts };
        }
        return {
          settled: true,
          runEnded: true,
          assistantText: combinedText,
          legacyText: combinedLegacyText,
          sawBusinessToolCall: combinedSawTool,
          charts: [...collectedCharts.values()],
          widgets: [...collectedWidgets.values()],
          chartSnapshots: runEndChartSnapshots,
          outboundBlocked: runEndOutboundBlocked,
          resolvedImageUrls: deliverableAcc.getGlobalImageUrlMap(),
          resolvedImagesByMessage: deliverableAcc.getImagesByMessage(),
          toolCallMessageIds: deliverableAcc.getToolCallMessageIds(),
        };
      }
      return { settled: false, runEnded: sawRunEnd, assistantText: "", ...emptyArtifacts };
    } catch {
      return { settled: false, runEnded: false, assistantText: "", ...emptyArtifacts };
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
    generation?: number | null;
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
      generation,
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

    const resolvedActionType =
      kind === "web_only_approval"
        ? resolveWebOnlyApprovalActionType({
            payloadActionType:
              typeof payload.action_type === "string"
                ? payload.action_type
                : undefined,
            rawKind,
            actionRequests,
            reviewConfigs: Array.isArray(payload.review_configs)
              ? payload.review_configs
              : undefined,
          }) ?? rawKind
        : typeof payload.action_type === "string"
          ? payload.action_type
          : rawKind;

    const pendingState: PendingInterruptState = {
      interrupt_id: interruptId,
      kind,
      category: kind,
      thread_id: threadId,
      run_id: runId,
      turn_index: turnIndex,
      action_type: resolvedActionType,
      ...(actionRequests.length > 0 ? { action_requests: actionRequests } : {}),
      questions: extractedQuestions,
      prompt: promptTextFromPayload,
    };

    const prog = await this.backend.updateInboundProgress({
      session_interrupt_update: {
        platform,
        scope_key: scopeKey,
        dalton_user_id: userId,
        generation: generation ?? null,
        thread_id: threadId,
        run_id: runId,
        interrupt_id: interruptId,
        pending_interrupt: pendingState,
      },
    });

    const isStaleSessionInterrupt =
      prog.session_is_current_for_interrupt === false;
    const hasKnownGeneration =
      typeof generation === "number" && generation > 0;
    // A failed prompt retried after /new or a workspace switch keeps the original
    // scope and generation and uses replay. A live send still uses conversation.
    const interruptActivityOrigin: DeliveryActivityOrigin = isStaleSessionInterrupt
      ? "replay"
      : hasKnownGeneration
        ? "conversation"
        : "none";
    const promptText = this.formatInterruptPrompt(
      pendingState,
      isStaleSessionInterrupt,
    );
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
      sourceScopeKey: scopeKey,
      sourceGeneration: hasKnownGeneration ? generation : null,
      activityOrigin: interruptActivityOrigin,
    });
    return deliveryRes.status === "sent" || deliveryRes.status === "partial";
  }

  private formatInterruptPrompt(
    state: PendingInterruptState,
    isStaleSessionInterrupt = false,
  ): string {
    const threadLink = `${this.webBaseUrl}/chat/t/${state.thread_id}`;
    if (state.kind === "web_only_approval") {
      return webOnlyApprovalText(
        state.action_type,
        threadLink,
        state.action_requests,
      );
    }
    if (state.kind === "credit_pause") {
      const baseText =
        state.prompt ||
        "This run is paused due to credit limits. Reply `continue` (or send additional instructions) after topping up credits to resume.";
      return isStaleSessionInterrupt
        ? `${baseText}\nThis request belongs to an earlier conversation thread. Please resume it in the web UI: ${threadLink}`
        : baseText;
    }
    if (state.questions && state.questions.length > 0) {
      const lines: string[] = [];
      for (const q of state.questions) {
        lines.push(q.question);
        (q.options ?? []).forEach((opt, idx) => {
          lines.push(`${idx + 1}. ${opt.label}`);
        });
      }
      if (isStaleSessionInterrupt) {
        lines.push(
          `This question belongs to an earlier conversation thread. Please reply in the web UI: ${threadLink}`,
        );
      } else {
        lines.push("Reply with an option number, your answer, or `skip`.");
      }
      return lines.join("\n");
    }
    const basePrompt =
      state.prompt || "Please reply to continue, or reply `skip`.";
    return isStaleSessionInterrupt
      ? `${basePrompt}\nThis request belongs to an earlier conversation thread. Please reply in the web UI: ${threadLink}`
      : basePrompt;
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
      generation: resolved.generation,
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
      if (
        params.scopeKey &&
        (params.scopeKey !== params.targetAddress || !existing.scopeKey)
      ) {
        existing.scopeKey = params.scopeKey;
      }
      if (params.generation > 0) {
        existing.generation = params.generation;
      }
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
                        generation: handle.generation,
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
        generation: handle.generation,
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
    const deliverableAcc = new DeliverableTextAccumulator();
    let isQuiet = false;
    let sawStreamErrorOrGap = false;
    let sawRunEnd = false;
    let sawInterrupt = false;
    let interruptDelivered = true;
    let runEndOutcome: string | null = null;
    let runEndDeliverables: TurnDeliverableItem[] = [];
    let runEndChartSnapshots: ChartSnapshotItem[] = [];
    let runEndOutboundBlocked: boolean | undefined;
    const collectedCharts = new Map<string, CollectedChartLayerRef>();
    const collectedWidgets = new Map<string, CollectedHtmlWidgetRef>();
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
              deliverableAcc.pushEvent("tool_calls", payload);
            }
            continue;
          }

          if (sse.event === "message_discarded") {
            if (this.isMainAgentLane(payload.agent)) {
              deliverableAcc.pushEvent("message_discarded", payload);
            }
            continue;
          }

          if (sse.event === "stream_gap" || sse.event === "error") {
            sawStreamErrorOrGap = true;
            break;
          }

          if (sse.event === "personal_integration_notice") {
            const noticeMsg =
              typeof payload.message === "string" ? payload.message.trim() : "";
            const noticeTid =
              typeof payload.thread_id === "string" && payload.thread_id
                ? payload.thread_id
                : handle.threadId;
            if (noticeMsg && noticeTid) {
              await this.deliverPersonalFirstTouchNotice({
                userId: handle.userId,
                threadId: noticeTid,
                runId,
                platform: handle.platform,
                chatType: isDmTargetAddress(handle.targetAddress) ? "dm" : "group",
                targetAddress: handle.targetAddress,
                threadScopeKey: handle.scopeKey,
                sourceGeneration: handle.generation,
                message: noticeMsg,
              });
            }
            continue;
          }

          if (sse.event === "artifact") {
            this.collectArtifactChartOrWidget(
              payload,
              collectedCharts,
              collectedWidgets,
            );
            continue;
          }

          if (sse.event === "tool_call_start") {
            if (this.isMainAgentLane(payload.agent)) {
              deliverableAcc.pushEvent("tool_call_start", payload);
            }
            continue;
          }

          if (sse.event === "tool_call_result") {
            if (
              payload.artifact &&
              typeof payload.artifact === "object" &&
              !Array.isArray(payload.artifact)
            ) {
              collectPublishedChartLayers(
                (payload.artifact as Record<string, unknown>).chart_layers,
                collectedCharts,
              );
            }
            if (this.isMainAgentLane(payload.agent)) {
              deliverableAcc.pushEvent("tool_call_result", payload);
            }
            continue;
          }

          if (sse.event === "metadata") {
            collectReportSourceChartLayers(payload, collectedCharts);
            continue;
          }

          if (sse.event === "user_message") {
            collectReportSourceChartLayers(payload.metadata, collectedCharts);
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
            runEndDeliverables = this.parseTurnDeliverables(
              payload.turn_deliverables,
            );
            runEndChartSnapshots = parseChartSnapshotsList(
              payload.chart_snapshots,
            );
            collectPublishedChartLayers(payload.chart_layers, collectedCharts);
            deliverableAcc.pushEvent("run_end", payload);
            if (typeof payload.outbound_blocked === "boolean") {
              runEndOutboundBlocked = payload.outbound_blocked;
            }
            continue;
          }

          if (
            sse.event === "message_chunk" ||
            sse.event === "text" ||
            sse.event === "content_block_delta"
          ) {
            if (this.isMainAgentLane(payload.agent)) {
              deliverableAcc.pushEvent(sse.event, payload);
            }
          } else if (sse.event === "message") {
            if (this.isMainAgentLane(payload.agent)) {
              deliverableAcc.pushEvent("message", payload);
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
              generation: handle.generation,
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
            generation: handle.generation,
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

      const bgExtracted = deliverableAcc.result();
      const assistantText = bgExtracted.text;

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
          const trimmed = resolveChannelFallbackText({
            extractedText: assistantText,
            sawBusinessToolCall: bgExtracted.sawBusinessToolCall,
            sentToCurrentCount: prog.sent_to_current_count,
            hasDeliveredCurrentAgentSend: prog.has_delivered_current_agent_send,
            hasFailedCurrentAgentSend: prog.has_failed_current_agent_send,
            strictDeliverableTextEnabled: prog.strict_deliverable_text_enabled !== false,
            legacyText: deliverableAcc.legacyResult(),
          });
          const bgFallbackDeliveryKey = `fallback:${runId}`;
          const bgHasUnresolvedAgentSendImages =
            prog.has_unresolved_current_agent_send_images === true;
          const bgResolvedImageUrls: Record<string, string> = {
            ...(prog.resolved_run_images ?? {}),
            ...deliverableAcc.getGlobalImageUrlMap(),
          };
          const bgResolvedImagesByMessage = mergeImagesByMessage(
            prog.resolved_run_images_by_message,
            deliverableAcc.getImagesByMessage(),
          );
          const bgToolCallMessageIds = deliverableAcc.getToolCallMessageIds();
          if (
            !sawStreamErrorOrGap &&
            (trimmed.length > 0 || bgHasUnresolvedAgentSendImages) &&
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
              sourceScopeKey: handle.scopeKey,
              sourceGeneration: handle.generation,
              activityOrigin: "notification",
              trimmedText: trimmed,
              sentToCurrentCount: prog.sent_to_current_count,
              hasUnresolvedCurrentAgentSendImages: bgHasUnresolvedAgentSendImages,
              unresolvedCurrentAgentSends: prog.unresolved_current_agent_sends,
              resolvedImageUrls: bgResolvedImageUrls,
              resolvedImagesByMessage: bgResolvedImagesByMessage,
              toolCallMessageIds: bgToolCallMessageIds,
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
                generation: handle.generation,
                isBackgroundRun: true,
              });
            }
          }
          if (
            !keepFallbackPending &&
            !sawStreamErrorOrGap &&
            !prog.has_in_flight_or_unknown_current_text &&
            runEndDeliverables.length > 0 &&
            handle.targetAddress.endsWith(":@me")
          ) {
            const delivOk = await this.settleDmTurnDeliverables({
              userId: handle.userId,
              threadId: handle.threadId,
              runId,
              platform: handle.platform,
              targetAddress: handle.targetAddress,
              threadScopeKey: handle.scopeKey,
              sourceScopeKey: handle.scopeKey,
              sourceGeneration: handle.generation,
              activityOrigin: "notification",
              assistantText: trimmed,
              deliverables: runEndDeliverables,
            });
            if (!delivOk) {
              keepFallbackPending = true;
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
              generation: handle.generation,
              isBackgroundRun: true,
            });
            if (!settled) {
              keepFallbackPending = true;
            }
          }
          if (
            !keepFallbackPending &&
            !sawStreamErrorOrGap &&
            !prog.has_in_flight_or_unknown_current_text &&
            (collectedCharts.size > 0 ||
              runEndChartSnapshots.length > 0 ||
              collectedWidgets.size > 0)
          ) {
            await this.settleTurnChartsAndWidgets({
              userId: handle.userId,
              threadId: handle.threadId,
              runId,
              platform: handle.platform,
              targetAddress: handle.targetAddress,
              threadScopeKey: handle.scopeKey,
              sourceScopeKey: handle.scopeKey,
              sourceGeneration: handle.generation,
              activityOrigin: "notification",
              charts: [...collectedCharts.values()],
              initialSnapshots: runEndChartSnapshots,
              widgets: [...collectedWidgets.values()],
              outboundBlocked: runEndOutboundBlocked,
            });
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
        try {
          await this.evaluateWatchCloseConditions(
            handle.userId,
            handle.threadId,
          );
        } catch (err) {
          if (isStaleGatewayEpochError(err)) {
            throw err;
          }
        }
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

  private isWatchBusyError(err: unknown): boolean {
    if (!(err instanceof BackendHttpError) || err.status !== 409) {
      return false;
    }
    const bodyObj = err.body as {
      code?: unknown;
      detail?: { code?: unknown } | null;
    } | null;
    const errCode =
      typeof bodyObj?.code === "string"
        ? bodyObj.code
        : typeof bodyObj?.detail?.code === "string"
          ? bodyObj.detail.code
          : "";
    return errCode === "watch_busy";
  }

  private async syncWatchReconnectState(
    handle: ActiveWatchHandle,
  ): Promise<boolean> {
    if (handle.syncingReconnect) {
      return !handle.needsReconnectSync;
    }
    handle.syncingReconnect = true;
    try {
      let rec: I7RecoverResponse;
      try {
        rec = await this.backend.recoverState({
          mode: "watch_reconnect",
          thread_id: handle.threadId,
        });
      } catch (err) {
        if (this.isWatchBusyError(err)) {
          handle.needsReconnectSync = true;
          return false;
        }
        throw err;
      }

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
        const bgOk = await this.recoverSingleBackgroundRun(
          bg,
          rec.thread_watches,
        );
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
                generation: w.generation,
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
    let rec: I7RecoverResponse;
    try {
      rec = await this.backend.recoverState({
        mode: "watch_reconnect",
        thread_id: threadId,
      });
    } catch (err) {
      if (this.isWatchBusyError(err)) {
        const existing = this.activeWatches.get(threadId);
        if (existing) {
          existing.needsReconnectSync = true;
        }
        return;
      }
      throw err;
    }

    for (const bg of rec.background_runs ?? []) {
      await this.recoverSingleBackgroundRun(bg, rec.thread_watches);
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
              generation: w.generation,
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

  async fetchStartupRecoverySnapshot(): Promise<I7RecoverResponse> {
    return await this.backend.recoverState({ mode: "startup" });
  }

  dispatchStartupRecoverySnapshot(recovery: I7RecoverResponse): Promise<void> {
    if (!this.startupRecoveryPromise) {
      this.startupRecoveryPromise = this.executeStartupRecoverySnapshot(
        recovery,
      ).catch(() => {
        // Prevent unhandled rejection during background startup recovery
      });
    }
    return this.startupRecoveryPromise;
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
    const recovery = await this.fetchStartupRecoverySnapshot();
    await this.executeStartupRecoverySnapshot(recovery);
  }

  private async executeStartupRecoverySnapshot(
    recovery: I7RecoverResponse,
  ): Promise<void> {
    // Run recovery tasks concurrently so one long-lived SSE stream does not block other rows
    await Promise.allSettled([
      ...(recovery.inbound_events ?? []).map((row) =>
        this.recoverSingleInboundRow(row),
      ),
      ...(recovery.background_runs ?? []).map((bg) =>
        this.recoverSingleBackgroundRun(bg, recovery.thread_watches),
      ),
      ...(recovery.thread_watches ?? []).map((w) =>
        this.recoverSingleThreadWatch(w),
      ),
      ...(recovery.automation_runs ?? []).map((autoRun) =>
        this.recoverSingleAutomationRun(
          autoRun,
          recovery.strict_deliverable_text_enabled,
          recovery.failure_notice_enabled,
        ),
      ),
    ]);
  }

  private parseRecoveredAttachments(
    primaryValue: unknown,
    fallbackWeixinValue?: unknown,
    fallbackLegacyValue?: unknown,
  ): InboundAttachmentDescriptor[] {
    const source =
      Array.isArray(primaryValue) && primaryValue.length > 0
        ? primaryValue
        : Array.isArray(fallbackWeixinValue) && fallbackWeixinValue.length > 0
          ? fallbackWeixinValue
          : Array.isArray(fallbackLegacyValue)
            ? fallbackLegacyValue
            : [];
    const attachments: InboundAttachmentDescriptor[] = [];
    for (const entry of source) {
      if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
        continue;
      }
      const record = entry as Record<string, unknown>;
      const id = typeof record.id === "string" ? record.id.trim() : "";
      const filename =
        typeof record.filename === "string" ? record.filename.trim() : "";
      const mimeType =
        typeof record.mimeType === "string" ? record.mimeType.trim() : "";
      if (!id || !filename || !mimeType) {
        continue;
      }
      const descriptor: InboundAttachmentDescriptor & Record<string, unknown> =
        {
          id,
          filename,
          mimeType,
          ...(typeof record.sizeBytes === "number" &&
          Number.isFinite(record.sizeBytes) &&
          record.sizeBytes >= 0
            ? { sizeBytes: record.sizeBytes }
            : {}),
        };
      if (typeof record.kind === "string") {
        descriptor.kind = record.kind;
      }
      if (typeof record.encryptQueryParam === "string") {
        descriptor.encryptQueryParam = record.encryptQueryParam;
      }
      if (typeof record.fullUrl === "string") {
        descriptor.fullUrl = record.fullUrl;
      }
      if (typeof record.aesKeyHex === "string") {
        descriptor.aesKeyHex = record.aesKeyHex;
      }
      if (typeof record.aesKeyBase64 === "string") {
        descriptor.aesKeyBase64 = record.aesKeyBase64;
      }
      attachments.push(descriptor);
    }
    return attachments;
  }

  private async recoverSingleInboundRow(
    row: RecoverInboundEventRow,
  ): Promise<void> {
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
      typeof row.session_generation === "number" && row.session_generation > 0
        ? row.session_generation
        : typeof row.generation === "number" && row.generation > 0
          ? row.generation
          : typeof row.raw_payload?.generation === "number" &&
              row.raw_payload.generation > 0
            ? row.raw_payload.generation
            : 0;

    // For Weixin rows, prioritize backend-decrypted attachment metadata over raw_payload (§3.5 / §7.2 #3).
    const recoveredAttachments =
      row.platform === "weixin"
        ? this.parseRecoveredAttachments(
            row.decrypted_weixin_attachments,
            row.raw_payload?.weixin_attachments,
            row.raw_payload?.attachments,
          )
        : this.parseRecoveredAttachments(
            row.raw_payload?.attachments,
            row.raw_payload?.weixin_attachments,
          );
    const decryptedWeixinMeta: WeixinPersistedAttachmentMeta[] | undefined =
      Array.isArray(row.decrypted_weixin_attachments) &&
      row.decrypted_weixin_attachments.length > 0
        ? row.decrypted_weixin_attachments
        : undefined;
    const recoveredWeixinAccountId =
      row.weixin_account_id ??
      (typeof row.raw_payload?.weixin_account_id === "string"
        ? row.raw_payload.weixin_account_id
        : undefined);
    const recoveredWeixinBotId =
      row.weixin_context_bot_id ??
      (typeof row.raw_payload?.ilink_bot_id === "string"
        ? row.raw_payload.ilink_bot_id
        : undefined);
    const recoveredWeixinBotGen =
      typeof row.weixin_context_bot_generation === "number"
        ? row.weixin_context_bot_generation
        : typeof row.raw_payload?.bot_generation === "number"
          ? row.raw_payload.bot_generation
          : undefined;
    const recoveredCommandText =
      typeof row.raw_payload?.command_text === "string"
        ? row.raw_payload.command_text
        : typeof row.raw_payload?.command === "string"
          ? `/${row.raw_payload.command}${
              typeof row.raw_payload.arg === "string" &&
              row.raw_payload.arg.length > 0
                ? ` ${row.raw_payload.arg}`
                : ""
            }`
          : undefined;
    const syntheticEvent: InboundChannelEvent = {
      platform: row.platform,
      platformUserId,
      ...(typeof row.raw_payload?.platform_username === "string" &&
      row.raw_payload.platform_username
        ? { platformUsername: row.raw_payload.platform_username }
        : {}),
      chatType: row.chat_type,
      chatAddress: row.chat_address,
      chatId:
        typeof row.raw_payload?.chat_id === "string" &&
        row.raw_payload.chat_id
          ? row.raw_payload.chat_id
          : row.chat_address,
      ...(typeof row.raw_payload?.chat_name === "string" &&
      row.raw_payload.chat_name
        ? { chatName: row.raw_payload.chat_name }
        : {}),
      threadScopeKey: row.thread_scope_key,
      messageId: row.message_id,
      text:
        typeof row.raw_payload?.text === "string" ? row.raw_payload.text : "",
      ...(recoveredCommandText !== undefined
        ? { commandText: recoveredCommandText }
        : {}),
      ...(row.raw_payload?.voice_without_transcription === true
        ? { voiceWithoutTranscription: true }
        : {}),
      ...(recoveredAttachments.length > 0
        ? { attachments: recoveredAttachments }
        : {}),
      ...(decryptedWeixinMeta ? { weixinMediaMeta: decryptedWeixinMeta } : {}),
      ...(recoveredWeixinAccountId
        ? { weixinAccountId: recoveredWeixinAccountId }
        : {}),
      ...(recoveredWeixinBotId ? { weixinBotId: recoveredWeixinBotId } : {}),
      ...(recoveredWeixinBotGen !== undefined
        ? { weixinBotGeneration: recoveredWeixinBotGen }
        : {}),
      ...(typeof row.raw_payload?.seq === "number"
        ? { weixinContextSeq: row.raw_payload.seq }
        : {}),
      rawPayload: {
        ...(row.raw_payload ?? {}),
        ...(decryptedWeixinMeta
          ? { decrypted_weixin_attachments: decryptedWeixinMeta }
          : {}),
      },
    };

    if (
      row.raw_payload?.invalid_inbound_event === true ||
      row.raw_payload?.rejected_reason === "invalid_inbound_event" ||
      row.raw_payload?.rejected_reason === "weixin_group_unsupported"
    ) {
      return;
    }

    if (await this.recoverStoredBindingConfirmation(row, syntheticEvent)) {
      return;
    }

    if (row.event_kind === "command") {
      const cmdReq = (row.command_request ?? {}) as Record<string, unknown>;
      const cmdResRaw = (row.command_result ?? {}) as Record<string, unknown>;
      const rawCmd =
        typeof cmdReq.cmd === "string"
          ? cmdReq.cmd
          : typeof cmdResRaw.cmd === "string"
            ? cmdResRaw.cmd
            : "workspace_list";
      const cmd: "workspace_list" | "workspace_switch" | "new" | "stop" =
        rawCmd === "workspace_switch" ||
        rawCmd === "new" ||
        rawCmd === "stop"
          ? rawCmd
          : "workspace_list";
      const targetKind: SessionCommandTargetKind | null =
        cmdReq.target_kind === "home" ||
        cmdReq.target_kind === "workspace_id" ||
        cmdReq.target_kind === "exact_name" ||
        cmdReq.target_kind === "shortcut_name"
          ? cmdReq.target_kind
          : null;
      const workspaceId =
        typeof cmdReq.workspace_id === "string" ? cmdReq.workspace_id : null;
      const workspaceQuery =
        typeof cmdReq.workspace_query === "string"
          ? cmdReq.workspace_query
          : null;
      const isNativeCmd =
        this.isNativeInteractionEvent(row.raw_payload) ||
        cmdReq.native_interaction === true ||
        cmdResRaw.native_interaction === true;
      const nativeAckStatus:
        | "acknowledged"
        | "unknown"
        | "failed"
        | undefined =
        cmdReq.native_ack_status === "acknowledged" ||
        cmdReq.native_ack_status === "unknown" ||
        cmdReq.native_ack_status === "failed"
          ? cmdReq.native_ack_status
          : row.raw_payload?.native_ack_status === "acknowledged" ||
              row.raw_payload?.native_ack_status === "unknown" ||
              row.raw_payload?.native_ack_status === "failed"
            ? row.raw_payload.native_ack_status
            : isNativeCmd
              ? "acknowledged"
              : undefined;
      const reauthorizedRes = await this.backend.sessionCommand({
        cmd,
        platform: row.platform,
        platform_user_id: platformUserId,
        chat_type: row.chat_type,
        chat_address: row.chat_address,
        chat_id: syntheticEvent.chatId,
        chat_name: syntheticEvent.chatName ?? null,
        scope_key: row.thread_scope_key,
        message_id: row.message_id,
        request_key:
          row.request_key ||
          makeInboundRequestKey(row.platform, row.message_id),
        raw_payload: row.raw_payload ?? {},
        weixin_account_id: syntheticEvent.weixinAccountId ?? null,
        weixin_context_token: syntheticEvent.weixinContextToken ?? null,
        weixin_bot_id: syntheticEvent.weixinBotId ?? null,
        weixin_bot_generation: syntheticEvent.weixinBotGeneration ?? null,
        weixin_context_seq: syntheticEvent.weixinContextSeq ?? null,
        weixin_context_source:
          syntheticEvent.weixinContextSeq != null ? "protocol" : null,
        target_kind: targetKind,
        workspace_id: workspaceId,
        workspace_query: workspaceQuery,
        ...(typeof cmdReq.command_arg === "string"
          ? { command_arg: cmdReq.command_arg }
          : {}),
        ...(isNativeCmd
          ? {
              native_interaction: true,
              ...(nativeAckStatus ? { native_ack_status: nativeAckStatus } : {}),
            }
          : {}),
      });
      const persistedStopRunId =
        typeof row.run_id === "string" && row.run_id.length > 0
          ? row.run_id
          : typeof cmdResRaw.run_id === "string" && cmdResRaw.run_id.length > 0
            ? cmdResRaw.run_id
            : null;
      const persistedStopThreadId =
        typeof row.thread_id === "string" && row.thread_id.length > 0
          ? row.thread_id
          : typeof cmdResRaw.thread_id === "string" &&
              cmdResRaw.thread_id.length > 0
            ? cmdResRaw.thread_id
            : null;
      const storedStopCancelStatus =
        typeof cmdResRaw.stop_cancel_status === "number" &&
        Number.isFinite(cmdResRaw.stop_cancel_status)
          ? cmdResRaw.stop_cancel_status
          : null;
      const reauthStopCancelStatus =
        typeof reauthorizedRes.stop_cancel_status === "number" &&
        Number.isFinite(reauthorizedRes.stop_cancel_status)
          ? reauthorizedRes.stop_cancel_status
          : null;
      const stopCancelDenied =
        cmd === "stop" &&
        (cmdResRaw.deny_reason === "stop_cancel_denied" ||
          reauthorizedRes.deny_reason === "stop_cancel_denied" ||
          storedStopCancelStatus !== null ||
          reauthStopCancelStatus !== null);
      let recoveredGeneration = generation;
      if (stopCancelDenied) {
        recoveredGeneration =
          typeof cmdResRaw.generation === "number"
            ? cmdResRaw.generation
            : generation;
      } else if (
        typeof reauthorizedRes.generation === "number" &&
        reauthorizedRes.generation > 0
      ) {
        recoveredGeneration = reauthorizedRes.generation;
      } else if (typeof cmdResRaw.generation === "number") {
        recoveredGeneration = cmdResRaw.generation;
      }
      const effectiveCmdRes: I3SessionCommandResponse = {
        ...(cmdResRaw as unknown as I3SessionCommandResponse),
        ...reauthorizedRes,
        generation: recoveredGeneration,
        external_thread_id:
          reauthorizedRes.external_thread_id ??
          (typeof cmdResRaw.external_thread_id === "string"
            ? cmdResRaw.external_thread_id
            : row.external_thread_id),
        thread_id:
          cmd === "stop"
            ? persistedStopThreadId
            : (reauthorizedRes.thread_id ??
              (typeof cmdResRaw.thread_id === "string"
                ? cmdResRaw.thread_id
                : row.thread_id)),
        run_id:
          cmd === "stop"
            ? persistedStopRunId
            : (reauthorizedRes.run_id ??
              (typeof cmdResRaw.run_id === "string"
                ? cmdResRaw.run_id
                : row.run_id)),
        command_effect_status: stopCancelDenied
          ? "done"
          : !reauthorizedRes.ok ||
              (cmd === "stop" && (!persistedStopThreadId || !persistedStopRunId))
            ? "done"
            : row.command_effect_status === "done"
              ? "done"
              : (reauthorizedRes.command_effect_status ??
                row.command_effect_status ??
                (cmd === "stop" ? "pending_cancel" : "done")),
        command_reply_pending: stopCancelDenied
          ? row.command_reply_pending !== false
          : !reauthorizedRes.ok &&
              reauthorizedRes.command_reply_pending === false
            ? false
            : row.command_reply_pending === false
              ? false
              : (reauthorizedRes.command_reply_pending ??
                row.command_reply_pending ??
                true),
        ...(isNativeCmd ? { native_interaction: true } : {}),
        ...(stopCancelDenied
          ? {
              ok: false,
              allowed: false,
              deny_reason: "stop_cancel_denied",
              stop_cancel_status:
                reauthStopCancelStatus ?? storedStopCancelStatus,
            }
          : {}),
      };
      await this.completeSessionCommandExecution({
        event: syntheticEvent,
        cmd,
        targetKind,
        workspaceQuery,
        cmdRes: effectiveCmdRes,
      });
      return;
    }

    const commandCandidate = (
      syntheticEvent.commandText ??
      syntheticEvent.text ??
      ""
    ).trim();
    if (
      (row.status === "received" ||
        row.status === "pending" ||
        row.status === "accepted") &&
      commandCandidate.startsWith("/")
    ) {
      const parsedRecoveryCommand = parseSlashCommand(
        commandCandidate,
        syntheticEvent.chatType,
        syntheticEvent.platform,
      );
      if (parsedRecoveryCommand) {
        const handled = await this.tryHandleSlashCommand(
          syntheticEvent,
          parsedRecoveryCommand,
        );
        if (handled) {
          return;
        }
      }
    }

    const userId = row.dalton_user_id;
    if (!userId) {
      const isNative = this.isNativeInteractionEvent(row.raw_payload);
      await this.backend.updateInboundProgress({
        platform: row.platform,
        message_id: row.message_id,
        status: "failed",
        fallback_pending: false,
        ...(isNative
          ? {
              command_reply_pending: false,
              command_effect_status: "done",
              raw_payload_patch: {
                native_interaction: true,
                native_reply_status: "abandoned_on_restart",
              },
            }
          : {}),
      });
      if (
        !isNative &&
        row.chat_type === "dm" &&
        row.raw_payload?.invalid_inbound_event !== true &&
        row.raw_payload?.rejected_reason !== "weixin_group_unsupported"
      ) {
        await this.sendInboundNotice(
          syntheticEvent,
          null,
          `notice:${row.platform}:${row.message_id}:deny_not_linked`,
          this.formatDenyNotice(row.platform, "not_linked"),
        );
      }
      return;
    }

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
        generation,
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
          generation,
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
          generation,
        });
        return;
      }

      const endedNoticeOk = await this.sendNoticeDelivery({
        deliveryKey: `notice:${row.platform}:${row.message_id}:rec_${runStatus}`,
        userId,
        threadId: row.thread_id,
        runId: row.run_id,
        platform: row.platform,
        target: row.chat_address,
        inboundMessageId: row.message_id,
        text: `The interrupted run ended with status "${runStatus}".`,
        threadScopeKey: row.thread_scope_key,
        sourceScopeKey: row.thread_scope_key,
        activityOrigin: "none",
      });
      // Only sent or partial confirms the notice. denied, unknown, and
      // in_flight do not. Leave dispatched or streaming, and the existing
      // fallback_pending flag, so the next recovery uses this same key.
      if (!endedNoticeOk) {
        return;
      }
      await this.backend.updateInboundProgress({
        platform: row.platform,
        message_id: row.message_id,
        status: "failed",
        fallback_pending: false,
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
      const reresolved = await this.backend.registerAndResolveInbound({
        platform: row.platform,
        platform_user_id: platformUserId,
        chat_type: row.chat_type,
        chat_address: row.chat_address,
        thread_scope_key: row.thread_scope_key,
        message_id: row.message_id,
        request_key: row.request_key,
        raw_payload: row.raw_payload ?? {},
        ...(row.platform === "weixin"
          ? {
              weixin_account_id: row.weixin_account_id ?? null,
              weixin_bot_id: row.weixin_context_bot_id ?? null,
              weixin_bot_generation: row.weixin_context_bot_generation ?? null,
            }
          : {}),
      });
      let threw = false;
      try {
        await this.executeRegisteredInboundTurn(syntheticEvent, {
          ...reresolved,
          pending_interrupt:
            reresolved.pending_interrupt ?? syntheticResolved.pending_interrupt,
          uploaded_attachments:
            reresolved.uploaded_attachments &&
            reresolved.uploaded_attachments.length > 0
              ? reresolved.uploaded_attachments
              : (row.uploaded_attachments ?? []),
        });
      } catch (err) {
        if (
          err instanceof WeixinAttachmentAbortedError &&
          err.reason === "superseded"
        ) {
          threw = true;
          await this.markWeixinInboundSuperseded(row.platform, row.message_id);
          return;
        }
        if (
          row.platform === "weixin" &&
          err instanceof BackendHttpError &&
          err.status === 409
        ) {
          const bodyObj = err.body as { code?: unknown } | null;
          const errCode = typeof bodyObj?.code === "string" ? bodyObj.code : "";
          if (errCode === "weixin_bot_generation_superseded") {
            threw = true;
            await this.markWeixinInboundSuperseded(row.platform, row.message_id);
            return;
          }
        }
        threw = true;
        throw err;
      } finally {
        // Mirror `launchInboundTurn`'s safety net: on uncaught error during startup
        // recovery, outcome is uncertain, so clear processing feedback without stamping
        // a permanent failure emoji.
        this.processingFeedback.settleMessage(
          syntheticEvent,
          threw ? "failed" : "done",
        );
      }
    }
  }

  private async recoverSingleBackgroundRun(
    bg: RecoverBackgroundRunRow,
    recoveredWatches?: RecoverThreadWatchRow[],
  ): Promise<boolean> {
    const activeWatch = this.activeWatches.get(bg.thread_id);
    const recoveredWatch = recoveredWatches?.find(
      (w) => w.thread_id === bg.thread_id,
    );
    const trimScope = (value: string | null | undefined): string | null => {
      if (typeof value !== "string") return null;
      const trimmed = value.trim();
      return trimmed.length > 0 ? trimmed : null;
    };
    const bgScopeKey =
      trimScope(bg.thread_scope_key) ??
      trimScope(bg.scope_key) ??
      trimScope(activeWatch?.scopeKey) ??
      trimScope(recoveredWatch?.thread_scope_key) ??
      trimScope(recoveredWatch?.scope_key) ??
      bg.target_address;
    const bgGeneration =
      typeof bg.session_generation === "number" && bg.session_generation > 0
        ? bg.session_generation
        : typeof bg.generation === "number" && bg.generation > 0
          ? bg.generation
          : activeWatch && activeWatch.generation > 0
            ? activeWatch.generation
            : recoveredWatch && recoveredWatch.generation > 0
              ? recoveredWatch.generation
              : 0;

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
        scopeKey: bgScopeKey,
        generation: bgGeneration > 0 ? bgGeneration : null,
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
          scopeKey: bgScopeKey,
          generation: bgGeneration,
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
          scopeKey: bgScopeKey,
          generation: bgGeneration > 0 ? bgGeneration : null,
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
          generation: w.generation,
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
    recoveryStrictDeliverableTextEnabled?: boolean,
    recoveryFailureNoticeEnabled?: boolean,
  ): Promise<void> {
    if (autoRun.status !== "finishing") return;
    await this.agentRoutes.executeAutomationRunFinish({
      executionId: autoRun.execution_id,
      userId: autoRun.user_id,
      threadId: autoRun.thread_id,
      runId: autoRun.run_id ?? null,
      initialSnapshots: autoRun.chart_snapshots,
      outboundBlocked: autoRun.outbound_blocked,
      hasPersonalIntegrationData: autoRun.has_personal_integration_data,
      automationName: autoRun.automation_name,
      finishStatus: autoRun.finish_status ?? "completed",
      finalText: autoRun.final_text,
      strictDeliverableTextEnabled:
        autoRun.strict_deliverable_text_enabled ??
        recoveryStrictDeliverableTextEnabled,
      failureNoticeEnabled:
        autoRun.failure_notice_enabled ?? recoveryFailureNoticeEnabled,
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

  private closeWatchesOlderThanStoredUnbind(
    userId: string,
    platform: ChannelPlatform,
    result: object,
  ): void {
    const raw = recordField(result, "generations");
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return;
    for (const [threadId, handle] of this.activeWatches.entries()) {
      if (handle.userId !== userId || handle.platform !== platform) continue;
      if (!Object.prototype.hasOwnProperty.call(raw, handle.scopeKey)) continue;
      const stored = Reflect.get(raw, handle.scopeKey);
      if (typeof stored !== "number" || !Number.isFinite(stored)) continue;
      if (
        typeof handle.generation !== "number" ||
        !Number.isFinite(handle.generation)
      ) {
        continue;
      }
      if (handle.generation < stored) {
        handle.abortController.abort();
        this.activeWatches.delete(threadId);
      }
    }
  }

  async stopAllWatches(): Promise<void> {
    this.shuttingDown = true;
    for (const ctrl of [...this.activeInboundUploadControllers.keys()]) {
      ctrl.abort();
    }
    this.activeInboundUploadControllers.clear();
    this.processingFeedback.settleAll("failed");
    for (const handle of this.activeWatches.values()) {
      handle.abortController.abort();
    }
    this.activeWatches.clear();
  }

  private parseTurnDeliverables(raw: unknown): TurnDeliverableItem[] {
    if (!Array.isArray(raw)) {
      return [];
    }
    const items: TurnDeliverableItem[] = [];
    for (const entry of raw) {
      if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
        continue;
      }
      const rec = entry as Record<string, unknown>;
      const wsId =
        typeof rec.workspace_id === "string" ? rec.workspace_id.trim() : "";
      const path = typeof rec.path === "string" ? rec.path.trim() : "";
      const sha256 = typeof rec.sha256 === "string" ? rec.sha256.trim() : "";
      const size =
        typeof rec.size === "number" && Number.isFinite(rec.size)
          ? rec.size
          : -1;
      if (!wsId || !path || !sha256 || size < 0) {
        continue;
      }
      const rawStamp =
        rec.stamp && typeof rec.stamp === "object" && !Array.isArray(rec.stamp)
          ? (rec.stamp as Record<string, unknown>)
          : null;
      const parsedStamp =
        rawStamp &&
        typeof rawStamp.size === "number" &&
        typeof rawStamp.mtime === "string" &&
        typeof rawStamp.ctime === "string" &&
        typeof rawStamp.inode === "string"
          ? {
              size: rawStamp.size,
              mtime: rawStamp.mtime,
              ctime: rawStamp.ctime,
              inode: rawStamp.inode,
            }
          : undefined;
      items.push({
        workspace_id: wsId,
        path,
        filename:
          typeof rec.filename === "string" && rec.filename.trim()
            ? rec.filename.trim()
            : basename(path),
        size,
        sha256,
        ...(parsedStamp ? { stamp: parsedStamp } : {}),
        ...(typeof rec.stamp_settled === "boolean"
          ? { stamp_settled: rec.stamp_settled }
          : {}),
        ...(typeof rec.operation === "string"
          ? { operation: rec.operation }
          : {}),
      });
    }
    return items;
  }

  private stripCodeFromText(text: string): string {
    if (!text) return "";
    // Remove fenced code blocks first, then inline code spans.
    return text
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/~~~[\s\S]*?~~~/g, " ")
      .replace(/`[^`\n]+`/g, " ");
  }

  /**
   * Independent DM deliverable settlement (`deliverables:${runId}`).
   * Enforces platform auto-attach caps (Feishu/Discord: 5, Weixin: 3), HTML/size
   * link-only rules, and delegates version-verified download, recipient checks,
   * and `prior_run_files` deduplication to `DeliveryService`.
   */
  private async settleDmTurnDeliverables(params: {
    userId: string;
    threadId: string | null;
    runId: string;
    platform: ChannelPlatform;
    targetAddress: string;
    inboundMessageId?: string | null;
    threadScopeKey?: string | null;
    sourceScopeKey?: string | null;
    sourceGeneration?: number | null;
    activityOrigin?: DeliveryActivityOrigin | null;
    assistantText: string;
    deliverables: TurnDeliverableItem[];
  }): Promise<boolean> {
    if (
      !params.targetAddress.endsWith(":@me") ||
      params.deliverables.length === 0
    ) {
      return true;
    }

    const deliveryKey = `deliverables:${params.runId}`;
    if (this.settledDeliverableKeys.has(deliveryKey)) {
      return true;
    }
    const existingInFlight =
      this.inFlightDeliverableSettlements.get(deliveryKey);
    if (existingInFlight) {
      return await existingInFlight;
    }

    const settlementPromise = (async (): Promise<boolean> => {
      const prose = this.stripCodeFromText(params.assistantText).toLowerCase();
      const isMentioned = (item: TurnDeliverableItem): boolean => {
        if (!prose) return false;
        const pLower = item.path.toLowerCase();
        const nameLower = (item.filename || basename(item.path)).toLowerCase();
        return (
          (pLower.length > 0 && prose.includes(pLower)) ||
          (nameLower.length > 0 && prose.includes(nameLower))
        );
      };

      const seenDeliverableKeys = new Set<string>();
      const uniqueDeliverables: TurnDeliverableItem[] = [];
      for (const item of params.deliverables) {
        const normPath = item.path.trim().replace(/^\/+/, "");
        const dedupeKey = `${item.workspace_id.trim()}::${normPath}`;
        if (!normPath || seenDeliverableKeys.has(dedupeKey)) {
          continue;
        }
        seenDeliverableKeys.add(dedupeKey);
        uniqueDeliverables.push(item);
      }
      if (uniqueDeliverables.length === 0) {
        return true;
      }

      const sorted = [
        ...uniqueDeliverables.filter((item) => isMentioned(item)),
        ...uniqueDeliverables.filter((item) => !isMentioned(item)),
      ];

      const maxAutoAttach = params.platform === "weixin" ? 3 : 5;
      const filesToAttach: Array<{ path: string; workspace_id: string | null }> =
        [];
      const linkOnlyFiles: Array<{
        path: string;
        workspace_id: string | null;
        filename: string;
        reason: string;
      }> = [];
      let plannedBytes = 0;

      for (const item of sorted) {
        const lowerPath = item.path.toLowerCase();
        const isHtml =
          lowerPath.endsWith(".html") || lowerPath.endsWith(".htm");
        const tooLargeSingle = item.size > MAX_ATTACHMENT_BYTES;
        const exceedsCap = filesToAttach.length >= maxAutoAttach;
        const exceedsTotalBytes =
          plannedBytes + item.size > MAX_TOTAL_ATTACHMENT_BYTES;

        if (isHtml || tooLargeSingle || exceedsCap || exceedsTotalBytes) {
          linkOnlyFiles.push({
            path: item.path,
            workspace_id: item.workspace_id,
            filename: item.filename || basename(item.path) || item.path,
            reason: isHtml
              ? "html_file_deep_link"
              : tooLargeSingle
                ? "deliverable_size_exceeded_link"
                : exceedsTotalBytes
                  ? "deliverable_total_size_exceeded_link"
                  : "deliverable_overflow_link",
          });
        } else {
          filesToAttach.push({
            path: item.path,
            workspace_id: item.workspace_id,
          });
          plannedBytes += item.size;
        }
      }

      const finalRes = await this.executeDeliverableFallbackWithPersonalRedirect({
        deliveryKey,
        userId: params.userId,
        threadId: params.threadId,
        workspaceId: sorted[0]?.workspace_id ?? null,
        runId: params.runId,
        platform: params.platform,
        targetAddress: params.targetAddress,
        inboundMessageId: params.inboundMessageId ?? null,
        files: filesToAttach,
        linkOnlyFiles,
        threadScopeKey: params.threadScopeKey ?? null,
        sourceScopeKey: params.sourceScopeKey ?? params.threadScopeKey ?? null,
        sourceGeneration: params.sourceGeneration ?? null,
        activityOrigin: params.activityOrigin ?? null,
      });
      if (!finalRes) {
        return false;
      }
      const settled = finalRes.status === "sent" || finalRes.status === "partial";
      if (settled) {
        if (
          this.settledDeliverableKeys.size >=
          ChannelGatewayOrchestrator.MAX_SETTLED_DELIVERABLE_KEYS
        ) {
          const oldest = this.settledDeliverableKeys.values().next().value;
          if (oldest !== undefined) {
            this.settledDeliverableKeys.delete(oldest);
          }
        }
        this.settledDeliverableKeys.add(deliveryKey);
      }
      return settled;
    })();

    this.inFlightDeliverableSettlements.set(deliveryKey, settlementPromise);
    try {
      return await settlementPromise;
    } finally {
      if (
        this.inFlightDeliverableSettlements.get(deliveryKey) ===
        settlementPromise
      ) {
        this.inFlightDeliverableSettlements.delete(deliveryKey);
      }
    }
  }

  private async executeDeliverableFallbackWithPersonalRedirect(params: {
    deliveryKey: string;
    userId: string;
    threadId: string | null;
    workspaceId: string | null;
    runId: string;
    platform: ChannelPlatform;
    targetAddress: string;
    inboundMessageId: string | null;
    files: Array<{ path: string; workspace_id: string | null }>;
    linkOnlyFiles: Array<{
      path: string;
      workspace_id: string | null;
      filename: string;
      reason: string;
    }>;
    threadScopeKey: string | null;
    sourceScopeKey: string | null;
    sourceGeneration: number | null;
    activityOrigin: DeliveryActivityOrigin | null;
  }): Promise<AgentSendResponse | null> {
    const res = await this.deliveryService.executeDelivery({
      deliveryKey: params.deliveryKey,
      kind: "fallback",
      userId: params.userId,
      threadId: params.threadId,
      workspaceId: params.workspaceId,
      runId: params.runId,
      platform: params.platform,
      target: params.targetAddress,
      inboundMessageId: params.inboundMessageId,
      text: "",
      files: params.files,
      linkOnlyFiles: params.linkOnlyFiles,
      downloadMode: "deliverable",
      linkFallbackOnDeliverableMismatch: true,
      threadScopeKey: params.threadScopeKey,
      sourceScopeKey: params.sourceScopeKey,
      sourceGeneration: params.sourceGeneration,
      activityOrigin: params.activityOrigin,
    });
    if (
      res.code === "personal_integration_private_only" &&
      !params.targetAddress.trim().toLowerCase().endsWith(":@me")
    ) {
      const dmRes = await this.deliveryService.executeDelivery({
        deliveryKey: `${params.deliveryKey}:personal_dm`,
        kind: "fallback",
        userId: params.userId,
        threadId: params.threadId,
        workspaceId: params.workspaceId,
        runId: params.runId,
        platform: params.platform,
        target: `${params.platform}:@me`,
        inboundMessageId: params.inboundMessageId,
        text: "",
        textIsAttachmentPreface: true,
        files: params.files,
        linkOnlyFiles: params.linkOnlyFiles,
        downloadMode: "deliverable",
        linkFallbackOnDeliverableMismatch: true,
        threadScopeKey: params.threadScopeKey,
        sourceScopeKey: params.sourceScopeKey,
        sourceGeneration: params.sourceGeneration,
        activityOrigin: params.activityOrigin,
      });
      const hasRequestedAttachments =
        params.files.length > 0 || params.linkOnlyFiles.length > 0;
      const anyAttachmentDelivered =
        !hasRequestedAttachments ||
        (dmRes.files ?? []).some(
          (f) => f.status === "sent" || f.status === "linked",
        );
      if (
        (dmRes.status !== "sent" && dmRes.status !== "partial") ||
        !anyAttachmentDelivered
      ) {
        return null;
      }
      await this.deliveryService.executeDelivery({
        deliveryKey: `${params.deliveryKey}:personal_dm_notice`,
        kind: "fallback",
        userId: params.userId,
        threadId: params.threadId,
        workspaceId: params.workspaceId,
        runId: params.runId,
        platform: params.platform,
        target: `${params.platform}:@me`,
        inboundMessageId: params.inboundMessageId,
        text: "本次结果含个人日历相关附件，已发送到你的私聊",
        threadScopeKey: params.threadScopeKey,
        sourceScopeKey: params.sourceScopeKey,
        sourceGeneration: params.sourceGeneration,
        activityOrigin: params.activityOrigin,
      });
      return await this.deliveryService.executeDelivery({
        deliveryKey: `fallback:${params.runId}:personal_redirect_notice`,
        kind: "fallback",
        userId: params.userId,
        threadId: params.threadId,
        workspaceId: params.workspaceId,
        runId: params.runId,
        platform: params.platform,
        target: params.targetAddress,
        inboundMessageId: params.inboundMessageId,
        text: "本次结果含个人日历内容，已发送到你的私聊",
        threadScopeKey: params.threadScopeKey,
        sourceScopeKey: params.sourceScopeKey,
        sourceGeneration: params.sourceGeneration,
        activityOrigin: params.activityOrigin,
      });
    }
    return res;
  }

  private collectArtifactChartOrWidget(
    rawPayload: Record<string, unknown>,
    chartsMap: Map<string, CollectedChartLayerRef>,
    widgetsMap: Map<string, CollectedHtmlWidgetRef>,
  ): void {
    const artifactType =
      typeof rawPayload.artifact_type === "string"
        ? rawPayload.artifact_type.trim()
        : "";
    const inner =
      rawPayload.payload &&
      typeof rawPayload.payload === "object" &&
      !Array.isArray(rawPayload.payload)
        ? (rawPayload.payload as Record<string, unknown>)
        : rawPayload;

    if (artifactType === "chart_layer") {
      const op =
        typeof inner.op === "string" ? inner.op.trim().toLowerCase() : "add";
      if (op === "remove") {
        const cid = typeof inner.chart_id === "string" ? inner.chart_id.trim() : "";
        const ids = Array.isArray(inner.ids) ? inner.ids : [];
        for (const id of ids) {
          if (typeof id === "string") {
            const lid = id.trim();
            if (!lid) continue;
            if (cid) {
              chartsMap.delete(chartLayerCompositeKey(cid, lid));
            } else {
              for (const [k, entry] of chartsMap.entries()) {
                if (entry.layerId === lid) {
                  chartsMap.delete(k);
                }
              }
            }
          }
        }
        return;
      }
      if (op === "clear") {
        const cid = typeof inner.chart_id === "string" ? inner.chart_id.trim() : "";
        if (cid) {
          for (const [k, entry] of chartsMap.entries()) {
            if (entry.chartId === cid) {
              chartsMap.delete(k);
            }
          }
        }
        return;
      }
      const layerId =
        typeof inner.layer_id === "string"
          ? inner.layer_id.trim()
          : typeof rawPayload.artifact_id === "string"
            ? rawPayload.artifact_id.trim()
            : "";
      const chartId =
        typeof inner.chart_id === "string" ? inner.chart_id.trim() : "";
      if (!layerId || !chartId) return;
      const layerObj =
        inner.layer && typeof inner.layer === "object" && !Array.isArray(inner.layer)
          ? (inner.layer as Record<string, unknown>)
          : null;
      const title =
        typeof inner.title === "string" && inner.title.trim()
          ? inner.title.trim()
          : typeof layerObj?.title === "string" && layerObj.title.trim()
            ? layerObj.title.trim()
            : chartId;
      chartsMap.set(chartLayerCompositeKey(chartId, layerId), {
        chartId,
        layerId,
        symbol: typeof inner.symbol === "string" ? inner.symbol : null,
        timeframe: typeof inner.timeframe === "string" ? inner.timeframe : null,
        title,
        workspaceId:
          typeof inner.workspace_id === "string" ? inner.workspace_id : null,
      });
      return;
    }

    if (artifactType === "html_widget") {
      const artifactId =
        typeof rawPayload.artifact_id === "string"
          ? rawPayload.artifact_id.trim()
          : typeof inner.artifact_id === "string"
            ? inner.artifact_id.trim()
            : "";
      if (!artifactId) return;
      const title =
        typeof inner.title === "string" && inner.title.trim()
          ? inner.title.trim()
          : artifactId;
      const monitorSlug =
        typeof inner.monitor_slug === "string" && inner.monitor_slug.trim()
          ? inner.monitor_slug.trim()
          : null;
      widgetsMap.set(artifactId, {
        artifactId,
        title,
        monitorSlug,
      });
    }
  }

  private async settleTurnChartsAndWidgets(params: {
    userId: string;
    threadId: string | null;
    runId: string;
    platform: ChannelPlatform;
    targetAddress: string;
    inboundMessageId?: string | null;
    threadScopeKey?: string | null;
    sourceScopeKey?: string | null;
    sourceGeneration?: number | null;
    activityOrigin?: DeliveryActivityOrigin | null;
    charts: CollectedChartLayerRef[];
    initialSnapshots?: ChartSnapshotItem[];
    widgets: CollectedHtmlWidgetRef[];
    outboundBlocked?: boolean;
  }): Promise<boolean> {
    const deliveryKey = `charts:${params.runId}`;
    if (this.settledChartKeys.has(deliveryKey)) {
      return true;
    }
    const existing = this.inFlightChartSettlements.get(deliveryKey);
    if (existing) {
      return await existing;
    }
    const task = (async (): Promise<boolean> => {
      const res = await deliverTurnChartsAndWidgets({
        backend: this.backend,
        adapters: this.adapters,
        webBaseUrl: this.webBaseUrl,
        userId: params.userId,
        threadId: params.threadId,
        runId: params.runId,
        platform: params.platform,
        targetAddress: params.targetAddress,
        inboundMessageId: params.inboundMessageId ?? null,
        threadScopeKey: params.threadScopeKey ?? null,
        sourceScopeKey: params.sourceScopeKey ?? params.threadScopeKey ?? null,
        sourceGeneration: params.sourceGeneration ?? null,
        activityOrigin: params.activityOrigin ?? "conversation",
        charts: params.charts,
        initialSnapshots: params.initialSnapshots,
        widgets: params.widgets,
        outboundBlocked: params.outboundBlocked,
        pendingReceipts: this.pendingChartReceipts,
        nowFn: this.nowFn,
      });
      if (res.settled) {
        if (
          this.settledChartKeys.size >=
          ChannelGatewayOrchestrator.MAX_SETTLED_DELIVERABLE_KEYS
        ) {
          const oldest = this.settledChartKeys.values().next().value;
          if (oldest !== undefined) {
            this.settledChartKeys.delete(oldest);
          }
        }
        this.settledChartKeys.add(deliveryKey);
      }
      return res.settled;
    })();
    this.inFlightChartSettlements.set(deliveryKey, task);
    try {
      return await task;
    } finally {
      if (this.inFlightChartSettlements.get(deliveryKey) === task) {
        this.inFlightChartSettlements.delete(deliveryKey);
      }
    }
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
    sourceScopeKey?: string | null;
    sourceGeneration?: number | null;
    activityOrigin?: DeliveryActivityOrigin | null;
    trimmedText: string;
    sentToCurrentCount: number;
    hasUnresolvedCurrentAgentSendImages?: boolean;
    unresolvedCurrentAgentSends?: UnresolvedCurrentAgentSend[];
    resolvedImageUrls?: Record<string, string>;
    resolvedImagesByMessage?: Record<string, Record<string, string>>;
    toolCallMessageIds?: Record<string, string>;
  }): Promise<boolean> {
    let needsPartialNotice = false;
<<<<<<< HEAD
    const deliverAgentSendImageCorrections = async (): Promise<boolean> => {
      if (params.hasUnresolvedCurrentAgentSendImages !== true) {
        return true;
      }
      const originals = params.unresolvedCurrentAgentSends ?? [];
      if (originals.length === 0) {
        return true;
      }
      for (let idx = 0; idx < originals.length; idx += 1) {
        const orig = originals[idx]!;
        const perDeliveryImageUrls = resolveDeliveryImageUrls({
          deliveryKey: orig.delivery_key,
          runId: params.runId,
          sourceMessageId: orig.source_message_id,
          capturedImages: orig.captured_images,
          imagesByMessage: params.resolvedImagesByMessage,
          toolCallMessageIds: params.toolCallMessageIds,
          globalImages: params.resolvedImageUrls,
        });
        const correctedText = buildImageCorrectionText({
          originalText: orig.delivered_text,
          unresolvedImagePaths: orig.unresolved_image_paths,
          resolvedImageUrls: perDeliveryImageUrls,
        });
        if (!correctedText) {
          continue;
        }
        const fixDeliveryKey =
          idx === 0
            ? `${params.fallbackDeliveryKey}:imgfix`
            : `${params.fallbackDeliveryKey}:imgfix:${idx}`;
        const fixRes = await this.deliveryService.executeDelivery({
          deliveryKey: fixDeliveryKey,
          kind: "fallback",
          userId: params.userId,
          threadId: params.threadId,
          runId: params.runId,
          platform: params.platform,
          target: params.targetAddress,
          inboundMessageId: params.inboundMessageId ?? null,
          text: correctedText,
          replyToMessageId: params.replyToMessageId ?? null,
          threadScopeKey: params.threadScopeKey ?? null,
          sourceScopeKey:
            params.sourceScopeKey ?? params.threadScopeKey ?? null,
          sourceGeneration: params.sourceGeneration ?? null,
          activityOrigin: params.activityOrigin ?? null,
        });
        if (fixRes.status !== "sent" && fixRes.status !== "partial") {
          return false;
        }
        if (fixRes.status === "partial") {
          needsPartialNotice = true;
        }
      }
      return true;
    };

    if (params.trimmedText.length === 0) {
      // Empty fallback text: never claim/deliver fallback text, but still run
      // original-bound :imgfix corrections for prior delivered agent_send messages.
      if (!(await deliverAgentSendImageCorrections())) {
        return false;
      }
    } else if (params.sentToCurrentCount === 0) {
      const deliveryRes = await this.deliveryService.executeDelivery({
=======
    if (params.sentToCurrentCount === 0) {
      let deliveryRes = await this.deliveryService.executeDelivery({
>>>>>>> refs/lane/82
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
        sourceScopeKey:
          params.sourceScopeKey ?? params.threadScopeKey ?? null,
        sourceGeneration: params.sourceGeneration ?? null,
        activityOrigin: params.activityOrigin ?? null,
      });
      if (
        deliveryRes.code === "personal_integration_private_only" &&
        !params.targetAddress.trim().toLowerCase().endsWith(":@me")
      ) {
        const dmRes = await this.deliveryService.executeDelivery({
          deliveryKey: `${params.fallbackDeliveryKey}:personal_dm`,
          kind: "fallback",
          userId: params.userId,
          threadId: params.threadId,
          runId: params.runId,
          platform: params.platform,
          target: `${params.platform}:@me`,
          inboundMessageId: params.inboundMessageId ?? null,
          text: params.trimmedText,
          threadScopeKey: params.threadScopeKey ?? null,
          sourceScopeKey:
            params.sourceScopeKey ?? params.threadScopeKey ?? null,
          sourceGeneration: params.sourceGeneration ?? null,
          activityOrigin: params.activityOrigin ?? null,
        });
        if (dmRes.status !== "sent" && dmRes.status !== "partial") {
          return false;
        }
        deliveryRes = await this.deliveryService.executeDelivery({
          deliveryKey: `${params.fallbackDeliveryKey}:personal_redirect_notice`,
          kind: "fallback",
          userId: params.userId,
          threadId: params.threadId,
          runId: params.runId,
          platform: params.platform,
          target: params.targetAddress,
          inboundMessageId: params.inboundMessageId ?? null,
          text: "本次结果含个人日历内容，已发送到你的私聊",
          replyToMessageId: params.replyToMessageId ?? null,
          threadScopeKey: params.threadScopeKey ?? null,
          sourceScopeKey:
            params.sourceScopeKey ?? params.threadScopeKey ?? null,
          sourceGeneration: params.sourceGeneration ?? null,
          activityOrigin: params.activityOrigin ?? null,
        });
      }
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
      // R6 #6 (Codex Medium #5): Check lease-loss BEFORE claim and BEFORE I-7 settlement so a late
      // `claimed` response arriving after lease loss NEVER emits I-7 `updateDeliveryProgress`.
      this.deliveryService.assertNotLeaseLost("fallback.claim");
      const claim = await this.backend.authorizeAndClaimDelivery(
        {
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
          source_scope_key:
            params.sourceScopeKey ?? params.threadScopeKey ?? null,
          source_generation: params.sourceGeneration ?? null,
          activity_origin: params.activityOrigin ?? null,
        },
        params.platform === "weixin"
          ? { signal: this.deliveryService.getRetryAbortSignal() }
          : undefined,
      );
      this.deliveryService.assertNotLeaseLost("fallback.post_claim");
      if (claim.claim_status === "already_completed") {
        const prev = (claim.first_response_json ?? {}) as {
          status?: string;
          unresolved_sandbox_images?: boolean;
          unresolved_sandbox_image_paths?: string[];
          delivered_text?: string;
        };
        const ledgerStatus = prev.status ?? claim.status ?? "sent";
        const fallbackCorrectionText =
          prev.unresolved_sandbox_images === true
            ? typeof prev.delivered_text === "string"
              ? buildImageCorrectionText({
                  originalText: prev.delivered_text,
                  unresolvedImagePaths: prev.unresolved_sandbox_image_paths,
                  resolvedImageUrls: params.resolvedImageUrls,
                })
              : !containsUnresolvedSandboxImagePath(params.trimmedText)
                ? params.trimmedText
                : null
            : null;
        if (fallbackCorrectionText) {
          // Earlier fallback delivered an unresolved sandbox image path; send the
          // corrected reply generated from the original delivered text.
          const fixRes = await this.deliveryService.executeDelivery({
            deliveryKey: `${params.fallbackDeliveryKey}:imgfix`,
            kind: "fallback",
            userId: params.userId,
            threadId: params.threadId,
            runId: params.runId,
            platform: params.platform,
            target: params.targetAddress,
            inboundMessageId: params.inboundMessageId ?? null,
            text: fallbackCorrectionText,
            replyToMessageId: params.replyToMessageId ?? null,
            threadScopeKey: params.threadScopeKey ?? null,
            sourceScopeKey:
              params.sourceScopeKey ?? params.threadScopeKey ?? null,
            sourceGeneration: params.sourceGeneration ?? null,
            activityOrigin: params.activityOrigin ?? null,
          });
          if (fixRes.status !== "sent" && fixRes.status !== "partial") {
            return false;
          }
          if (fixRes.status === "partial") {
            needsPartialNotice = true;
          }
        } else if (ledgerStatus === "partial") {
          needsPartialNotice = true;
        }
      } else if (
        claim.claim_status === "in_flight_or_unknown" ||
        claim.claim_status === "denied"
      ) {
        if (
          claim.claim_status === "denied" &&
          claim.code &&
          WEIXIN_ROTATION_DENY_CODES.has(claim.code)
        ) {
          this.notifyWeixinAccountRotation(
            claim.weixin_account_id ?? null,
            claim.code,
          );
        }
        return false;
      } else if (claim.claim_status === "claimed") {
        this.deliveryService.assertNotLeaseLost("fallback.settlement");
        await this.backend.updateDeliveryProgress(
          {
            delivery_key: params.fallbackDeliveryKey,
            status: "failed",
            sent_chunks: 0,
            total_chunks: 1,
            error_code: "already_sent_to_current",
            error_message:
              "Text already sent to current conversation via /agent/send.",
          },
          { platform: claim.platform ?? params.platform },
        );
        if (!(await deliverAgentSendImageCorrections())) {
          return false;
        }
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

  private async sendInboundNotice(
    event: InboundChannelEvent,
    userId: string | null | undefined,
    deliveryKey: string,
    text: string,
  ): Promise<boolean> {
    const targetAddress =
      event.chatType === "dm" ? `${event.platform}:@me` : event.chatAddress;
    return await this.sendNoticeDelivery({
      deliveryKey,
      userId:
        userId && userId.trim().length > 0
          ? userId
          : `unlinked:${event.platform}:${event.platformUserId}`,
      platform: event.platform,
      target: targetAddress,
      inboundMessageId: event.messageId,
      text,
      replyToMessageId: event.messageId,
      threadScopeKey: event.threadScopeKey,
    });
  }

  private async sendNoticeDeliveryOutcome(options: {
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
    sourceScopeKey?: string | null;
    sourceGeneration?: number | null;
    activityOrigin?: DeliveryActivityOrigin | null;
  }): Promise<{ ok: boolean; definitiveFailure: boolean }> {
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
      sourceScopeKey:
        options.sourceScopeKey ?? options.threadScopeKey ?? null,
      sourceGeneration: options.sourceGeneration ?? null,
      activityOrigin: options.activityOrigin ?? "none",
    });
    return {
      ok: res.status === "sent" || res.status === "partial",
      definitiveFailure: res.status === "failed",
    };
  }

  /**
   * Settles processing feedback after a non-conflict SSE `error` or terminal
   * `run_end` (`error` / `failed` / `cancelled`). When a current-conversation
   * content delivery for `currentRunId` is still in-flight, waits for its final
   * outcome before settling so a successful or uncertain delivery is never
   * stamped with `delivery_failed`. A cancelled or failed turn only clears the
   * processing marker: the user already sees the cancellation or the error
   * notice, and several bodies can still be settling at that moment, so a
   * failure mark here could land on a reply that later arrives.
   */
  private async settleTerminalTurnErrorFeedback(params: {
    event: InboundChannelEvent;
    currentRunId: string | null;
    progress: I7InboundProgressResponse | null | undefined;
    noticeOutcome: { ok: boolean; definitiveFailure: boolean };
    inFlightWaitPromise: Promise<AgentSendResponse[]> | null;
  }): Promise<void> {
    const { event, currentRunId, progress, noticeOutcome, inFlightWaitPromise } =
      params;

    if ((progress?.sent_to_current_count ?? 0) > 0) {
      this.processingFeedback.settleMessage(event, "done");
      this.processingFeedback.settleRun(currentRunId, "done");
      return;
    }

    const collectedOutcomes: AgentSendResponse[] = [];
    if (inFlightWaitPromise) {
      collectedOutcomes.push(...(await inFlightWaitPromise));
    }
    if (this.deliveryService.hasInFlightCurrentDelivery(currentRunId)) {
      collectedOutcomes.push(
        ...(await this.deliveryService.waitForInFlightCurrentDeliveries(
          currentRunId,
        )),
      );
    }

    if (collectedOutcomes.length > 0) {
      const currentOutcomes = collectedOutcomes.filter((r) => r.current);
      if (
        currentOutcomes.some(
          (r) => r.status === "sent" || r.status === "partial",
        )
      ) {
        this.processingFeedback.settleMessage(event, "done");
        this.processingFeedback.settleRun(currentRunId, "done");
        return;
      }
      if (
        currentOutcomes.some((r) => r.status === "unknown") ||
        Boolean(progress?.has_unknown_current_text)
      ) {
        this.processingFeedback.settleMessage(event, "failed");
        this.processingFeedback.settleRun(currentRunId, "failed");
        return;
      }
      // Every tracked in-flight current delivery ended in definitive failure (or was non-current).
      const failOutcome = "failed";
      this.processingFeedback.settleMessage(event, failOutcome);
      this.processingFeedback.settleRunPrimary(currentRunId, failOutcome);
      this.processingFeedback.settleRun(currentRunId, "failed");
      return;
    }

    if (progress?.has_in_flight_or_unknown_current_text) {
      this.processingFeedback.settleMessage(event, "failed");
      this.processingFeedback.settleRun(currentRunId, "failed");
      return;
    }

    const failOutcome = "failed";
    this.processingFeedback.settleMessage(event, failOutcome);
    // Do not stamp permanent failure emoji on steered follow-ups whose reconcileSteer
    // may still resubmit them or send their own notice.
    this.processingFeedback.settleRunPrimary(currentRunId, failOutcome);
    this.processingFeedback.settleRun(currentRunId, "failed");
  }

  private async deliverPersonalFirstTouchNotice(params: {
    userId: string;
    threadId: string | null;
    runId?: string | null;
    platform: ChannelPlatform;
    chatType?: string | null;
    targetAddress: string;
    inboundMessageId?: string | null;
    threadScopeKey?: string | null;
    sourceGeneration?: number | null;
    message: string;
  }): Promise<boolean> {
    const threadId = (params.threadId ?? "").trim();
    const text = params.message.trim();
    if (!threadId || !text) {
      return false;
    }
    // Deliver ONLY in the owner's verified 1:1 DM (never in groups/channels).
    const isDm =
      params.chatType === "dm" && isDmTargetAddress(params.targetAddress);
    if (!isDm) {
      return false;
    }
    if (this.deliveredPersonalFirstTouchThreads.has(threadId)) {
      return true;
    }
    const ok = await this.sendNoticeDelivery({
      deliveryKey: `notice:personal_first_touch:${threadId}`,
      userId: params.userId,
      threadId,
      runId: params.runId ?? null,
      platform: params.platform,
      target: `${params.platform}:@me`,
      inboundMessageId: params.inboundMessageId ?? null,
      text,
      threadScopeKey: params.threadScopeKey ?? null,
      sourceScopeKey: params.threadScopeKey ?? null,
      sourceGeneration: params.sourceGeneration ?? null,
      activityOrigin: "conversation",
    });
    if (ok) {
      if (
        this.deliveredPersonalFirstTouchThreads.size >=
        ChannelGatewayOrchestrator.MAX_SETTLED_DELIVERABLE_KEYS
      ) {
        const oldest = this.deliveredPersonalFirstTouchThreads.values().next().value;
        if (oldest !== undefined) {
          this.deliveredPersonalFirstTouchThreads.delete(oldest);
        }
      }
      this.deliveredPersonalFirstTouchThreads.add(threadId);
    }
    return ok;
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
    sourceScopeKey?: string | null;
    sourceGeneration?: number | null;
    activityOrigin?: DeliveryActivityOrigin | null;
  }): Promise<boolean> {
    const outcome = await this.sendNoticeDeliveryOutcome(options);
    return outcome.ok;
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
        inboundMessageId: event.messageId,
      });
    }
  }

  private formatDenyNotice(
    platform: ChannelPlatform,
    denyReason: string,
  ): string {
    switch (denyReason) {
      case "not_linked":
        return notLinkedBindHelp(this.webBaseUrl, platform);
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
