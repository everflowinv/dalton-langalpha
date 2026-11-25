import {
  readStreamWithLimit,
  SafeFetchError,
} from "./shared/safe-fetch.js";
import type { SessionCommandTargetKind } from "./commands.js";
import type { WeixinPersistedAttachmentMeta } from "./channels/weixin/types.js";
import { StaleGatewayEpochError } from "./gateway-lease.js";
import type {
  DataTaskAuthorizePlanResponse,
  DataTaskDownloadedArtifact,
  DataTaskNotificationRequest,
} from "./data-task-notification-types.js";
import {
  type AutomationRunTargetRecord,
  type ChannelPlatform,
  type ChatContentItem,
  MAX_ATTACHMENT_BYTES,
  type OneTimeWeixinReplyEnvelope,
  type PendingInterruptState,
  type UploadedAttachmentRecord,
} from "./types.js";

export type { SessionCommandTargetKind };

export type DeliveryActivityOrigin =
  | "conversation"
  | "replay"
  | "notification"
  | "command"
  | "none";

// ---------------------------------------------------------------------------
// SSE Stream Parser
// ---------------------------------------------------------------------------

export interface SseEvent {
  event: string;
  data: string;
  id?: string;
  rawComment?: string;
}

/**
 * Parse a `text/event-stream` byte stream into discrete SSE events and `: keepalive` comments.
 */
export async function* parseSseStream(
  body: ReadableStream<Uint8Array> | AsyncIterable<Uint8Array>,
): AsyncGenerator<SseEvent, void, unknown> {
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  const processBlock = (block: string): SseEvent | null => {
    const lines = block.split(/\r?\n/);
    let eventName = "message";
    const dataLines: string[] = [];
    let eventId: string | undefined;
    const comments: string[] = [];

    for (const line of lines) {
      if (!line) continue;
      if (line.startsWith(":")) {
        comments.push(line.slice(1).trim());
        continue;
      }
      const colonIdx = line.indexOf(":");
      const field = colonIdx === -1 ? line : line.slice(0, colonIdx);
      let value = colonIdx === -1 ? "" : line.slice(colonIdx + 1);
      if (value.startsWith(" ")) {
        value = value.slice(1);
      }
      if (field === "event") {
        eventName = value;
      } else if (field === "data") {
        dataLines.push(value);
      } else if (field === "id") {
        eventId = value;
      }
    }

    if (dataLines.length === 0 && comments.length > 0) {
      return {
        event: ":comment",
        data: "",
        rawComment: comments.join("\n"),
      };
    }
    if (dataLines.length === 0 && eventName === "message" && eventId === undefined) {
      return null;
    }
    return {
      event: eventName,
      data: dataLines.join("\n"),
      ...(eventId !== undefined ? { id: eventId } : {}),
    };
  };

  for await (const chunk of body as AsyncIterable<Uint8Array>) {
    buffer += decoder.decode(chunk, { stream: true });
    let match: RegExpExecArray | null;
    while ((match = /\r?\n\r?\n/.exec(buffer)) !== null) {
      const block = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);
      const parsed = processBlock(block);
      if (parsed) {
        yield parsed;
      }
    }
  }
  buffer += decoder.decode();
  if (buffer.trim().length > 0) {
    const parsed = processBlock(buffer);
    if (parsed) {
      yield parsed;
    }
  }
}

// ---------------------------------------------------------------------------
// I-1 ~ I-9 Backend Internal Persistence Shapes (`PROTOCOL.md` §9)
// ---------------------------------------------------------------------------

export interface I1RegisterAndResolveRequest {
  platform: ChannelPlatform;
  platform_user_id: string;
  chat_type: "dm" | "group";
  chat_address: string;
  thread_scope_key: string;
  message_id: string;
  request_key: string;
  raw_payload?: Record<string, unknown>;
  weixin_account_id?: string | null;
  weixin_context_token?: string | null;
  weixin_bot_id?: string | null;
  weixin_bot_generation?: number | null;
  weixin_context_seq?: number | null;
  weixin_media_meta?: WeixinPersistedAttachmentMeta[] | null;
  unsupported_item_types?: number[] | null;
}

export interface I1RegisterAndResolveResponse {
  existing_status:
    | "pending"
    | "accepted"
    | "streaming"
    | "received"
    | "dispatched"
    | "steered"
    | "pending_resubmit"
    | "returned_unresubmitted"
    | "completed"
    | "failed"
    | "ignored"
    | null;
  linked: boolean;
  allowed: boolean;
  deny_reason?:
    | "not_linked"
    | "external_member_group_forbidden"
    | "workspace_write_denied"
    | "colleague_dispatch_disabled"
    | "workspace_not_found"
    | "weixin_group_unsupported"
    | "unsupported_merged_forward"
    | "ilink_user_conflict_auto_unbound"
    | "conflict_auto_unbound"
    | "invalid_inbound_event"
    | string
    | null;
  dalton_user_id: string | null;
  user_tier?: string | null;
  request_key: string;
  retry_request_key: string | null;
  resubmitted_request_key: string | null;
  steered_into_run_id: string | null;
  thread_id: string | null;
  run_id: string | null;
  turn_index: number | null;
  workspace_id: string | null;
  workspace_status?: string | null;
  workspace_owner_id?: string | null;
  external_thread_id: string | null;
  generation: number;
  fallback_pending: boolean;
  sent_to_current_count: number;
  uploaded_attachments: UploadedAttachmentRecord[];
  raw_payload: Record<string, unknown>;
  pending_interrupt: PendingInterruptState | null;
  consumed_bind_result?: Record<string, unknown> | null;
  /** Present when this message already has a stored bind or session command. */
  command_request?: Record<string, unknown> | null;
  /** Stored bind/unbind or session result. Old bind rows have `ok` and no `delivery_key`. */
  command_result?: Record<string, unknown> | null;
  /** False means the confirmation was already accepted and must not be sent again. */
  command_reply_pending?: boolean;
  /** Whether a usable context_token exists for sending a hint (e.g. merged-forward unsupported). */
  has_usable_weixin_context_token?: boolean;
  /** D16 one-time Weixin reply envelope (e.g. conflict auto-unbind). */
  one_time_weixin_reply?: OneTimeWeixinReplyEnvelope | null;
  weixin_account_id?: string | null;
  weixin_context_token_version?: number | null;
}

export interface I2BindCodeConsumeRequest {
  action: "bind" | "unbind";
  platform: ChannelPlatform;
  platform_user_id: string;
  platform_username?: string | null;
  platform_tenant_key?: string | null;
  weixin_account_id?: string | null;
  weixin_context_token?: string | null;
  weixin_bot_id?: string | null;
  weixin_bot_generation?: number | null;
  weixin_context_seq?: number | null;
  inbound_message_id?: string | null;
  code?: string | null;
  is_group: boolean;
  user_id?: string | null;
  message_id?: string | null;
  chat_type?: "dm" | "group" | string | null;
  chat_address?: string | null;
  scope_key?: string | null;
  request_key?: string | null;
}

export interface I2BindCodeConsumeResponse {
  ok: boolean;
  idempotent?: boolean;
  reason?:
    | "revoked_in_group"
    | "invalid_or_expired"
    | "platform_mismatch"
    | "not_linked"
    | string;
  expected_platform?: string;
  user_id?: string;
  platform?: string;
  platform_user_id?: string;
  generation?: number;
  generations?: Record<string, number>;
  action?: "bind" | "unbind" | string;
  delivery_key?: string;
  duplicate?: boolean;
  command_effect_status?: string | null;
  command_reply_pending?: boolean;
  accounts_revision?: number | null;
  one_time_weixin_reply?: OneTimeWeixinReplyEnvelope | null;
}

export interface I3SessionCommandRequest {
  cmd:
    | "workspace_list"
    | "workspace_switch"
    | "new"
    | "stop";
  platform: ChannelPlatform;
  dalton_user_id?: string | null;
  platform_user_id?: string | null;
  chat_type: "dm" | "group";
  chat_address?: string;
  chat_id?: string;
  chat_name?: string | null;
  scope_key: string;
  workspace_id?: string | null;
  workspace_query?: string | null;
  target_kind?: SessionCommandTargetKind | null;
  command_arg?: string | null;
  external_thread_id?: string | null;
  message_id?: string | null;
  request_key?: string | null;
  raw_payload?: Record<string, unknown> | null;
  weixin_account_id?: string | null;
  weixin_context_token?: string | null;
  weixin_bot_id?: string | null;
  weixin_bot_generation?: number | null;
  weixin_context_source?: "protocol" | "platform_time" | "receive" | string | null;
  weixin_context_seq?: number | null;
  is_internal_query?: boolean;
  native_interaction?: boolean;
  native_ack_status?: "acknowledged" | "failed" | "unknown" | null;
  native_reply_status?:
    | "pending"
    | "sent"
    | "failed"
    | "expired"
    | "abandoned_on_restart"
    | "not_applicable"
    | null;
}

export interface I3SessionCommandResponse {
  ok: boolean;
  linked?: boolean;
  allowed?: boolean;
  dalton_user_id?: string | null;
  user_tier?: string | null;
  duplicate?: boolean;
  is_home?: boolean;
  not_executed?: boolean;
  native_interaction?: boolean;
  native_ack_status?: "acknowledged" | "failed" | "unknown" | string | null;
  native_reply_status?:
    | "pending"
    | "sent"
    | "failed"
    | "expired"
    | "abandoned_on_restart"
    | "not_applicable"
    | string
    | null;
  deny_reason?:
    | "not_linked"
    | "external_member_group_forbidden"
    | "workspace_not_found"
    | "workspace_ambiguous"
    | "workspace_write_denied"
    | "colleague_dispatch_disabled"
    | "interaction_ack_unknown"
    | "interaction_ack_failed"
    | "ilink_user_conflict_auto_unbound"
    | "conflict_auto_unbound"
    | string;
  candidates?: Array<{ workspace_id: string; name: string }>;
  current_workspace_id?: string | null;
  current_workspace_name?: string | null;
  home_workspace_id?: string | null;
  workspaces?: Array<{ workspace_id: string; name: string }>;
  workspace_id?: string | null;
  workspace_name?: string | null;
  override_workspace_id?: string | null;
  bound_workspace_id?: string | null;
  generation?: number;
  external_thread_id?: string | null;
  thread_id?: string | null;
  run_id?: string | null;
  command_effect_status?: string | null;
  command_reply_pending?: boolean;
  /** HTTP status from a permanent `/stop` cancel denial, kept with the command result. */
  stop_cancel_status?: number | null;
  switched?: boolean;
  /** Optional Weixin help/command reply fields. */
  reply_text?: string | null;
  message?: string | null;
  help_text?: string | null;
  one_time_weixin_reply?: OneTimeWeixinReplyEnvelope | null;
}

