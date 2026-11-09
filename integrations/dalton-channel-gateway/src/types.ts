export type ChannelPlatform = "feishu" | "discord" | "weixin";

export const SUPPORTED_PLATFORMS: readonly ChannelPlatform[] = [
  "feishu",
  "discord",
  "weixin",
] as const;

export function isSupportedPlatform(value: string): value is ChannelPlatform {
  return (
    value === "feishu" ||
    value === "discord" ||
    value === "weixin"
  );
}

// ---------------------------------------------------------------------------
// §1.3.3 Internal Outcomes vs Public Statuses (strictly separated)
// ---------------------------------------------------------------------------

/**
 * Internal `conversation_turns.outcome` / `hook_outbox` values returned by `I-7 steers/reconcile` (§1.3.3).
 */
export const INTERNAL_OUTCOMES: ReadonlySet<string> = new Set([
  "success",
  "error",
  "interrupted",
  "cancelled",
  "stale_recovery",
  "delivered",
  "turn",
  "resumed",
  "returned",
  "dropped",
  "not_running",
]);

export const INTERNAL_REPLAY_NEEDED_OUTCOMES: ReadonlySet<string> = new Set([
  "success",
  "interrupted",
]);

/**
 * Public `status` values returned by `GET /api/v1/threads/{id}/status` and
 * `GET /api/v1/threads/dispatches/liveness` (§1.3.3 & `src/server/app/threads/messaging.py`).
 */
export const PUBLIC_LIVE_STATUSES: ReadonlySet<string> = new Set([
  "in_progress",
  "queued",
  "starting",
  "running",
  "streaming",
  "reconnecting",
  "recovering",
  "stopping",
  "compacting",
]);

export const PUBLIC_TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "idle",
  "completed",
  "interrupted",
  "error",
  "failed",
  "cancelled",
  "not_found",
]);

export const PUBLIC_REPLAY_NEEDED_STATUSES: ReadonlySet<string> = new Set([
  "completed",
  "interrupted",
]);

/**
 * Active wakeup states from `GET /api/v1/threads/{id}/wakeups` (`src/server/models/wakeup.py:29`).
 */
export const ACTIVE_WAKEUP_STATES: ReadonlySet<string> = new Set([
  "armed",
  "held",
  "held_usage",
]);

// ---------------------------------------------------------------------------
// Attachment & Text Limits (§1.2.1, §1.2.7, §1.3.1, `src/tools/messaging/tools.py:42`)
// ---------------------------------------------------------------------------

export const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024; // 20 MB per file
export const MAX_ATTACHMENT_COUNT = 10; // up to 10 files
export const MAX_TOTAL_ATTACHMENT_BYTES = 50 * 1024 * 1024; // 50 MB total
export const MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024; // 5 MB inline image_url limit
export const MAX_SEND_TEXT_CHARS = 32_000;
export const MAX_AUTOMATION_FINAL_TEXT_CHARS = 20_000; // Matches `src/server/services/automation_delivery.py:74`

export const INLINE_IMAGE_MIME_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);

// ---------------------------------------------------------------------------
// Surface Rules (§1.3.1)
// ---------------------------------------------------------------------------

export function buildSurfaceRules(platform: ChannelPlatform): string {
  return [
    `<surface_rules platform="${platform}">`,
    `The user is chatting with you from ${platform}.`,
    `- Keep replies concise and mobile-friendly.`,
    `- To send files (charts, PDFs, spreadsheets, reports) or extra messages into this conversation, call \`send_message(text=..., files=[...])\` with \`target\` omitted.`,
    `- Do NOT paste local sandbox paths expecting the user to open them; attach the file via \`send_message(files=[...])\`.`,
    `- Avoid wide Markdown tables (> 4 columns); use bullet lists or attach a file.`,
    `</surface_rules>`,
  ].join("\n");
}

// ---------------------------------------------------------------------------
// HITL Pending Interrupt Shapes (§1.3.1, `src/server/services/runs/sse_producer.py:999`)
// ---------------------------------------------------------------------------

