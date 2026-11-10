import type { SessionCommandTargetKind } from "./commands.js";
import type {
  AutomationRunTargetRecord,
  ChannelPlatform,
  ChatContentItem,
  PendingInterruptState,
  UploadedAttachmentRecord,
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
  weixin_context_token?: string | null;
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
}

export interface I2BindCodeConsumeRequest {
  action: "bind" | "unbind";
  platform: ChannelPlatform;
  platform_user_id: string;
  platform_username?: string | null;
  platform_tenant_key?: string | null;
  weixin_context_token?: string | null;
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
  duplicate?: boolean;
  command_effect_status?: string | null;
  command_reply_pending?: boolean;
}

export interface I3SessionCommandRequest {
  cmd: "workspace_list" | "workspace_switch" | "new" | "stop";
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
  external_thread_id?: string | null;
  message_id?: string | null;
  request_key?: string | null;
  raw_payload?: Record<string, unknown> | null;
  weixin_context_token?: string | null;
  is_internal_query?: boolean;
}

export interface I3SessionCommandResponse {
  ok: boolean;
  linked?: boolean;
  allowed?: boolean;
  dalton_user_id?: string | null;
  user_tier?: string | null;
  duplicate?: boolean;
  is_home?: boolean;
  deny_reason?:
    | "not_linked"
    | "external_member_group_forbidden"
    | "workspace_not_found"
    | "workspace_ambiguous"
    | "workspace_write_denied"
    | "colleague_dispatch_disabled"
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
  kind: "agent_send" | "fallback" | "interrupt" | "automation" | "notice";
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
    | null;
  message?: string;
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
  thread_scope_key?: string | null;
  is_current?: boolean;
  home_workspace_id?: string;
  weixin_context_token?: string | null;
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
    status: "sent" | "linked" | "failed";
    reason: string | null;
  }>;
  first_response_json?: Record<string, unknown>;
  status?: "in_flight" | "sent" | "partial" | "failed" | "unknown";
  error_code?: string | null;
  error_message?: string | null;
  /** Full Discord thread address. Omitted unless this call confirms a thread. */
  confirmed_thread_address?: string | null;
}

export interface I7DeliveriesProgressResponse {
  ok: boolean;
  delivery_key: string;
  status: string;
  sent_to_current_count: number;
  /** Echo of the stored Discord thread address, or null when none is stored. */
  confirmed_thread_address?: string | null;
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
  pending_interrupt_snapshot?: PendingInterruptState | null;
  status: InboundEventStatus;
  fallback_pending: boolean;
  sent_to_current_count: number;
  uploaded_attachments: UploadedAttachmentRecord[];
  raw_payload: Record<string, unknown>;
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

export interface I9WeixinStateGetResponse {
  configured: boolean;
  ilink_bot_id: string | null;
  owner_user_id: string | null;
  bot_token?: string | null;
  base_url: string;
  get_updates_buf: string;
  status: "active" | "expired" | "unconfigured" | string;
  updated_at: string | null;
}

export interface I9WeixinStatePutRequest {
  ilink_bot_id?: string | null;
  owner_user_id?: string | null;
  bot_token?: string | null;
  base_url?: string | null;
  get_updates_buf?: string | null;
  status?: string | null;
  ilink_user_id?: string | null;
  ilink_username?: string | null;
}

export interface I9WeixinStatePutResponse {
  ok: boolean;
  ilink_bot_id: string;
  owner_user_id: string;
  base_url: string;
  get_updates_buf: string;
  status: string;
  updated_at: string | null;
}

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

export interface BackendClient {
  // I-1 ~ I-9 Internal Persistence Endpoints
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
  ): Promise<I6AuthorizeAndClaimResponse>;

  updateDeliveryProgress(
    req: I7DeliveriesProgressRequest,
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

  getWeixinState(includeToken?: boolean): Promise<I9WeixinStateGetResponse>;

  putWeixinState(req: I9WeixinStatePutRequest): Promise<I9WeixinStatePutResponse>;

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
  ): Promise<void>;

  downloadWorkspaceFile(
    userId: string,
    workspaceId: string,
    workspacePath: string,
  ): Promise<{ content: Buffer; mimeType: string }>;
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

  private headers(userId?: string | null, extra?: Record<string, string>): Record<string, string> {
    const h: Record<string, string> = {
      "X-Service-Token": this.serviceToken,
      ...(extra ?? {}),
    };
    if (userId) {
      h["X-User-Id"] = userId;
    }
    return h;
  }

  private async jsonRequest<T>(
    method: string,
    path: string,
    options?: {
      userId?: string | null;
      query?: Record<string, string | undefined | null>;
      body?: unknown;
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
    const init: RequestInit = {
      method,
      headers: this.headers(
        options?.userId,
        options?.body !== undefined ? { "Content-Type": "application/json" } : undefined,
      ),
      ...(options?.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
    };
    const resp = await this.fetchImpl(url, init);
    const text = await resp.text();
    let parsed: unknown = {};
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = { message: text };
      }
    }
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

  // --- I-1 ---
  async registerAndResolveInbound(
    req: I1RegisterAndResolveRequest,
  ): Promise<I1RegisterAndResolveResponse> {
    const { status, data } = await this.jsonRequest<I1RegisterAndResolveResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
      { body: req },
    );
    if (status >= 400) {
      throw new Error(`I-1 register-and-resolve failed (${status}): ${JSON.stringify(data)}`);
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
      { userId: req.user_id, body: req },
    );
    if (status >= 400) {
      throw new Error(`I-2 bind-code/consume failed (${status}): ${JSON.stringify(data)}`);
    }
    return data;
  }