/** Stored I-2 bind/unbind result. `user_id` may remain after the platform identity is removed. */
export interface StoredBindingCommandResult {
  ok: boolean;
  reason?: string | null;
  expected_platform?: string | null;
  user_id?: string | null;
  platform?: string | null;
  platform_user_id?: string | null;
  duplicate?: boolean;
  command_effect_status?: string | null;
  command_reply_pending?: boolean;
  generation?: number | null;
  generations?: Record<string, number> | null;
}

export interface I4StateResponse {
  user_id: string;
  user_tier: string;
  home_workspace_id: string;
  identities: Record<
    string,
    {
      platform_user_id: string;
      platform_username: string | null;
      platform_tenant_key: string | null;
      weixin_context_token: string | null;
      created_at?: string | null;
      updated_at?: string | null;
    }
  >;
  settings: {
    version: string;
    apps: Record<string, unknown>;
  };
  chat_bindings: Array<{
    platform: string;
    chat_address: string;
    chat_name: string | null;
    workspace_id: string | null;
    bound_by_user_id: string | null;
    last_used_at: number | null;
  }>;
  current_conversation: {
    address: string;
    platform: string;
    thread_id: string | null;
    run_id: string | null;
    external_thread_id: string | null;
    thread_scope_key?: string | null;
  } | null;
  resolved_thread_id?: string | null;
}

export interface I5SettingsPutRequest {
  user_id?: string;
  version?: string;
  settings?: Record<string, unknown>;
  automation_output_update?: {
    workspace_id: string;
    platform: string;
    address: string | null;
  };
}

export interface I5SettingsPutResult {
  status: number;
  data: {
    version?: string;
    settings?: Record<string, unknown>;
    changes?: string[];
    address?: string | null;
    name?: string | null;
    code?: string;
    message?: string;
    detail?: string;
    problems?: Array<{ field: string; message: string }>;
    applied?: string[];
  };
}

export interface I6AuthorizeAndClaimRequest {
  delivery_key: string;
  kind:
    | "agent_send"
    | "fallback"
    | "interrupt"
    | "automation"
    | "notice"
    | "data_task"
    | "automation_notify";
  user_id?: string | null;
  thread_id?: string | null;
  run_id?: string | null;
  execution_id?: string | null;
  automation_execution_id?: string | null;
  platform?: string | null;
  target?: string | null;
  inbound_message_id?: string | null;
  has_non_empty_text: boolean;
  total_chunks: number;
  source_scope_key?: string | null;
  source_generation?: number | null;
  activity_origin?: DeliveryActivityOrigin | null;
}

export interface I6AuthorizeAndClaimResponse {
  claim_status:
    | "already_completed"
    | "in_flight_or_unknown"
    | "denied"
    | "claimed";
  status?: "sent" | "partial" | "failed" | "unknown";
  code?:
    | "not_linked"
    | "not_allowed"
    | "no_current_conversation"
    | "unknown_delivery_blocks_fallback"
    | "in_flight"
    | "roster_lookup_error"
    | string
    | null;
  message?: string;
  retryable?: boolean;
  first_response_json?: Record<string, unknown> | null;
  /** Platform recipient identifier (`platform_user_id` for DM, `chat_id` for group). */
  recipient_id?: string;
  platform?: string | null;
  target_address?: string | null;
  /**
   * Discord thread address confirmed before the first message.
   * Null when this delivery has not confirmed a thread.
   * Distinct from `target_address`, which stays the original parent.
   */
  confirmed_thread_address?: string | null;
  /**
   * `before_body` means no Discord body request has been authorized.
   * `dispatching` means a body request may already have reached Discord.
   * Null does not prove the body is unsent.
   */
  discord_body_phase?: "before_body" | "dispatching" | null;
  /**
   * Claim generation returned by I-6.
   * A claimed response is the generation this attempt must send back.
   * A blocked or completed response is the generation already stored.
   */
  delivery_attempt?: number;
  thread_scope_key?: string | null;
  is_current?: boolean;
  home_workspace_id?: string;
  weixin_account_id?: string | null;
  weixin_bot_id?: string | null;
  weixin_bot_generation?: number | null;
  weixin_context_token?: string | null;
  weixin_context_token_version?: number | null;
  source_scope_key?: string | null;
  source_generation?: number | null;
  activity_origin?: string | null;
}

export interface I7DeliveriesProgressRequest {
  delivery_key: string;
  sent_chunks?: number;
  total_chunks?: number;
  platform_message_ids?: string[];
  files_status?: Array<{
    path: string;
    status: "sent" | "linked" | "failed" | "unknown";
    reason: string | null;
  }>;
  first_response_json?: Record<string, unknown>;
  status?: "in_flight" | "sent" | "partial" | "failed" | "unknown";
  outcome?: "sent" | "partial" | "failed" | "unknown" | null;
  error_code?: string | null;
  /** Optional supplementary failure detail (e.g. `"preflight_deadline_exceeded"` while `error_code` preserves the root retryable cause). */
  error_detail?: string | null;
  error_message?: string | null;
  /** Full Discord thread address. Omitted unless this call confirms a thread. */
  confirmed_thread_address?: string | null;
  /**
   * Forward-only Discord body phase. Omitted when this call does not set one.
   * `before_body` authorizes no message POST. `dispatching` does.
   */
  discord_body_phase?: "before_body" | "dispatching" | null;
  /**
   * Claim generation from I-6. Omitted by callers that have not adopted it.
   * An older generation does not change the delivery.
   */
  delivery_attempt?: number;
}

export interface I7DeliveriesProgressResponse {
  ok: boolean;
  delivery_key: string;
  status: string;
  outcome?: string | null;
  sent_chunks?: number;
  total_chunks?: number;
  platform_message_ids?: string[];
  files_status?: Array<{
    path: string;
    status: "sent" | "linked" | "failed" | "unknown";
    reason: string | null;
  }>;
  sent_to_current_count: number;
  /** Echo of the stored Discord thread address, or null when none is stored. */
  confirmed_thread_address?: string | null;
  /** Echo of the stored Discord body phase, or null when none is stored. */
  discord_body_phase?: "before_body" | "dispatching" | null;
  /** Echo of the stored claim generation. Progress does not increment it. */
  delivery_attempt?: number;
}

export type InboundEventStatus =
  | "pending"
  | "accepted"
  | "streaming"
  | "received"
  | "dispatched"
  | "steered"
  | "pending_resubmit"
  | "returned_unresubmitted"
  | "completed"
  | "failed"
  | "ignored";

export interface I7InboundProgressRequest {
  platform?: string;
  message_id?: string;
  status?: InboundEventStatus;
  thread_id?: string | null;
  run_id?: string | null;
  turn_index?: number | null;
  request_key?: string | null;
  retry_request_key?: string | null;
  resubmitted_request_key?: string | null;
  steered_into_run_id?: string | null;
  fallback_pending?: boolean;
  command_effect_status?: string | null;
  command_reply_pending?: boolean;
  /** Replaces the stored command result. A `/stop` cancel denial is recorded here. */
  command_result?: I3SessionCommandResponse | null;
  uploaded_attachments?: UploadedAttachmentRecord[];
  raw_payload?: Record<string, unknown>;
  raw_payload_patch?: Record<string, unknown>;
  session_interrupt_update?: {
    platform: string;
    scope_key: string;
    dalton_user_id: string;
    generation?: number | null;
    thread_id?: string | null;
    run_id?: string | null;
    interrupt_id?: string | null;
    pending_interrupt: PendingInterruptState | null;
  };
  background_run?: {
    run_id: string;
    thread_id: string;
    dalton_user_id: string;
    platform: string;
    target_address: string;
    turn_index?: number | null;
    status?: "streaming" | "running" | "completed" | "failed";
    fallback_pending?: boolean;
  };
  thread_watch?: {
    thread_id: string;
    dalton_user_id: string;
    platform: string;
    target_address: string;
    scope_key: string;
    generation?: number;
    last_seen_turn_index?: number;
    watch_active?: boolean;
    pending_interrupt?: PendingInterruptState | null;
  };
}

export interface I7InboundProgressResponse {
  ok: boolean;
  run_id: string | null;
  has_in_flight_current_text: boolean;
  has_unknown_current_text: boolean;
  has_in_flight_or_unknown_current_text: boolean;
  sent_to_current_count: number;
  session_is_current_for_interrupt?: boolean | null;
}

export interface I7SteersReconcileRequest {
  platform: string;
  message_id: string;
  run_id?: string | null;
}

export interface I7SteersReconcileResponse {
  ok: boolean;
  reconcile_status:
    | "consumed"
    | "pending_resubmit"
    | "returned_unresubmitted"
    | "still_pending"
    | "not_found";
  outcome: string | null;
  run_status: string | null;
  inbound_status: string;
  resubmitted_request_key: string | null;
}