export type PendingInterruptKind =
  | "ask_user_question"
  | "web_only_approval"
  | "credit_pause";

export interface AskUserQuestionOption {
  id?: string;
  label: string;
}

export interface AskUserQuestionItem {
  id: string;
  question: string;
  allow_multiple?: boolean;
  options: AskUserQuestionOption[];
}

export interface InterruptActionRequest {
  type?: string;
  name?: string;
  tool?: string;
  action?: string;
  description?: string;
  prompt?: string;
  question?: string;
  options?: Array<string | AskUserQuestionOption>;
  allow_multiple?: boolean;
  message?: string;
  args?: Record<string, unknown>;
  questions?: unknown[];
  [key: string]: unknown;
}

export interface PendingInterruptState {
  interrupt_id: string;
  kind: PendingInterruptKind;
  category?: PendingInterruptKind;
  thread_id: string;
  run_id: string;
  turn_index?: number | null;
  action_type?: string;
  action_requests?: InterruptActionRequest[];
  questions?: AskUserQuestionItem[];
  prompt?: string;
}

// ---------------------------------------------------------------------------
// Inbound Message & Attachment Types (§1.3.1, §4.2 ~ §4.4)
// ---------------------------------------------------------------------------

export interface InboundAttachmentDescriptor {
  /** Platform-specific attachment key/URL/resource id. */
  id: string;
  filename: string;
  mimeType: string;
  sizeBytes?: number;
}

export interface UploadedAttachmentRecord {
  filename: string;
  workspace_path: string;
  mime_type: string;
  size_bytes: number;
  data_url?: string;
}