  // --- I-3 ---
  async sessionCommand(
    req: I3SessionCommandRequest,
  ): Promise<I3SessionCommandResponse> {
    const { status, data } = await this.jsonRequest<I3SessionCommandResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/session/command",
      { userId: req.dalton_user_id, body: req },
    );
    if (status >= 400) {
      throw new Error(`I-3 session/command failed (${status}): ${JSON.stringify(data)}`);
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
  async authorizeAndClaimDelivery(
    req: I6AuthorizeAndClaimRequest,
  ): Promise<I6AuthorizeAndClaimResponse> {
    const { status, data } = await this.jsonRequest<I6AuthorizeAndClaimResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
      { userId: req.user_id, body: req },
    );
    if (status >= 400) {
      throw new Error(`I-6 authorize-and-claim failed (${status}): ${JSON.stringify(data)}`);
    }
    return data;
  }

  // --- I-7 (1) ---
  async updateDeliveryProgress(
    req: I7DeliveriesProgressRequest,
  ): Promise<I7DeliveriesProgressResponse> {
    const { status, data } = await this.jsonRequest<I7DeliveriesProgressResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/deliveries/progress",
      { body: req },
    );
    if (status >= 400) {
      throw new Error(`I-7 deliveries/progress failed (${status}): ${JSON.stringify(data)}`);
    }
    return data;
  }

  // --- I-7 (2) ---
  async updateInboundProgress(
    req: I7InboundProgressRequest,
  ): Promise<I7InboundProgressResponse> {
    const { status, data } = await this.jsonRequest<I7InboundProgressResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/inbound/progress",
      { body: req },
    );
    if (status >= 400) {
      throw new Error(`I-7 inbound/progress failed (${status}): ${JSON.stringify(data)}`);
    }
    return data;
  }

  // --- I-7 (3) ---
  async reconcileSteer(
    req: I7SteersReconcileRequest,
  ): Promise<I7SteersReconcileResponse> {
    const { status, data } = await this.jsonRequest<I7SteersReconcileResponse>(
      "POST",
      "/api/v1/dalton/channels/internal/steers/reconcile",
      { body: req },
    );
    if (status >= 400) {
      throw new Error(`I-7 steers/reconcile failed (${status}): ${JSON.stringify(data)}`);
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
      },
    );
    if (status >= 400) {
      throw new Error(`I-7 recover failed (${status}): ${JSON.stringify(data)}`);
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
      throw new Error(`I-8 automation-runs failed (${status}): ${JSON.stringify(data)}`);
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
      throw new Error(`I-8 automation-runs/finish failed (${status}): ${JSON.stringify(data)}`);
    }
    return { status, data };
  }

  // --- I-9 (GET) ---
  async getWeixinState(includeToken = true): Promise<I9WeixinStateGetResponse> {
    const { status, data } = await this.jsonRequest<I9WeixinStateGetResponse>(
      "GET",
      "/api/v1/dalton/channels/internal/weixin-state",
      { query: { include_token: includeToken ? "true" : "false" } },
    );
    if (status >= 400) {
      throw new Error(`I-9 getWeixinState failed (${status}): ${JSON.stringify(data)}`);
    }
    return data;
  }

  // --- I-9 (PUT) ---
  async putWeixinState(req: I9WeixinStatePutRequest): Promise<I9WeixinStatePutResponse> {
    const { status, data } = await this.jsonRequest<I9WeixinStatePutResponse>(
      "PUT",
      "/api/v1/dalton/channels/internal/weixin-state",
      { body: req },
    );
    if (status >= 400) {
      throw new Error(`I-9 putWeixinState failed (${status}): ${JSON.stringify(data)}`);
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
  ): Promise<void> {
    const url = new URL(
      `${this.baseUrl}/api/v1/workspaces/${encodeURIComponent(workspaceId)}/files/upload`,
    );
    url.searchParams.set("path", workspacePath);

    const form = new FormData();
    const blob = new Blob([new Uint8Array(content)], { type: mimeType });
    form.append("file", blob, filename);

    const resp = await this.fetchImpl(url, {
      method: "POST",
      headers: this.headers(userId),
      body: form,
    });
    if (!resp.ok) {
      const errText = await resp.text();
      throw new Error(`uploadWorkspaceFile failed (${resp.status}): ${errText}`);
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
    const arrayBuffer = await resp.arrayBuffer();
    const mimeType = resp.headers.get("content-type") ?? "application/octet-stream";
    return {
      content: Buffer.from(arrayBuffer),
      mimeType,
    };
  }
}