export interface RecoverInboundEventRow {
  platform: ChannelPlatform;
  message_id: string;
  platform_user_id?: string;
  dalton_user_id: string | null;
  chat_type: "dm" | "group";
  chat_address: string;
  thread_scope_key: string;
  request_key: string;
  retry_request_key: string | null;
  resubmitted_request_key: string | null;
  steered_into_run_id: string | null;
  thread_id: string | null;
  run_id: string | null;
  turn_index: number | null;
  workspace_id: string | null;
  external_thread_id: string | null;
  generation?: number;
  session_generation?: number | null;
  event_kind?: "message" | "command" | string;
  command_request?: Record<string, unknown> | null;
  command_result?:
    | (I3SessionCommandResponse & Partial<StoredBindingCommandResult>)
    | StoredBindingCommandResult
    | null;
  command_effect_status?: string | null;
  command_reply_pending?: boolean;
  consumed_bind_result?: Record<string, unknown> | null;
  pending_interrupt_snapshot?: PendingInterruptState | null;
  status: InboundEventStatus;
  fallback_pending: boolean;
  sent_to_current_count: number;
  uploaded_attachments: UploadedAttachmentRecord[];
  raw_payload: Record<string, unknown>;
  /** Backend-decrypted Weixin attachment metadata (takes precedence over `raw_payload` during recovery). */
  decrypted_weixin_attachments?: WeixinPersistedAttachmentMeta[] | null;
  weixin_account_id?: string | null;
  weixin_context_bot_id?: string | null;
  weixin_context_bot_generation?: number | null;
  pending_interrupt?: PendingInterruptState | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface RecoverBackgroundRunRow {
  run_id: string;
  thread_id: string;
  dalton_user_id: string;
  platform: ChannelPlatform;
  target_address: string;
  scope_key?: string | null;
  thread_scope_key?: string | null;
  generation?: number | null;
  session_generation?: number | null;
  turn_index: number | null;
  status: "streaming" | "running" | "completed" | "failed";
  fallback_pending: boolean;
  sent_to_current_count: number;
}

export interface RecoverThreadWatchRow {
  thread_id: string;
  dalton_user_id: string;
  platform: ChannelPlatform;
  target_address: string;
  scope_key: string;
  thread_scope_key?: string | null;
  generation: number;
  last_seen_turn_index: number;
  watch_active: boolean;
  pending_interrupt?: PendingInterruptState | null;
}

export interface RecoverAutomationRunRow {
  execution_id: string;
  user_id: string;
  workspace_id: string | null;
  thread_id: string | null;
  automation_name: string;
  entries: string[];
  status: "registered" | "finishing" | "finished";
  finish_status:
    | "completed"
    | "failed"
    | "stopped"
    | "interrupted"
    | "cancelled"
    | null;
  final_text: string | null;
  targets: AutomationRunTargetRecord[];
}

export interface I7RecoverResponse {
  mode: "startup" | "watch_reconnect";
  inbound_events?: RecoverInboundEventRow[];
  background_runs: RecoverBackgroundRunRow[];
  thread_watches: RecoverThreadWatchRow[];
  automation_runs?: RecoverAutomationRunRow[];
}

export interface I8AutomationRunStartRequest {
  execution_id: string;
  user_id: string;
  workspace_id?: string | null;
  thread_id?: string | null;
  automation_name: string;
  entries: string[];
}

export interface I8AutomationRunStartResponse {
  targets: AutomationRunTargetRecord[];
}

export interface I8AutomationRunFinishRequest {
  status?: string | null;
  thread_id?: string | null;
  final_text?: string | null;
  targets?: AutomationRunTargetRecord[] | null;
  mark_finished?: boolean;
}

export interface I8AutomationRunFinishResponse {
  execution_id: string;
  status: "registered" | "finishing" | "finished";
  previous_status: "registered" | "finishing" | "finished";
  user_id: string;
  workspace_id: string | null;
  thread_id: string | null;
  automation_name: string;
  finish_status: string | null;
  final_text: string | null;
  targets: AutomationRunTargetRecord[];
}

// ---------------------------------------------------------------------------
// Gateway Lease & Weixin Multi-Account Internal Contracts (§3.1 ~ §3.5, §4.4)
// ---------------------------------------------------------------------------

export interface GatewayLeaseAcquireRequest {
  instance_id: string;
  ttl_seconds: number;
}

export interface GatewayLeaseAcquireResponse {
  granted: boolean;
  epoch: number;
  expires_at: string;
  retry_after_ms?: number;
}

export interface GatewayLeaseHeartbeatRequest {
  instance_id: string;
  epoch: number;
  ttl_seconds: number;
}

export interface GatewayLeaseHeartbeatResponse {
  ok: boolean;
  epoch: number;
  expires_at: string;
}

export interface GatewayLeaseReleaseRequest {
  instance_id: string;
  epoch: number;
}

export interface GatewayLeaseReleaseResponse {
  ok: boolean;
  released: boolean;
}

export interface WeixinAccountSnapshot {
  account_id: string;
  user_id: string;
  ilink_bot_id: string;
  ilink_user_id: string | null;
  prev_ilink_user_id?: string | null;
  ilink_username?: string | null;
  bot_token: string;
  base_url: string;
  cdn_base_url?: string | null;
  get_updates_buf: string;
  status: "active" | "pending_verification" | "expired" | "unbound" | string;
  bot_generation: number;
  last_inbound_at?: string | null;
  updated_at?: string | null;
}

export interface WeixinAccountsReconcileResponse {
  changed: boolean;
  enabled: boolean;
  allow_external?: boolean;
  max_concurrent_accounts: number;
  accounts_revision: number;
  accounts: WeixinAccountSnapshot[];
}

export interface WeixinAccountDetailResponse {
  account_id: string;
  user_id: string;
  ilink_bot_id: string;
  ilink_user_id: string | null;
  ilink_username?: string | null;
  bot_token: string;
  base_url: string;
  cdn_base_url?: string | null;
  bot_generation: number;
  status: string;
  get_updates_buf?: string | null;
  weixin_context_token?: string | null;
  weixin_context_token_version?: number | null;
}

export interface WeixinAccountCursorUpdateRequest {
  expected_bot_generation: number;
  expected_ilink_bot_id: string;
  expected_get_updates_buf: string;
  get_updates_buf: string;
}

export interface WeixinAccountCursorUpdateResponse {
  ok: boolean;
  updated: boolean;
  stale_generation?: boolean;
  cas_mismatch?: boolean;
  bot_generation?: number;
  get_updates_buf?: string;
  status?: string;
}

export interface WeixinAccountStatusUpdateRequest {
  action: "mark_session_expired" | "mark_context_token_stale" | "reset_corrupt_cursor";
  expected_bot_generation: number;
  expected_ilink_bot_id: string;
  error_code?: string | null;
  recipient_platform_user_id?: string | null;
  expected_token_version?: number | null;
}

export interface WeixinAccountStatusUpdateResponse {
  ok: boolean;
  updated: boolean;
  stale_generation?: boolean;
  version_mismatch?: boolean;
  accounts_revision?: number | null;
}

export interface WeixinLoginSessionGetResponse {
  session_key: string;
  user_id: string;
  target_account_id?: string | null;
  login_version: number;
  expected_bot_generation: number;
  status: string;
  account_id?: string | null;
  account_status?: string | null;
  ilink_bot_id?: string | null;
  ilink_user_id?: string | null;
  ilink_username?: string | null;
  bot_generation?: number | null;
  accounts_revision?: number | null;
  qrcode?: string | null;
  qrcode_img_content?: string | null;
  redirect_base_url?: string | null;
  expires_at?: string | null;
  connected?: boolean;
  already_connected?: boolean;
  confirmed_result?: Record<string, unknown> | null;
}

export interface WeixinLoginSessionPatchRequest {
  action: "save_qr" | "update" | "confirm";
  expected_login_version?: number | null;
  expected_bot_generation?: number | null;
  status?: string | null;
  qrcode?: string | null;
  qrcode_img_content?: string | null;
  redirect_base_url?: string | null;
  ilink_bot_id?: string | null;
  bot_token?: string | null;
  base_url?: string | null;
  cdn_base_url?: string | null;
  ilink_user_id?: string | null;
  ilink_username?: string | null;
}

export interface WeixinLoginSessionPatchResponse {
  ok: boolean;
  revoked?: boolean;
  session_key?: string;
  status?: string;
  account_status?: string | null;
  login_version?: number;
  expires_at?: string | null;
  redirect_base_url?: string | null;
  ilink_bot_id?: string | null;
  ilink_user_id?: string | null;
  ilink_username?: string | null;
  account_id?: string | null;
  connected?: boolean;
  already_connected?: boolean;
  bot_generation?: number | null;
  accounts_revision?: number | null;
  confirmed_result?: Record<string, unknown> | null;
  code?: string | null;
  message?: string | null;
}

export interface WeixinPreflightSendRequest {
  delivery_attempt: number;
  chunk_index: number;
}

export interface WeixinPreflightAuthorizedData {
  authorized: true;
  decision: "allow";
  retryable: false;
  chunk_index?: number;
  account_id: string;
  ilink_bot_id: string;
  bot_generation: number;
  bot_token: string;
  base_url: string;
  cdn_base_url?: string | null;
  recipient_id: string;
  weixin_context_token: string;
  weixin_context_token_version?: number | null;
}

export interface WeixinPreflightDeniedData {
  authorized: false;
  decision: "defer" | "deny";
  retryable: boolean;
  deny_code: string;
  message?: string | null;
}

export type WeixinPreflightSendResponse =
  | WeixinPreflightAuthorizedData
  | WeixinPreflightDeniedData;

// ---------------------------------------------------------------------------
// Upstream Conversation & Workspace Shapes (`PROTOCOL.md` §1 ~ §8)
// ---------------------------------------------------------------------------

/**
 * Matches backend `ChatRequest` (`src/server/models/chat.py:423-815`).
 * Note: `platform` is the authoritative field read by `src/server/app/threads/messaging.py:238`.
 */
export interface ThreadMessageDispatchPayload {
  messages: Array<{
    role: "user";
    content: string | ChatContentItem[];
  }>;
  request_key?: string;
  workspace_id?: string | null;
  external_thread_id?: string | null;
  platform?: string | null;
  turn_platform?: string | null;
  surface_rules?: string | null;
  steer_only?: boolean;
  hitl_response?: Record<string, { decisions: Array<Record<string, unknown>> }>;
}

/**
 * Matches backend `RetryRequest` (`src/server/models/workflow.py:459-496`).
 * `workspace_id` is required on `/retry`.
 */
export interface ThreadRetryPayload {
  workspace_id: string;
  request_key: string;
  run_id?: string | null;
  checkpoint_id?: string | null;
  platform?: string | null;
  turn_platform?: string | null;
  surface_rules?: string | null;
}

export interface StreamThreadMessagesOptions {
  runId?: string | null;
  lastEventId?: string | number | null;
}

export interface ReplayThreadMessagesOptions {
  beforeTurn?: number;
  limit?: number;
  source?: "auto" | "checkpoint";
}

export interface SseOrErrorResponse {
  status: number;
  isSse: boolean;
  events?: AsyncIterable<SseEvent>;
  errorBody?: Record<string, unknown>;
}

export interface DispatchLivenessRunInfo {
  run_id?: string;
  status: string;
  thread_id?: string | null;
  turn_index?: number | null;
  run_started_at?: string | null;
  run_finished_at?: string | null;
  error?: string | null;
}

export interface OwedThreadItem {
  thread_id?: string;
  workspace_id?: string | null;
  workspace_name?: string | null;
  title?: string | null;
  status?: string | null;
  run_id?: string | null;
}

export interface ThreadStatusSliceBucket {
  count?: number;
  active_count?: number;
  running_count?: number;
  items?: unknown[];
}

/**
 * Matches `GET /api/v1/threads/{id}/status` and `?fields=report_back`
 * (`src/server/app/threads/messaging.py:1341-1444`, `src/server/services/thread_status.py:284-302`,
 * `src/server/services/report_back/handoff/status.py:60-107`, `src/tools/thread_status/status_slice.py`).
 */
export interface ThreadStatusInfo {
  thread_id?: string;
  status?: string;
  run_id?: string | null;
  turn_index?: number | null;
  latest_turn_index?: number | null;
  progress?: {
    has_plan?: boolean;
    has_final_report?: boolean;
    completed?: boolean;
    checkpoint_id?: string | null;
  } | null;
  pending_report_back?: boolean | null;
  report_back_run_id?: string | null;
  recent_report_back_run_ids?: string[];
  owed_threads?: OwedThreadItem[] | null;
  owed_report_kinds?: string[] | null;
  active_tasks?: unknown[];
  active_commands?: unknown[];
  pending_steers?: unknown[];
  subagents?: ThreadStatusSliceBucket;
  commands?: ThreadStatusSliceBucket;
  analysts?: ThreadStatusSliceBucket;
  automations?: ThreadStatusSliceBucket & { running?: unknown[] };
  wakeups?: { armed_count?: number; items?: unknown[] };
  // Legacy / test convenience aliases
  active_subagents?: unknown[];
  running_commands?: unknown[];
  delegated_tasks?: unknown[];
  running_automations?: unknown[];
  pending_settlements?: number | ThreadStatusSliceBucket;
}

export interface ThreadWakeupItem {
  wakeup_id?: string;
  id?: string;
  state?: string;
  status?: string;
}

// ---------------------------------------------------------------------------
// BackendClient Interface & HTTP Implementation
// ---------------------------------------------------------------------------

export interface WorkspaceUploadWeixinContext {
  weixinAccountId?: string | null;
  weixinBotId?: string | null;
  weixinBotGeneration?: number | null;
}

/**
 * Validates required fields of a `POST /internal/weixin/accounts/reconcile` response
 * (contract §2.1 / R6 #4). Rejects empty objects, missing booleans/revisions, or non-array accounts.
 */
export function isValidWeixinAccountsReconcileResponse(
  val: unknown,
): val is WeixinAccountsReconcileResponse {
  if (!val || typeof val !== "object" || Array.isArray(val)) {
    return false;
  }
  const rec = val as Record<string, unknown>;
  if (typeof rec.enabled !== "boolean" || typeof rec.changed !== "boolean") {
    return false;
  }
  if (
    typeof rec.accounts_revision !== "number" ||
    !Number.isInteger(rec.accounts_revision) ||
    rec.accounts_revision < 0
  ) {
    return false;
  }
  if (
    typeof rec.max_concurrent_accounts !== "number" ||
    !Number.isInteger(rec.max_concurrent_accounts) ||
    rec.max_concurrent_accounts <= 0
  ) {
    return false;
  }
  if (!Array.isArray(rec.accounts)) {
    return false;
  }
  return true;
}

export interface BackendClient {
  // Gateway epoch fencing
  setGatewayEpoch?(epoch: number | null): void;
  getGatewayEpoch?(): number | null;
  setOnStaleGatewayEpoch?(handler: ((err: StaleGatewayEpochError) => void) | null): void;