export interface InboundChannelEvent {
  platform: ChannelPlatform;
  platformUserId: string;
  platformUsername?: string;
  platformTenantKey?: string;
  /** `"dm"` for direct messages, `"group"` for group/channel chats. */
  chatType: "dm" | "group";
  /** Canonical address: `<platform>:@me` for DM or `<platform>:<chat_id>` for group. */
  chatAddress: string;
  /** Raw platform chat id (e.g. `oc_xxx`, `${guildId}/${channelId}`). */
  chatId: string;
  /** Optional display name of the group/channel. */
  chatName?: string;
  /**
   * Scope key isolating threads (`dm:${platform_user_id}`, `chat:${chat_id}`,
   * `chat:${chat_id}:topic:${root_id}`, `chat:${guild_id}/${channel_id}:thread:${thread_id}`).
   */
  threadScopeKey: string;
  /** Unique platform message ID (`message_id` / `msg.id`). */
  messageId: string;
  /** User text content (trimmed or raw). */
  text: string;
  /** Optional command candidate text with structured bot mentions already removed. */
  commandText?: string;
  /** Whether the message is a valid trigger in a group chat (structured @bot mention). */
  isGroupTrigger?: boolean;
  /** Inbound attachments on this message. */
  attachments?: InboundAttachmentDescriptor[];
  /** Weixin `context_token` when `platform === "weixin"`. */
  weixinContextToken?: string;
  /** Arbitrary platform payload saved in `raw_payload`. */
  rawPayload?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// `/agent/*` Request & Response Shapes (§1.2.1 ~ §1.2.7)
// ---------------------------------------------------------------------------

export interface AgentSendFileRef {
  path: string;
  workspace_id: string | null;
}

export interface AgentSendRequest {
  thread_id: string;
  run_id: string;
  tool_call_id?: string | null;
  turn_platform?: string | null;
  workspace_id?: string | null;
  target?: string | null;
  text: string;
  files?: AgentSendFileRef[];
  reply?: boolean;
  new_thread?: boolean;
  automation_execution_id?: string | null;
}

export interface AgentSendFileOutcome {
  path: string;
  status: "sent" | "linked" | "failed";
  reason: string | null;
}

export interface AgentSendResponse {
  status: "sent" | "partial" | "failed" | "unknown";
  code:
    | "not_linked"
    | "not_allowed"
    | "no_current_conversation"
    | "target_not_found"
    | "rate_limited"
    | "unavailable"
    | "in_flight"
    | null;
  address: string | null;
  current: boolean;
  duplicate: boolean;
  message: string;
  files: AgentSendFileOutcome[];
}

export interface AgentTargetItem {
  address: string;
  kind: "dm" | "channel" | "group";
  name: string;
  preferred: boolean;
  thread?: { last_used_at: number } | null;
}

export interface AgentTargetUnavailable {
  platform: string;
  reason: string;
  message: string;
}

export interface AgentTargetsResponse {
  current: { address: string } | null;
  targets: AgentTargetItem[];
  unavailable: AgentTargetUnavailable[];
  settings_url: string;
}

export interface AgentSettingsResponse {
  version: string;
  settings: Record<string, unknown>;
}

export interface AgentSettingsPutResponse {
  version?: string;
  changes?: string[];
  code?: string;
  message?: string;
  problems?: Array<{ field: string; message: string }>;
  applied?: string[];
}

export interface AvailableChatItem {
  address: string;
  name: string;
  kind: "dm" | "channel" | "group";
}

export interface AgentSettingsAvailableResponse {
  apps: Record<
    string,
    {
      chats: AvailableChatItem[];
      complete: boolean;
      error: string | null;
    }
  >;
}

export interface AgentCheckTargetRequest {
  address: string;
  purpose: "automation" | string;
}

export interface AgentCheckTargetResponse {
  ok: boolean;
  address: string | null;
  name?: string | null;
  message: string | null;
}

export interface DeliveryDefaultTarget {
  address: string;
  name: string;
  via: "workspace" | "preferred" | "dm";
}

export interface AgentAutomationTargetsResponse {
  apps: Record<
    string,
    {
      chats: AvailableChatItem[];
      /** Required by `web/src/pages/Automations/components/DeliveryPicker.tsx` */
      default: DeliveryDefaultTarget | null;
      default_address: string | null;
      resolves_to: {
        address: string | null;
        name: string | null;
        via: "workspace" | "preferred" | "dm" | null;
      };
      error: string | null;
    }
  >;
}

export interface AgentAutomationOutputPutRequest {
  workspace_id: string;
  platform: string;
  address: string | null;
}

export interface AgentAutomationOutputPutResponse {
  address?: string | null;
  name?: string | null;
  message?: string;
  problems?: Array<{ field: string; message: string }>;
}

export interface AgentAutomationRunStartRequest {
  execution_id: string;
  workspace_id?: string | null;
  automation_name: string;
  entries: string[];
  thread_id?: string | null;
}

export interface AutomationRunTargetRecord {
  entry: string;
  address: string | null;
  name: string | null;
  ok: boolean;
  message: string | null;
  agent_sent?: boolean;
  reached?: boolean;
  via?: "agent" | "fallback" | "notice" | null;
  error?: string | null;
}

export interface AgentAutomationRunFinishRequest {
  status: "completed" | "failed" | "stopped" | "interrupted" | "cancelled";
  thread_id?: string | null;
  final_text?: string | null;
}

export interface AgentAutomationRunFinishTargetRow {
  entry: string;
  address: string | null;
  name: string | null;
  reached: boolean;
  via: "agent" | "fallback" | "notice" | null;
  error: string | null;
}

// ---------------------------------------------------------------------------
// Multimodal Content Item (`src/server/models/chat.py:282-314` `ContentItem`)
// ---------------------------------------------------------------------------

/**
 * Matches backend `ContentItem` (`src/server/models/chat.py:282-314` &
 * `src/server/handlers/chat/request_prep.py:362`):
 * - Text item: `{ type: "text", text: string }`
 * - Image item: `{ type: "image", image_url: string }` (data URL string, NOT `{ url }`)
 */
export type ChatContentItem =
  | { type: "text"; text: string }
  | { type: "image"; image_url: string };