  // Gateway Deployment Lease (D1)
  acquireGatewayLease(
    req: GatewayLeaseAcquireRequest,
  ): Promise<GatewayLeaseAcquireResponse>;

  heartbeatGatewayLease(
    req: GatewayLeaseHeartbeatRequest,
  ): Promise<GatewayLeaseHeartbeatResponse>;

  releaseGatewayLease(
    req: GatewayLeaseReleaseRequest,
  ): Promise<GatewayLeaseReleaseResponse>;

  // I-1 ~ I-8 Internal Persistence Endpoints
  registerAndResolveInbound(
    req: I1RegisterAndResolveRequest,
  ): Promise<I1RegisterAndResolveResponse>;

  consumeBindCode(req: I2BindCodeConsumeRequest): Promise<I2BindCodeConsumeResponse>;

  sessionCommand(req: I3SessionCommandRequest): Promise<I3SessionCommandResponse>;

  getChannelState(params: {
    user_id: string;
    thread_id?: string | null;
    run_id?: string | null;
    platform?: string | null;
    external_thread_id?: string | null;
  }): Promise<I4StateResponse>;

  putChannelSettings(req: I5SettingsPutRequest): Promise<I5SettingsPutResult>;

  authorizeAndClaimDelivery(
    req: I6AuthorizeAndClaimRequest,
    options?: { signal?: AbortSignal },
  ): Promise<I6AuthorizeAndClaimResponse>;

  updateDeliveryProgress(
    req: I7DeliveriesProgressRequest,
    options?: { platform?: string | null },
  ): Promise<I7DeliveriesProgressResponse>;

  updateInboundProgress(
    req: I7InboundProgressRequest,
  ): Promise<I7InboundProgressResponse>;

  reconcileSteer(req: I7SteersReconcileRequest): Promise<I7SteersReconcileResponse>;

  recoverState(params: {
    mode: "startup" | "watch_reconnect";
    thread_id?: string;
  }): Promise<I7RecoverResponse>;

  startAutomationRun(
    req: I8AutomationRunStartRequest,
  ): Promise<I8AutomationRunStartResponse>;

  finishAutomationRun(
    executionId: string,
    req: I8AutomationRunFinishRequest,
    userId?: string,
  ): Promise<{ status: number; data: I8AutomationRunFinishResponse | null }>;

  // Weixin Multi-Account, QR Login, Collection & Preflight Internal Endpoints
  reconcileWeixinAccounts(
    sinceRevision?: number | null,
    options?: { signal?: AbortSignal },
  ): Promise<WeixinAccountsReconcileResponse>;

  getWeixinAccountDetail(
    accountId: string,
    options?: {
      recipient_platform_user_id?: string | null;
      inbound_message_id?: string | null;
      signal?: AbortSignal;
    },
  ): Promise<WeixinAccountDetailResponse>;

  updateWeixinAccountCursor(
    accountId: string,
    req: WeixinAccountCursorUpdateRequest,
  ): Promise<WeixinAccountCursorUpdateResponse>;

  updateWeixinAccountStatus(
    accountId: string,
    req: WeixinAccountStatusUpdateRequest,
  ): Promise<WeixinAccountStatusUpdateResponse>;

  getWeixinLoginSession(
    sessionKey: string,
  ): Promise<WeixinLoginSessionGetResponse>;

  patchWeixinLoginSession(
    sessionKey: string,
    req: WeixinLoginSessionPatchRequest,
  ): Promise<WeixinLoginSessionPatchResponse>;

  preflightWeixinSend(
    deliveryKey: string,
    req: WeixinPreflightSendRequest,
    options?: { signal?: AbortSignal },
  ): Promise<WeixinPreflightSendResponse>;

  // Upstream Conversation, Replay, Watch & Workspace Endpoints
  createThreadMessages(
    userId: string,
    payload: ThreadMessageDispatchPayload,
  ): Promise<SseOrErrorResponse>;

  appendThreadMessages(
    userId: string,
    threadId: string,
    payload: ThreadMessageDispatchPayload,
  ): Promise<SseOrErrorResponse>;

  retryThreadTurn(
    userId: string,
    threadId: string,
    payload: ThreadRetryPayload,
  ): Promise<SseOrErrorResponse>;

  cancelThreadTurn(
    userId: string,
    threadId: string,
    runId?: string | null,
  ): Promise<{ status: number }>;

  streamThreadMessages(
    userId: string,
    threadId: string,
    optionsOrLastEventId?: string | StreamThreadMessagesOptions,
  ): Promise<SseOrErrorResponse>;

  replayThreadMessages(
    userId: string,
    threadId: string,
    beforeTurnOrOptions?: number | ReplayThreadMessagesOptions,
  ): Promise<SseOrErrorResponse>;

  watchThread(
    userId: string,
    threadId: string,
    signal?: AbortSignal,
  ): Promise<SseOrErrorResponse>;

  getDispatchesLiveness(
    userId: string,
    runIds: string[],
  ): Promise<Record<string, DispatchLivenessRunInfo>>;

  getThreadStatus(
    userId: string,
    threadId: string,
    fields?: string,
  ): Promise<ThreadStatusInfo>;

  getThreadWakeups(
    userId: string,
    threadId: string,
  ): Promise<ThreadWakeupItem[]>;

  ensureHomeWorkspace(
    userId: string,
  ): Promise<{ workspace_id: string; status: string }>;

  startWorkspace(
    userId: string,
    workspaceId: string,
  ): Promise<{ status: string }>;

  uploadWorkspaceFile(
    userId: string,
    workspaceId: string,
    workspacePath: string,
    filename: string,
    content: Buffer,
    mimeType: string,
    signal?: AbortSignal,
    weixinContext?: WorkspaceUploadWeixinContext,
  ): Promise<void>;

  downloadWorkspaceFile(
    userId: string,
    workspaceId: string,
    workspacePath: string,
  ): Promise<{ content: Buffer; mimeType: string }>;

  authorizeDataTaskNotificationPlan?(
    req: DataTaskNotificationRequest,
  ): Promise<DataTaskAuthorizePlanResponse>;

  authorizeAutomationNotificationPlan?(req: {
    delivery_id: string;
  }): Promise<DataTaskAuthorizePlanResponse>;

  downloadDataTaskRunArtifact?(
    runId: string,
    artifactId: string,
    maxBytes?: number,
  ): Promise<DataTaskDownloadedArtifact>;
}

export class BackendHttpError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(message: string, status: number, body?: unknown) {
    super(message);
    this.name = "BackendHttpError";
    this.status = status;
    this.body = body;
  }
}

export class HttpBackendClient implements BackendClient {
  private readonly baseUrl: string;
  private readonly serviceToken: string;
  private readonly fetchImpl: typeof fetch;
  private gatewayEpoch: number | null = null;
  private hasHeldGatewayLease = false;
  private onStaleGatewayEpoch: ((err: StaleGatewayEpochError) => void) | null = null;

  constructor(options: {
    baseUrl: string;
    serviceToken: string;
    fetchImpl?: typeof fetch;
    fetchFn?: typeof fetch;
  }) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.serviceToken = options.serviceToken;
    this.fetchImpl = options.fetchImpl ?? options.fetchFn ?? globalThis.fetch;
  }

  setGatewayEpoch(epoch: number | null): void {
    this.gatewayEpoch = epoch;
    if (typeof epoch === "number" && Number.isInteger(epoch) && epoch > 0) {
      this.hasHeldGatewayLease = true;
    }
  }

  getGatewayEpoch(): number | null {
    return this.gatewayEpoch;
  }

  setOnStaleGatewayEpoch(handler: ((err: StaleGatewayEpochError) => void) | null): void {
    this.onStaleGatewayEpoch = handler;
  }

  private headers(
    userId?: string | null,
    extra?: Record<string, string>,
    includeGatewayEpoch?: boolean,
  ): Record<string, string> {
    const h: Record<string, string> = {
      "X-Service-Token": this.serviceToken,
      ...(extra ?? {}),
    };
    if (userId) {
      h["X-User-Id"] = userId;
    }
    if (includeGatewayEpoch && this.gatewayEpoch !== null) {
      h["X-Gateway-Epoch"] = String(this.gatewayEpoch);
    }
    return h;
  }

  private checkStaleGatewayEpoch(status: number, parsed: unknown, path: string): void {
    if (status !== 409) {
      return;
    }
    const obj =
      parsed && typeof parsed === "object"
        ? (parsed as Record<string, unknown>)
        : null;
    const detailObj =
      obj?.detail && typeof obj.detail === "object"
        ? (obj.detail as Record<string, unknown>)
        : null;
    const code =
      typeof obj?.code === "string"
        ? obj.code
        : typeof detailObj?.code === "string"
          ? detailObj.code
          : null;
    if (code === "stale_gateway_epoch") {
      const msg =
        typeof obj?.message === "string"
          ? obj.message
          : typeof detailObj?.message === "string"
            ? detailObj.message
            : `Stale gateway epoch on ${path}`;
      const err = new StaleGatewayEpochError(msg, {
        status: 409,
        epoch: this.gatewayEpoch,
      });
      // Decision #2: Only treat 409 stale_gateway_epoch as lease loss if this process
      // has actually held a gateway lease; pre-lease startup requests must not trigger fatal exit.
      if (this.hasHeldGatewayLease) {
        this.onStaleGatewayEpoch?.(err);
      }
      throw err;
    }
  }

  private async jsonRequest<T>(
    method: string,
    path: string,
    options?: {
      userId?: string | null;
      query?: Record<string, string | undefined | null>;
      body?: unknown;
      timeoutMs?: number;
      signal?: AbortSignal;
      redirect?: RequestRedirect;
      cache?: RequestCache;
      strictJson?: boolean;
      operationLabel?: string;
      includeGatewayEpoch?: boolean;
    },
  ): Promise<{ status: number; data: T }> {
    const url = new URL(`${this.baseUrl}${path}`);
    if (options?.query) {
      for (const [k, v] of Object.entries(options.query)) {
        if (v !== undefined && v !== null && v !== "") {
          url.searchParams.set(k, v);
        }
      }
    }
    const timeoutSignal =
      typeof options?.timeoutMs === "number" && options.timeoutMs > 0
        ? AbortSignal.timeout(options.timeoutMs)
        : undefined;
    const effectiveSignal =
      options?.signal && timeoutSignal
        ? AbortSignal.any([options.signal, timeoutSignal])
        : (options?.signal ?? timeoutSignal);
    const init: RequestInit = {
      method,
      headers: this.headers(
        options?.userId,
        options?.body !== undefined ? { "Content-Type": "application/json" } : undefined,
        options?.includeGatewayEpoch,
      ),
      ...(options?.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
      ...(options?.redirect ? { redirect: options.redirect } : {}),
      ...(options?.cache ? { cache: options.cache } : {}),
      ...(effectiveSignal ? { signal: effectiveSignal } : {}),
    };
    const resp = await this.fetchImpl(url, init);
    const text = await resp.text();
    if (resp.status === 409 && text.trim()) {
      try {
        const maybeStale = JSON.parse(text);
        this.checkStaleGatewayEpoch(resp.status, maybeStale, path);
      } catch (err) {
        if (err instanceof StaleGatewayEpochError) {
          throw err;
        }
      }
    }
    if (options?.strictJson) {
      const label = options.operationLabel ?? path;
      if (resp.status >= 300 && resp.status < 400) {
        throw new BackendHttpError(
          `${label} redirect rejected (${resp.status})`,
          resp.status,
          null,
        );
      }
      if (resp.status >= 400) {
        throw new BackendHttpError(
          `${label} failed (${resp.status})`,
          resp.status,
          null,
        );
      }
      if (!text.trim()) {
        throw new BackendHttpError(
          `${label} returned an empty response body (${resp.status})`,
          resp.status,
          null,
        );
      }
      let strictParsed: unknown;
      try {
        strictParsed = JSON.parse(text);
      } catch {
        throw new BackendHttpError(
          `${label} returned malformed JSON (${resp.status})`,
          resp.status,
          null,
        );
      }
      return { status: resp.status, data: strictParsed as T };
    }
    let parsed: unknown = {};
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = { message: text };
      }
    }
    this.checkStaleGatewayEpoch(resp.status, parsed, path);
    return { status: resp.status, data: parsed as T };
  }

  private async sseRequest(
    method: string,
    path: string,
    options: {
      userId: string;
      query?: Record<string, string | undefined | null>;
      body?: unknown;
      headers?: Record<string, string>;
      signal?: AbortSignal;
    },
  ): Promise<SseOrErrorResponse> {
    const url = new URL(`${this.baseUrl}${path}`);
    if (options.query) {
      for (const [k, v] of Object.entries(options.query)) {
        if (v !== undefined && v !== null && v !== "") {
          url.searchParams.set(k, v);
        }
      }
    }
    const reqHeaders = this.headers(options.userId, {
      Accept: "text/event-stream, application/json",
      ...(options.body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(options.headers ?? {}),
    });
    const resp = await this.fetchImpl(url, {
      method,
      headers: reqHeaders,
      ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
      ...(options.signal ? { signal: options.signal } : {}),
    });

    const contentType = (resp.headers.get("content-type") ?? "").toLowerCase();
    if (resp.ok && contentType.includes("text/event-stream") && resp.body) {
      return {
        status: resp.status,
        isSse: true,
        events: parseSseStream(resp.body),
      };
    }

    const text = await resp.text();
    let errorBody: Record<string, unknown> = {};
    if (text) {
      try {
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed === "object") {
          errorBody = parsed as Record<string, unknown>;
        } else {
          errorBody = { detail: String(parsed) };
        }
      } catch {
        errorBody = { detail: text };
      }
    }
    return {
      status: resp.status,
      isSse: false,
      errorBody,
    };
  }

  // --- Gateway Deployment Lease (D1) ---
  async acquireGatewayLease(
    req: GatewayLeaseAcquireRequest,
  ): Promise<GatewayLeaseAcquireResponse> {
    const { status, data } = await this.jsonRequest<GatewayLeaseAcquireResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/gateway-lease/acquire",
      { body: req, timeoutMs: 15_000 },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `gateway-lease/acquire failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  async heartbeatGatewayLease(
    req: GatewayLeaseHeartbeatRequest,
  ): Promise<GatewayLeaseHeartbeatResponse> {
    const { status, data } = await this.jsonRequest<GatewayLeaseHeartbeatResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/gateway-lease/heartbeat",
      { body: req, timeoutMs: 15_000 },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `gateway-lease/heartbeat failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  async releaseGatewayLease(
    req: GatewayLeaseReleaseRequest,
  ): Promise<GatewayLeaseReleaseResponse> {
    const { status, data } = await this.jsonRequest<GatewayLeaseReleaseResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/gateway-lease/release",
      { body: req, timeoutMs: 10_000 },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `gateway-lease/release failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  // --- I-1 ---
  async registerAndResolveInbound(
    req: I1RegisterAndResolveRequest,
  ): Promise<I1RegisterAndResolveResponse> {
    const { status, data } = await this.jsonRequest<I1RegisterAndResolveResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
      { body: req, includeGatewayEpoch: true },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `I-1 register-and-resolve failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  // --- I-2 ---
  async consumeBindCode(
    req: I2BindCodeConsumeRequest,
  ): Promise<I2BindCodeConsumeResponse> {
    const { status, data } = await this.jsonRequest<I2BindCodeConsumeResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/bind-code/consume",
      { userId: req.user_id, body: req, includeGatewayEpoch: true },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `I-2 bind-code/consume failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  // --- I-3 ---
  async sessionCommand(
    req: I3SessionCommandRequest,
  ): Promise<I3SessionCommandResponse> {
    const sanitizedBody: I3SessionCommandRequest & Record<string, unknown> = {
      ...req,
    };
    delete sanitizedBody.token;
    delete sanitizedBody.interaction_token;
    delete sanitizedBody.interactionToken;
    if (sanitizedBody.raw_payload && typeof sanitizedBody.raw_payload === "object") {
      const cleanedRaw: Record<string, unknown> = { ...sanitizedBody.raw_payload };
      delete cleanedRaw.token;
      delete cleanedRaw.interaction_token;
      delete cleanedRaw.interactionToken;
      if (
        cleanedRaw.discord_interaction &&
        typeof cleanedRaw.discord_interaction === "object" &&
        !Array.isArray(cleanedRaw.discord_interaction)
      ) {
        const cleanedInteraction: Record<string, unknown> = {
          ...(cleanedRaw.discord_interaction as Record<string, unknown>),
        };
        delete cleanedInteraction.token;
        delete cleanedInteraction.interaction_token;
        delete cleanedInteraction.interactionToken;
        cleanedRaw.discord_interaction = cleanedInteraction;
      }
      sanitizedBody.raw_payload = cleanedRaw;
    }
    const { status, data } = await this.jsonRequest<I3SessionCommandResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/session/command",
      {
        userId: req.dalton_user_id,
        body: sanitizedBody,
        includeGatewayEpoch: !req.is_internal_query,
      },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `I-3 session/command failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  // --- I-4 ---
  async getChannelState(params: {
    user_id: string;
    thread_id?: string | null;
    run_id?: string | null;
    platform?: string | null;
    external_thread_id?: string | null;
  }): Promise<I4StateResponse> {
    const { status, data } = await this.jsonRequest<I4StateResponse>(
      "GET",
      "/api/v1/dalton/channels/internal/state",
      {
        userId: params.user_id,
        query: {
          user_id: params.user_id,
          thread_id: params.thread_id,
          run_id: params.run_id,
          platform: params.platform,
          external_thread_id: params.external_thread_id,
        },
      },
    );
    if (status >= 400) {
      throw new Error(`I-4 getChannelState failed (${status}): ${JSON.stringify(data)}`);
    }
    return data;
  }

  // --- I-5 ---
  async putChannelSettings(req: I5SettingsPutRequest): Promise<I5SettingsPutResult> {
    return await this.jsonRequest<I5SettingsPutResult["data"]>(
      "PUT",
      "/api/v1/dalton/channels/internal/settings",
      { userId: req.user_id, body: req },
    );
  }

  // --- I-6 ---
  private inferPlatformForEpoch(
    explicitPlatform?: string | null,
    targetAddress?: string | null,
  ): string | null {
    // R4 #1: Target address platform ALWAYS takes precedence over turn/origin platform
    // so cross-channel sends (e.g. Discord turn -> `weixin:@me` or Weixin turn -> `feishu:...`)
    // attach/omit `X-Gateway-Epoch` strictly according to the actual target platform.
    const cleanTarget = targetAddress?.trim().toLowerCase();
    if (cleanTarget && cleanTarget.includes(":")) {
      const prefix = cleanTarget.split(":", 1)[0]?.trim();
      if (prefix === "feishu" || prefix === "discord" || prefix === "weixin") {
        return prefix;
      }
    }
    const cleanExplicit = explicitPlatform?.trim().toLowerCase();
    if (cleanExplicit) return cleanExplicit;
    return null;
  }

  async authorizeAndClaimDelivery(
    req: I6AuthorizeAndClaimRequest,
    options?: { signal?: AbortSignal },
  ): Promise<I6AuthorizeAndClaimResponse> {
    // Contract §0 & §7 (R4 #1): attach X-Gateway-Epoch on I-6 ONLY when the resolved TARGET
    // platform is Weixin (or ambiguous current-conversation target with no platform/target);
    // omit for Feishu / Discord targets even when sent from a Weixin turn.
    const inferredPlatform = this.inferPlatformForEpoch(
      req.platform,
      req.target,
    );
    const includeGatewayEpoch =
      inferredPlatform === "weixin" || inferredPlatform === null;
    if (req.kind === "data_task" || req.kind === "automation_notify") {
      const { data } = await this.jsonRequest<I6AuthorizeAndClaimResponse>(
        "POST",
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        {
          userId: req.user_id,
          body: req,
          timeoutMs: 15_000,
          signal: options?.signal,
          redirect: "error",
          cache: "no-store",
          strictJson: true,
          operationLabel: "I-6 authorize-and-claim",
          includeGatewayEpoch,
        },
      );
      return data;
    }
    const { status, data } = await this.jsonRequest<I6AuthorizeAndClaimResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
      {
        userId: req.user_id,
        body: req,
        signal: options?.signal,
        includeGatewayEpoch,
      },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `I-6 authorize-and-claim failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  // --- I-7 (1) ---
  async updateDeliveryProgress(
    req: I7DeliveriesProgressRequest,
    options?: { platform?: string | null },
  ): Promise<I7DeliveriesProgressResponse> {
    // Contract §0 & §7 (Round 3 Decision #9): attach X-Gateway-Epoch on I-7 deliveries/progress
    // ONLY when target platform is Weixin; omit for Feishu / Discord.
    const rawAddr =
      typeof req.first_response_json?.address === "string"
        ? req.first_response_json.address
        : null;
    const inferredFromKey =
      typeof req.delivery_key === "string" &&
      req.delivery_key.startsWith("notice:weixin:")
        ? "weixin"
        : typeof req.delivery_key === "string" &&
            (req.delivery_key.startsWith("notice:feishu:") ||
              req.delivery_key.startsWith("notice:discord:"))
          ? req.delivery_key.split(":")[1]
          : null;
    const resolvedPlatform = this.inferPlatformForEpoch(
      options?.platform ?? inferredFromKey,
      rawAddr,
    );
    const includeGatewayEpoch = resolvedPlatform === "weixin";
    if (
      typeof req.delivery_key === "string" &&
      (req.delivery_key.startsWith("data_task:") ||
        req.delivery_key.startsWith("automation_notify:"))
    ) {
      const { data } = await this.jsonRequest<I7DeliveriesProgressResponse>(
        "POST",
        "/api/v1/dalton/channels/internal/deliveries/progress",
        {
          body: req,
          timeoutMs: 15_000,
          redirect: "error",
          cache: "no-store",
          strictJson: true,
          operationLabel: "I-7 deliveries/progress",
          includeGatewayEpoch,
        },
      );
      return data;
    }
    const { status, data } = await this.jsonRequest<I7DeliveriesProgressResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/deliveries/progress",
      { body: req, includeGatewayEpoch },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `I-7 deliveries/progress failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  // --- I-7 (2) ---
  async updateInboundProgress(
    req: I7InboundProgressRequest,
  ): Promise<I7InboundProgressResponse> {
    const inferredPlatform = (
      req.platform ??
      req.thread_watch?.platform ??
      req.background_run?.platform ??
      req.session_interrupt_update?.platform ??
      ""
    )
      .trim()
      .toLowerCase();
    const includeGatewayEpoch = inferredPlatform === "weixin";
    const { status, data } = await this.jsonRequest<I7InboundProgressResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/inbound/progress",
      { body: req, includeGatewayEpoch },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `I-7 inbound/progress failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  // --- I-7 (3) ---
  async reconcileSteer(
    req: I7SteersReconcileRequest,
  ): Promise<I7SteersReconcileResponse> {
    const includeGatewayEpoch =
      (req.platform ?? "").trim().toLowerCase() === "weixin";
    const { status, data } = await this.jsonRequest<I7SteersReconcileResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/steers/reconcile",
      { body: req, includeGatewayEpoch },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `I-7 steers/reconcile failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  // --- I-7 (4) ---
  async recoverState(params: {
    mode: "startup" | "watch_reconnect";
    thread_id?: string;
  }): Promise<I7RecoverResponse> {
    const { status, data } = await this.jsonRequest<I7RecoverResponse>(
      "GET",
      "/api/v1/dalton/channels/internal/recover",
      {
        query: {
          mode: params.mode,
          thread_id: params.thread_id,
        },
        includeGatewayEpoch: true,
      },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `I-7 recover failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  // --- I-8 (1) (`src/dalton/app/channels_router.py:477`: `/internal/automation-runs`) ---
  async startAutomationRun(
    req: I8AutomationRunStartRequest,
  ): Promise<I8AutomationRunStartResponse> {
    const { status, data } = await this.jsonRequest<I8AutomationRunStartResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/automation-runs",
      { userId: req.user_id, body: req },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `I-8 automation-runs failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  // --- I-8 (2) ---
  async finishAutomationRun(
    executionId: string,
    req: I8AutomationRunFinishRequest,
    userId?: string,
  ): Promise<{ status: number; data: I8AutomationRunFinishResponse | null }> {
    const { status, data } = await this.jsonRequest<I8AutomationRunFinishResponse>(
      "POST",
      `/api/v1/dalton/channels/internal/automation-runs/${encodeURIComponent(executionId)}/finish`,
      { userId, body: req },
    );
    if (status === 404) {
      return { status: 404, data: null };
    }
    if (status >= 400) {
      throw new BackendHttpError(
        `I-8 automation-runs/finish failed (${status})`,
        status,
        data,
      );
    }
    return { status, data };
  }

  // --- Weixin Multi-Account, QR Login, Collection & Preflight Internal Endpoints ---

  async reconcileWeixinAccounts(
    sinceRevision?: number | null,
    options?: { signal?: AbortSignal },
  ): Promise<WeixinAccountsReconcileResponse> {
    let status: number;
    let data: WeixinAccountsReconcileResponse;
    try {
      const res = await this.jsonRequest<WeixinAccountsReconcileResponse>(
        "POST",
        "/api/v1/dalton/channels/internal/weixin/accounts/reconcile",
        {
          body:
            sinceRevision !== undefined && sinceRevision !== null
              ? { since_revision: sinceRevision }
              : {},
          timeoutMs: 15_000,
          signal: options?.signal,
          includeGatewayEpoch: true,
          strictJson: true,
          operationLabel: "weixin/accounts/reconcile",
        },
      );
      status = res.status;
      data = res.data;
    } catch (err) {
      // R6 #4: Normalize empty body / malformed JSON on 2xx responses from `strictJson`
      // to 502 `invalid_reconcile_snapshot`; preserve >= 400 (and 409 stale epoch / 3xx / network) as-is.
      if (
        err instanceof BackendHttpError &&
        err.status >= 200 &&
        err.status < 300
      ) {
        throw new BackendHttpError(
          "weixin/accounts/reconcile returned invalid snapshot payload",
          502,
          { code: "invalid_reconcile_snapshot" },
        );
      }
      throw err;
    }
    if (status >= 400) {
      throw new BackendHttpError(
        `weixin/accounts/reconcile failed (${status})`,
        status,
        data,
      );
    }
    if (!isValidWeixinAccountsReconcileResponse(data)) {
      throw new BackendHttpError(
        "weixin/accounts/reconcile returned invalid snapshot payload",
        502,
        { code: "invalid_reconcile_snapshot" },
      );
    }
    return data;
  }

  async getWeixinAccountDetail(
    accountId: string,
    options?: {
      recipient_platform_user_id?: string | null;
      inbound_message_id?: string | null;
      signal?: AbortSignal;
    },
  ): Promise<WeixinAccountDetailResponse> {
    const { status, data } = await this.jsonRequest<WeixinAccountDetailResponse>(
      "GET",
      `/api/v1/dalton/channels/internal/weixin/accounts/${encodeURIComponent(accountId)}`,
      {
        query: {
          recipient_platform_user_id: options?.recipient_platform_user_id ?? undefined,
          inbound_message_id: options?.inbound_message_id ?? undefined,
        },
        timeoutMs: 15_000,
        signal: options?.signal,
      },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `weixin/accounts/detail failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  async updateWeixinAccountCursor(
    accountId: string,
    req: WeixinAccountCursorUpdateRequest,
  ): Promise<WeixinAccountCursorUpdateResponse> {
    const { status, data } = await this.jsonRequest<WeixinAccountCursorUpdateResponse>(
      "POST",
      `/api/v1/dalton/channels/internal/weixin/accounts/${encodeURIComponent(accountId)}/cursor`,
      {
        body: req,
        timeoutMs: 15_000,
        includeGatewayEpoch: true,
      },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `weixin/accounts/cursor failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  async updateWeixinAccountStatus(
    accountId: string,
    req: WeixinAccountStatusUpdateRequest,
  ): Promise<WeixinAccountStatusUpdateResponse> {
    const { status, data } = await this.jsonRequest<WeixinAccountStatusUpdateResponse>(
      "POST",
      `/api/v1/dalton/channels/internal/weixin/accounts/${encodeURIComponent(accountId)}/status`,
      {
        body: req,
        timeoutMs: 15_000,
        includeGatewayEpoch: true,
      },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `weixin/accounts/status failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  async getWeixinLoginSession(
    sessionKey: string,
  ): Promise<WeixinLoginSessionGetResponse> {
    const { status, data } = await this.jsonRequest<WeixinLoginSessionGetResponse>(
      "GET",
      `/api/v1/dalton/channels/internal/weixin/login-sessions/${encodeURIComponent(sessionKey)}`,
      { timeoutMs: 15_000 },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `weixin/login-sessions GET failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  async patchWeixinLoginSession(
    sessionKey: string,
    req: WeixinLoginSessionPatchRequest,
  ): Promise<WeixinLoginSessionPatchResponse> {
    const { status, data } = await this.jsonRequest<WeixinLoginSessionPatchResponse>(
      "PATCH",
      `/api/v1/dalton/channels/internal/weixin/login-sessions/${encodeURIComponent(sessionKey)}`,
      {
        body: req,
        timeoutMs: 15_000,
        includeGatewayEpoch: true,
      },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `weixin/login-sessions PATCH failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  async preflightWeixinSend(
    deliveryKey: string,
    req: WeixinPreflightSendRequest,
    options?: { signal?: AbortSignal },
  ): Promise<WeixinPreflightSendResponse> {
    const { status, data } = await this.jsonRequest<WeixinPreflightSendResponse>(
      "POST",
      `/api/v1/dalton/channels/internal/deliveries/${encodeURIComponent(deliveryKey)}/preflight-send`,
      {
        body: req,
        timeoutMs: 15_000,
        signal: options?.signal,
        includeGatewayEpoch: true,
      },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `deliveries/preflight-send failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  // --- Upstream Conversation & Workspace Endpoints ---

  private normalizeDispatchBody(payload: ThreadMessageDispatchPayload): Record<string, unknown> {
    const platform = payload.platform ?? payload.turn_platform ?? null;
    const out: Record<string, unknown> = {
      ...payload,
      platform,
    };
    delete out.turn_platform;
    return out;
  }

  async createThreadMessages(
    userId: string,
    payload: ThreadMessageDispatchPayload,
  ): Promise<SseOrErrorResponse> {
    return await this.sseRequest("POST", "/api/v1/threads/messages", {
      userId,
      body: this.normalizeDispatchBody(payload),
    });
  }

  async appendThreadMessages(
    userId: string,
    threadId: string,
    payload: ThreadMessageDispatchPayload,
  ): Promise<SseOrErrorResponse> {
    return await this.sseRequest(
      "POST",
      `/api/v1/threads/${encodeURIComponent(threadId)}/messages`,
      {
        userId,
        body: this.normalizeDispatchBody(payload),
      },
    );
  }

  async retryThreadTurn(
    userId: string,
    threadId: string,
    payload: ThreadRetryPayload,
  ): Promise<SseOrErrorResponse> {
    return await this.sseRequest(
      "POST",
      `/api/v1/threads/${encodeURIComponent(threadId)}/retry`,
      {
        userId,
        body: payload,
      },
    );
  }

  async cancelThreadTurn(
    userId: string,
    threadId: string,
    runId?: string | null,
  ): Promise<{ status: number }> {
    const { status } = await this.jsonRequest<unknown>(
      "POST",
      `/api/v1/threads/${encodeURIComponent(threadId)}/cancel`,
      {
        userId,
        query: runId ? { run_id: runId } : undefined,
      },
    );
    return { status };
  }

  async streamThreadMessages(
    userId: string,
    threadId: string,
    optionsOrLastEventId?: string | StreamThreadMessagesOptions,
  ): Promise<SseOrErrorResponse> {
    const opts: StreamThreadMessagesOptions =
      typeof optionsOrLastEventId === "string"
        ? { lastEventId: optionsOrLastEventId }
        : (optionsOrLastEventId ?? {});
    const lastIdStr =
      opts.lastEventId !== undefined && opts.lastEventId !== null && String(opts.lastEventId) !== ""
        ? String(opts.lastEventId)
        : undefined;
    return await this.sseRequest(
      "GET",
      `/api/v1/threads/${encodeURIComponent(threadId)}/messages/stream`,
      {
        userId,
        query: {
          run_id: opts.runId ?? undefined,
          last_event_id: lastIdStr,
        },
        headers: lastIdStr ? { "Last-Event-ID": lastIdStr } : undefined,
      },
    );
  }

  async replayThreadMessages(
    userId: string,
    threadId: string,
    beforeTurnOrOptions?: number | ReplayThreadMessagesOptions,
  ): Promise<SseOrErrorResponse> {
    const opts: ReplayThreadMessagesOptions =
      typeof beforeTurnOrOptions === "number"
        ? { beforeTurn: beforeTurnOrOptions }
        : (beforeTurnOrOptions ?? {});
    return await this.sseRequest(
      "GET",
      `/api/v1/threads/${encodeURIComponent(threadId)}/messages/replay`,
      {
        userId,
        query: {
          source: opts.source ?? "auto",
          limit: String(opts.limit ?? 5),
          ...(opts.beforeTurn !== undefined ? { before_turn: String(opts.beforeTurn) } : {}),
        },
      },
    );
  }

  async watchThread(
    userId: string,
    threadId: string,
    signal?: AbortSignal,
  ): Promise<SseOrErrorResponse> {
    return await this.sseRequest(
      "GET",
      `/api/v1/threads/${encodeURIComponent(threadId)}/watch`,
      {
        userId,
        signal,
      },
    );
  }

  /**
   * Matches `GET /api/v1/threads/dispatches/liveness` (`src/server/app/threads/messaging.py:1484-1595`).
   * Backend returns `{ liveness: [...], runs: [{ run_id, thread_id, status, ... }], steers: [...] }`.
   * Normalized into a map keyed by `run_id`.
   */
  async getDispatchesLiveness(
    userId: string,
    runIds: string[],
  ): Promise<Record<string, DispatchLivenessRunInfo>> {
    if (runIds.length === 0) return {};
    const { status, data } = await this.jsonRequest<{
      runs?: DispatchLivenessRunInfo[] | Record<string, DispatchLivenessRunInfo>;
    }>("GET", "/api/v1/threads/dispatches/liveness", {
      userId,
      query: { runs: runIds.join(",") },
    });
    if (status >= 400) {
      throw new Error(`getDispatchesLiveness failed (${status})`);
    }
    const out: Record<string, DispatchLivenessRunInfo> = {};
    if (data && typeof data === "object" && "runs" in data && data.runs) {
      if (Array.isArray(data.runs)) {
        for (const item of data.runs) {
          if (item && typeof item === "object" && typeof item.run_id === "string") {
            out[item.run_id] = item;
          }
        }
        return out;
      }
      if (typeof data.runs === "object") {
        return data.runs as Record<string, DispatchLivenessRunInfo>;
      }
    }
    return out;
  }

  async getThreadStatus(
    userId: string,
    threadId: string,
    fields?: string,
  ): Promise<ThreadStatusInfo> {
    const { status, data } = await this.jsonRequest<ThreadStatusInfo>(
      "GET",
      `/api/v1/threads/${encodeURIComponent(threadId)}/status`,
      {
        userId,
        query: fields ? { fields } : undefined,
      },
    );
    if (status === 404) {
      return { status: "not_found" };
    }
    if (status >= 400) {
      throw new BackendHttpError(
        `getThreadStatus failed (${status})`,
        status,
        data,
      );
    }
    return data;
  }

  async getThreadWakeups(
    userId: string,
    threadId: string,
  ): Promise<ThreadWakeupItem[]> {
    const { status, data } = await this.jsonRequest<
      ThreadWakeupItem[] | { wakeups?: ThreadWakeupItem[] }
    >("GET", `/api/v1/threads/${encodeURIComponent(threadId)}/wakeups`, {
      userId,
    });
    if (status >= 400) {
      throw new BackendHttpError(
        `getThreadWakeups failed (${status})`,
        status,
        data,
      );
    }
    if (Array.isArray(data)) return data;
    return data?.wakeups ?? [];
  }

  async ensureHomeWorkspace(
    userId: string,
  ): Promise<{ workspace_id: string; status: string }> {
    const { status, data } = await this.jsonRequest<{
      workspace_id: string;
      status: string;
    }>("POST", "/api/v1/workspaces/home", { userId });
    if (status >= 400) {
      throw new Error(`ensureHomeWorkspace failed (${status}): ${JSON.stringify(data)}`);
    }
    return data;
  }

  async startWorkspace(
    userId: string,
    workspaceId: string,
  ): Promise<{ status: string }> {
    const { status, data } = await this.jsonRequest<{ status: string }>(
      "POST",
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/start`,
      {
        userId,
        query: { lazy: "false" },
      },
    );
    if (status >= 400) {
      throw new BackendHttpError(
        `startWorkspace failed (${status}): ${JSON.stringify(data)}`,
        status,
        data,
      );
    }
    return data;
  }

  async uploadWorkspaceFile(
    userId: string,
    workspaceId: string,
    workspacePath: string,
    filename: string,
    content: Buffer,
    mimeType: string,
    signal?: AbortSignal,
    weixinContext?: WorkspaceUploadWeixinContext,
  ): Promise<void> {
    const url = new URL(
      `${this.baseUrl}/api/v1/workspaces/${encodeURIComponent(workspaceId)}/files/upload`,
    );
    url.searchParams.set("path", workspacePath);

    const form = new FormData();
    const blob = new Blob([new Uint8Array(content)], { type: mimeType });
    form.append("file", blob, filename);

    const weixinHeaders: Record<string, string> = {};
    if (weixinContext) {
      if (weixinContext.weixinAccountId?.trim()) {
        weixinHeaders["X-Weixin-Account-Id"] =
          weixinContext.weixinAccountId.trim();
      }
      if (weixinContext.weixinBotId?.trim()) {
        weixinHeaders["X-Weixin-Bot-Id"] = weixinContext.weixinBotId.trim();
      }
      if (
        typeof weixinContext.weixinBotGeneration === "number" &&
        Number.isInteger(weixinContext.weixinBotGeneration)
      ) {
        weixinHeaders["X-Weixin-Bot-Generation"] = String(
          weixinContext.weixinBotGeneration,
        );
      }
    }

    const resp = await this.fetchImpl(url, {
      method: "POST",
      headers: this.headers(
        userId,
        Object.keys(weixinHeaders).length > 0 ? weixinHeaders : undefined,
        Boolean(weixinContext),
      ),
      body: form,
      ...(signal ? { signal } : {}),
    });
    if (!resp.ok) {
      const errText = await resp.text();
      if (resp.status === 409 && errText.trim()) {
        try {
          const parsedErr = JSON.parse(errText);
          this.checkStaleGatewayEpoch(resp.status, parsedErr, url.pathname);
        } catch (err) {
          if (err instanceof StaleGatewayEpochError) {
            throw err;
          }
        }
      }
      throw new BackendHttpError(
        `uploadWorkspaceFile failed (${resp.status}): ${errText}`,
        resp.status,
        (() => {
          try {
            return errText ? JSON.parse(errText) : null;
          } catch {
            return { message: errText };
          }
        })(),
      );
    }
  }

  async downloadWorkspaceFile(
    userId: string,
    workspaceId: string,
    workspacePath: string,
  ): Promise<{ content: Buffer; mimeType: string }> {
    const url = new URL(
      `${this.baseUrl}/api/v1/workspaces/${encodeURIComponent(workspaceId)}/files/download`,
    );
    url.searchParams.set("path", workspacePath);

    const resp = await this.fetchImpl(url, {
      method: "GET",
      headers: this.headers(userId),
    });
    if (!resp.ok) {
      const errText = await resp.text();
      throw new Error(`downloadWorkspaceFile failed (${resp.status}): ${errText}`);
    }
    const contentLengthHeader = resp.headers.get("content-length");
    if (contentLengthHeader) {
      const parsedLen = Number.parseInt(contentLengthHeader, 10);
      if (Number.isFinite(parsedLen) && parsedLen > MAX_ATTACHMENT_BYTES) {
        await resp.body?.cancel().catch(() => undefined);
        throw new SafeFetchError(
          "size_exceeded",
          `Workspace file exceeds 20 MB limit (${parsedLen} bytes)`,
        );
      }
    }
    const content = resp.body
      ? await readStreamWithLimit(resp.body, { maxBytes: MAX_ATTACHMENT_BYTES })
      : Buffer.from(await resp.arrayBuffer());
    if (content.byteLength > MAX_ATTACHMENT_BYTES) {
      throw new SafeFetchError(
        "size_exceeded",
        `Workspace file exceeds 20 MB limit (${content.byteLength} bytes)`,
      );
    }
    const mimeType = resp.headers.get("content-type") ?? "application/octet-stream";
    return {
      content,
      mimeType,
    };
  }

  async authorizeDataTaskNotificationPlan(
    req: DataTaskNotificationRequest,
  ): Promise<DataTaskAuthorizePlanResponse> {
    const { data } = await this.jsonRequest<DataTaskAuthorizePlanResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/data-task-notifications/plan",
      {
        body: {
          run_id: req.run_id,
          target: req.target,
        },
        timeoutMs: 15_000,
        redirect: "error",
        cache: "no-store",
        strictJson: true,
        operationLabel: "authorizeDataTaskNotificationPlan",
      },
    );
    return data;
  }

  async authorizeAutomationNotificationPlan(req: {
    delivery_id: string;
  }): Promise<DataTaskAuthorizePlanResponse> {
    const { data } = await this.jsonRequest<DataTaskAuthorizePlanResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/automation-notifications/plan",
      {
        body: {
          delivery_id: req.delivery_id,
        },
        timeoutMs: 15_000,
        redirect: "error",
        cache: "no-store",
        strictJson: true,
        operationLabel: "authorizeAutomationNotificationPlan",
      },
    );
    return data;
  }

  async downloadDataTaskRunArtifact(
    runId: string,
    artifactId: string,
    maxBytes = 20 * 1024 * 1024,
  ): Promise<DataTaskDownloadedArtifact> {
    const url = new URL(
      `${this.baseUrl}/api/v1/dalton/channels/internal/data-task-notifications/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`,
    );
    const resp = await this.fetchImpl(url, {
      method: "GET",
      headers: this.headers(),
      redirect: "error",
      cache: "no-store",
      signal: AbortSignal.timeout(30_000),
    });
    if (resp.status >= 300 && resp.status < 400) {
      void resp.body?.cancel().catch(() => undefined);
      throw new BackendHttpError(
        `downloadDataTaskRunArtifact redirect rejected (${resp.status})`,
        resp.status,
        null,
      );
    }
    if (!resp.ok) {
      void resp.body?.cancel().catch(() => undefined);
      throw new BackendHttpError(
        `downloadDataTaskRunArtifact failed (${resp.status})`,
        resp.status,
        null,
      );
    }

    const declaredLengthRaw = resp.headers.get("content-length");
    if (declaredLengthRaw) {
      const declaredLength = Number.parseInt(declaredLengthRaw, 10);
      if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
        throw new Error(
          `Artifact exceeds maximum byte limit (${declaredLength} > ${maxBytes})`,
        );
      }
    }

    const chunks: Buffer[] = [];
    let total = 0;
    if (resp.body) {
      for await (const rawChunk of resp.body as AsyncIterable<Uint8Array>) {
        const buf = Buffer.from(rawChunk);
        total += buf.byteLength;
        if (total > maxBytes) {
          throw new Error(
            `Artifact stream exceeded maximum byte limit (${maxBytes})`,
          );
        }
        chunks.push(buf);
      }
    } else {
      const ab = await resp.arrayBuffer();
      if (ab.byteLength > maxBytes) {
        throw new Error(
          `Artifact body exceeded maximum byte limit (${maxBytes})`,
        );
      }
      chunks.push(Buffer.from(ab));
    }

    const content = Buffer.concat(chunks);
    const mimeType = (
      resp.headers.get("content-type") ?? "application/octet-stream"
    )
      .split(";")[0]!
      .trim()
      .toLowerCase();
    const filename =
      resp.headers.get("x-artifact-filename")?.trim() || artifactId;
    const sha256Header = resp.headers.get("x-artifact-sha256")?.trim() ?? null;

    return {
      content,
      mimeType,
      filename,
      sha256Header,
    };
  }
}
